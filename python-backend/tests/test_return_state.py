"""退货状态机测试 —— **本文件在 T4 完成前预期为红**。

覆盖三处只有"多轮"才暴露的缺陷（现有 smoke 测试是单轮"输入→断言状态码"，
永远发现不了）：
  E-7  同一订单重复发起退货会产生多个不同单号；且全仓无撤销 / 无查询退货进度的入口
  E-8  cancel_order 只改 Context，下一次 order_status 查询把状态覆盖回数据源的值
  E-9  演示数据里没有「待发货」订单，cancel_order 的成功分支不可达

与实现层的约定（T4 需满足）：
  - 退货统一入口 initiate_return_tool，通过 action 参数区分 create / cancel / status
    （不新增 function_tool，见决策 D2）
  - 权威数据源是 order_store（orders / return_cases 两张表），Context 只作展示缓存
    （见决策 D7）

关键技巧：@function_tool 装饰后不能直接调用，需取出其包装的原始协程函数
（第一个参数是 RunContextWrapper）。参考实现见
`ai-cs-agent-personal-notes/audit_probes/state_probe.py`。
"""
from __future__ import annotations

import asyncio
import re
import sqlite3

from ecommerce import tools

# 从工具返回的文本里抽退货单号：断言"顾客看到的东西"，而不是内部字段
CASE_ID_RE = re.compile(r"RTN-[A-Za-z0-9\-]+")
# 从 order_status_tool 的返回文本里抽订单状态（"状态: 已签收"）
ORDER_STATUS_RE = re.compile(r"状态[:：]\s*(\S+)")


# ==================== 会话与工具调用基础设施 ====================

def _raw(tool):
    """取出 @function_tool 包装前的原始 async 函数"""
    return tool.on_invoke_tool._get_wrapped_callable()


class _Session:
    """模拟一次买家会话：跨轮复用同一个 Context，才能暴露"多轮状态不一致"。

    每个用例用独立的订单号 + 独立的会话，保证彼此不干扰、可重复执行。
    """

    def __init__(self) -> None:
        from datetime import datetime

        from agents import RunContextWrapper
        from chatkit.types import ThreadMetadata

        from ecommerce.context import ECommerceAgentChatContext, ECommerceAgentContext
        from memory_store import MemoryStore

        self.state = ECommerceAgentContext()
        chat = ECommerceAgentChatContext(
            thread=ThreadMetadata(id="test-return", created_at=datetime.now()),
            store=MemoryStore(),
            request_context={},
            state=self.state,
        )
        self.wrap = RunContextWrapper(context=chat)

    def call(self, tool, **kwargs) -> str:
        return asyncio.run(_raw(tool)(self.wrap, **kwargs))


def _order_store():
    """延迟取 order_store。

    它是 T4 才新建的模块，放在函数内导入有两个好处：
      1. T4 之前失败的是"功能未实现"（ImportError），而不是整个文件收集失败；
      2. 该文件在 T4 之前本就预期为红，失败点清晰可见。
    """
    import order_store

    return order_store


def _new_order(order_number: str, status: str = "已签收", paid: str = "69.90") -> str:
    """插入一张独立订单，返回单号。

    E-9：演示数据 MOCK_ORDERS 的三单全是「已发货 / 已签收 / 已签收」，
    没有「待发货」，导致 cancel_order 的成功分支不可达。
    因此用例所需的订单一律由测试自己造，不依赖演示数据。
    """
    os_ = _order_store()
    os_.init_order_store()
    os_.upsert_order(
        {
            "order_number": order_number,
            "customer_name": "测试用户",
            "account_id": "U-TEST-01",
            "status": status,
            "items": [
                {
                    "product_id": "P1002",
                    "name": "三只松鼠每日坚果 750g",
                    "sku": "30袋装",
                    "price": "69.90",
                    "quantity": 1,
                }
            ],
            "total_amount": paid,
            "paid_amount": paid,
            "coupon_used": "",
            "tracking": {},
            "order_time": "2026-09-28 10:00:00",
            "shipping_address": "测试地址",
        }
    )
    return order_number


def _case_ids(text: str) -> list[str]:
    return CASE_ID_RE.findall(text or "")


def _order_status(session: _Session, order_number: str) -> str:
    text = session.call(tools.order_status_tool, order_number=order_number)
    match = ORDER_STATUS_RE.search(text)
    assert match, f"未能从 order_status_tool 返回中解析状态：{text!r}"
    return match.group(1)


# ==================== 用例 ====================

def test_initiate_return_is_idempotent():
    """用例 1（E-7 幂等）：同一订单连续两次发起退货，必须返回同一个退货单号。

    现状：单号由 random.randint 生成（tools.py），同一订单会堆积多个在途单。
    """
    order = _new_order("TB20260928IDEM")
    session = _Session()

    first = session.call(
        tools.initiate_return_tool, order_number=order, reason="第一次申请", action="create"
    )
    second = session.call(
        tools.initiate_return_tool, order_number=order, reason="第二次申请", action="create"
    )

    ids_first, ids_second = _case_ids(first), _case_ids(second)
    assert ids_first, f"第一次发起退货未返回退货单号：{first!r}"
    assert ids_second, f"第二次发起退货未返回退货单号：{second!r}"
    assert ids_first[0] == ids_second[0], (
        f"同一订单重复发起退货产生了不同单号：{ids_first[0]} vs {ids_second[0]}"
    )


def test_cancel_return_marks_case_cancelled():
    """用例 2（E-7 撤销）：存在撤销能力，撤销后状态为「已撤销」。

    现状：全仓没有任何撤销退货的入口（13 个工具里无 cancel_return / withdraw）。
    """
    order = _new_order("TB20260928CANCEL")
    session = _Session()

    session.call(
        tools.initiate_return_tool, order_number=order, reason="包装破损", action="create"
    )
    text = session.call(tools.initiate_return_tool, order_number=order, action="cancel")

    assert "已撤销" in text, f"撤销退货未返回「已撤销」状态：{text!r}"


def test_cancel_return_rolls_back_order_status():
    """用例 3（E-8 回归护栏）：撤销退货后，订单状态回到发起退货之前的值。

    注意这条**不是一个能驱动实现的失败用例**：现状的 initiate_return_tool 根本不修改
    订单状态（只看不改，见 tools.py），所以"撤销后回到原值"在 T4 之前就是恒真的。
    它当前之所以是红的，只是因为夹具 `_new_order` 依赖 T4 才有的 order_store。
    保留它的目的是防止 T4 引入"撤销时把订单状态写成已取消 / 写丢"这类新缺陷。
    """
    order = _new_order("TB20260928ROLLBACK")
    session = _Session()

    before = _order_status(session, order)
    session.call(
        tools.initiate_return_tool, order_number=order, reason="不想要了", action="create"
    )
    session.call(tools.initiate_return_tool, order_number=order, action="cancel")
    after = _order_status(session, order)

    assert after == before, f"撤销退货后订单状态未回到原值：{before!r} → {after!r}"


def test_return_status_can_be_queried():
    """用例 4（E-7 可查）：有查询退货进度的入口，且返回当前状态。

    现状：return_status 写进 Context 后成为孤岛，没有任何工具能读取它。
    """
    order = _new_order("TB20260928STATUS")
    session = _Session()

    session.call(
        tools.initiate_return_tool, order_number=order, reason="质量问题", action="create"
    )
    text = session.call(tools.initiate_return_tool, order_number=order, action="status")

    assert "待审核" in text, f"查询退货进度未返回「待审核」：{text!r}"


def test_return_survives_session_restart():
    """用例 5（持久化）：状态落在 SQLite 里，换一个新会话（Context 全空）仍能查到。

    这里不真的起子进程（成本高、CI 慢），而是双保险：
      a) 直接查 return_cases 表，确认退货单已落盘；
      b) 换一个全新的 _Session（等价于"进程重启后 Context 为空"）再查进度。
    真实的重启验证由 T4.4 手工验收覆盖。
    """
    order = _new_order("TB20260928PERSIST")
    session = _Session()

    first = session.call(
        tools.initiate_return_tool, order_number=order, reason="持久化验证", action="create"
    )
    case_id = _case_ids(first)[0]

    os_ = _order_store()
    conn = sqlite3.connect(str(os_.DB_PATH))
    try:
        rows = conn.execute(
            "SELECT case_id, status FROM return_cases WHERE case_id = ?", (case_id,)
        ).fetchall()
    finally:
        conn.close()
    assert rows, f"退货单 {case_id} 未落盘到 {os_.DB_PATH}"
    assert rows[0][1] == "待审核", f"落盘状态不是「待审核」：{rows[0][1]!r}"

    restarted = _Session()
    text = restarted.call(
        tools.initiate_return_tool, order_number=order, action="status"
    )
    assert "待审核" in text, f"新会话查不到退货进度（状态仍在内存里？）：{text!r}"


def test_only_pending_return_can_be_cancelled():
    """用例 6（状态机合法性）：已通过 / 已寄回的退货单不可撤销，返回业务拒绝。"""
    order = _new_order("TB20260928ADVANCED")
    session = _Session()

    first = session.call(
        tools.initiate_return_tool, order_number=order, reason="尺码不合适", action="create"
    )
    case_id = _case_ids(first)[0]

    os_ = _order_store()
    os_.update_return_status(case_id, "已通过")

    text = session.call(tools.initiate_return_tool, order_number=order, action="cancel")

    assert "已撤销" not in text, f"已通过的退货单不该被撤销成功：{text!r}"
    assert "待审核" in text or "无法" in text or "不能" in text, (
        f"撤销被拒时应返回业务原因（说明仅「待审核」可撤）：{text!r}"
    )


def test_cancel_order_status_not_overwritten():
    """用例 7（E-8）：取消订单成功后，再查订单状态仍是「已取消」。

    现状：cancel_order_tool 只写 ctx.order_status，数据源未变，下一轮
    order_status_tool 从数据源读回原值覆盖掉「已取消」——用户看到的取消凭空消失。
    依赖 E-9 的修复：用例自造一张「待发货」订单，否则成功分支不可达。
    """
    order = _new_order("TB20260928PENDING", status="待发货")
    session = _Session()

    cancel_text = session.call(tools.cancel_order_tool, order_number=order)
    assert "无法取消" not in cancel_text, (
        f"取消被拒（订单状态可能不是「待发货」，E-9 未修）：{cancel_text!r}"
    )
    assert "成功取消" in cancel_text, f"未提示取消成功：{cancel_text!r}"

    # 关键断言：再查一次，读取的必须是权威数据源里的「已取消」，
    # 而不是被覆盖回原值「待发货」（那正是 E-8 的复现）。
    after = _order_status(session, order)
    assert after == "已取消", f"取消后再次查询，订单状态被覆盖回 {after!r}（E-8 复现）"


def test_return_on_missing_order_returns_friendly_error():
    """用例 8（健壮性）：对不存在的订单发起退货，返回友好错误而不是抛异常。"""
    session = _Session()

    text = session.call(
        tools.initiate_return_tool,
        order_number="TB20260928NOTEXIST",
        reason="随便",
        action="create",
    )

    assert isinstance(text, str) and text, "未返回文本"
    assert "未找到" in text, f"未给出友好错误提示：{text!r}"
