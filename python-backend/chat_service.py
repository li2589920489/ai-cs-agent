"""共享聊天服务 — REST 接口与抖音 webhook 共用的核心 Agent 调用逻辑

把「消息 → Triage 分流 → 业务 Agent → 回复 + trace + 转人工标记」这条链路
从 /api/chat 抽出来，供两个入口复用，避免逻辑分叉：
- main.py 的 /api/chat（买家 Web 端）
- douyin_adapter.py 的抖音客服消息 webhook

设计说明：
- 会话状态（_sessions）在这里统一管理，30 分钟过期，两个渠道共享同一份状态。
- 转人工工单（_escalation_tickets / _manual_sessions）属于坐席工作台 UI 的范畴，
  仍留在 main.py —— 本服务只负责返回 state（含 escalation 标记），由调用方决定如何入工单。
"""

import asyncio
import os
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# 必须在 import agents 之前禁用 SDK tracing，否则后台线程会尝试向 OpenAI 上传 trace 并超时
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"

from agents import (  # noqa: E402
    HandoffOutputItem,
    ItemHelpers,
    MessageOutputItem,
    Runner,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.exceptions import (  # noqa: E402
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
)
from chatkit.types import ThreadMetadata  # noqa: E402

from ecommerce.agents import triage_agent  # noqa: E402
from ecommerce.context import ECommerceAgentChatContext, ECommerceAgentContext  # noqa: E402
from memory_store import MemoryStore  # noqa: E402
import knowledge_store  # noqa: E402

# 首次导入时初始化商品知识库（建表 + 灌默认数据，幂等）
knowledge_store.init_knowledge_store()

# 会话存储：session_id -> (ECommerceAgentContext, 最近活跃时间戳)，30 分钟过期
_sessions: dict[str, tuple[ECommerceAgentContext, float]] = {}


def _cleanup_sessions() -> None:
    """清理 30 分钟未活动的会话"""
    now = time.time()
    expired = [sid for sid, (_, ts) in _sessions.items() if now - ts > 1800]
    for sid in expired:
        del _sessions[sid]


async def run_chat(msg: str, session_id: str | None = None) -> dict:
    """核心聊天逻辑：消息 → Triage 分流 → 业务 Agent → 回复 + trace + 转人工标记。

    返回字段：
    - reply: 文本回复
    - final_agent: 最终处理 Agent 名
    - trace: Agent 轨迹（handoff / tool_call / tool_output）
    - escalation: 是否触发转人工
    - escalation_reason: 转人工原因
    - session_id: 会话 ID（跨轮次复用）
    - state: ECommerceAgentContext（供调用方生成转人工工单）
    """
    _cleanup_sessions()

    msg = (msg or "").strip()
    if not msg:
        return {"reply": "请发送有效消息。", "final_agent": "Triage Agent", "trace": [],
                "escalation": False, "escalation_reason": "", "session_id": session_id,
                "state": ECommerceAgentContext()}
    if len(msg) > 1000:
        return {"reply": "消息过长，请控制在1000字以内。", "final_agent": "Triage Agent", "trace": [],
                "escalation": False, "escalation_reason": "", "session_id": session_id,
                "state": ECommerceAgentContext()}

    store = MemoryStore()
    thread = ThreadMetadata(id=session_id or f"api_{int(time.time())}", created_at=datetime.now())
    entry = _sessions.get(session_id) if session_id else None
    state = entry[0] if entry else ECommerceAgentContext()
    ctx = ECommerceAgentChatContext(thread=thread, store=store, request_context={}, state=state)

    result = None
    last_model_error = None
    for attempt in range(1, 4):  # 最多 3 次，缓解 DeepSeek 偶发非法 JSON
        try:
            result = await Runner.run(triage_agent, msg, context=ctx, max_turns=10)
            break
        except InputGuardrailTripwireTriggered as e:
            guardrail_name = e.guardrail_result.guardrail.get_name()
            if "Jailbreak" in guardrail_name:
                reply = "抱歉，我无法执行该请求。我是电商客服助手，只能帮您处理订单、商品、售后、优惠券等购物相关问题。"
            else:
                reply = "抱歉，我只能处理电商购物相关的问题，无法回答与客服无关的内容。请问有什么购物方面可以帮您？"
            return {"reply": reply, "final_agent": "Triage Agent", "trace": [],
                    "escalation": False, "escalation_reason": "",
                    "session_id": session_id or thread.id, "state": state}
        except MaxTurnsExceeded:
            return {"reply": "抱歉，处理您的问题步骤过多，请尝试简化并重新描述。",
                    "final_agent": "Triage Agent", "trace": [],
                    "escalation": False, "escalation_reason": "",
                    "session_id": session_id or thread.id, "state": state}
        except ModelBehaviorError as e:
            last_model_error = e
            if attempt < 3:
                await asyncio.sleep(0.5)
                continue
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] run_chat: {type(e).__name__}: {e}")
            return {"reply": "抱歉，系统暂时无法处理该请求。请稍后重试。",
                    "final_agent": "Triage Agent", "trace": [],
                    "escalation": False, "escalation_reason": "",
                    "session_id": session_id or thread.id, "state": state}

    if result is None:
        print(f"[WARN] run_chat: ModelBehaviorError 重试 3 次仍失败: {last_model_error}")
        return {"reply": "抱歉，系统遇到临时技术问题。请稍后重试。",
                "final_agent": "Triage Agent", "trace": [],
                "escalation": False, "escalation_reason": "",
                "session_id": session_id or thread.id, "state": state}

    session_id = session_id or thread.id
    _sessions[session_id] = (state, time.time())

    reply = ""
    trace = []
    for item in result.new_items:
        if isinstance(item, MessageOutputItem):
            reply = ItemHelpers.text_message_output(item)
        elif isinstance(item, HandoffOutputItem):
            trace.append({"type": "handoff", "from": item.source_agent.name, "to": item.target_agent.name})
        elif isinstance(item, ToolCallItem):
            trace.append({"type": "tool_call", "agent": item.agent.name, "tool": item.raw_item.name})
        elif isinstance(item, ToolCallOutputItem):
            trace.append({"type": "tool_output", "agent": item.agent.name, "result": str(item.output)[:200]})

    # 兜底：分流到 Human Escalation Agent 但 LLM 未调用 escalate_to_human_tool 时，
    # 强制标记转人工，保证工单一定生成（避免 DeepSeek 非确定性导致转人工闭环断裂）
    final_agent = result.last_agent.name if result.last_agent else "unknown"
    if final_agent == "Human Escalation Agent" and not state.escalation_flag:
        state.escalation_flag = True
        state.escalation_reason = state.escalation_reason or "用户诉求需人工介入处理"

    return {
        "reply": reply or "(Agent未生成文本回复)",
        "final_agent": final_agent,
        "trace": trace,
        "escalation": state.escalation_flag,
        "escalation_reason": state.escalation_reason or "",
        "session_id": session_id,
        "state": state,
    }
