"""
H2 验证 — 商品三工具（详情 / 搜索 / 库存）是否已认「真实导入商品」

背景（问题文档 H2）：
    真实导入的 1300+ 商品（编号 EC*）只进了知识库与向量库，
    商品三工具（product_info_tool / search_products_tool / inventory_check_tool）
    过去只查 demo 的 4 件商品，导致「搜电饭煲」一律返回「未找到」。

本脚本直接调用 FunctionTool.on_invoke_tool，绕开 LLM，验证工具层返回文本。

用法:
    python scripts/verify_h2_product_tools.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import RunConfig  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402

from ecommerce import tools as T  # noqa: E402
from ecommerce.context import ECommerceAgentContext  # noqa: E402


def make_tc(tool, args: dict) -> ToolContext:
    """构造一次工具调用所需的 ToolContext（v0.22 为 dataclass，tool_arguments 必填）"""
    return ToolContext(
        context=SimpleNamespace(state=ECommerceAgentContext()),
        tool_name=tool.name,
        tool_call_id="call_verify_h2",
        tool_arguments=json.dumps(args),
        run_config=RunConfig(),
    )


async def call(tool, args: dict) -> str:
    return await tool.on_invoke_tool(make_tc(tool, args), json.dumps(args))


async def main() -> None:
    cases: list[tuple[str, object, dict, str]] = [
        # 工具, 参数, 预期
        (T.product_info_tool, {"product_id": "EC1066"}, "知识库收录 + 价格/库存如实说未收录"),
        (T.product_info_tool, {"product_id": "P1003"}, "demo 完整详情（含价格/规格库存）"),
        (T.product_info_tool, {"product_id": "EC999999"}, "知识库与商品库均无记录"),
        (T.search_products_tool, {"keyword": "电饭煲"}, "语义检索召回 EC* 真实商品"),
        (T.search_products_tool, {"keyword": "三只松鼠每日坚果"}, "demo 兜底或语义命中"),
        (T.inventory_check_tool, {"product_id": "EC1066"}, "商品存在但未收录库存"),
        (T.inventory_check_tool, {"product_id": "P1003"}, "demo 全规格库存"),
        (T.knowledge_search_tool, {"keyword": "加湿器"}, "知识库语义检索"),
    ]

    for tool, args, expect in cases:
        print("=" * 78)
        print(f"[{tool.name}] {json.dumps(args, ensure_ascii=False)}")
        print(f"  预期: {expect}")
        try:
            out = await call(tool, args)
        except Exception as e:  # noqa: BLE001
            print(f"  !! 调用异常: {type(e).__name__}: {e}")
            continue
        text = str(out)
        for line in text.splitlines()[:14]:
            print("  | " + line[:150])
        if len(text.splitlines()) > 14:
            print(f"  | ...（共 {len(text.splitlines())} 行）")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
