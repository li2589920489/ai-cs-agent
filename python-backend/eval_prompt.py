"""Prompt 级评测 — 对 5 个业务/兜底 Agent 的 system prompt 做四类指标评测

对 data/eval/prompt_eval.json 逐条跑对应 Agent，按用例声明的断言打分，输出：
  - 各 category（instruction_follow / tool_choice / format / hallucination）通过率
  - 各 Agent 通过率
  - 错题清单（含实际工具调用与最终回复摘要，便于归因）

断言类型（assert 字段，可组合，全部满足才算通过）：
  - must_call / must_call_any : [工具名...]   至少调用其中一个工具
  - must_not_call / not_call_any: [工具名...] 不得调用其中任一工具
  - contains                  : [子串...]     最终回复至少包含其中一个子串（OR）
  - not_contains               : [子串...]    最终回复不得包含其中任一子串（AND）
  - max_chars                  : int           最终回复长度不得超过该字符数
  - route_to                   : str           Triage 应路由到该 Agent（按 last_agent.name）

用法：
  python eval_prompt.py                 # 全量
  python eval_prompt.py --limit 5       # 只跑前 5 条（快速自测）
  python eval_prompt.py --agent 商品知识 Agent
  python eval_prompt.py --category hallucination
  python eval_prompt.py --ids p014,p015 # 指定 ID 跑
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

# 必须在 import agents 之前禁用 SDK tracing，否则后台线程会尝试上传 trace 超时
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"

from agents import Runner
from agents.exceptions import (
    MaxTurnsExceeded,
    ModelBehaviorError,
    InputGuardrailTripwireTriggered,
)
from agents.items import ToolCallItem, HandoffOutputItem
from chatkit.types import ThreadMetadata

from ecommerce.agents import (
    triage_agent,
    order_tracking_agent,
    product_inquiry_agent,
    after_sales_agent,
    faq_agent,
    escalation_agent,
)
from ecommerce.context import ECommerceAgentChatContext, ECommerceAgentContext
from memory_store import MemoryStore

EVAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "eval", "prompt_eval.json")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "eval", "prompt_eval_results.json")

MAX_TURNS = 6
MAX_ATTEMPTS = 3  # DeepSeek 偶发 ModelBehaviorError 的重试次数

AGENTS = {
    "Triage Agent": triage_agent,
    "商品知识 Agent": product_inquiry_agent,
    "订单详情 Agent": order_tracking_agent,
    "售后退换货 Agent": after_sales_agent,
    "店铺政策 Agent": faq_agent,
    "Human Escalation Agent": escalation_agent,
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--agent", type=str, default=None)
    ap.add_argument("--category", type=str, default=None)
    ap.add_argument("--ids", type=str, default=None, help="逗号分隔的用例 ID 列表")
    return ap.parse_args()


def load_cases(args: argparse.Namespace) -> list[dict]:
    with open(EVAL_PATH, encoding="utf-8") as f:
        data = json.load(f)
    cases = data["cases"]
    if args.agent:
        cases = [c for c in cases if c["agent"] == args.agent]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if args.ids:
        ids = [i.strip() for i in args.ids.split(",")]
        cases = [c for c in cases if c["id"] in ids]
    if args.limit:
        cases = cases[: args.limit]
    return cases


def make_context(idx: str) -> ECommerceAgentChatContext:
    store = MemoryStore()
    thread = ThreadMetadata(id=f"prompt_eval_{idx}", created_at=datetime.now())
    state = ECommerceAgentContext()
    return ECommerceAgentChatContext(
        thread=thread, store=store, request_context={}, state=state,
    )


def extract_tools_called(result) -> list[str]:
    """从 new_items 提取本次运行实际调用的工具名（含 handoff 目标名）。"""
    tools = []
    for item in result.new_items:
        if isinstance(item, ToolCallItem):
            name = item.tool_name
            if name:
                tools.append(name)
        elif isinstance(item, HandoffOutputItem):
            tools.append(f"handoff:{item.target_agent.name}")
    return tools


def extract_target_agent(result) -> str | None:
    """从 result.last_agent 提取最终负责回复的 Agent 名称（用于 route_to 断言）。"""
    if hasattr(result, "last_agent") and result.last_agent is not None:
        return getattr(result.last_agent, "name", None)
    return None


def check_asserts(assert_spec: dict, tools_called: list[str], final_output: str,
                  target_agent: str | None = None) -> tuple[bool, list[str]]:
    """按 assert 逐项校验，返回 (是否全部通过, 失败项描述列表)。"""
    failures = []

    if "must_call" in assert_spec or "must_call_any" in assert_spec:
        allowed = assert_spec.get("must_call") or assert_spec.get("must_call_any")
        if not any(t in tools_called for t in allowed):
            failures.append(f"must_call {allowed} (actual {tools_called or 'none'})")

    if "must_not_call" in assert_spec or "not_call_any" in assert_spec:
        banned = assert_spec.get("must_not_call") or assert_spec.get("not_call_any")
        hit = [t for t in banned if t in tools_called]
        if hit:
            failures.append(f"must_not_call violation {hit}")

    if "contains" in assert_spec:
        values = assert_spec["contains"]
        if not any(v in final_output for v in values):
            failures.append(f"contains miss {values}")

    if "not_contains" in assert_spec:
        values = assert_spec["not_contains"]
        hit = [v for v in values if v in final_output]
        if hit:
            failures.append(f"not_contains violation {hit}")

    if "max_chars" in assert_spec:
        cap = assert_spec["max_chars"]
        if len(final_output) > cap:
            failures.append(f"max_chars over ({len(final_output)} > {cap})")

    if "route_to" in assert_spec:
        target = assert_spec["route_to"]
        if target_agent != target:
            failures.append(f"route_to={target} (actual {target_agent or 'None'})")

    return (len(failures) == 0, failures)


async def run_one(agent, query: str, idx: str) -> tuple[list[str], str, str | None]:
    ctx = make_context(idx)
    result = await Runner.run(agent, query, context=ctx, max_turns=MAX_TURNS)
    tools_called = extract_tools_called(result)
    final_output = str(result.final_output) if result.final_output is not None else ""
    target_agent = extract_target_agent(result)
    return tools_called, final_output, target_agent


async def run_one_with_retry(agent, query: str, idx: str) -> tuple[list[str], str, str | None, str | None]:
    """返回 (tools_called, final_output, target_agent, error)"""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            tools, out, target = await run_one(agent, query, idx)
            return tools, out, target, None
        except InputGuardrailTripwireTriggered as e:
            return [], "", None, f"guardrail:{e.guardrail_result.guardrail.get_name()}"
        except MaxTurnsExceeded:
            return [], "", None, "MaxTurnsExceeded"
        except ModelBehaviorError:
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(0.5)
                continue
            return [], "", None, "ModelBehaviorError"
        except Exception as e:  # noqa: BLE001
            return [], "", None, f"{type(e).__name__}"
    return [], "", None, "ModelBehaviorError"


async def main() -> None:
    args = parse_args()
    cases = load_cases(args)

    print("=== Prompt 级评测 ===")
    print(f"评测集: {os.path.basename(EVAL_PATH)}")
    print(f"用例数: {len(cases)}"
          + (f" (agent={args.agent})" if args.agent else "")
          + (f" (category={args.category})" if args.category else "")
          + (f" (ids={args.ids})" if args.ids else "")
          + "")

    results = []
    t0 = time.time()
    for i, ex in enumerate(cases):
        agent = AGENTS[ex["agent"]]
        tools, out, target, error = await run_one_with_retry(agent, ex["input"], ex["id"])
        passed, failures = check_asserts(ex["assert"], tools, out, target)
        results.append({
            "id": ex["id"],
            "agent": ex["agent"],
            "category": ex["category"],
            "input": ex["input"],
            "passed": passed,
            "failures": failures,
            "tools_called": tools,
            "target_agent": target,
            "final_output": out,
            "error": error,
        })
        mark = "OK" if passed else ("ERR" if error else "FAIL")
        print(f"[{i + 1}/{len(cases)}] id={ex['id']:>4} [{ex['category']:<18}] {ex['agent']:<14} {mark}"
              + (f"  {failures[0] if failures else ''}" if not passed else "")
              + (f"  [{error}]" if error else ""))

    elapsed = time.time() - t0

    total = len(results)
    valid = [r for r in results if not r["error"]]
    passed_count = sum(1 for r in results if r["passed"])
    error_count = sum(1 for r in results if r["error"])

    def group_metric(key):
        g: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in results:
            if r["error"]:
                continue
            g[r[key]][1] += 1
            if r["passed"]:
                g[r[key]][0] += 1
        return g

    by_category = group_metric("category")
    by_agent = group_metric("agent")

    report = {
        "dataset": os.path.basename(EVAL_PATH),
        "total": total,
        "passed": passed_count,
        "errors": error_count,
        "pass_rate": round(passed_count / total, 4) if total else None,
        "pass_rate_excl_error": round(passed_count / len(valid), 4) if valid else None,
        "by_category": {
            k: {"pass": c, "total": t, "rate": round(c / t, 4) if t else None}
            for k, (c, t) in sorted(by_category.items())
        },
        "by_agent": {
            k: {"pass": c, "total": t, "rate": round(c / t, 4) if t else None}
            for k, (c, t) in sorted(by_agent.items())
        },
        "failed_cases": [r for r in results if not r["passed"]],
        "elapsed_seconds": round(elapsed, 1),
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n===== Prompt 评测结果 =====")
    print(f"总用例: {total}   通过: {passed_count}   异常: {error_count}   通过率: {passed_count / total * 100:.2f}%"
          + (f" (excl-err {report['pass_rate_excl_error'] * 100:.2f}%)" if valid and error_count else "")
          + f"   耗时: {elapsed:.1f}s")

    print(f"\n--- 按指标类别 ---")
    for k, (c, t) in sorted(by_category.items()):
        rate = c / t * 100 if t else 0
        print(f"  {k:<18} {c}/{t} ({rate:.1f}%)")

    print(f"\n--- 按 Agent ---")
    for k, (c, t) in sorted(by_agent.items()):
        rate = c / t * 100 if t else 0
        print(f"  {k:<14} {c}/{t} ({rate:.1f}%)")

    if report["failed_cases"]:
        print(f"\n--- 失败用例 ({len(report['failed_cases'])} 条) ---")
        for r in report["failed_cases"]:
            err = f" [{r['error']}]" if r["error"] else ""
            snippet = r["final_output"].replace("\n", " ")[:60]
            print(f"  id={r['id']} [{r['category']}] {r['input']}")
            print(f"      fail: {'; '.join(r['failures'])}{err}")
            print(f"      tools: {r['tools_called'] or 'none'} | reply: {snippet}")

    print(f"\n结果已写入: {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())