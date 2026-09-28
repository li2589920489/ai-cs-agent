"""
H5 验证 — 新增知识能否进入向量检索（reindex 闭环）

背景（问题文档 H5）：
    过去向量索引只在「库为空 / embedding backend 变化」时重建。
    运行中的服务持有管线单例 → 经前端或 API 新增的知识 SQLite 里有、向量库里没有，
    检索永远召回不到，且不报错。

修复机制三层：
    1. 知识库指纹 knowledge_sig = "行数:最新updated_at" 写入索引 metadata
    2. load_pipeline 第三判据比对指纹 → 不一致即重建
    3. 写操作后 mark_pipeline_stale()，管线单例在下次取用时重载

本脚本验证闭环：
    baseline 检索（应无命中）
      → 写入一条带唯一关键词的知识
      → mark_pipeline_stale()（等价于 main.py 写操作后所做的事）
      → 再取 pipeline（应检测到指纹变化并重建）
      → 同关键词检索（应命中）
      → 清理：删除该条 → 再次刷新 → 确认回到无命中

用法:
    python scripts/verify_h5_reindex.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import knowledge_store  # noqa: E402
from rag import get_pipeline, mark_pipeline_stale  # noqa: E402

# 唯一关键词，确保不会与语料里的任何真实商品撞车
MARKER = "紫云英曜石"
TEST_ITEM = {
    "product_id": "ZZ_H5_TEST",
    "name": f"测试商品{MARKER}限定款",
    "category": "测试类目",
    "description": f"这是一条仅用于验证索引刷新的测试商品，关键词{MARKER}。",
    "selling_points": [f"{MARKER}验货专用"],
}


def search_kw() -> list[dict]:
    """走检索管线按唯一关键词搜索（只取知识库商品 chunk）

    注意：重排器没有分数阈值，任何 query 都会返回 top-k，
    所以不能用「命中 0 条」判断基线，必须按 product_id 做身份比对。
    """
    pipe = get_pipeline()
    results = pipe.search(MARKER, k=5, use_hybrid=False, use_rerank=True)
    return [r for r in results if r.get("metadata", {}).get("type") == "product"]


def hit_ids(results: list[dict]) -> list[str]:
    return [r.get("metadata", {}).get("product_id") for r in results]


def main() -> None:
    print("=" * 78)
    print(f"唯一关键词: {MARKER}")
    probe = TEST_ITEM["product_id"]

    # ---- 0. 清理历史残留（上一次跑挂了可能留下测试条目） ----
    for old in knowledge_store.search_knowledge(MARKER, limit=5):
        if old["product_id"] == probe:
            knowledge_store.delete_knowledge(old["id"])
            print(f"[0] 清理历史残留 id={old['id']}")

    # ---- 1. baseline：写入前检索 ----
    t0 = time.time()
    before = search_kw()
    sig_before = knowledge_store.knowledge_signature()
    print(f"[1] 写入前：返回 {len(before)} 条（重排无阈值）| knowledge_sig={sig_before} "
          f"| 耗时 {time.time() - t0:.1f}s")
    print(f"    top-5 商品编号: {hit_ids(before)}")
    assert probe not in hit_ids(before), f"baseline 不该命中 {probe} —— 测试条目已在索引中"

    # ---- 2. 写入新知识（模拟前端/API 新增） ----
    created = knowledge_store.create_knowledge(dict(TEST_ITEM))
    print(f"[2] 已写入：id={created['id']} name={created['name']}")

    # ---- 3. 标记管线过期（main.py 写操作后做的同一件事） ----
    mark_pipeline_stale()
    print("[3] 已 mark_pipeline_stale()")

    # ---- 4. 再取 pipeline：应检测到指纹变化并重建 ----
    t1 = time.time()
    after = search_kw()
    sig_after = knowledge_store.knowledge_signature()
    print(f"[4] 写入后：返回 {len(after)} 条 | knowledge_sig={sig_after} "
          f"| 耗时 {time.time() - t1:.1f}s（含索引重建）")
    print(f"    top-5 商品编号: {hit_ids(after)}")
    for r in after[:3]:
        meta = r.get("metadata", {})
        print(f"    - [{meta.get('product_id')}] {meta.get('name')} score={r.get('score'):.4f}")

    ok_new = probe in hit_ids(after)
    assert sig_after != sig_before, "指纹未变化 —— 知识库写入没反映到指纹"
    assert ok_new, f"新增知识未被检索命中（{probe} 不在 top-5）—— reindex 闭环断了"

    # ---- 5. 清理：删除测试条目，恢复检索环境 ----
    knowledge_store.delete_knowledge(created["id"])
    mark_pipeline_stale()
    t2 = time.time()
    cleaned = search_kw()
    print(f"[5] 清理后：返回 {len(cleaned)} 条 | knowledge_sig={knowledge_store.knowledge_signature()} "
          f"| 耗时 {time.time() - t2:.1f}s（含索引重建）")
    print(f"    top-5 商品编号: {hit_ids(cleaned)}")
    assert probe not in hit_ids(cleaned), f"清理失败 —— 测试条目 {probe} 仍在索引中"

    print("=" * 78)
    print("结论：H5 闭环通过 —— 新增知识经 mark_pipeline_stale 后可被检索，删除后亦可回收。")
    print("=" * 78)


if __name__ == "__main__":
    main()
