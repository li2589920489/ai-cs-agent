"""电商客服AI智能体 — 淘宝风格多Agent客服系统入口"""

import os
import time
import csv
import io
from datetime import datetime
from typing import Any, Dict

from fastapi import FastAPI, Request, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from chat_service import run_chat
import knowledge_store
import doc_importer

app = FastAPI(title="AI电商客服智能体", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:3000")],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# 初始化商品知识库（首次启动自动建表+灌入默认数据）
knowledge_store.init_knowledge_store()


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
        _escalation_tickets.insert(0, {
            "id": f"TKT-{int(time.time()) % 100000:05d}",
            "session_id": session_id,
            "reason": state.escalation_reason or "用户请求人工",
            "order_number": state.order_number or "",
            "customer_name": state.customer_name or "",
            "return_case_id": state.return_case_id or "",
            "product_name": state.product_name or "",
            "created_at": datetime.now().isoformat(),
            "status": "pending",
            "messages": [],
        })

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
async def api_escalations():
    """返回人工接管工单列表（含待处理和处理中）"""
    tickets = [t for t in _escalation_tickets if t["status"] != "closed"]
    pending = sum(1 for t in tickets if t["status"] == "pending")
    return {"tickets": tickets, "total": len(tickets), "pending": pending}


class ReplyRequest(BaseModel):
    content: str


@app.post("/api/escalations/{ticket_id}/accept")
async def api_accept_ticket(ticket_id: str):
    """坐席接入工单，标记处理中并绑定会话"""
    ticket = next((t for t in _escalation_tickets if t["id"] == ticket_id), None)
    if not ticket:
        return {"error": "工单不存在"}
    ticket["status"] = "processing"
    # 绑定会话：之后该 session 的消息直接转给坐席
    if ticket.get("session_id"):
        _manual_sessions[ticket["session_id"]] = ticket_id
    return {"ticket": ticket}


@app.get("/api/escalations/{ticket_id}/messages")
async def api_ticket_messages(ticket_id: str):
    """获取工单的对话记录"""
    ticket = next((t for t in _escalation_tickets if t["id"] == ticket_id), None)
    if not ticket:
        return {"error": "工单不存在", "messages": []}
    return {"messages": ticket.get("messages", [])}


@app.post("/api/escalations/{ticket_id}/reply")
async def api_ticket_reply(ticket_id: str, req: ReplyRequest):
    """坐席回复买家"""
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
async def api_close_ticket(ticket_id: str):
    """坐席关闭工单"""
    ticket = next((t for t in _escalation_tickets if t["id"] == ticket_id), None)
    if not ticket:
        return {"error": "工单不存在"}
    ticket["status"] = "closed"
    if ticket.get("session_id"):
        _manual_sessions.pop(ticket["session_id"], None)
    return {"ticket": ticket}


@app.get("/api/chat/poll")
async def api_chat_poll(session_id: str):
    """买家端轮询：获取坐席最新回复"""
    # 按 session_id 直接匹配工单（不依赖坐席是否已接入），
    # 这样转人工后买家即可持续轮询，坐席回复后实时可见，工单关闭后停止。
    ticket = next((t for t in _escalation_tickets if t.get("session_id") == session_id), None)
    if not ticket:
        return {"manual": False, "messages": []}
    agent_msgs = [m for m in ticket.get("messages", []) if m["role"] == "agent"]
    return {"manual": True, "messages": agent_msgs, "ticket_status": ticket["status"]}


# ==================== 商品知识库 API ====================

class KnowledgeItem(BaseModel):
    product_id: str = ""
    name: str = ""
    description: str = ""
    selling_points: list[str] = []
    specs: list[str] = []
    faq: list[str] = []


@app.get("/api/knowledge")
async def api_knowledge_list(tenant_id: str = "default"):
    """商品知识库列表"""
    items = knowledge_store.list_knowledge(tenant_id)
    return {"items": items, "total": len(items)}


@app.post("/api/knowledge")
async def api_knowledge_create(item: KnowledgeItem, tenant_id: str = "default"):
    """新增商品知识"""
    created = knowledge_store.create_knowledge(item.model_dump(), tenant_id)
    return {"item": created}


@app.put("/api/knowledge/{knowledge_id}")
async def api_knowledge_update(knowledge_id: int, item: KnowledgeItem, tenant_id: str = "default"):
    """更新商品知识"""
    updated = knowledge_store.update_knowledge(knowledge_id, item.model_dump(), tenant_id)
    if updated is None:
        return {"error": "未找到该知识条目"}
    return {"item": updated}


@app.delete("/api/knowledge/{knowledge_id}")
async def api_knowledge_delete(knowledge_id: int, tenant_id: str = "default"):
    """删除商品知识"""
    deleted = knowledge_store.delete_knowledge(knowledge_id, tenant_id)
    return {"deleted": deleted}


@app.get("/api/knowledge/search")
async def api_knowledge_search(q: str, tenant_id: str = "default"):
    """搜索商品知识"""
    items = knowledge_store.search_knowledge(q, tenant_id)
    return {"items": items, "total": len(items)}


@app.post("/api/knowledge/import")
async def api_knowledge_import(file: UploadFile = File(...), tenant_id: str = "default"):
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
    return result


@app.post("/api/knowledge/import-doc")
async def api_knowledge_import_doc(file: UploadFile = File(...), tenant_id: str = "default"):
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
    result["extracted_text_len"] = len(text)
    return result
