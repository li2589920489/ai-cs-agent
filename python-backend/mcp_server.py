"""
MCP Server — 把电商知识库检索 / 订单查询能力封装为标准化 MCP 工具

用途：任何支持 MCP 协议的客户端（Claude Desktop、Cline、其他 Agent 等）
都可以通过统一协议调用本店的知识库检索与订单查询能力，
实现「工具层与 Agent 编排解耦、可复用」。

运行方式：
    python mcp_server.py                 # stdio 传输（默认）
    fastmcp run mcp_server.py            # 等价

接入示例（MCP 客户端配置）：
    {
      "mcpServers": {
        "ecommerce-kb": {
          "command": "python",
          "args": ["mcp_server.py"],
          "cwd": "<本目录>"
        }
      }
    }
"""
from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP(
    "ecommerce-knowledge",
    instructions="电商店铺知识库与订单查询 MCP 服务，提供语义检索商品知识/店铺政策与订单查询能力。",
)

_rag_pipeline = None


def _get_pipeline():
    global _rag_pipeline
    if _rag_pipeline is None:
        from rag import load_pipeline

        _rag_pipeline = load_pipeline()
    return _rag_pipeline


@mcp.tool()
def search_knowledge(query: str, k: int = 3) -> str:
    """语义检索知识库（含商品知识与店铺政策），返回最相关的知识片段。

    Args:
        query: 用户问题或查询关键词
        k: 返回条数，默认 3
    """
    results = _get_pipeline().search(query, k=k, use_hybrid=True, use_rerank=True)
    if not results:
        return "未检索到相关知识。"
    parts = []
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        src = meta.get("name") or meta.get("policy_name") or "知识库"
        parts.append(f"[{i}] 来源：{src}\n{r['text'].strip()[:300]}")
    return "\n\n---\n\n".join(parts)


@mcp.tool()
def search_policy(query: str, k: int = 3) -> str:
    """仅检索店铺政策（退货/退款/发货/物流/优惠券/会员等），返回政策原文片段。"""
    results = _get_pipeline().search(query, k=max(k, 6), use_hybrid=True, use_rerank=True)
    policies = [r for r in results if r.get("metadata", {}).get("type") == "policy"][:k]
    if not policies:
        return "未检索到相关店铺政策。"
    parts = []
    for i, r in enumerate(policies, 1):
        name = r.get("metadata", {}).get("policy_name", "政策")
        parts.append(f"[{i}] {name}\n{r['text'].strip()[:300]}")
    return "\n\n---\n\n".join(parts)


@mcp.tool()
def get_order(order_number: str) -> str:
    """根据订单号查询订单详情与物流信息。"""
    from ecommerce.demo_data import get_order

    order = get_order(order_number)
    if not order:
        return f"未找到订单 {order_number}。"
    tracking = order.get("tracking", {})
    items = "、".join(f"{it['name']}×{it['quantity']}" for it in order["items"])
    return (
        f"订单号: {order['order_number']}\n"
        f"状态: {order['status']}\n"
        f"商品: {items}\n"
        f"实付: ¥{order['paid_amount']}\n"
        f"物流: {tracking.get('company', '未知')} {tracking.get('number', '')} — {tracking.get('location', '暂无')}"
    )


@mcp.tool()
def search_products(keyword: str) -> str:
    """按关键词搜索店铺商品。"""
    from ecommerce.demo_data import search_products

    results = search_products(keyword)
    if not results:
        return f"未找到与「{keyword}」相关的商品。"
    lines = [f"「{keyword}」相关商品（{len(results)}件）:"]
    for p in results:
        lines.append(f"- [{p['product_id']}] {p['name']} ¥{p['price']} 评分{p['rating']}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
