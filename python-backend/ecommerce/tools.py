"""电商智能体工具集 — 每个工具对应一个具体的业务操作"""
from __future__ import annotations as _annotations

import json
import os
import string

from agents import RunContextWrapper, function_tool

import knowledge_store

# ==================== RAG 管线（进程内单例；运行时默认纯向量 + 重排，BM25/混合检索保留为可降级兜底） ====================


def _get_rag_pipeline():
    """获取 RAG 管线单例（首次调用会加载本地 embedding 模型）

    单例由 rag.get_pipeline 维护：知识库写入后会被标记为过期并在下次取用时重载，
    因此新增/导入的商品无需重启服务即可进入检索范围。
    """
    from rag import get_pipeline

    return get_pipeline()


def _rag_search(
    query: str,
    k: int = 3,
    type_filter: str | None = None,
    use_hybrid: bool = False,
) -> list[dict]:
    """走完整 RAG 管线检索，可按 chunk 类型（product/policy）过滤

    默认纯向量检索 + 重排（BM25 已降级为可插拔兜底）。
    如需混合检索（如 BM25 互补性验证脚本），显式传 use_hybrid=True。
    """
    try:
        results = _get_rag_pipeline().search(query, k=k, use_hybrid=use_hybrid, use_rerank=True)
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


import order_store  # 订单 / 退货单的权威数据源（原先读 demo_data 常量，见决策 D7）

from .context import ECommerceAgentChatContext
from .demo_data import (
    AVAILABLE_COUPONS,
    STORE_POLICIES,
    get_product,
    search_products,
)


# ==================== 政策 / FAQ 检索（RAG 优先 + 规则兜底） ====================

# 规则匹配兜底表：仅在 RAG 检索失败/无结果时使用，应对 embedding 模型不可用
# 或向量库为空等异常场景；不作为政策检索的默认路径。
_FALLBACK_TOPIC_MAP = [
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


@function_tool(
    name_override="faq_lookup_tool",
    description_override="搜索店铺政策和常见问题知识库，覆盖退货、退款、发货、物流、优惠券、会员权益、售后等。默认走 RAG 语义检索；规则匹配仅作降级兜底。",
)
async def faq_lookup_tool(question: str) -> str:
    """在店铺政策知识库中搜索相关答案（RAG 语义检索优先；规则匹配仅作降级兜底）"""
    # 1. 优先走 RAG 语义检索（向量 + 重排；命中真实政策 chunk）
    results = _rag_search(question, k=3, type_filter="policy")
    if results:
        return _format_rag_results(question, results)

    # 2. RAG 无结果时，回退到规则匹配（应对 embedding 模型不可用 / 向量库为空等异常场景）
    q = question.lower()
    matched_policies = []
    for keyword, policy_name in _FALLBACK_TOPIC_MAP:
        if keyword in q and policy_name not in matched_policies:
            matched_policies.append(policy_name)

    if not matched_policies:
        return "抱歉，我在知识库中未找到关于该问题的明确政策。建议转接人工客服获取更准确的帮助。你可以要求我「转人工」来联系客服专员。"

    result_parts = []
    for policy_name in matched_policies[:3]:
        content = STORE_POLICIES.get(policy_name, "")
        if content:
            result_parts.append(content.strip())

    if not result_parts:
        return "抱歉，我在知识库中未找到关于该问题的明确政策。建议转接人工客服获取更准确的帮助。你可以要求我「转人工」来联系客服专员。"

    return "\n\n---\n\n".join(result_parts)


# ==================== 纯语义检索（不走规则匹配兜底） ====================

@function_tool(
    name_override="rag_search_tool",
    description_override="使用向量语义检索在店铺知识库中搜索最相关的政策信息（不依赖规则匹配，纯 RAG 路径）",
)
async def rag_search_tool(question: str) -> str:
    """基于完整 RAG 管线的纯语义搜索 —— 不走规则匹配兜底；RAG 失败时返回错误提示而非 fallback"""
    results = _rag_search(question, k=3, type_filter="policy")
    if not results:
        return "RAG 语义检索暂无命中结果。请尝试换一种问法，或使用 faq_lookup_tool（含规则匹配兜底）。"
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
    order = order_store.get_order(order_number)
    if not order:
        return f"未找到订单号为 {order_number} 的订单。请核实订单号是否正确，或提供其他可识别信息（如收件人姓名、手机号后四位）。"

    ctx.customer_name = order["customer_name"]
    ctx.account_id = order["account_id"]
    ctx.order_status = order["status"]
    ctx.order_items = order["items"]

    tracking = order.get("tracking", {})
    items_str = ", ".join(
        f"{item['name']}({item['sku']})×{item['quantity']}" for item in order["items"]
    )
    lines = [
        f"订单号: {order['order_number']}",
        f"状态: {order['status']}",
        f"下单时间: {order['order_time']}",
        f"支付金额: ¥{order['paid_amount']}",
        f"商品: {items_str}",
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

    # account_id 过滤下推到 SQL（order_store.list_orders），不在应用层全表筛
    orders = order_store.list_orders(account_id=account_id)
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
    order = order_store.get_order(order_number)
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

def _kb_field_lines(kb: dict) -> list[str]:
    """拼装知识库商品的字段行，并跳过与标题重复的字段。

    为什么需要去重：真实导入的商品里有一部分标题不含分隔符
    （如「电热饭盒插电加热上班便当…」），抽取逻辑会把整段标题
    同时落到 description 与 selling_points，直接输出就会出现
    「名称 / 介绍 / 卖点」三行一模一样 —— 既浪费 token 也误导模型。
    """
    name = (kb.get("name") or "").strip()
    lines: list[str] = []

    desc = (kb.get("description") or "").strip()
    if desc and desc != name:
        lines.append(f"介绍: {desc}")

    points = [s.strip() for s in (kb.get("selling_points") or []) if s and s.strip()]
    points = [s for s in points if s != name]
    if points:
        lines.append(f"卖点: {'；'.join(points)}")

    specs = [s.strip() for s in (kb.get("specs") or []) if s and s.strip()]
    if specs:
        lines.append(f"规格: {'；'.join(specs)}")

    faq = [s.strip() for s in (kb.get("faq") or []) if s and s.strip()]
    if faq:
        lines.append(f"常见问题: {'；'.join(faq)}")

    return lines


@function_tool(
    name_override="product_info_tool",
    description_override="按商品编号查询商品详情（名称、介绍、卖点、规格、常见问题；demo 商品另含价格与库存）",
)
async def product_info_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    product_id: str,
) -> str:
    """按商品编号查详情：知识库（含真实导入商品）优先，demo 商品库兜底。

    真实导入的商品只有标题/卖点等字段，没有价格与库存——此时如实说明「未收录」，
    既不要返回「未找到」（商品确实存在），也不要编造价格库存。
    """
    ctx = context.context.state
    kb = knowledge_store.get_knowledge_by_product_id(product_id)
    product = get_product(product_id)

    if kb:
        ctx.product_id = product_id
        ctx.product_name = kb["name"]
        lines = [
            "商品详情（知识库收录）:",
            f"名称: {kb['name']}",
            f"编号: {kb['product_id']}",
        ]
        if kb.get("category"):
            lines.append(f"类目: {kb['category']}")
        lines.extend(_kb_field_lines(kb))
        if product:
            lines.append(f"价格: ¥{product['price']}（原价¥{product['original_price']}）")
            lines.append(f"是否包邮: {'是' if product['free_shipping'] else '否'}")
        else:
            lines.append(
                "价格/库存: 知识库未收录该字段。请如实告知顾客「暂未收录价格与库存信息，"
                "建议咨询客服确认」，不要编造。"
            )
        return "\n".join(lines)

    if not product:
        return (
            f"未找到商品ID为 {product_id} 的商品（知识库与商品库均无记录）。"
            f"请确认编号是否正确，或改用 search_products_tool / knowledge_search_tool 按关键词检索。"
        )

    ctx.product_id = product_id
    ctx.product_name = product["name"]

    stock_info = "\n".join(f"  - {sku}: {qty}件" for sku, qty in product["stock"].items())

    shipping_text = (
        "是"
        if product["free_shipping"]
        else f"否，运费¥{product.get('shipping_fee', '8.00')}"
    )
    return (
        f"商品详情:\n"
        f"名称: {product['name']}\n"
        f"价格: ¥{product['price']}（原价¥{product['original_price']}）\n"
        f"分类: {product['category']}\n"
        f"描述: {product['description']}\n"
        f"可选规格:\n{stock_info}\n"
        f"评分: {product['rating']}/5.0（{product['reviews']}条评价）\n"
        f"是否包邮: {shipping_text}\n"
    )


@function_tool(
    name_override="search_products_tool",
    description_override="根据关键词搜索店铺内的商品（语义检索 → 知识库关键词匹配 → demo 商品库逐级兜底）",
)
async def search_products_tool(keyword: str) -> str:
    """按关键词搜索商品：语义检索优先，逐级降级兜底。

    为什么不能只查 demo 商品库：知识库里的真实导入商品不在 demo 目录中，
    而「搜一下 X」是买家最自然的动作——只查 demo 会一律返回「未找到」。
    """
    # 1. 语义检索（可召回真实导入商品）
    results = _rag_search(keyword, k=5, type_filter="product")
    if results:
        return _format_rag_results(keyword, results)

    # 2. 知识库关键词匹配兜底
    like_results = knowledge_store.search_knowledge(keyword, limit=5)
    if like_results:
        lines = [f"知识库中与「{keyword}」相关的商品（共{len(like_results)}条）:"]
        for k in like_results:
            lines.append(f"\n【{k['name']}】（{k['product_id']}）")
            lines.extend("  " + ln for ln in _kb_field_lines(k))
        return "\n".join(lines)

    # 3. demo 商品库兜底（字段完整，含价格/评分）
    demo_results = search_products(keyword)
    if demo_results:
        lines = [f"搜索「{keyword}」的结果（共{len(demo_results)}件）:"]
        for p in demo_results:
            lines.append(f"- [{p['product_id']}] {p['name']} | ¥{p['price']} | 评分{p['rating']} | {p['reviews']}条评价")
        return "\n".join(lines)

    return f"未找到与「{keyword}」相关的商品。请尝试其他关键词。"


@function_tool(
    name_override="knowledge_search_tool",
    description_override="从商品知识库语义检索商品的详细介绍、卖点、规格、常见问题（向量 + 重排）",
)
async def knowledge_search_tool(keyword: str) -> str:
    """从商品知识库语义检索商品知识（向量 + 重排，BM25 不默认启用）"""
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
        lines.extend("  " + ln for ln in _kb_field_lines(k))
    return "\n".join(lines)


@function_tool(
    name_override="inventory_check_tool",
    description_override="检查某商品的某个规格是否有库存；不指定规格(sku)时返回该商品全部规格的库存情况。注意：仅 demo 商品库收录库存字段，知识库商品会返回「未收录」",
)
async def inventory_check_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    product_id: str,
    sku: str = "",
) -> str:
    product = get_product(product_id)
    if not product:
        # 区分「商品存在但无库存字段」与「商品不存在」——前者不应答「未找到商品」
        kb = knowledge_store.get_knowledge_by_product_id(product_id)
        if kb:
            ctx = context.context.state
            ctx.product_id = product_id
            ctx.product_name = kb["name"]
            return (
                f"商品「{kb['name']}」（{product_id}）存在，但知识库未收录其库存信息。"
                f"请如实告知顾客「暂未收录库存信息，建议咨询客服确认」，不要编造库存数量。"
            )
        return f"未找到商品 {product_id}（知识库与商品库均无记录）。"

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

# 进度提示语：让 LLM 拿到可直接转述给顾客的一句话，而不只是一个状态词
_RETURN_PROGRESS_HINT = {
    "待审核": "当前进度：等待商家审核（承诺 24 小时内完成）。",
    "已通过": "当前进度：审核已通过，请按短信指引寄回商品。",
    "已寄回": "当前进度：商品已寄回，等待商家确认收货。",
    "已退款": "当前进度：退款已完成，请留意原支付账户。",
    "已撤销": "当前进度：该退货申请已撤销。",
}


def _return_status_text(order_number: str, ctx) -> str:
    case = order_store.get_return_status(order_number)
    if case is None:
        return f"订单 {order_number} 没有退货记录。如需申请退货，告诉我原因即可。"
    ctx.return_case_id = case["case_id"]
    ctx.return_status = case["status"]
    lines = [
        f"退货单号: {case['case_id']}",
        f"订单号: {case['order_number']}",
        f"退货状态: {case['status']}",
    ]
    if case.get("reason"):
        lines.append(f"退货原因: {case['reason']}")
    if case.get("refund_amount"):
        lines.append(f"退款金额: ¥{case['refund_amount']}")
    if case.get("created_at"):
        lines.append(f"申请时间: {case['created_at']}")
    lines.append(_RETURN_PROGRESS_HINT.get(case["status"], ""))
    return "\n".join(line for line in lines if line)


def _cancel_return_text(order_number: str, ctx) -> str:
    case = order_store.cancel_return_case(order_number)
    if case is None:
        return f"订单 {order_number} 没有正在进行的退货申请，无需撤销。"
    if case["status"] != "已撤销":
        # 决策 D6「未进入下一状态即可撤销」：已通过 / 已寄回 / 已退款一律拒绝
        return (
            f"订单 {order_number} 的退货单 {case['case_id']} 当前状态为「{case['status']}」，"
            f"已进入处理流程，无法撤销。如确需中止，我可以帮你转人工处理。"
        )
    ctx.return_case_id = case["case_id"]
    ctx.return_status = case["status"]
    order = order_store.get_order(order_number) or {}
    return (
        f"退货申请已撤销。\n"
        f"退货单号: {case['case_id']}\n"
        f"订单号: {order_number}\n"
        f"订单状态已恢复为「{order.get('status', '未知')}」。"
    )


def _create_return_text(order: dict, order_number: str, reason: str, ctx) -> str:
    if order["status"] not in ("已签收", "已发货", "运输中"):
        return (
            f"订单 {order_number} 当前状态为「{order['status']}」，暂不支持退货。"
            f"已签收的订单可在7天内申请退货。"
        )

    # 幂等：已有在途单就复用它，不再建新单。
    # 旧实现用 random.randint 生成单号，用户在多轮对话里重复说"我要退货"就会堆出
    # 多张在途单，退款金额被重复计 —— 这是 E-7 的根因。
    existing = order_store.active_return_for(order_number)
    if existing:
        ctx.return_case_id = existing["case_id"]
        ctx.return_status = existing["status"]
        return (
            f"该订单已有一张在途退货单，无需重复申请。\n"
            f"退货单号: {existing['case_id']}\n"
            f"订单号: {order_number}\n"
            f"当前状态: {existing['status']}\n"
            f"如需查询进度或撤销，直接告诉我就行。"
        )

    case = order_store.create_return_case(
        order_number=order_number,
        reason=reason,
        refund_amount=order["paid_amount"],
        prev_order_status=order["status"],
    )
    ctx.return_case_id = case["case_id"]
    ctx.return_status = case["status"]

    food_items = [
        item for item in order["items"]
        if any(kw in item["name"] for kw in ["坚果", "零食", "食品"])
    ]
    warning = ""
    if food_items and order["status"] == "已签收":
        food_names = ", ".join(f"「{item['name']}」" for item in food_items)
        warning = (
            "\n\n⚠️ 注意：您的订单包含食品类商品（{food}），"
            "如已拆封则不支持无理由退货。如存在质量问题，请拍照留证。"
        ).format(food=food_names)
        ctx.escalation_flag = True
        ctx.escalation_reason = "食品退货需人工审核"

    return (
        f"退货申请已提交！\n"
        f"退货单号: {case['case_id']}\n"
        f"订单号: {order_number}\n"
        f"退货原因: {case.get('reason') or reason or '[未填写]'}\n"
        f"预计退款金额: ¥{case.get('refund_amount') or order['paid_amount']}\n"
        f"审核将在24小时内完成，审核通过后会短信通知退货地址和寄回指引。" + warning
    )


@function_tool(
    name_override="initiate_return_tool",
    description_override=(
        "退货相关的统一入口。action=create 发起退货申请（同一订单会复用已有在途单，不会重复建单）；"
        "action=cancel 撤销退货申请（仅「待审核」状态可撤销）；"
        "action=status 查询退货进度。"
    ),
)
async def initiate_return_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    order_number: str,
    reason: str = "",
    action: str = "create",
) -> str:
    """退货统一入口。

    决策 D2：加 action 参数而不新增工具，工具数保持 13（AGENTS.md §7 禁止加工具）。
    reason 必须给默认值：action=status / cancel 时模型不应被要求提供退货原因，
    留成必填会导致漏传参数直接触发工具 schema 校验错误。
    """
    if action not in ("create", "cancel", "status"):
        return f"不支持的退货操作「{action}」，可选：create（发起）/ cancel（撤销）/ status（查询）。"

    order = order_store.get_order(order_number)
    if not order:
        return f"未找到订单 {order_number}。"

    ctx = context.context.state
    ctx.order_number = order_number
    ctx.order_status = order["status"]

    if action == "status":
        return _return_status_text(order_number, ctx)
    if action == "cancel":
        return _cancel_return_text(order_number, ctx)
    return _create_return_text(order, order_number, reason, ctx)


@function_tool(
    name_override="cancel_order_tool",
    description_override="取消订单（仅支持未发货的订单）",
)
async def cancel_order_tool(
    context: RunContextWrapper[ECommerceAgentChatContext],
    order_number: str,
) -> str:
    order = order_store.get_order(order_number)
    if not order:
        return f"未找到订单 {order_number}。"

    ctx = context.context.state
    ctx.order_number = order_number

    if order["status"] != "待发货":
        return (
            f"订单 {order_number} 当前状态为「{order['status']}」，无法取消。\n"
            f"如已发货，可等收到后申请退货退款。需要我帮你发起退货吗？"
        )

    # 必须落盘：只写 Context 的话，下一轮 order_status_tool 会从权威数据源读回原值，
    # 把用户刚看到的「已取消」覆盖掉 —— 就是 E-8"取消凭空消失"的根因。
    order_store.update_order_status(order_number, "已取消")
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
