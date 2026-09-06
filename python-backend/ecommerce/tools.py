"""电商智能体工具集 — 每个工具对应一个具体的业务操作"""
from __future__ import annotations as _annotations

import json
import os
import random
import string

from agents import RunContextWrapper, function_tool

import knowledge_store

# ==================== RAG 管线懒加载（完整技术栈：向量 + BM25 + RRF + 重排） ====================

_rag_pipeline = None


def _get_rag_pipeline():
    """懒加载 RAG 管线单例（首次调用会加载本地 embedding 模型）"""
    global _rag_pipeline
    if _rag_pipeline is None:
        from rag import load_pipeline

        _rag_pipeline = load_pipeline()
    return _rag_pipeline


def _rag_search(query: str, k: int = 3, type_filter: str | None = None) -> list[dict]:
    """走完整 RAG 管线检索，可按 chunk 类型（product/policy）过滤"""
    try:
        results = _get_rag_pipeline().search(query, k=k, use_hybrid=True, use_rerank=True)
    except Exception as e:  # noqa: BLE001
        print(f"[RAG] 检索失败，回退规则匹配: {e}")
        return []
    if type_filter:
        results = [r for r in results if r.get("metadata", {}).get("type") == type_filter]
    return results


def _format_rag_results(keyword: str, results: list[dict]) -> str:
    """把 RAG 检索结果格式化为给 LLM 阅读的文本"""
    lines = [f"「{keyword}」语义检索结果（共{len(results)}条）:"]
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        src = meta.get("name") or meta.get("policy_name") or "知识库"
        lines.append(f"\n[{i}] 来源：{src}")
        lines.append(f"    {r['text'].strip()[:300]}")
    return "\n".join(lines)


from .context import ECommerceAgentChatContext
from .demo_data import (
    AVAILABLE_COUPONS,
    STORE_POLICIES,
    get_customer_orders,
    get_order,
    get_product,
    search_products,
)


# ==================== 知识库检索（RAG + 规则混合） ====================

@function_tool(
    name_override="faq_lookup_tool",
    description_override="搜索店铺政策和常见问题知识库，覆盖退货、退款、发货、物流、优惠券、会员权益、售后等。",
)
async def faq_lookup_tool(question: str) -> str:
    """在店铺政策知识库中搜索相关答案（当前为规则匹配版，后续可升级为ChromaDB向量检索）"""
    q = question.lower()

    topic_map = [
        ("退货", "退货政策"),
        ("退款", "退款时效"),
        ("发货", "发货时效"),
        ("物流", "物流查询"),
        ("快递", "物流查询"),
        ("优惠券", "优惠券规则"),
        ("满减", "优惠券规则"),
        ("会员", "会员权益"),
        ("破损", "常见售后问题"),
        ("质量问题", "常见售后问题"),
        ("少发", "常见售后问题"),
        ("漏发", "常见售后问题"),
        ("描述不符", "常见售后问题"),
        ("换货", "退货政策"),
        ("售后", "常见售后问题"),
    ]

    matched_policies = []
    for keyword, policy_name in topic_map:
        if keyword in q:
            if policy_name not in matched_policies:
                matched_policies.append(policy_name)

    if not matched_policies:
        return "抱歉，我在知识库中未找到关于该问题的明确政策。建议转接人工客服获取更准确的帮助。你可以要求我「转人工」来联系客服专员。"

    result_parts = []
    for policy_name in matched_policies[:3]:
        content = STORE_POLICIES.get(policy_name, "")
        if content:
            result_parts.append(content.strip())

    return "\n\n---\n\n".join(result_parts)


# ==================== RAG增强检索（ChromaDB向量检索版） ====================

@function_tool(
    name_override="rag_search_tool",
    description_override="使用语义检索（向量+关键词混合+RRF融合+重排）在店铺知识库中搜索最相关的政策信息",
)
async def rag_search_tool(question: str) -> str:
    """基于完整 RAG 管线的语义搜索，当 faq_lookup_tool 找不到答案时 fallback 到此工具"""
    results = _rag_search(question, k=3, type_filter="policy")
    if not results:
        return await faq_lookup_tool(question)
    return _format_rag_results(question, results)


# ==================== 订单工具 ====================

@function_tool(
    name_override="order_status_tool",
    description_override="查询订单的当前状态（待付款/已发货/运输中/已签收等）及物流信息",
)
async def order_status_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    order_number: str,
) -> str:
    ctx = context.context.state
    ctx.order_number = order_number
    order = get_order(order_number)
    if not order:
        return f"未找到订单号为 {order_number} 的订单。请核实订单号是否正确，或提供其他可识别信息（如收件人姓名、手机号后四位）。"

    ctx.customer_name = order["customer_name"]
    ctx.account_id = order["account_id"]
    ctx.order_status = order["status"]
    ctx.order_items = order["items"]

    tracking = order.get("tracking", {})
    lines = [
        f"订单号: {order['order_number']}",
        f"状态: {order['status']}",
        f"下单时间: {order['order_time']}",
        f"支付金额: ¥{order['paid_amount']}",
        f"商品: {', '.join(f'{item['name']}({item['sku']})×{item['quantity']}' for item in order['items'])}",
    ]

    if order.get("coupon_used"):
        lines.append(f"已用优惠券: {order['coupon_used']}")

    if tracking:
        lines.append(f"物流公司: {tracking.get('company', '未知')}")
        lines.append(f"运单号: {tracking.get('number', '未知')}")
        lines.append(f"物流状态: {tracking.get('location', '暂无')}")
        lines.append(f"预计送达: {tracking.get('estimated_delivery', '暂无')}")

    return "\n".join(lines)


@function_tool(
    name_override="list_customer_orders_tool",
    description_override="列出用户的所有历史订单",
)
async def list_customer_orders_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
) -> str:
    ctx = context.context.state
    account_id = ctx.account_id
    if not account_id:
        return "请先提供您的账户信息或订单号，我才能查询您的订单列表。"

    orders = get_customer_orders(account_id)
    if not orders:
        return f"账户 {account_id} 暂无历史订单。"

    lines = [f"账户 {account_id} 的订单列表:"]
    for o in orders:
        lines.append(
            f"- {o['order_number']} | {o['order_time'][:10]} | "
            f"¥{o['paid_amount']} | {o['status']} | "
            f"{', '.join(item['name'][:10] for item in o['items'][:2])}"
        )
    return "\n".join(lines)


@function_tool(
    name_override="tracking_lookup_tool",
    description_override="查询订单的详细物流轨迹",
)
async def tracking_lookup_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    order_number: str,
) -> str:
    ctx = context.context.state
    order = get_order(order_number)
    if not order:
        return f"未找到订单 {order_number}。"

    ctx.order_number = order_number
    tracking = order.get("tracking", {})

    if not tracking:
        return f"订单 {order_number} 暂无物流信息。可能尚未发货，请确认订单状态。"

    ctx.tracking_number = tracking.get("number")
    ctx.order_status = order["status"]

    return (
        f"订单 {order_number} 物流详情:\n"
        f"快递公司: {tracking.get('company')}\n"
        f"运单号: {tracking.get('number')}\n"
        f"最新状态: {tracking.get('location')}\n"
        f"预计送达: {tracking.get('estimated_delivery')}\n"
        f"如物流信息超过48小时未更新，我可以帮你联系快递公司核查。"
    )


# ==================== 商品工具 ====================

@function_tool(
    name_override="product_info_tool",
    description_override="查询商品详情（名称、价格、规格、库存、评价等）",
)
async def product_info_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    product_id: str,
) -> str:
    product = get_product(product_id)
    if not product:
        return f"未找到商品ID为 {product_id} 的商品。请确认商品ID是否正确。"

    ctx = context.context.state
    ctx.product_id = product_id
    ctx.product_name = product["name"]

    stock_info = "\n".join(f"  - {sku}: {qty}件" for sku, qty in product["stock"].items())

    return (
        f"商品详情:\n"
        f"名称: {product['name']}\n"
        f"价格: ¥{product['price']}（原价¥{product['original_price']}）\n"
        f"分类: {product['category']}\n"
        f"描述: {product['description']}\n"
        f"可选规格:\n{stock_info}\n"
        f"评分: {product['rating']}/5.0（{product['reviews']}条评价）\n"
        f"是否包邮: {'是' if product['free_shipping'] else f'否，运费¥{product.get('shipping_fee', '8.00')}'}\n"
    )


@function_tool(
    name_override="search_products_tool",
    description_override="根据关键词搜索店铺内的商品",
)
async def search_products_tool(keyword: str) -> str:
    results = search_products(keyword)
    if not results:
        return f"未找到与「{keyword}」相关的商品。请尝试其他关键词。"

    lines = [f"搜索「{keyword}」的结果（共{len(results)}件）:"]
    for p in results:
        lines.append(f"- [{p['product_id']}] {p['name']} | ¥{p['price']} | 评分{p['rating']} | {p['reviews']}条评价")
    return "\n".join(lines)


@function_tool(
    name_override="knowledge_search_tool",
    description_override="从商品知识库语义检索商品的详细介绍、卖点、规格、常见问题（向量+关键词混合检索）",
)
async def knowledge_search_tool(keyword: str) -> str:
    """从商品知识库语义检索商品知识（BM25 + 向量 + RRF 融合 + 重排）"""
    results = _rag_search(keyword, k=3, type_filter="product")
    if results:
        return _format_rag_results(keyword, results)

    # 语义检索无结果时，回退到关键词 LIKE 兜底
    like_results = knowledge_store.search_knowledge(keyword)
    if not like_results:
        return f"知识库中未找到与「{keyword}」相关的商品知识。"

    lines = [f"知识库中「{keyword}」相关的商品知识（共{len(like_results)}条）:"]
    for k in like_results:
        lines.append(f"\n【{k['name']}】（{k['product_id']}）")
        if k["description"]:
            lines.append(f"  介绍: {k['description']}")
        if k["selling_points"]:
            lines.append(f"  卖点: {'；'.join(k['selling_points'])}")
        if k["specs"]:
            lines.append(f"  规格: {'；'.join(k['specs'])}")
        if k["faq"]:
            lines.append(f"  常见问题: {'；'.join(k['faq'])}")
    return "\n".join(lines)


@function_tool(
    name_override="inventory_check_tool",
    description_override="检查某商品的某个规格是否有库存；不指定规格(sku)时返回该商品全部规格的库存情况",
)
async def inventory_check_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    product_id: str,
    sku: str = "",
) -> str:
    product = get_product(product_id)
    if not product:
        return f"未找到商品 {product_id}。"

    ctx = context.context.state
    ctx.product_id = product_id
    ctx.product_name = product["name"]

    # sku 未指定时，列出该商品所有规格的库存（避免模型漏填必填参数导致校验失败）
    if not sku:
        lines = [f"商品「{product['name']}」各规格库存："]
        for s, qty in product["stock"].items():
            if qty == 0:
                status = "已售罄"
            elif qty < 20:
                status = f"仅剩{qty}件（紧张）"
            else:
                status = f"{qty}件（充足）"
            lines.append(f"  - {s}: {status}")
        return "\n".join(lines)

    ctx.selected_sku = sku

    stock = product["stock"].get(sku)
    if stock is None:
        available = ", ".join(product["stock"].keys())
        return f"商品「{product['name']}」没有「{sku}」这个规格。可选规格: {available}"

    if stock == 0:
        return f"抱歉，「{product['name']}」的「{sku}」已售罄。其他规格可能还有库存，需要我帮你查看吗？"
    elif stock < 20:
        return f"「{product['name']}」的「{sku}」仅剩{stock}件，建议尽快下单！"
    else:
        return f"「{product['name']}」的「{sku}」库存充足（{stock}件），可以放心购买。"


# ==================== 售后工具 ====================

@function_tool(
    name_override="initiate_return_tool",
    description_override="发起退货/退款申请",
)
async def initiate_return_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    order_number: str,
    reason: str,
) -> str:
    order = get_order(order_number)
    if not order:
        return f"未找到订单 {order_number}。"

    ctx = context.context.state
    ctx.order_number = order_number
    ctx.order_status = order["status"]

    if order["status"] not in ("已签收", "已发货", "运输中"):
        return f"订单 {order_number} 当前状态为「{order['status']}」，暂不支持退货。已签收的订单可在7天内申请退货。"

    # 食品类检查
    food_items = [item for item in order["items"] if any(kw in item["name"] for kw in ["坚果", "零食", "食品"])]
    if food_items and order["status"] == "已签收":
        food_names = ", ".join(f"「{item['name']}」" for item in food_items)

    case_id = f"RTN-{order_number[-6:]}-{random.randint(100, 999)}"
    ctx.return_case_id = case_id
    ctx.return_status = "待审核"

    refund_amount = order["paid_amount"]
    warning = ""
    if food_items and order["status"] == "已签收":
        warning = (
            "\n\n⚠️ 注意：您的订单包含食品类商品（{food}），"
            "如已拆封则不支持无理由退货。如存在质量问题，请拍照留证。"
        ).format(food=food_names)
        ctx.escalation_flag = True
        ctx.escalation_reason = "食品退货需人工审核"

    return (
        f"退货申请已提交！\n"
        f"退货单号: {case_id}\n"
        f"订单号: {order_number}\n"
        f"退货原因: {reason}\n"
        f"预计退款金额: ¥{refund_amount}\n"
        f"审核将在24小时内完成，审核通过后会短信通知退货地址和寄回指引。" + warning
    )


@function_tool(
    name_override="cancel_order_tool",
    description_override="取消订单（仅支持未发货的订单）",
)
async def cancel_order_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    order_number: str,
) -> str:
    order = get_order(order_number)
    if not order:
        return f"未找到订单 {order_number}。"

    ctx = context.context.state
    ctx.order_number = order_number

    if order["status"] != "待发货":
        return (
            f"订单 {order_number} 当前状态为「{order['status']}」，无法取消。\n"
            f"如已发货，可等收到后申请退货退款。需要我帮你发起退货吗？"
        )

    ctx.order_status = "已取消"
    return (
        f"订单 {order_number} 已成功取消。\n"
        f"退款金额 ¥{order['paid_amount']} 将在1-3个工作日内退回原支付账户。"
    )


# ==================== 优惠券工具 ====================

@function_tool(
    name_override="coupon_lookup_tool",
    description_override="查询当前可用的店铺优惠券",
)
async def coupon_lookup_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    account_id: str | None = None,
) -> str:
    available = [c for c in AVAILABLE_COUPONS if c["status"] == "可用"]
    if not available:
        return "当前没有可用的优惠券。"

    ctx = context.context.state
    ctx.account_id = account_id or ctx.account_id
    ctx.available_coupons = [c["code"] for c in available]

    lines = ["当前可用的优惠券:"]
    for c in available:
        lines.append(
            f"- [{c['code']}] {c['type']}: {c['discount']}（有效期至{c['expire_date']}）"
        )
    lines.append("\n注意：每笔订单限用一张优惠券，不可叠加。会员折扣可与优惠券叠加。")
    return "\n".join(lines)


# ==================== 人工接管工具 ====================

@function_tool(
    name_override="escalate_to_human_tool",
    description_override="当问题过于复杂、需要人工判断或用户明确要求转人工时，生成对话摘要并转接人工客服",
)
async def escalate_to_human_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    reason: str,
) -> str:
    """转接人工客服 — 生成摘要并标记"""
    ctx = context.context.state
    ctx.escalation_flag = True
    ctx.escalation_reason = reason

    summary_parts = [f"转人工原因: {reason}"]
    if ctx.order_number:
        summary_parts.append(f"关联订单: {ctx.order_number}")
    if ctx.customer_name:
        summary_parts.append(f"客户: {ctx.customer_name}")
    if ctx.return_case_id:
        summary_parts.append(f"退货单号: {ctx.return_case_id}")
    if ctx.product_name:
        summary_parts.append(f"涉及商品: {ctx.product_name}")

    return (
        "已为您转接人工客服，请稍候...\n\n"
        "【转接摘要】\n" + "\n".join(summary_parts) + "\n\n"
        "人工客服将很快接入，查看完整对话记录后可继续为您服务。"
        "在此期间您无需重复描述问题。"
    )
