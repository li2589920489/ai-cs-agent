"""API 契约测试 —— 锁定接口的鉴权行为，避免无意间改动破坏前端。

本文件的定位与 test_smoke.py 不同：smoke 测的是「功能是否可用」，
这里测的是「接口对外契约是否被改动」。

⚠️ 用例 1 断言的是**现状（200）而非正确（401）**，这是刻意的，见下。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from main import app  # noqa: E402

client = TestClient(app)


def test_chat_poll_allows_anonymous():
    """用例 1（特征化测试）：`/api/chat/poll` 匿名可读，返回 200。

    ⚠️ 这条断言的是**现状**，不是**正确**。该接口当前无鉴权，可用任意 session_id
    读到他人的坐席回复（IDOR）。维护者已决策（D3）本轮不加鉴权——加鉴权需要前端
    买家侧同时携带会话令牌改造，而 T4（退货状态机）已是最高风险任务，不叠加新变量。
    风险作为已知限制保留，在代码评审时主动说明该项取舍。

    本用例的价值是**变更探测**：一旦将来有人给该接口加上鉴权，它会立刻变红，
    提示"必须同步改造前端"。请勿为了让本用例变绿而删掉它，也请勿"顺手"给接口加鉴权。
    """
    r = client.get("/api/chat/poll", params={"session_id": "no-such-session"})

    assert r.status_code == 200
    assert r.json() == {"manual": False, "messages": []}


def test_escalations_list_requires_agent_role():
    """用例 2（回归保护）：工单列表无凭据 → 401。现状已正确，防止被改回去。"""
    r = client.get("/api/escalations")

    assert r.status_code == 401


def test_ticket_messages_requires_agent_role():
    """用例 3（回归保护）：工单消息读取无凭据 → 401（曾为零鉴权，已修复）。"""
    r = client.get("/api/escalations/TKT-NOTEXIST/messages")

    assert r.status_code == 401


def test_knowledge_list_allows_anonymous():
    """用例 4：匿名读知识库 → 200，且返回 {items, total} 结构。

    与 test_smoke.py::test_knowledge_get_without_key_allowed 有意重叠：smoke 那条验的是
    "匿名读放行"这一功能点，本条验的是响应结构契约（前端 knowledge-panel 依赖 items/total）。
    """
    r = client.get("/api/knowledge")

    assert r.status_code == 200
    body = r.json()
    assert "items" in body and isinstance(body["items"], list)
    assert "total" in body and body["total"] == len(body["items"])
