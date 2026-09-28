"""Step B 验证: 用 Runner.run 真实调 Agent，验证 faq_lookup_tool 默认走 RAG 路径

策略：让 LLM 决定调哪个工具，然后从 result.new_items 提取工具调用名和输出文本。
对比调用前后的行为差异：
  - faq_lookup_tool(退货政策) → 应调 faq_lookup_tool，输出含 [1] 来源：xxx
  - rag_search_tool(退货运费) → 应调 rag_search_tool
  - faq_lookup_tool(会员) → 应调 faq_lookup_tool，走 RAG（7 条 policy 含会员权益）
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import Runner
from agents.items import ToolCallItem

from chatkit.types import ThreadMetadata

from datetime import datetime

from dotenv import load_dotenv
load_dotenv()
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"

from chatkit.types import ThreadMetadata

from memory_store import MemoryStore

from ecommerce.agents import faq_agent
from ecommerce.context import ECommerceAgentChatContext, ECommerceAgentContext

MAX_TURNS = 6


def make_context(idx: str) -> ECommerceAgentChatContext:
    store = MemoryStore()
    thread = ThreadMetadata(id=f"step_b_{idx}", created_at=datetime.now())
    state = ECommerceAgentContext()
    return ECommerceAgentChatContext(
        thread=thread, store=store, request_context={}, state=state,
    )


async def run_query(query: str, idx: str = "x") -> tuple[list[str], str]:
    ctx = make_context(idx)
    result = await Runner.run(faq_agent, query, context=ctx, max_turns=MAX_TURNS)
    tools_called = []
    for item in result.new_items:
        if isinstance(item, ToolCallItem):
            # ToolCallItem 直接有 tool_name 属性（SDK 1.x+）
            name = getattr(item, "tool_name", None)
            if name:
                tools_called.append(name)
    final_output = str(result.final_output) if result.final_output else ""
    return tools_called, final_output


async def main():
    cases = [
        ("退货政策", "我想了解退货政策"),
        ("发货时效", "你们多久发货"),
        ("食品拆封", "零食拆封了还能退吗"),
        ("退货运费", "退货运费谁承担"),
        ("会员权益", "会员有什么权益"),
    ]
    print(f"{'query':<22} {'工具调用':<25} 输出片段")
    print("-" * 80)
    for label, q in cases:
        t0 = time.time()
        tools, output = await run_query(q)
        dt = time.time() - t0
        tools_str = ",".join(tools) if tools else "(无)"
        out_snippet = output[:80].replace("\n", " ")
        print(f"{label:<22} {tools_str:<25} {out_snippet}... ({dt:.1f}s)")
        # 额外打印完整输出
        print(f"  [full output]: {output}")
        print()


asyncio.run(main())