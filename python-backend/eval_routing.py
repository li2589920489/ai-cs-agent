"""Triage 分流准确率评测

对 data/eval/agent_routing_eval.json（80 条自建路由评测集）逐条跑 Triage Agent，
取「第一跳 handoff 目标」作为分流预测，与期望 Agent 对比，输出：
  - 总体准确率 Accuracy
  - easy / hard 分层准确率
  - 混淆矩阵（expected × predicted）
  - 错题清单 + 异常清单

用法：
  python eval_routing.py                 # 全量 80 条
  python eval_routing.py --limit 10      # 只跑前 10 条（快速自测）
  python eval_routing.py --difficulty hard   # 只跑 hard 用例
"""
from __future__ import annotations

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

from agents import Runner, HandoffOutputItem
from agents.exceptions import (
    MaxTurnsExceeded,
    ModelBehaviorError,
    InputGuardrailTripwireTriggered,
)
from chatkit.types import ThreadMetadata

from ecommerce.agents import triage_agent
from ecommerce.context import ECommerceAgentChatContext, ECommerceAgentContext
from memory_store import MemoryStore

EVAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "eval", "agent_routing_eval.json")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "eval", "routing_eval_results.json")

MAX_TURNS = 10
MAX_ATTEMPTS = 3  # DeepSeek 偶发 ModelBehaviorError 的重试次数


def parse_args() -> tuple[int | None, str | None]:
    """解析命令行参数，返回 (limit, difficulty)。"""
    limit = None
    difficulty = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        elif a == "--difficulty" and i + 1 < len(args):
            difficulty = args[i + 1]
    return limit, difficulty


def load_examples(limit: int | None, difficulty: str | None) -> tuple[list[dict], list[str]]:
    with open(EVAL_PATH, encoding="utf-8") as f:
        data = json.load(f)
    examples = data["examples"]
    if difficulty:
        examples = [e for e in examples if e.get("difficulty") == difficulty]
    if limit:
        examples = examples[:limit]
    return examples, data.get("target_agents", [])


def make_context(idx: int) -> ECommerceAgentChatContext:
    """每条 query 用独立上下文，避免跨样本状态污染。"""
    store = MemoryStore()
    thread = ThreadMetadata(id=f"eval_{idx}", created_at=datetime.now())
    state = ECommerceAgentContext()
    return ECommerceAgentChatContext(
        thread=thread, store=store, request_context={}, state=state,
    )


def first_handoff_target(result) -> str | None:
    """取 Triage 第一跳 handoff 的目标 Agent 名。

    注意：不能用 result.last_agent.name —— 子 Agent 后续可能再 handoff 回 Triage
    或转去其他 Agent，last_agent 是最终落点而非 Triage 的初始分流决策。
    """
    for item in result.new_items:
        if isinstance(item, HandoffOutputItem):
            return item.target_agent.name
    return None


async def run_one(query: str, idx: int) -> tuple[str | None, str | None]:
    """跑一条 query，返回 (预测 agent 名, 错误信息)。"""
    ctx = make_context(idx)
    result = await Runner.run(triage_agent, query, context=ctx, max_turns=MAX_TURNS)
    return first_handoff_target(result), None


async def run_one_with_retry(query: str, idx: int) -> tuple[str | None, str | None]:
    """带有限重试的单条评测。"""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await run_one(query, idx)
        except InputGuardrailTripwireTriggered as e:
            # 护栏成功拦截（内容无关/越狱）——评测集应全部为电商相关，若触发说明护栏误伤
            return None, f"guardrail:{e.guardrail_result.guardrail.get_name()}"
        except MaxTurnsExceeded:
            return None, "MaxTurnsExceeded"
        except ModelBehaviorError:
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(0.5)
                continue
            return None, "ModelBehaviorError"
        except Exception as e:
            return None, f"{type(e).__name__}"
    return None, "ModelBehaviorError"


async def main() -> None:
    limit, difficulty = parse_args()
    examples, target_agents = load_examples(limit, difficulty)

    print(f"=== Triage 分流评测 ===")
    print(f"评测集: {os.path.basename(EVAL_PATH)}")
    print(f"样本数: {len(examples)}" + (f"（difficulty={difficulty}）" if difficulty else "") + "\n")

    results = []
    t0 = time.time()
    for i, ex in enumerate(examples):
        query = ex["query"]
        expected = ex["expected_agent"]
        predicted, error = await run_one_with_retry(query, ex["id"])
        correct = (predicted == expected)
        results.append({
            "id": ex["id"],
            "query": query,
            "expected": expected,
            "predicted": predicted,
            "correct": correct,
            "difficulty": ex.get("difficulty", "easy"),
            "error": error,
        })
        mark = "OK" if correct else ("ERR" if error else "WRONG")
        print(f"[{i + 1}/{len(examples)}] id={ex['id']:>2} exp={expected:<14} pred={(predicted or '(无分流)'):<14} {mark}"
              + (f"  [{error}]" if error else ""))

    elapsed = time.time() - t0

    # ---- 统计 ----
    total = len(results)
    correct_count = sum(1 for r in results if r["correct"])
    accuracy = correct_count / total if total else 0.0

    by_diff: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [correct, total]
    for r in results:
        by_diff[r["difficulty"]][1] += 1
        if r["correct"]:
            by_diff[r["difficulty"]][0] += 1

    # 混淆矩阵（expected → predicted 计数）
    matrix: dict[str, dict[str, int]] = {a: defaultdict(int) for a in target_agents}
    for r in results:
        matrix[r["expected"]][r["predicted"] or "(无分流/异常)"] += 1

    report = {
        "dataset": os.path.basename(EVAL_PATH),
        "total": total,
        "correct": correct_count,
        "accuracy": round(accuracy, 4),
        "by_difficulty": {
            d: {"correct": c, "total": t, "accuracy": round(c / t, 4) if t else None}
            for d, (c, t) in sorted(by_diff.items())
        },
        "confusion_matrix": {k: dict(v) for k, v in matrix.items()},
        "wrong_cases": [r for r in results if not r["correct"]],
        "errors": [r for r in results if r["error"]],
        "elapsed_seconds": round(elapsed, 1),
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # ---- 打印汇总 ----
    print(f"\n===== 分流评测结果 =====")
    print(f"总样本: {total}   正确: {correct_count}   准确率: {accuracy * 100:.2f}%   耗时: {elapsed:.1f}s")
    for d, (c, t) in sorted(by_diff.items()):
        print(f"  [{d}] {c}/{t} ({c / t * 100:.2f}%)")

    print(f"\n--- 混淆矩阵（行=期望，列=预测）---")
    header = ["expected \\ predicted"] + target_agents + ["(无分流/异常)"]
    print("  " + "  ".join(f"{h:<12}" for h in header))
    for exp in target_agents:
        row = [matrix[exp].get(a, 0) for a in target_agents] + [matrix[exp].get("(无分流/异常)", 0)]
        print("  " + f"{exp:<22}" + "  ".join(f"{v:<12}" for v in row))

    if report["wrong_cases"]:
        print(f"\n--- 错题清单（{len(report['wrong_cases'])} 条）---")
        for r in report["wrong_cases"]:
            err = f" [{r['error']}]" if r["error"] else ""
            print(f"  id={r['id']:>2} [{r['difficulty']}] {r['query']}  → 期望[{r['expected']}] 实际[{r['predicted'] or '无分流'}]{err}")

    print(f"\n结果已写入: {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
