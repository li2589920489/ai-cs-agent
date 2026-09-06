"""电商智能体编排 — 6个Agent：Triage分流 / 订单详情 / 商品知识 / 售后退换货 / 店铺政策 / 人工接管"""
from __future__ import annotations as _annotations

import random
import string

from agents import Agent, RunContextWrapper, handoff
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

from .context import ECommerceAgentChatContext
from .guardrails import jailbreak_guardrail, relevance_guardrail
from .tools import (
    cancel_order_tool,
    coupon_lookup_tool,
    escalate_to_human_tool,
    faq_lookup_tool,
    initiate_return_tool,
    inventory_check_tool,
    knowledge_search_tool,
    list_customer_orders_tool,
    order_status_tool,
    product_info_tool,
    rag_search_tool,
    search_products_tool,
    tracking_lookup_tool,
)

MODEL = "deepseek-chat"


# ==================== 1. Triage Agent（智能分流） ====================

triage_agent = Agent[ECommerceAgentChatContext](
    name="Triage Agent",
    model=MODEL,
    handoff_description="智能分流中心，根据用户意图路由到最合适的专业Agent。",
    instructions=(
        f"{RECOMMENDED_PROMPT_PREFIX} "
        "你是电商客服的智能分流员。分析用户消息的意图，立即路由到正确的专业Agent：\n"
        "- 订单查询/物流跟踪 → 订单详情 Agent\n"
        "- 商品咨询/搜索/比价 → 商品知识 Agent\n"
        "- 退货/退款/换货/售后 → 售后退换货 Agent\n"
        "- 优惠券/促销/店铺政策/会员 → 店铺政策 Agent\n"
        "- 要求转人工/投诉/举报/找领导/12315/索赔赔偿/复杂纠纷 → Human Escalation Agent\n\n"
        "规则：识别意图后立即handoff，不要自己回答问题。每个消息最多一次handoff。"
        "如果用户问题涉及多个领域，优先handoff到最紧急的Agent，让该Agent完成后再回传。"
        "区分要点：'赔偿/索赔'（诉求是赔钱、常伴随过退换期或金额纠纷）→ Human Escalation Agent；'退款/退货/换货'（常规售后诉求）→ 售后退换货 Agent。"
    ),
    tools=[],
    handoffs=[],
    input_guardrails=[relevance_guardrail, jailbreak_guardrail],
)


# ==================== 2. 订单详情 Agent（订单物流） ====================

def order_tracking_instructions(
    run_context: RunContextWrapper[ECommerceAgentChatContext], agent: Agent[ECommerceAgentChatContext]
) -> str:
    ctx = run_context.context.state
    order_no = ctx.order_number or "[未知]"
    name = ctx.customer_name or "[未知]"
    return (
        f"{RECOMMENDED_PROMPT_PREFIX}\n"
        "你是订单详情专员。负责查询订单状态、物流轨迹、历史订单、物流异常处理。\n"
        f"当前上下文：客户={name}，订单号={order_no}\n"
        "1. 如果用户提供了订单号，直接用 order_status_tool 查询。\n"
        "2. 如果用户没有订单号但想查所有订单，用 list_customer_orders_tool。\n"
        "3. 如果用户问物流细节，用 tracking_lookup_tool。\n"
        "4. 查询完成后，用简洁的中文总结给用户，不要逐条念数据。\n"
        "5. 物流超过48小时未更新，主动告知用户可以联系快递公司核查。\n"
        "6. 物流异常（退回、丢件），主动建议转售后。\n"
        "7. 用户对订单不满意，主动询问是否转售后。"
        "如果需要退货/退款/换货 → handoff to 售后退换货 Agent\n"
        "其他无关问题 → handoff back to Triage Agent"
    )


order_tracking_agent = Agent[ECommerceAgentChatContext](
    name="订单详情 Agent",
    model=MODEL,
    handoff_description="查询订单状态、物流轨迹、历史订单列表、处理物流异常。",
    instructions=order_tracking_instructions,
    tools=[order_status_tool, list_customer_orders_tool, tracking_lookup_tool],
    input_guardrails=[relevance_guardrail, jailbreak_guardrail],
)


# ==================== 3. 商品知识 Agent（商品咨询） ====================

def product_inquiry_instructions(
    run_context: RunContextWrapper[ECommerceAgentChatContext], agent: Agent[ECommerceAgentChatContext]
) -> str:
    ctx = run_context.context.state
    product = ctx.product_name or "[未知]"
    return (
        f"{RECOMMENDED_PROMPT_PREFIX}\n"
        "你是商品知识专员。负责帮用户找商品、介绍卖点、比规格、查库存、看评价。\n"
        f"当前商品上下文：{product}\n"
        "1. 用户问商品的详细介绍/卖点/规格/常见问题时，优先用 knowledge_search_tool 从知识库检索。\n"
        "2. 用户提到具体商品名或ID时，用 product_info_tool 查价格库存等实时详情。\n"
        "3. 用户搜索关键词时，用 search_products_tool。\n"
        "4. 用户选中商品后，用 inventory_check_tool 确认库存。\n"
        "5. 用口语化中文介绍商品，突出卖点和用户评价。\n"
        "6. 如果库存紧张（<20件），提醒用户尽快下手。\n"
        "7. 可以主动告知当前可用的优惠券（handoff to 店铺政策 Agent）。\n"
        "如果用户想下单/付款相关 → 这是模拟店铺，告知用户「在APP中点击购买即可」。\n"
        "其他无关问题 → handoff back to Triage Agent"
    )


product_inquiry_agent = Agent[ECommerceAgentChatContext](
    name="商品知识 Agent",
    model=MODEL,
    handoff_description="商品搜索、详情查询、卖点介绍、规格对比、库存检查、推荐。",
    instructions=product_inquiry_instructions,
    tools=[knowledge_search_tool, product_info_tool, search_products_tool, inventory_check_tool],
    input_guardrails=[relevance_guardrail, jailbreak_guardrail],
)


# ==================== 4. 售后退换货 Agent（售后处理） ====================

def after_sales_instructions(
    run_context: RunContextWrapper[ECommerceAgentChatContext], agent: Agent[ECommerceAgentChatContext]
) -> str:
    ctx = run_context.context.state
    order_no = ctx.order_number or "[未知]"
    return_id = ctx.return_case_id or "[无]"
    return (
        f"{RECOMMENDED_PROMPT_PREFIX}\n"
        "你是售后退换货专员。负责退货、退款、换货、取消订单。\n"
        f"当前上下文：订单号={order_no}，退货单号={return_id}\n"
        "1. 先了解用户要售后处理的订单号和原因。\n"
        "2. 如果需要了解退货政策，先用 faq_lookup_tool 查询。\n"
        "3. 退货用 initiate_return_tool，取消用 cancel_order_tool。\n"
        "4. 对于食品类退货，必须提醒用户「拆封后不支持无理由退货，质量问题需拍照」。\n"
        "5. 对于复杂情况（金额争议、超过7天、食品质量问题等），主动 escalate_to_human_tool 转人工。\n"
        "6. 售后处理完成后，总结处理结果给用户。\n"
        "如果用户问优惠券/政策 → handoff to 店铺政策 Agent\n"
        "其他无关问题 → handoff back to Triage Agent"
    )


after_sales_agent = Agent[ECommerceAgentChatContext](
    name="售后退换货 Agent",
    model=MODEL,
    handoff_description="处理退货、退款、换货、订单取消等售后问题。复杂情况自动转人工。",
    instructions=after_sales_instructions,
    tools=[initiate_return_tool, cancel_order_tool, faq_lookup_tool, escalate_to_human_tool],
    input_guardrails=[relevance_guardrail, jailbreak_guardrail],
)


# ==================== 5. 店铺政策 Agent（政策问答 + 优惠券） ====================

faq_agent = Agent[ECommerceAgentChatContext](
    name="店铺政策 Agent",
    model=MODEL,
    handoff_description="回答店铺政策、常见问题（退货规则、发货时间、会员权益等），查询优惠券与满减活动。",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    你是店铺政策专员。负责回答店铺政策、常见问题，并查询优惠券与促销活动。
    1. 用户询问政策相关（退货规则、发货时间、会员权益等），先用 faq_lookup_tool 查找答案，不要凭记忆回答。
    2. 用户询问优惠券/满减/促销，用 coupon_lookup_tool 查询，帮用户算出最优组合（提醒不能叠加）。
    3. faq_lookup_tool 找不到时，用 rag_search_tool 语义搜索。
    4. 工具都找不到时，建议转人工（handoff to Human Escalation Agent）。
    5. 用口语化中文总结，不要直接复制粘贴长文本；优惠券突出「省多少钱」。
    其他无关问题 → handoff back to Triage Agent""",
    tools=[faq_lookup_tool, rag_search_tool, coupon_lookup_tool],
    input_guardrails=[relevance_guardrail, jailbreak_guardrail],
)


# ==================== 6. Human Escalation Agent（人工接管） ====================

escalation_agent = Agent[ECommerceAgentChatContext](
    name="Human Escalation Agent",
    model=MODEL,
    handoff_description="处理复杂问题、用户投诉、或用户明确要求转人工的场景。",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    你是人工客服接管专员。当问题超出AI能力范围时，由你负责：
    1. 立即用 escalate_to_human_tool 生成对话摘要并转接。
    2. 安抚用户情绪，告知预计等待时间（通常1-3分钟）。
    3. 不需要自己解决问题——你的唯一职责是高效、准确地完成转接。
    转接完成后 → 等待人工客服接管对话""",
    tools=[escalate_to_human_tool],
    input_guardrails=[relevance_guardrail, jailbreak_guardrail],
)


# ==================== Handoff 关系配置 ====================
# 注意：Agent 的 name 为中文（供前端展示），但 OpenAI Agents SDK 会根据
# agent.name 生成 handoff 工具名 `transfer_to_{name}`，中文字符会被清洗成
# 下划线，导致多个中文名 Agent 的 handoff 工具名冲突（tool name collision）。
# 因此这里统一用 handoff() 显式指定唯一的英文工具名（tool_name_override），
# 既保留中文展示名，又避免工具名冲突。

triage_agent.handoffs = [
    handoff(order_tracking_agent, tool_name_override="transfer_to_order_tracking"),
    handoff(product_inquiry_agent, tool_name_override="transfer_to_product_inquiry"),
    handoff(after_sales_agent, tool_name_override="transfer_to_after_sales"),
    handoff(faq_agent, tool_name_override="transfer_to_store_policy"),
    handoff(escalation_agent, tool_name_override="transfer_to_human_escalation"),
]

order_tracking_agent.handoffs.extend([
    handoff(after_sales_agent, tool_name_override="transfer_to_after_sales"),
    handoff(triage_agent, tool_name_override="transfer_to_triage"),
])
product_inquiry_agent.handoffs.extend([
    handoff(faq_agent, tool_name_override="transfer_to_store_policy"),
    handoff(triage_agent, tool_name_override="transfer_to_triage"),
])
after_sales_agent.handoffs.extend([
    handoff(faq_agent, tool_name_override="transfer_to_store_policy"),
    handoff(escalation_agent, tool_name_override="transfer_to_human_escalation"),
    handoff(triage_agent, tool_name_override="transfer_to_triage"),
])
faq_agent.handoffs.extend([
    handoff(escalation_agent, tool_name_override="transfer_to_human_escalation"),
    handoff(triage_agent, tool_name_override="transfer_to_triage"),
])
escalation_agent.handoffs.append(
    handoff(triage_agent, tool_name_override="transfer_to_triage")
)
