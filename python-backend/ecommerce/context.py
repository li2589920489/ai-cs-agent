from __future__ import annotations as _annotations

from chatkit.agents import AgentContext
from pydantic import BaseModel


class ECommerceAgentContext(BaseModel):
    """电商客服智能体上下文 — 贯穿整个对话的共享状态"""

    # 用户信息
    customer_name: str | None = None
    account_id: str | None = None

    # 订单相关
    order_number: str | None = None
    order_status: str | None = None
    tracking_number: str | None = None
    order_items: list[dict] | None = None  # 订单中包含的商品列表

    # 商品相关
    product_id: str | None = None
    product_name: str | None = None
    selected_sku: str | None = None  # 用户选中的规格

    # 售后相关
    return_case_id: str | None = None
    refund_amount: str | None = None
    return_status: str | None = None

    # 优惠券
    available_coupons: list[str] | None = None
    applied_coupon: str | None = None

    # 人工接管
    escalation_flag: bool = False
    escalation_reason: str | None = None

    # 内部字段（不展示给前端）
    internal_notes: str | None = None


class ECommerceAgentChatContext(AgentContext[dict]):
    """ChatKit 运行时的 AgentContext 包装"""
    state: ECommerceAgentContext


def create_initial_context() -> ECommerceAgentContext:
    return ECommerceAgentContext()


def public_context(ctx: ECommerceAgentContext) -> dict:
    """返回给前端展示的过滤后上下文"""
    data = ctx.model_dump()
    hidden_keys = {"internal_notes"}
    for key in list(data.keys()):
        if key in hidden_keys:
            data.pop(key, None)
    return data
