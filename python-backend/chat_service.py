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
from dataclasses import dataclass
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


@dataclass
class ChatPrep:
    """一次对话的公共前置产物，与「是否流式」无关。

    ctx 为 None 表示入参校验未通过 —— 此时 early_reply 即应直接返回给用户的提示，
    session_id 保持调用方传入的原值（可能是 None），以与既有 /api/chat 响应完全一致。
    """
    ctx: ECommerceAgentChatContext | None
    state: ECommerceAgentContext
    session_id: str | None
    early_reply: str | None = None


def _prepare_chat(msg: str, session_id: str | None) -> ChatPrep:
    """消息校验 + 会话上下文构造。run_chat 与 run_chat_stream 共用，避免两份实现分叉。"""
    _cleanup_sessions()

    msg = (msg or "").strip()
    if not msg:
        return ChatPrep(ctx=None, state=ECommerceAgentContext(), session_id=session_id,
                        early_reply="请发送有效消息。")
    if len(msg) > 1000:
        return ChatPrep(ctx=None, state=ECommerceAgentContext(), session_id=session_id,
                        early_reply="消息过长，请控制在1000字以内。")

    store = MemoryStore()
    thread = ThreadMetadata(id=session_id or f"api_{int(time.time())}", created_at=datetime.now())
    entry = _sessions.get(session_id) if session_id else None
    state = entry[0] if entry else ECommerceAgentContext()
    ctx = ECommerceAgentChatContext(thread=thread, store=store, request_context={}, state=state)
    return ChatPrep(ctx=ctx, state=state, session_id=session_id or thread.id)


def reset_session_escalation(session_id: str | None) -> bool:
    """关闭工单后重置会话的转人工标记，允许买家继续与 AI 对话。"""
    if not session_id:
        return False
    entry = _sessions.get(session_id)
    if not entry:
        return False
    state = entry[0]
    state.escalation_flag = False
    state.escalation_reason = None
    _sessions[session_id] = (state, time.time())
    return True


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
    msg = (msg or "").strip()
    prep = _prepare_chat(msg, session_id)
    if prep.early_reply:
        return {"reply": prep.early_reply, "final_agent": "Triage Agent", "trace": [],
                "escalation": False, "escalation_reason": "", "session_id": prep.session_id,
                "state": prep.state}
    ctx, state = prep.ctx, prep.state

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

    session_id = prep.session_id
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


async def run_chat_stream(msg: str, session_id: str | None = None):
    """流式版聊天 —— 逐帧产出 (event_name, data)，由 /api/chat/stream 序列化成 SSE。

    帧协议（5 类，见 SSE流式改造_原子任务_v1.md §4-D8 / §4-D15）：
      delta      {"text": str, "mid": str|None}            每个 token；mid = 本条助手消息的标识
      trace      {"type": "tool_call"|"tool_output"|"handoff", "agent": str, ...}
      escalation {"flag": True, "reason": str}              仅流末尾、且仅当触发转人工
      error      {"msg": str, "retryable": bool}
      done       {"reply": str, "session_id": str, "final_agent": str}   恒为最后一帧

    关于 delta 的 `mid`（§4-D15）：
      一次 run 里模型可能产出**多条**助手消息 —— 例如"我来帮您查一下…"这类
      调用工具前的前言，以及工具返回后的正式答复。非流式 run_chat 遍历
      result.new_items 时对 MessageOutputItem 是**覆盖**语义，只保留最后一条；
      流式若把所有 delta 无差别下发，中间前言就会泄漏给用户（实测 4 组查询
      3 组复现，且前言多为英文）。
      `mid` 取自 Responses API 的 item_id，前端据此判断"这是一条新消息"→
      替换气泡内容而非追加，从而与非流式语义对齐。mid 为 None 表示该帧
      不由模型文本产出（如入参校验提示），前端按"新消息"处理即可。

    关于 done 的 `reply`（§4-D15 兜底）：
      携带最终答复全文，前端在收流时用它校订气泡内容，保证展示文本与
      /api/chat 逐字一致 —— 即使模型仍产出前言、或前端替换逻辑失效也能收敛。

    与 run_chat 的差异只有三处：
      1) 用 run_streamed 取代 run，边收边发；
      2) trace 不再等全量 result.new_items 才提取；
      3) 重试窗口收缩到「首 token 之前」—— 已发出的 token 收不回，
         此时重试会让用户看到重复的内容。
    """
    msg = (msg or "").strip()
    prep = _prepare_chat(msg, session_id)

    if prep.early_reply:
        yield ("delta", {"text": prep.early_reply, "mid": None})
        yield ("done", {"reply": prep.early_reply, "session_id": prep.session_id,
                        "final_agent": "Triage Agent"})
        return

    ctx, state = prep.ctx, prep.state
    emitted = False   # 是否已发出过 token —— 决定还能不能重试
    result = None

    for attempt in range(1, 4):   # 最多 3 次，缓解 DeepSeek 偶发非法 JSON
        if emitted:
            break                 # 已发出 token，放弃重试
        try:
            result = Runner.run_streamed(triage_agent, msg, context=ctx, max_turns=10)
            async for event in result.stream_events():
                if event.type == "raw_response_event":
                    # 权威判断写法见 chatkit/agents.py:846
                    if event.data.type == "response.output_text.delta":
                        emitted = True
                        yield ("delta", {
                            "text": event.data.delta,
                            "mid": getattr(event.data, "item_id", None),
                        })

                elif event.type == "run_item_stream_event":
                    name, item = event.name, event.item
                    if name == "tool_called":
                        yield ("trace", {"type": "tool_call", "agent": item.agent.name,
                                         "tool": getattr(item.raw_item, "name", "")})
                    elif name == "tool_output":
                        yield ("trace", {"type": "tool_output", "agent": item.agent.name})
                    elif name == "handoff_occured":
                        # 只处理 handoff_occured：它的 item 是 HandoffOutputItem，带
                        # target_agent，信息完整。handoff_requested 的 item 是
                        # HandoffCallItem（工具调用），取不到 target_agent，会发出
                        # to=null 的无用帧（实测确认），故忽略。
                        # 注："handoff_occured" 是 SDK 的拼写错误，勿改（agents/stream_events.py:32）
                        target = getattr(item, "target_agent", None)
                        yield ("trace", {"type": "handoff", "agent": item.agent.name,
                                         "to": getattr(target, "name", None)})
            break

        except InputGuardrailTripwireTriggered as e:
            guardrail_name = e.guardrail_result.guardrail.get_name()
            reply = ("抱歉，我无法执行该请求。我是电商客服助手，只能帮您处理订单、商品、售后、优惠券等购物相关问题。"
                     if "Jailbreak" in guardrail_name else
                     "抱歉，我只能处理电商购物相关的问题，无法回答与客服无关的内容。请问有什么购物方面可以帮您？")
            yield ("delta", {"text": reply, "mid": None})
            yield ("done", {"reply": reply, "session_id": prep.session_id,
                            "final_agent": "Triage Agent"})
            return

        except MaxTurnsExceeded:
            reply = "抱歉，处理您的问题步骤过多，请尝试简化并重新描述。"
            yield ("delta", {"text": reply, "mid": None})
            yield ("done", {"reply": reply, "session_id": prep.session_id,
                            "final_agent": "Triage Agent"})
            return

        except ModelBehaviorError as e:
            if emitted:
                # 已发出内容 → 不能重试（会造成重复），只能提示用户重发
                yield ("error", {"msg": "生成过程中断，请重试。", "retryable": True})
                return
            if attempt < 3:
                await asyncio.sleep(0.5)
                continue
            print(f"[WARN] run_chat_stream: ModelBehaviorError 重试 3 次仍失败: {e}")

        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] run_chat_stream: {type(e).__name__}: {e}")
            yield ("error", {"msg": "抱歉，系统暂时无法处理该请求。请稍后重试。", "retryable": False})
            return

    if result is None:
        yield ("error", {"msg": "抱歉，系统遇到临时技术问题。请稍后重试。", "retryable": True})
        return

    session_id = prep.session_id
    _sessions[session_id] = (state, time.time())

    # escalation 只能在流末尾判定：兜底置位依赖 result.last_agent，
    # 而 last_agent 要等整个 run 结束才确定（与 run_chat 的兜底逻辑同源）
    final_agent = result.last_agent.name if result.last_agent else "unknown"
    if final_agent == "Human Escalation Agent" and not state.escalation_flag:
        state.escalation_flag = True
        state.escalation_reason = state.escalation_reason or "用户诉求需人工介入处理"

    if state.escalation_flag:
        yield ("escalation", {"flag": True, "reason": state.escalation_reason or ""})
    # reply 口径必须与 run_chat 完全一致：取最后一条 MessageOutputItem 的文本
    yield ("done", {"reply": _final_reply_text(result), "session_id": session_id,
                    "final_agent": final_agent})


def _final_reply_text(result) -> str:
    """取出「最终答复」全文 —— 与 run_chat 的取法同源（最后一条助手消息覆盖前面的）。

    流式路径无法收回已下发的中间前言，因此把权威文本随 done 帧带给前端，
    由前端在收流时校订气泡内容（§4-D15 兜底）。
    """
    reply = ""
    for item in getattr(result, "new_items", []) or []:
        if isinstance(item, MessageOutputItem):
            reply = ItemHelpers.text_message_output(item)
    return reply or "(Agent未生成文本回复)"

