"""流式聊天（SSE）回归测试。

关键设计：**不真调 LLM** —— CI 没有 OPENAI_API_KEY（现有 test_smoke.py 也正是靠
skip 兼容无 key 环境）。这里用假的 RunResultStreaming 喂固定事件序列，断言
「协议正确性」与「顺序正确性」，而不是模型输出内容。

覆盖：
1. 帧序列正确：delta 全部早于 done，且 delta 能拼出完整文本
2. trace 帧带出被调用的工具名与 handoff 目标
3. SSE 必需响应头齐全（缺 X-Accel-Buffering 会被 nginx 缓冲）
4. /api/chat（非流式）返回结构未变 —— 抖音 webhook 与前端降级路径都依赖它
5. 空消息在 HTTP 层被拒（与 /api/chat 判错路径一致）
6. **delta 帧带 mid，且 done.reply 只含最终答复**（§4-D15：防中间前言泄漏）
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agents import MessageOutputItem
from main import app

client = TestClient(app)


# ==================== 假事件对象（模拟 agents.stream_events 的三类事件）====================

@dataclass
class _FakeAgent:
    name: str


@dataclass
class _FakeRawItem:
    name: str = ""


@dataclass
class _FakeItem:
    agent: _FakeAgent
    raw_item: _FakeRawItem = field(default_factory=_FakeRawItem)
    target_agent: _FakeAgent | None = None


@dataclass
class _FakeDelta:
    delta: str
    item_id: str | None = None
    type: str = "response.output_text.delta"


@dataclass
class _FakeRawEvent:
    data: object
    type: str = "raw_response_event"


@dataclass
class _FakeItemEvent:
    name: str
    item: object
    type: str = "run_item_stream_event"


def _msg_item(text: str) -> MessageOutputItem:
    """构造真实的 MessageOutputItem —— run_chat 与 _final_reply_text 都靠
    isinstance(item, MessageOutputItem) 取文本，用假对象测不出来。"""
    return MessageOutputItem(
        agent=_FakeAgent("商品知识 Agent"),
        raw_item=ResponseOutputMessage(
            id="msg_test", type="message", role="assistant", status="completed",
            content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
        ),
    )


class _FakeResultStreaming:
    """只实现被测代码用到的接口：stream_events() / last_agent / new_items。"""

    def __init__(self, events, new_items=None, last_agent_name="商品知识 Agent"):
        self._events = events
        self.last_agent = _FakeAgent(last_agent_name)
        self.new_items = new_items if new_items is not None else []

    async def stream_events(self):
        for e in self._events:
            yield e


MID_FINAL = "item_final"


def _default_events():
    """一条完整链路：分诊 handoff → 工具调用 → 文本增量（单条消息，无前言）。"""
    events = [
        _FakeItemEvent(
            name="handoff_occured",
            item=_FakeItem(agent=_FakeAgent("Triage Agent"),
                           target_agent=_FakeAgent("商品知识 Agent")),
        ),
        _FakeItemEvent(
            name="tool_called",
            item=_FakeItem(agent=_FakeAgent("商品知识 Agent"),
                           raw_item=_FakeRawItem("search_products_tool")),
        ),
        _FakeRawEvent(data=_FakeDelta(delta="我们", item_id=MID_FINAL)),
        _FakeRawEvent(data=_FakeDelta(delta="有坚果。", item_id=MID_FINAL)),
    ]
    return events, [_msg_item("我们有坚果。")]


def _preamble_events():
    """真实复现的形态：模型先吐一句英文前言（mid=A），再调工具，最后给中文答复（mid=B）。

    §4-D15 的核心场景 —— 非流式只保留 B，流式必须让前端能凭 mid 把 A 替换掉。
    """
    events = [
        _FakeItemEvent(
            name="handoff_occured",
            item=_FakeItem(agent=_FakeAgent("Triage Agent"),
                           target_agent=_FakeAgent("商品知识 Agent")),
        ),
        _FakeRawEvent(data=_FakeDelta(delta="I'll route", item_id="item_preamble")),
        _FakeRawEvent(data=_FakeDelta(delta=" you to our specialist.", item_id="item_preamble")),
        _FakeItemEvent(
            name="tool_called",
            item=_FakeItem(agent=_FakeAgent("商品知识 Agent"),
                           raw_item=_FakeRawItem("search_products_tool")),
        ),
        _FakeItemEvent(
            name="tool_output",
            item=_FakeItem(agent=_FakeAgent("商品知识 Agent")),
        ),
        _FakeRawEvent(data=_FakeDelta(delta="为您找到", item_id=MID_FINAL)),
        _FakeRawEvent(data=_FakeDelta(delta="几款坚果。", item_id=MID_FINAL)),
    ]
    # 非流式口径：只有最后一条助手消息
    return events, [_msg_item("I'll route you to our specialist."), _msg_item("为您找到几款坚果。")]


def _patch_stream(monkeypatch, events, new_items):
    import chat_service

    monkeypatch.setattr(
        chat_service.Runner, "run_streamed",
        lambda *a, **k: _FakeResultStreaming(events, new_items),
    )


@pytest.fixture()
def fake_stream(monkeypatch):
    """把 chat_service.Runner.run_streamed 换成返回固定事件序列的假函数。"""
    events, items = _default_events()
    _patch_stream(monkeypatch, events, items)
    return events


def _parse_sse(raw: str):
    """把 SSE 原始文本解析成 [(event, data_dict), ...]"""
    frames = []
    for block in [b for b in raw.split("\n\n") if b.strip()]:
        ev, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if ev:
            frames.append((ev, data))
    return frames


def _collect(agen):
    """同步收集异步生成器的全部产出（服务层直调用例需要）。"""
    async def _run():
        return [f async for f in agen]
    return asyncio.run(_run())


_SENTINEL = object()


def _last_text_of_mid(frames, mid=None):
    """前端替换语义的等价实现：同一 mid 内追加，mid 变化则替换 → 返回末段文本。"""
    text, cur = "", _SENTINEL
    for name, d in frames:
        if name != "delta":
            continue
        if d.get("mid") != cur:
            cur, text = d.get("mid"), d["text"]
        else:
            text += d["text"]
    return text


# ==================== 用例 ====================

def test_stream_frame_sequence(fake_stream):
    """帧序列：delta 必须在 done 之前，且 delta 能拼出完整文本。"""
    r = client.post("/api/chat/stream",
                    json={"message": "你们有什么坚果", "session_id": "t-frame"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(r.text)
    names = [n for n, _ in frames]
    assert names, "没有解析出任何帧"
    assert names[-1] == "done", f"末帧必须是 done，实际是 {names[-1]}"

    delta_idx = [i for i, n in enumerate(names) if n == "delta"]
    assert delta_idx, "没有任何 delta 帧"
    assert max(delta_idx) < names.index("done"), "delta 出现在 done 之后"

    text = "".join(d["text"] for n, d in frames if n == "delta")
    assert text == "我们有坚果。", f"delta 拼接结果不符：{text!r}"


def test_stream_trace_carries_tool_and_handoff(fake_stream):
    """trace 帧必须带出工具名与 handoff 目标（前端进度提示依赖它）。"""
    r = client.post("/api/chat/stream",
                    json={"message": "你们有什么坚果", "session_id": "t-trace"})
    traces = [d for n, d in _parse_sse(r.text) if n == "trace"]

    assert any(t.get("type") == "tool_call" and t.get("tool") == "search_products_tool"
               for t in traces), f"未取到 tool_call 帧：{traces}"
    handoffs = [t for t in traces if t.get("type") == "handoff"]
    assert any(t.get("to") == "商品知识 Agent" for t in handoffs), \
        f"handoff 帧未带出目标 Agent：{handoffs}"
    # 回归保护：不得出现 to=null 的无用 handoff 帧（handoff_requested 取不到 target_agent）
    assert all(t.get("to") for t in handoffs), f"存在 to 为空的 handoff 帧：{handoffs}"


def test_stream_sse_headers(fake_stream):
    """SSE 必需响应头齐全（缺 X-Accel-Buffering 会被 nginx 缓冲成一次性返回）。"""
    r = client.post("/api/chat/stream",
                    json={"message": "你好", "session_id": "t-headers"})
    assert r.headers.get("x-accel-buffering") == "no"
    assert "no-cache" in r.headers.get("cache-control", "")


def test_non_stream_endpoint_unchanged(monkeypatch):
    """回归：/api/chat 仍返回原结构（抖音 webhook 与降级路径依赖）。"""
    import chat_service

    async def _fake_run(msg, session_id=None):
        return {"reply": "ok", "final_agent": "Triage Agent", "trace": [],
                "escalation": False, "escalation_reason": "",
                "session_id": session_id, "state": chat_service.ECommerceAgentContext()}

    monkeypatch.setattr(chat_service, "run_chat", _fake_run)
    monkeypatch.setattr("main.run_chat", _fake_run)   # main 是 from import 的绑定名

    r = client.post("/api/chat", json={"message": "你好", "session_id": "t-nonstream"})
    assert r.status_code == 200
    body = r.json()
    for key in ("reply", "agent_trace", "final_agent", "session_id"):
        assert key in body, f"/api/chat 返回缺字段 {key}"


def test_stream_empty_message_rejected():
    """空消息在 HTTP 层被拒（与 /api/chat 一致的判错路径，不进 SSE 流）。"""
    r = client.post("/api/chat/stream", json={"message": "   ", "session_id": "t-empty"})
    assert r.status_code == 422


# ==================== §4-D15：中间前言不得泄漏 ====================

def test_delta_carries_message_id(monkeypatch):
    """delta 帧必须带 mid；同一条消息内 mid 不变。"""
    events, items = _default_events()
    _patch_stream(monkeypatch, events, items)

    r = client.post("/api/chat/stream", json={"message": "坚果", "session_id": "t-mid"})
    deltas = [d for n, d in _parse_sse(r.text) if n == "delta"]
    assert deltas, "无 delta 帧"
    assert all("mid" in d for d in deltas), f"delta 缺 mid 字段：{deltas}"
    assert {d["mid"] for d in deltas} == {MID_FINAL}, f"同一消息的 mid 应恒定：{deltas}"


def test_preamble_replaced_by_final_answer(monkeypatch):
    """核心回归：模型先吐英文前言再给中文答复时，
    ① delta 能按 mid 分段；② 末段文本 == 非流式口径的最终答复（前言被替换掉）。
    """
    events, items = _preamble_events()
    _patch_stream(monkeypatch, events, items)

    r = client.post("/api/chat/stream", json={"message": "坚果", "session_id": "t-preamble"})
    frames = _parse_sse(r.text)

    # ① 出现了两个不同的 mid（前言一段、答复一段）
    mids = [d.get("mid") for n, d in frames if n == "delta"]
    assert len(set(mids)) == 2, f"应出现两段消息，实际 mids={mids}"

    # ② 前端替换语义下，最终展示的文本只剩正式答复 —— 前言不残留
    shown = _last_text_of_mid(frames)
    assert shown == "为您找到几款坚果。", f"末段文本不符：{shown!r}"

    # ③ 非流式口径也是同一条（只取最后一条助手消息）
    from chat_service import _final_reply_text
    assert _final_reply_text(_FakeResultStreaming(events, items)) == "为您找到几款坚果。"


def test_done_frame_carries_authoritative_reply(monkeypatch):
    """done 帧带 reply 全文，供前端收流时校订 —— 展示文本终归要与 /api/chat 一致。"""
    events, items = _preamble_events()
    _patch_stream(monkeypatch, events, items)

    r = client.post("/api/chat/stream", json={"message": "坚果", "session_id": "t-done-reply"})
    frames = _parse_sse(r.text)
    done = [d for n, d in frames if n == "done"]
    assert len(done) == 1, f"应恰好一帧 done：{done}"
    assert done[0]["reply"] == "为您找到几款坚果。", f"done.reply 应为最终答复：{done[0]}"
    assert "I'll route" not in done[0]["reply"], "done.reply 不得含中间前言"


def test_early_reply_at_service_level():
    """服务层校验兜底：直调 run_chat_stream（绕过 HTTP 层的 422），
    应产出 delta + done 两帧且文本一致，不抛异常。
    """
    import chat_service

    frames = _collect(chat_service.run_chat_stream("   ", "t-early"))
    names = [n for n, _ in frames]
    assert names == ["delta", "done"], f"帧序列不符：{names}"
    assert frames[0][1]["text"] == "请发送有效消息。"
    assert frames[0][1]["mid"] is None, "非模型产出的帧 mid 应为 None"
    assert frames[1][1]["reply"] == "请发送有效消息。"
