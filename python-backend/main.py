"""电商客服AI智能体 — 淘宝风格多Agent客服系统入口"""

import asyncio
import json
import os
import time
import csv
import io
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict

from fastapi import Depends, FastAPI, Request, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from chat_service import run_chat, reset_session_escalation, run_chat_stream
import knowledge_store
import order_store
import doc_importer
import auth
from auth import get_current_tenant, require_agent_role
# 知识库写入后标记检索管线过期：单例在下次取用时按知识库指纹决定是否重建索引
from rag import mark_pipeline_stale, warmup


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动期预热 RAG 管线（embedding + reranker + 索引）。

    为什么放这里：模型加载与索引重建约 27s，若等首个请求才做，会同步阻塞事件循环
    （单进程 uvicorn 下卡住所有并发请求），实测首个请求要 70.9s。
    lifespan 完成前 uvicorn 不接受请求 —— 用「启动慢 27s」换「每个请求都快」。
    用 to_thread 是必须的：warmup 内部是同步的重 CPU/IO 操作，直接在事件循环里跑会卡住 loop。
    """
    await asyncio.to_thread(warmup)
    yield


app = FastAPI(title="AI电商客服智能体", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:3000")],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# 初始化商品知识库（首次启动自动建表+灌入默认数据）
knowledge_store.init_knowledge_store()
# 初始化订单/退货状态库（首次启动自动建表+灌入演示订单）
# 订单与退货单的权威数据源落在 SQLite，Context 只作展示缓存（决策 D7）
order_store.init_order_store()
# 初始化租户鉴权存储（首次启动自动建 tenants 表）
auth.init_auth_store()
# 从环境变量幂等 seed 内置凭据：default 租户（知识库面板）/ agent 坐席（坐席工作台）
# 环境变量为凭据唯一事实源，未配置则跳过（不阻断启动）
for _seed in auth.ensure_seed_tenants():
    print(f"[auth] seed tenant '{_seed['tenant_id']}': {_seed['action']}")


@app.get("/health")
async def health_check() -> Dict[str, str]:
    return {"status": "healthy", "service": "AI电商客服智能体", "agents": "6"}


@app.get("/api/agents")
async def api_agents():
    """返回所有Agent列表，供前端左侧面板使用"""
    from ecommerce.agents import (
        triage_agent, order_tracking_agent, product_inquiry_agent,
        after_sales_agent, faq_agent, escalation_agent,
    )
    agents = [
        triage_agent, order_tracking_agent, product_inquiry_agent,
        after_sales_agent, faq_agent, escalation_agent,
    ]
    # Guardrail名称映射：原始名→中文名
    guardrail_name_map = {
        "Relevance Guardrail": "内容相关性检测",
        "Jailbreak Guardrail": "越狱攻击检测",
        "relevance_guardrail": "内容相关性检测",
        "jailbreak_guardrail": "越狱攻击检测",
    }

    def guardrail_name(g):
        name = getattr(g, "name", str(g))
        return guardrail_name_map.get(name, name)

    return {
        "agents": [
            {
                "name": a.name,
                "description": getattr(a, "handoff_description", ""),
                "handoffs": [getattr(h, "agent_name", getattr(h, "name", "")) for h in getattr(a, "handoffs", [])],
                "tools": [getattr(t, "name", getattr(t, "__name__", "")) for t in getattr(a, "tools", [])],
                "input_guardrails": [guardrail_name(g) for g in getattr(a, "input_guardrails", [])],
            }
            for a in agents
        ],
        "current_agent": triage_agent.name,
        "context": {},
    }


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None  # 可选：复用之前对话上下文


# 人工接管工单存储
_escalation_tickets: list[dict] = []

# 被坐席接管的会话映射：session_id -> ticket_id
_manual_sessions: Dict[str, str] = {}

# 频率限制（内存级滑动窗口）
RATE_LIMIT_MAX = 30      # 每窗口最多请求数
RATE_LIMIT_WINDOW = 60   # 窗口时长（秒）
_rate_limits: Dict[str, list[float]] = {}


def check_rate_limit(key: str) -> bool:
    """滑动窗口限流，返回 True 表示放行，False 表示超限"""
    now = time.time()
    timestamps = [t for t in _rate_limits.get(key, []) if now - t < RATE_LIMIT_WINDOW]
    if len(timestamps) >= RATE_LIMIT_MAX:
        _rate_limits[key] = timestamps
        return False
    timestamps.append(now)
    _rate_limits[key] = timestamps
    # 定期清理，防止字典无限增长
    if len(_rate_limits) > 10000:
        _rate_limits.clear()
    return True


@app.post("/api/chat")
async def api_chat(req: ChatRequest, request: Request):
    """REST聊天接口：发送消息，返回AI回复+Agent轨迹"""
    # 频率限制（按客户端 IP）
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip):
        return {
            "reply": "您的操作过于频繁，请稍后再试。",
            "agent_trace": [],
            "final_agent": "Triage Agent",
            "rate_limited": True,
        }

    # 输入校验
    msg = req.message.strip()
    if len(msg) > 1000:
        return {
            "reply": "消息过长，请控制在1000字以内。",
            "agent_trace": [],
            "final_agent": "Triage Agent",
        }
    if not msg:
        return {
            "reply": "请发送有效消息。",
            "agent_trace": [],
            "final_agent": "Triage Agent",
        }

    # 人工接管判断：会话已被坐席接管时，消息直接转给坐席，不再走 Agent
    if req.session_id and req.session_id in _manual_sessions:
        ticket_id = _manual_sessions[req.session_id]
        ticket = next((t for t in _escalation_tickets if t["id"] == ticket_id), None)
        if ticket:
            ticket["messages"].append({
                "role": "user", "content": msg,
                "time": datetime.now().isoformat(timespec="seconds"),
            })
            return {
                "reply": "您已接入人工客服，消息已送达，坐席将尽快回复。",
                "agent_trace": [],
                "final_agent": "Human Escalation Agent",
                "session_id": req.session_id,
                "manual": True,
            }

    # 核心：复用共享 run_chat（与抖音 webhook 同一份逻辑）
    result = await run_chat(msg, req.session_id)
    state = result["state"]
    session_id = result["session_id"]

    # 记录转接工单
    if state.escalation_flag:
        ticket_id = f"TKT-{int(time.time()) % 100000:05d}"
        # 把触发转人工的这一轮买家消息也写进 ticket.messages，避免坐席看不到
        # 「买家为什么要转人工」的原始诉求（之前 messages=[] 是空，坐席端只看到
        # 后续 buyer 在 manual pool 里发送的消息，丢掉了首条触发消息）。
        buyer_first_msg = {
            "role": "user", "content": msg,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        _escalation_tickets.insert(0, {
            "id": ticket_id,
            "session_id": session_id,
            "reason": state.escalation_reason or "用户请求人工",
            "order_number": state.order_number or "",
            "customer_name": state.customer_name or "",
            "return_case_id": state.return_case_id or "",
            "product_name": state.product_name or "",
            "created_at": datetime.now().isoformat(),
            "status": "pending",
            "messages": [buyer_first_msg],
        })
        # P0 fix：工单一创建，立即把 session 写入静音池。
        # 之后该 session 的任何用户消息都直接 hardcoded reply「您已接入…坐席将尽快回复」，
        # 不再走 Agent，避免 LLM 在 escalation agent 上反复二次回答造成「重复回答」。
        # accept 只是把 pending → processing，不影响 session 静音状态（close 时才解除）。
        if session_id:
            _manual_sessions[session_id] = ticket_id

    return {
        "reply": result["reply"],
        "agent_trace": result["trace"],
        "final_agent": result["final_agent"],
        "session_id": session_id,
        "manual": state.escalation_flag,
        "escalation": {
            "flag": state.escalation_flag,
            "reason": state.escalation_reason or "",
            "order_number": state.order_number or "",
            "customer_name": state.customer_name or "",
            "return_case_id": state.return_case_id or "",
            "product_name": state.product_name or "",
        },
    }


@app.get("/api/escalations")
async def api_escalations(_agent: str = Depends(require_agent_role)):
    """返回人工接管工单列表（含待处理和处理中）。需坐席角色——工单含客户姓名/订单号等隐私字段。"""
    tickets = [t for t in _escalation_tickets if t["status"] != "closed"]
    pending = sum(1 for t in tickets if t["status"] == "pending")
    return {"tickets": tickets, "total": len(tickets), "pending": pending}


class ReplyRequest(BaseModel):
    content: str


@app.post("/api/escalations/{ticket_id}/accept")
async def api_accept_ticket(ticket_id: str, _agent: str = Depends(require_agent_role)):
    """坐席接入工单，标记处理中并绑定会话（需坐席角色）"""
    ticket = next((t for t in _escalation_tickets if t["id"] == ticket_id), None)
    if not ticket:
        return {"error": "工单不存在"}
    ticket["status"] = "processing"
    # 绑定会话：之后该 session 的消息直接转给坐席
    if ticket.get("session_id"):
        _manual_sessions[ticket["session_id"]] = ticket_id
    return {"ticket": ticket}


@app.get("/api/escalations/{ticket_id}/messages")
async def api_ticket_messages(ticket_id: str, _agent: str = Depends(require_agent_role)):
    """获取工单的对话记录。需坐席角色——ticket_id 可枚举，不鉴权即可读任意工单会话。"""
    ticket = next((t for t in _escalation_tickets if t["id"] == ticket_id), None)
    if not ticket:
        return {"error": "工单不存在", "messages": []}
    return {"messages": ticket.get("messages", [])}


@app.post("/api/escalations/{ticket_id}/reply")
async def api_ticket_reply(ticket_id: str, req: ReplyRequest, _agent: str = Depends(require_agent_role)):
    """坐席回复买家（需坐席角色）"""
    ticket = next((t for t in _escalation_tickets if t["id"] == ticket_id), None)
    if not ticket:
        return {"error": "工单不存在"}
    content = req.content.strip()
    if not content:
        return {"error": "回复内容为空"}
    ticket.setdefault("messages", []).append({
        "role": "agent", "content": content,
        "time": datetime.now().isoformat(timespec="seconds"),
    })
    return {"ticket": ticket}


@app.post("/api/escalations/{ticket_id}/close")
async def api_close_ticket(ticket_id: str, _agent: str = Depends(require_agent_role)):
    """坐席关闭工单（需坐席角色）"""
    ticket = next((t for t in _escalation_tickets if t["id"] == ticket_id), None)
    if not ticket:
        return {"error": "工单不存在"}
    ticket["status"] = "closed"
    session_id = ticket.get("session_id")
    if session_id:
        _manual_sessions.pop(session_id, None)
        # 重置会话转人工标记，否则买家继续发消息会因 escalation_flag=true 再次触发转人工
        reset_session_escalation(session_id)
    return {"ticket": ticket}


@app.get("/api/chat/poll")
async def api_chat_poll(session_id: str):
    """买家端轮询：获取坐席最新回复"""
    # 按 session_id 直接匹配工单（不依赖坐席是否已接入），
    # 这样转人工后买家即可持续轮询，坐席回复后实时可见，工单关闭后停止。
    ticket = next((t for t in _escalation_tickets if t.get("session_id") == session_id), None)
    if not ticket or ticket["status"] == "closed":
        return {"manual": False, "messages": []}
    agent_msgs = [m for m in ticket.get("messages", []) if m["role"] == "agent"]
    return {"manual": True, "messages": agent_msgs, "ticket_status": ticket["status"]}


# ==================== 流式聊天 API（SSE） ====================

# SSE 响应头：
# - Cache-Control: no-cache —— 避免中间层缓存整包
# - Connection: keep-alive —— 维持长连接
# - X-Accel-Buffering: no —— 要求 nginx 不缓冲（保险；正解是 nginx.conf 的 proxy_buffering off）
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@app.post("/api/chat/stream")
async def api_chat_stream(req: ChatRequest, request: Request):
    """SSE 流式聊天接口。帧格式见 chat_service.run_chat_stream 的 docstring。

    与 /api/chat 的关系：二者并存。非流式端点保留给抖音 webhook 链路、
    前端降级路径，以及 A/B 对比基线（见 SSE流式改造_原子任务_v1.md §4-D10）。
    """
    # 限流与校验放在 StreamingResponse 之前：失败可直接回 JSON，
    # 前端无需在 SSE 解析器里再处理「HTTP 层错误」，判错路径与 /api/chat 一致。
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip):
        return JSONResponse({"error": "操作过于频繁，请稍后再试。"}, status_code=429)

    msg = req.message.strip()
    if not msg or len(msg) > 1000:
        return JSONResponse({"error": "消息为空或过长"}, status_code=422)

    # 人工接管：语义与 /api/chat 一致（把消息写进工单消息列表，否则坐席看不到）。
    # 只回 done、不发内容帧 —— 前端在 manualMode 下本就不渲染后端回复，
    # 发内容帧会引入协议外的帧类型（见 §4-D8）。
    if req.session_id and req.session_id in _manual_sessions:
        ticket_id = _manual_sessions[req.session_id]
        ticket = next((t for t in _escalation_tickets if t["id"] == ticket_id), None)
        if ticket:
            ticket["messages"].append({
                "role": "user", "content": msg,
                "time": datetime.now().isoformat(timespec="seconds"),
            })

            async def manual_gen():
                payload = json.dumps(
                    {"session_id": req.session_id, "final_agent": "Human Escalation Agent"},
                    ensure_ascii=False,
                )
                yield f"event: done\ndata: {payload}\n\n"

            return StreamingResponse(manual_gen(), media_type="text/event-stream",
                                     headers=_SSE_HEADERS)

    async def gen():
        try:
            async for name, data in run_chat_stream(msg, req.session_id):
                payload = json.dumps(data, ensure_ascii=False)
                yield f"event: {name}\ndata: {payload}\n\n"
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] chat stream: {type(e).__name__}: {e}")

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ==================== 商品知识库 API ====================

class KnowledgeItem(BaseModel):
    product_id: str = ""
    name: str = ""
    description: str = ""
    selling_points: list[str] = []
    specs: list[str] = []
    faq: list[str] = []
    category: str = ""


@app.get("/api/knowledge")
async def api_knowledge_list(tenant_id: str = Depends(get_current_tenant)):
    """商品知识库列表"""
    items = knowledge_store.list_knowledge(tenant_id)
    return {"items": items, "total": len(items)}


@app.post("/api/knowledge")
async def api_knowledge_create(item: KnowledgeItem, tenant_id: str = Depends(get_current_tenant)):
    """新增商品知识"""
    created = knowledge_store.create_knowledge(item.model_dump(), tenant_id)
    mark_pipeline_stale()  # 新知识需在下次检索时进入向量索引
    return {"item": created}


@app.put("/api/knowledge/{knowledge_id}")
async def api_knowledge_update(knowledge_id: int, item: KnowledgeItem, tenant_id: str = Depends(get_current_tenant)):
    """更新商品知识"""
    updated = knowledge_store.update_knowledge(knowledge_id, item.model_dump(), tenant_id)
    if updated is None:
        return {"error": "未找到该知识条目"}
    mark_pipeline_stale()
    return {"item": updated}


@app.delete("/api/knowledge/{knowledge_id}")
async def api_knowledge_delete(knowledge_id: int, tenant_id: str = Depends(get_current_tenant)):
    """删除商品知识"""
    deleted = knowledge_store.delete_knowledge(knowledge_id, tenant_id)
    if deleted:
        mark_pipeline_stale()
    return {"deleted": deleted}


@app.get("/api/knowledge/search")
async def api_knowledge_search(q: str, tenant_id: str = Depends(get_current_tenant)):
    """搜索商品知识"""
    items = knowledge_store.search_knowledge(q, tenant_id)
    return {"items": items, "total": len(items)}


@app.post("/api/knowledge/reindex")
async def api_knowledge_reindex(tenant_id: str = Depends(get_current_tenant)):
    """重建 RAG 向量索引，使新增/导入的知识立即可被检索。

    全量重建需要重新向量化全部 chunk，属重活，因此不放进 CRUD 请求里同步执行：
    CRUD 只把检索管线标记为过期，由下次检索按知识库指纹自动重建；
    本接口用于需要「立刻生效」的场景（如批量导入后）。
    """
    from rag import build_index, reset_pipeline

    pipeline = build_index(reset=True)
    reset_pipeline()  # 丢弃旧单例，下次取用时加载刚建好的索引
    return {
        "status": "reindexed",
        "chunks": pipeline.vector_store.count(),
        "embedding_backend": pipeline.embedder.backend,
        "knowledge_sig": knowledge_store.knowledge_signature(tenant_id),
    }


@app.post("/api/knowledge/import")
async def api_knowledge_import(file: UploadFile = File(...), tenant_id: str = Depends(get_current_tenant)):
    """批量导入 CSV 商品知识"""
    # 读取并解码（utf-8-sig 兼容 Excel 导出的 BOM）
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="ignore")

    reader = csv.DictReader(io.StringIO(text))

    # 表头映射：兼容中文/英文表头
    header_map = {
        "product_id": "product_id", "商品id": "product_id", "商品ID": "product_id",
        "name": "name", "商品名称": "name", "名称": "name",
        "description": "description", "介绍": "description", "商品介绍": "description",
        "selling_points": "selling_points", "卖点": "selling_points",
        "specs": "specs", "规格": "specs",
        "faq": "faq", "常见问题": "faq", "FAQ": "faq", "问答": "faq",
        "category": "category", "类目": "category", "分类": "category",
    }

    items: list[dict] = []
    for row in reader:
        item: dict = {}
        for raw_key, raw_val in row.items():
            key = header_map.get((raw_key or "").strip(), (raw_key or "").strip())
            val = (raw_val or "").strip()
            if key in ("selling_points", "specs", "faq"):
                # 多条用分号或换行分隔
                parts = [p.strip() for p in val.replace("；", ";").replace("\n", ";").split(";") if p.strip()]
                item[key] = parts
            else:
                item[key] = val
        items.append(item)

    if not items:
        return {"error": "CSV 内容为空或表头不匹配", "success": 0, "failed": 0}

    result = knowledge_store.bulk_import(items, tenant_id)
    if result.get("success"):
        mark_pipeline_stale()  # 批量导入的新知识需进入向量索引
    return result


@app.post("/api/knowledge/import-doc")
async def api_knowledge_import_doc(file: UploadFile = File(...), tenant_id: str = Depends(get_current_tenant)):
    """导入文档（PDF/Word/TXT），用 LLM 提取商品知识后入库"""
    content = await file.read()
    text = doc_importer.extract_text(file.filename or "", content)

    if not text.strip():
        return {"error": "未能从文档中解析出文本内容", "success": 0, "failed": 0}

    try:
        items = await doc_importer.extract_knowledge_with_llm(text)
    except Exception as e:
        print(f"[ERROR] 文档导入 LLM 提取失败: {e}")
        return {"error": f"LLM 提取失败：{e}", "success": 0, "failed": 0}

    if not items:
        return {"error": "未能从文档中提取出商品知识", "success": 0, "failed": 0}

    # 把 LLM 输出的字段名规范化（兼容大小写/中英文）
    normalized = []
    for it in items:
        normalized.append({
            "product_id": str(it.get("product_id") or it.get("productId") or "").strip(),
            "name": str(it.get("name") or "").strip(),
            "description": str(it.get("description") or "").strip(),
            "selling_points": it.get("selling_points") or it.get("sellingPoints") or [],
            "specs": it.get("specs") or [],
            "faq": it.get("faq") or it.get("FAQ") or [],
        })
    # 过滤掉商品名为空的
    normalized = [n for n in normalized if n["name"]]

    if not normalized:
        return {"error": "提取到的商品知识缺少商品名称", "success": 0, "failed": 0}

    result = knowledge_store.bulk_import(normalized, tenant_id)
    if result.get("success"):
        mark_pipeline_stale()  # 文档抽取的新知识需进入向量索引
    result["extracted_text_len"] = len(text)
    return result
