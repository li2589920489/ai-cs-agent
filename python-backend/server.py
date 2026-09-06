"""电商客服AI智能体服务器 — fork自 openai/openai-cs-agents-demo，改造为淘宝风格电商客服"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel

from agents import (
    Handoff,
    HandoffOutputItem,
    InputGuardrailTripwireTriggered,
    ItemHelpers,
    MessageOutputItem,
    Runner,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.exceptions import MaxTurnsExceeded
from chatkit.agents import stream_agent_response
from chatkit.server import ChatKitServer
from chatkit.types import (
    Action,
    AssistantMessageContent,
    AssistantMessageItem,
    ClientEffectEvent,
    ThreadItemDoneEvent,
    ThreadMetadata,
    ThreadStreamEvent,
    UserMessageItem,
    WidgetItem,
)

from ecommerce.context import ECommerceAgentChatContext, ECommerceAgentContext, create_initial_context, public_context
from ecommerce.agents import (
    after_sales_agent,
    escalation_agent,
    faq_agent,
    order_tracking_agent,
    product_inquiry_agent,
    triage_agent,
)
from memory_store import MemoryStore


ALL_AGENTS = [
    triage_agent,
    order_tracking_agent,
    product_inquiry_agent,
    after_sales_agent,
    faq_agent,
    escalation_agent,
]

AGENT_MAP = {a.name: a for a in ALL_AGENTS}


class AgentEvent(BaseModel):
    id: str
    type: str
    agent: str
    content: str
    metadata: Optional[Dict[str, Any]] = None
    timestamp: Optional[float] = None


class GuardrailCheck(BaseModel):
    id: str
    name: str
    input: str
    reasoning: str
    passed: bool
    timestamp: float


def _get_agent_by_name(name: str):
    return AGENT_MAP.get(name, triage_agent)


def _get_guardrail_name(g) -> str:
    name_attr = getattr(g, "name", None)
    if isinstance(name_attr, str) and name_attr:
        return name_attr
    guard_fn = getattr(g, "guardrail_function", None)
    if guard_fn is not None and hasattr(guard_fn, "__name__"):
        return guard_fn.__name__.replace("_", " ").title()
    fn_name = getattr(g, "__name__", None)
    if isinstance(fn_name, str) and fn_name:
        return fn_name.replace("_", " ").title()
    return str(g)


def _build_agents_list() -> List[Dict[str, Any]]:
    def make_agent_dict(agent):
        return {
            "name": agent.name,
            "description": getattr(agent, "handoff_description", ""),
            "handoffs": [getattr(h, "agent_name", getattr(h, "name", "")) for h in getattr(agent, "handoffs", [])],
            "tools": [getattr(t, "name", getattr(t, "__name__", "")) for t in getattr(agent, "tools", [])],
            "input_guardrails": [_get_guardrail_name(g) for g in getattr(agent, "input_guardrails", [])],
        }
    return [make_agent_dict(a) for a in ALL_AGENTS]


def _user_message_to_text(message: UserMessageItem) -> str:
    parts: List[str] = []
    for part in message.content:
        text = getattr(part, "text", "")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _parse_tool_args(raw_args: Any) -> Any:
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args)
        except Exception:
            return raw_args
    return raw_args


@dataclass
class ConversationState:
    input_items: List[Any] = field(default_factory=list)
    context: ECommerceAgentContext = field(default_factory=create_initial_context)
    current_agent_name: str = triage_agent.name
    events: List[AgentEvent] = field(default_factory=list)
    guardrails: List[GuardrailCheck] = field(default_factory=list)


class ECommerceServer(ChatKitServer[dict[str, Any]]):
    """电商客服 AI 智能体服务器"""

    def __init__(self) -> None:
        self.store = MemoryStore()
        super().__init__(self.store)
        self._state: Dict[str, ConversationState] = {}
        self._listeners: Dict[str, list[asyncio.Queue]] = {}
        self._last_event_index: Dict[str, int] = {}
        self._last_snapshot: Dict[str, str] = {}

    def _state_for_thread(self, thread_id: str) -> ConversationState:
        if thread_id not in self._state:
            self._state[thread_id] = ConversationState()
        return self._state[thread_id]

    async def _ensure_thread(self, thread_id: Optional[str], context: dict[str, Any]) -> ThreadMetadata:
        if thread_id:
            try:
                return await self.store.load_thread(thread_id, context)
            except NotFoundError:
                pass
        new_thread = ThreadMetadata(id=self.store.generate_thread_id(context), created_at=datetime.now())
        await self.store.save_thread(new_thread, context)
        self._state_for_thread(new_thread.id)
        return new_thread

    async def ensure_thread(self, thread_id: Optional[str], context: dict[str, Any]) -> ThreadMetadata:
        return await self._ensure_thread(thread_id, context)

    def _record_guardrails(self, agent_name: str, input_text: str, guardrail_results: List[Any]) -> List[GuardrailCheck]:
        checks: List[GuardrailCheck] = []
        timestamp = time.time() * 1000
        agent = _get_agent_by_name(agent_name)
        for guardrail in getattr(agent, "input_guardrails", []):
            result = next((r for r in guardrail_results if r.guardrail == guardrail), None)
            reasoning = ""
            passed = True
            if result:
                info = getattr(result.output, "output_info", None)
                reasoning = getattr(info, "reasoning", "") or reasoning
                passed = not result.output.tripwire_triggered
            checks.append(
                GuardrailCheck(
                    id=uuid4().hex, name=_get_guardrail_name(guardrail),
                    input=input_text, reasoning=reasoning, passed=passed, timestamp=timestamp,
                )
            )
        return checks

    @staticmethod
    def _truncate(val: Any, limit: int = 200) -> Any:
        if isinstance(val, str) and len(val) > limit:
            return val[:limit] + "…"
        return val

    def _record_events(self, run_items: List[Any], current_agent_name: str, thread_id: str):
        events: List[AgentEvent] = []
        active_agent = current_agent_name
        for item in run_items:
            now_ms = time.time() * 1000
            if isinstance(item, MessageOutputItem):
                events.append(AgentEvent(
                    id=uuid4().hex, type="message", agent=item.agent.name,
                    content=self._truncate(ItemHelpers.text_message_output(item)), timestamp=now_ms,
                ))
            elif isinstance(item, HandoffOutputItem):
                events.append(AgentEvent(
                    id=uuid4().hex, type="handoff", agent=item.source_agent.name,
                    content=f"{item.source_agent.name} -> {item.target_agent.name}",
                    metadata={"source_agent": item.source_agent.name, "target_agent": item.target_agent.name},
                    timestamp=now_ms,
                ))
                active_agent = item.target_agent.name
            elif isinstance(item, ToolCallItem):
                tool_name = getattr(item.raw_item, "name", None)
                raw_args = getattr(item.raw_item, "arguments", None)
                events.append(AgentEvent(
                    id=uuid4().hex, type="tool_call", agent=item.agent.name,
                    content=self._truncate(tool_name or ""),
                    metadata={"tool_args": self._truncate(_parse_tool_args(raw_args))},
                    timestamp=now_ms,
                ))
            elif isinstance(item, ToolCallOutputItem):
                events.append(AgentEvent(
                    id=uuid4().hex, type="tool_output", agent=item.agent.name,
                    content=self._truncate(str(item.output)),
                    metadata={"tool_result": self._truncate(item.output)},
                    timestamp=now_ms,
                ))
        return events, active_agent

    async def respond(
        self, thread: ThreadMetadata, input_user_message: UserMessageItem | None, context: dict[str, Any],
    ) -> AsyncIterator[ThreadStreamEvent]:
        state = self._state_for_thread(thread.id)
        user_text = ""
        if input_user_message is not None:
            user_text = _user_message_to_text(input_user_message)
            state.input_items.append({"content": user_text, "role": "user"})

        previous_context = public_context(state.context)
        chat_context = ECommerceAgentChatContext(
            thread=thread, store=self.store, request_context=context, state=state.context,
        )

        yield ClientEffectEvent(name="runner_bind_thread", data={"thread_id": thread.id, "ts": time.time()})

        try:
            result = Runner.run_streamed(
                _get_agent_by_name(state.current_agent_name), state.input_items, context=chat_context,
            )
            async for event in stream_agent_response(chat_context, result):
                if hasattr(event, "item"):
                    try:
                        new_events, active_agent = self._record_events(
                            [getattr(event, "item")], state.current_agent_name, thread.id
                        )
                        if new_events:
                            state.events.extend(new_events)
                            state.current_agent_name = active_agent
                            yield ClientEffectEvent(
                                name="runner_event_delta",
                                data={"thread_id": thread.id, "ts": time.time(),
                                      "events": [e.model_dump() for e in new_events]},
                            )
                    except Exception:
                        pass
                yield event
        except MaxTurnsExceeded:
            pass
        except InputGuardrailTripwireTriggered as exc:
            refusal = "抱歉，我只能回答电商客服相关的问题。有什么购物方面需要帮助的吗？"
            state.input_items.append({"role": "assistant", "content": refusal})
            yield ThreadItemDoneEvent(
                item=AssistantMessageItem(
                    id=self.store.generate_item_id("message", thread, context),
                    thread_id=thread.id, created_at=datetime.now(),
                    content=[AssistantMessageContent(text=refusal)],
                )
            )
            return

        state.input_items = result.to_input_list()
        state.current_agent_name = getattr(result.last_agent, "name", state.current_agent_name)
        state.guardrails = self._record_guardrails(
            agent_name=state.current_agent_name, input_text=user_text,
            guardrail_results=result.input_guardrail_results,
        )

    async def action(
        self, thread: ThreadMetadata, action: Action[str, Any],
        sender: WidgetItem | None, context: dict[str, Any],
    ) -> AsyncIterator[ThreadStreamEvent]:
        if False:
            yield

    async def snapshot(self, thread_id: Optional[str], context: dict[str, Any]) -> Dict[str, Any]:
        thread = await self._ensure_thread(thread_id, context)
        state = self._state_for_thread(thread.id)
        return {
            "thread_id": thread.id,
            "current_agent": state.current_agent_name,
            "context": public_context(state.context),
            "agents": _build_agents_list(),
            "events": [e.model_dump() for e in state.events],
            "guardrails": [g.model_dump() for g in state.guardrails],
        }

    def _register_listener(self, thread_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._listeners.setdefault(thread_id, []).append(q)
        last = self._last_snapshot.get(thread_id)
        if last:
            try:
                q.put_nowait(last)
            except asyncio.QueueFull:
                pass
        return q

    def register_listener(self, thread_id: str) -> asyncio.Queue:
        return self._register_listener(thread_id)

    def _unregister_listener(self, thread_id: str, queue: asyncio.Queue) -> None:
        listeners = self._listeners.get(thread_id, [])
        if queue in listeners:
            listeners.remove(queue)
        if not listeners and thread_id in self._listeners:
            self._listeners.pop(thread_id, None)

    def unregister_listener(self, thread_id: str, queue: asyncio.Queue) -> None:
        self._unregister_listener(thread_id, queue)
