"""订单与退货状态存储 —— 订单 / 退货单的权威数据源（SQLite）。

为什么需要它：此前订单数据是 `ecommerce/demo_data.py` 里的常量字典，退货状态只写在
Agent Context 里，于是长出两个只有「多轮」才暴露的缺陷：

  - 退货单号每次用 `random.randint` 生成 → 同一订单可以堆出多个在途单（无幂等）
  - `cancel_order` 只改 Context，数据源没变 → 下一轮 `order_status_tool` 从数据源读回
    原值，把用户看到的「已取消」覆盖掉（E-8）

状态一旦落到权威数据源，就同时获得了三件事：可查询、可持久化（重启不丢）、可幂等。

设计约束（对齐 AGENTS.md）：
  - 两表都带 `tenant_id`（决策 D4：建字段、默认 'default'、暂不做隔离查询）
  - 不引入新依赖（只用标准库 sqlite3）
  - 订单状态取值沿用 demo_data，不新增；退货状态按决策 D5 固定 5 态

**写库纪律（承接知识库指纹的教训）**：任何"幂等 / 无变更"路径不得无条件刷新
`updated_at`，否则会把"什么都没发生"写成"刚变更过"。本模块只在内容确实改变时写。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

# 数据库路径可被环境变量覆盖（与 knowledge_store / auth 同一模式）：
# 测试指向临时库，避免验收脚本把订单和退货单写进开发库、污染 T4.4 的重启验收。
DB_PATH = Path(
    os.getenv("ORDER_DB_PATH")
    or (Path(__file__).resolve().parent / "data" / "orders.db")
)

# 订单状态：与 demo_data 保持一致，不新增取值
ORDER_STATUSES = ("待付款", "待发货", "已发货", "运输中", "已签收", "已取消")

# 退货状态机（决策 D5）：待审核 → 已通过 → 已寄回 → 已退款，外加 已撤销
RETURN_STATUSES = ("待审核", "已通过", "已寄回", "已退款", "已撤销")
# 在途 = 尚未走到终态（已退款 / 已撤销）
ACTIVE_RETURN_STATUSES = ("待审核", "已通过", "已寄回")
# 可撤销（决策 D6「未进入下一状态即可撤销」在当前状态机下的具体化）
CANCELLABLE_RETURN_STATUSES = ("待审核",)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ==================== 连接与建表 ====================

def _get_conn() -> sqlite3.Connection:
    """获取连接并确保表结构存在"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_number     TEXT NOT NULL,
            tenant_id        TEXT NOT NULL DEFAULT 'default',
            customer_name    TEXT DEFAULT '',
            account_id       TEXT DEFAULT '',
            status           TEXT NOT NULL,
            items_json       TEXT DEFAULT '[]',
            total_amount     TEXT DEFAULT '',
            paid_amount      TEXT DEFAULT '',
            coupon_used      TEXT DEFAULT '',
            tracking_json    TEXT DEFAULT '{}',
            order_time       TEXT DEFAULT '',
            shipping_address TEXT DEFAULT '',
            updated_at       TEXT DEFAULT '',
            PRIMARY KEY (order_number, tenant_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS return_cases (
            case_id           TEXT PRIMARY KEY,
            order_number      TEXT NOT NULL,
            tenant_id         TEXT NOT NULL DEFAULT 'default',
            status            TEXT NOT NULL,
            reason            TEXT DEFAULT '',
            refund_amount     TEXT DEFAULT '',
            prev_order_status TEXT DEFAULT '',
            created_at        TEXT DEFAULT '',
            updated_at        TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_return_cases_order "
        "ON return_cases(order_number, tenant_id)"
    )
    # 一张订单最多一张在途退货单 —— 把幂等约束下沉到数据库，防住并发下的重复建单
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_return_cases_active "
        "ON return_cases(order_number, tenant_id) "
        "WHERE status IN ('待审核', '已通过', '已寄回')"
    )
    conn.commit()
    return conn


_initialized = False
_init_lock = threading.Lock()


def _ensure_init() -> None:
    """首次使用时自动建表 + 灌入演示订单。

    放在存储层而不是只靠 main.py 调用：工具可能被直接导入使用（测试、脚本、
    MCP server），不能假设调用方记得先初始化。
    """
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if not _initialized:
            init_order_store()
            _initialized = True


def init_order_store(seed: bool = True) -> int:
    """建表；orders 为空时把演示订单灌入。返回本次灌入的条数。

    只在空表时灌入：否则每次启动都会把演示订单写回去，用户已取消的订单会"复活"。
    """
    conn = _get_conn()
    try:
        if not seed:
            return 0
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        if count:
            return 0
        # 延迟导入：避免与 ecommerce.tools 形成导入环
        from ecommerce.demo_data import MOCK_ORDERS

        for order in MOCK_ORDERS.values():
            _write_order(conn, order, "default")
        conn.commit()
        print(f"[order] 首次建库：灌入 {len(MOCK_ORDERS)} 张演示订单", flush=True)
        return len(MOCK_ORDERS)
    finally:
        conn.close()


# ==================== 行 <-> 字典 ====================

def _row_to_order(row: sqlite3.Row) -> dict:
    """把订单行转成工具层原本期望的结构（items 是 list，tracking 是 dict）"""
    d = dict(row)
    d["items"] = json.loads(d.pop("items_json") or "[]")
    d["tracking"] = json.loads(d.pop("tracking_json") or "{}")
    return d


def _write_order(conn: sqlite3.Connection, order: dict, tenant_id: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO orders
        (order_number, tenant_id, customer_name, account_id, status, items_json,
         total_amount, paid_amount, coupon_used, tracking_json, order_time,
         shipping_address, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order.get("order_number", ""),
            tenant_id,
            order.get("customer_name", ""),
            order.get("account_id", ""),
            order.get("status", ""),
            json.dumps(order.get("items", []), ensure_ascii=False),
            order.get("total_amount", ""),
            order.get("paid_amount", ""),
            order.get("coupon_used") or "",
            json.dumps(order.get("tracking", {}) or {}, ensure_ascii=False),
            order.get("order_time", ""),
            order.get("shipping_address", ""),
            _now(),
        ),
    )


# ==================== 订单读写 ====================

def get_order(order_number: str, tenant_id: str = "default") -> dict | None:
    """按订单号取订单（工具层原本用 demo_data.get_order 的地方改用它）"""
    _ensure_init()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_number = ? AND tenant_id = ?",
            (order_number, tenant_id),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_order(row) if row else None


def list_orders(tenant_id: str = "default", account_id: str | None = None) -> list[dict]:
    """列出订单；给 account_id 时只返回该账户的订单。

    account_id 过滤必须下推到 SQL：`list_customer_orders_tool` 依赖它，
    不能全表捞出后在应用层筛。
    """
    _ensure_init()
    sql = "SELECT * FROM orders WHERE tenant_id = ?"
    params: list[str] = [tenant_id]
    if account_id:
        sql += " AND account_id = ?"
        params.append(account_id)
    sql += " ORDER BY order_time DESC"

    conn = _get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_order(r) for r in rows]


def upsert_order(order: dict, tenant_id: str = "default") -> None:
    """写入 / 覆盖一张订单。

    这是订单数据的唯一写入口（演示订单灌入、测试夹具、将来的真实订单接入都走它）。
    """
    if not order.get("order_number"):
        raise ValueError("order 必须包含 order_number")
    _ensure_init()
    conn = _get_conn()
    try:
        _write_order(conn, order, tenant_id)
        conn.commit()
    finally:
        conn.close()


def update_order_status(order_number: str, status: str, tenant_id: str = "default") -> bool:
    """更新订单状态。返回是否命中订单。

    只有状态真的要变时才写 —— 无变更的 UPDATE 会把 updated_at 刷成"刚刚变过"，
    与知识库指纹踩过的坑是同一类问题。
    """
    if status not in ORDER_STATUSES:
        raise ValueError(f"未知订单状态：{status}（可选：{'/'.join(ORDER_STATUSES)}）")
    _ensure_init()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM orders WHERE order_number = ? AND tenant_id = ?",
            (order_number, tenant_id),
        ).fetchone()
        if row is None:
            return False
        if row["status"] == status:
            return True
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_number = ? AND tenant_id = ?",
            (status, _now(), order_number, tenant_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# ==================== 退货单 ====================

def _row_to_case(row: sqlite3.Row) -> dict:
    return dict(row)


def _active_return(conn: sqlite3.Connection, order_number: str, tenant_id: str) -> dict | None:
    placeholders = ", ".join("?" * len(ACTIVE_RETURN_STATUSES))
    row = conn.execute(
        f"""
        SELECT * FROM return_cases
        WHERE order_number = ? AND tenant_id = ? AND status IN ({placeholders})
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (order_number, tenant_id, *ACTIVE_RETURN_STATUSES),
    ).fetchone()
    return _row_to_case(row) if row else None


def _get_return_case(conn: sqlite3.Connection, case_id: str, tenant_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM return_cases WHERE case_id = ? AND tenant_id = ?",
        (case_id, tenant_id),
    ).fetchone()
    return _row_to_case(row) if row else None


def active_return_for(order_number: str, tenant_id: str = "default") -> dict | None:
    """查该订单的在途退货单（待审核 / 已通过 / 已寄回），没有则 None"""
    _ensure_init()
    conn = _get_conn()
    try:
        return _active_return(conn, order_number, tenant_id)
    finally:
        conn.close()


def create_return_case(
    order_number: str,
    reason: str = "",
    refund_amount: str = "",
    prev_order_status: str = "",
    tenant_id: str = "default",
) -> dict:
    """发起退货（**幂等**）：该订单已有在途单时直接返回它，不新建。

    为什么必须有幂等：此前单号靠 random 生成，用户在多轮对话里重复说"我要退货"
    就会堆出多个在途单，退款金额会被重复计。数据库层还有一条唯一索引兜底并发。
    """
    _ensure_init()
    conn = _get_conn()
    try:
        existing = _active_return(conn, order_number, tenant_id)
        if existing:
            return existing

        now = _now()
        suffix = order_number[-6:]
        total = conn.execute(
            "SELECT COUNT(*) FROM return_cases WHERE order_number = ? AND tenant_id = ?",
            (order_number, tenant_id),
        ).fetchone()[0]

        for attempt in range(20):
            case_id = f"RTN-{suffix}-{100 + total + attempt}"
            try:
                conn.execute(
                    """
                    INSERT INTO return_cases
                    (case_id, order_number, tenant_id, status, reason,
                     refund_amount, prev_order_status, created_at, updated_at)
                    VALUES (?, ?, ?, '待审核', ?, ?, ?, ?, ?)
                    """,
                    (case_id, order_number, tenant_id, reason, refund_amount,
                     prev_order_status, now, now),
                )
                conn.commit()
                return _get_return_case(conn, case_id, tenant_id)
            except sqlite3.IntegrityError:
                # 两种情况：case_id 撞号（换一个重试），或唯一索引拦下并发建单
                # （说明另一个请求已经建好了在途单，直接复用它）。
                concurrent = _active_return(conn, order_number, tenant_id)
                if concurrent:
                    return concurrent
        raise RuntimeError(f"为订单 {order_number} 创建退货单失败：连续 20 次单号冲突")
    finally:
        conn.close()


def cancel_return_case(order_number: str, tenant_id: str = "default") -> dict | None:
    """撤销在途退货单。

    返回值的三种情况，调用方据此给出不同话术：
      - `None`                    该订单没有在途退货单
      - `status == '已撤销'`      撤销成功
      - 其它 status               进入下一状态后不可撤，按业务拒绝处理

    可撤销条件（决策 D6）：**未进入下一状态即可撤销**，在当前状态机下等于「仅待审核可撤」。
    """
    _ensure_init()
    conn = _get_conn()
    try:
        case = _active_return(conn, order_number, tenant_id)
        if case is None:
            return None
        if case["status"] not in CANCELLABLE_RETURN_STATUSES:
            return case  # 不可撤销：原样返回当前状态，由工具层拒绝

        conn.execute(
            "UPDATE return_cases SET status = '已撤销', updated_at = ? WHERE case_id = ?",
            (_now(), case["case_id"]),
        )

        # 回滚订单状态到发起退货之前。当前状态机下发起退货并不改订单状态（订单没有
        # "退货中"这个取值），所以通常是 no-op —— 这里只在状态确实不同时才写，
        # 并且避免把别的流程刚刚改过的状态覆盖回去。
        prev = case.get("prev_order_status") or ""
        if prev in ORDER_STATUSES:
            row = conn.execute(
                "SELECT status FROM orders WHERE order_number = ? AND tenant_id = ?",
                (order_number, tenant_id),
            ).fetchone()
            if row is not None and row["status"] != prev:
                conn.execute(
                    "UPDATE orders SET status = ?, updated_at = ? WHERE order_number = ? AND tenant_id = ?",
                    (prev, _now(), order_number, tenant_id),
                )

        conn.commit()
        return _get_return_case(conn, case["case_id"], tenant_id)
    finally:
        conn.close()


def get_return_status(order_number: str, tenant_id: str = "default") -> dict | None:
    """查询退货进度：优先返回在途单，否则返回最近的一张（含已撤销 / 已退款）"""
    _ensure_init()
    conn = _get_conn()
    try:
        case = _active_return(conn, order_number, tenant_id)
        if case:
            return case
        row = conn.execute(
            """
            SELECT * FROM return_cases
            WHERE order_number = ? AND tenant_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (order_number, tenant_id),
        ).fetchone()
        return _row_to_case(row) if row else None
    finally:
        conn.close()


def update_return_status(case_id: str, status: str, tenant_id: str = "default") -> bool:
    """推进退货单状态（已通过 / 已寄回 / 已退款 / 已撤销）。返回是否命中。

    决策 D5 保留了「已通过 / 已寄回 / 已退款」三态，但如果没有推进入口，这三态就是
    死状态、永远不可达。本函数是唯一的推进点（真正的"审核"动作不在本计划范围内，
    目前只服务于测试与将来的坐席端）。
    """
    if status not in RETURN_STATUSES:
        raise ValueError(f"未知退货状态：{status}（可选：{'/'.join(RETURN_STATUSES)}）")
    _ensure_init()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM return_cases WHERE case_id = ? AND tenant_id = ?",
            (case_id, tenant_id),
        ).fetchone()
        if row is None:
            return False
        if row["status"] == status:
            return True
        conn.execute(
            "UPDATE return_cases SET status = ?, updated_at = ? WHERE case_id = ? AND tenant_id = ?",
            (status, _now(), case_id, tenant_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()
