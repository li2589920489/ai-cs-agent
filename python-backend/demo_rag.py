"""
RAG 检索演示脚本 — 独立验证完整检索管线

用法：python demo_rag.py [查询词] ...
不传参数则运行一组内置测试查询。
"""
from __future__ import annotations

import sys
import time

from rag import build_index, load_pipeline


def pretty_print(query: str, results: list[dict]) -> None:
    print("\n" + "=" * 70)
    print(f"查询：{query}")
    print("-" * 70)
    if not results:
        print("  (无结果)")
        return
    for i, r in enumerate(results, 1):
        meta = r["metadata"]
        src = meta.get("type", "?")
        label = meta.get("name") or meta.get("policy_name") or "-"
        print(f"  [{i}] ({src}|{label}) score={r['score']}")
        print(f"      {r['text'][:100].strip()}")


def main() -> None:
    queries = sys.argv[1:] or [
        "退货政策是什么？",
        "坚果可以无理由退货吗",
        "优惠券可以叠加使用吗",
        "小米加湿器有什么卖点",
        "物流多久没更新算异常",
    ]

    print("正在构建/加载索引 ...")
    t0 = time.time()
    pipeline = load_pipeline()
    print(f"索引就绪：{pipeline.describe()}，耗时 {time.time() - t0:.1f}s")

    for q in queries:
        results = pipeline.search(q, k=3, use_hybrid=True, use_rerank=True)
        pretty_print(q, results)


if __name__ == "__main__":
    main()
