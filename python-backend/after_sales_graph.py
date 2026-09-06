"""
LangGraph 售后流程状态机 — 用图编排表达售后处理的确定性业务流

相比线性 if/else 编排，LangGraph 提供：
    - 显式状态（State）在节点间流转
    - 条件路由（conditional edges）表达业务分支
    - 可中断/恢复、可插人工审批（human-in-the-loop）
    - 状态可持久化（checkpointer），便于审计与回放

流程：受理 → 查单(validate) → 判责(classify) ──条件路由──> 退货/退款/人工
本模块用确定性业务逻辑实现节点，不依赖 LLM 即可独立运行验证编排。
"""
from __future__ import annotations

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class AfterSalesState(TypedDict, total=False):
    """售后处理状态：在节点间流转的共享数据"""

    order_number: str
    reason: str
    order_status: str
    has_food_item: bool
    decision: str  # return | refund | escalate
    result: str


# ---------------- 节点：确定性业务逻辑 ----------------


def validate_order(state: AfterSalesState) -> dict:
    """查单：读取订单状态，判断是否含食品类商品"""
    from ecommerce.demo_data import get_order

    order = get_order(state["order_number"])
    if not order:
        return {"order_status": "不存在", "has_food_item": False, "decision": "escalate",
                "result": "订单号不存在，需人工核实"}

    food_keywords = ("坚果", "零食", "食品")
    has_food = any(kw in item["name"] for item in order["items"] for kw in food_keywords)
    return {"order_status": order["status"], "has_food_item": has_food}


def classify(state: AfterSalesState) -> dict:
    """判责：根据订单状态与商品类型决定处理路径"""
    if state.get("order_status") == "不存在":
        return {"decision": "escalate"}

    status = state["order_status"]
    # 食品类已签收 → 人工审核（拆封不支持无理由退货）
    if state.get("has_food_item") and status == "已签收":
        return {"decision": "escalate", "result": "食品已签收，需人工审核是否支持退货"}

    # 未发货 → 直接退款；已发货/运输中/已签收 → 走退货流程
    if status == "待发货":
        return {"decision": "refund"}
    return {"decision": "return"}


def route_after_classify(state: AfterSalesState) -> Literal["return", "refund", "escalate"]:
    """条件路由：根据判责结果进入对应处理节点"""
    return state["decision"]


def process_return(state: AfterSalesState) -> dict:
    """退货流程：生成退货单号"""
    order_no = state["order_number"]
    case_id = f"RTN-{order_no[-6:]}-{hash(order_no) % 1000:03d}"
    return {"result": f"退货单已生成：{case_id}，等待用户寄回商品后 1-3 个工作日退款"}


def process_refund(state: AfterSalesState) -> dict:
    """仅退款流程（未发货订单）"""
    return {"result": f"订单 {state['order_number']} 未发货，已受理仅退款，24 小时内原路退回"}


def escalate(state: AfterSalesState) -> dict:
    """人工兜底：转人工坐席"""
    reason = state.get("result") or state.get("reason", "复杂售后")
    return {"result": f"已转人工坐席处理，原因：{reason}"}


# ---------------- 图编排 ----------------


def build_after_sales_graph():
    """构建售后处理状态机"""
    g = StateGraph(AfterSalesState)

    g.add_node("validate", validate_order)
    g.add_node("classify", classify)
    g.add_node("process_return", process_return)
    g.add_node("process_refund", process_refund)
    g.add_node("escalate", escalate)

    g.add_edge(START, "validate")
    g.add_edge("validate", "classify")
    g.add_conditional_edges(
        "classify",
        route_after_classify,
        {"return": "process_return", "refund": "process_refund", "escalate": "escalate"},
    )
    g.add_edge("process_return", END)
    g.add_edge("process_refund", END)
    g.add_edge("escalate", END)

    return g.compile()


after_sales_graph = build_after_sales_graph()


def run_after_sales(order_number: str, reason: str) -> dict:
    """运行售后状态机，返回最终状态"""
    return after_sales_graph.invoke({"order_number": order_number, "reason": reason})


if __name__ == "__main__":
    # 三种典型场景验证编排
    cases = [
        ("TB20260810002", "坚果不想要了"),   # 食品已签收 → 人工
        ("TB20260808003", "加湿器不好用"),   # 家电已签收 → 退货
        ("TB20260812001", "买错了想退"),     # 食品已发货 → 退货（未签收）
    ]
    for order_no, reason in cases:
        final = run_after_sales(order_no, reason)
        print("=" * 60)
        print(f"订单 {order_no} | 原因：{reason}")
        print(f"  订单状态={final.get('order_status')} | 含食品={final.get('has_food_item')}")
        print(f"  决策={final.get('decision')} → 结果：{final.get('result')}")
