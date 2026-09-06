"""
RRF 调优实验 — 诊断 BM25 互补性 + 扫描加权 RRF 配置
======================================================
复用 EcomRetrieval 已缓存的 corpus/query embedding，快速扫描多种 RRF 配置，
回答两个问题：
  1. BM25 是否与向量互补（能找到向量 top-10 找不到的相关 doc）？
  2. 加权 RRF 能否让混合检索反超纯向量？

用法：python eval_rrf_tuning.py
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

RRF_K = 60
RECALL_N = 20
DATA_DIR = Path(__file__).resolve().parent / "data" / "eval" / "ecom_retrieval"
CACHE_DIR = DATA_DIR / "cache"


def topk_by_scores(scores: np.ndarray, k: int) -> np.ndarray:
    idx = np.argpartition(-scores, kth=k - 1, axis=1)[:, :k]
    rows = np.arange(scores.shape[0])[:, None]
    topk_scores = scores[rows, idx]
    order = np.argsort(-topk_scores, axis=1)
    return idx[rows, order]


def metric_of(ranked_indices, gold_idx):
    m = len(ranked_indices)
    hits = {k: 0 for k in (1, 5, 10)}
    rr = ndcg = found = 0.0
    for ranked, g in zip(ranked_indices, gold_idx):
        try:
            rank = ranked.index(g) + 1
        except ValueError:
            rank = 10**9
        if rank <= 10:
            found += 1
            rr += 1.0 / rank
            ndcg += 1.0 / math.log2(rank + 1)
        for k in hits:
            if rank <= k:
                hits[k] += 1
    return {
        "recall@1": round(hits[1] / m, 4),
        "recall@5": round(hits[5] / m, 4),
        "recall@10": round(hits[10] / m, 4),
        "mrr@10": round(rr / m, 4),
        "ndcg@10": round(ndcg / m, 4),
    }


def main() -> None:
    # 1. 数据
    corpus_df = pq.read_table(DATA_DIR / "corpus.parquet").to_pandas()
    queries_df = pq.read_table(DATA_DIR / "queries.parquet").to_pandas()
    qrels_df = pq.read_table(DATA_DIR / "qrels.parquet").to_pandas()

    corpus = corpus_df["text"].astype(str).tolist()
    corpus_ids = corpus_df["id"].astype(str).tolist()
    id2idx = {cid: i for i, cid in enumerate(corpus_ids)}
    query_texts = queries_df["text"].astype(str).tolist()
    gold_idx = [id2idx.get(str(r["corpus-id"]), -1)
                for _, r in qrels_df.iterrows()]

    # 2. 缓存 embedding
    corpus_emb = np.load(CACHE_DIR / "ecom_corpus_emb.npy")
    query_emb = np.load(CACHE_DIR / "ecom_query_emb.npy")

    # 3. BM25
    from rag.bm25 import BM25Retriever
    print("构建 BM25 索引...", flush=True)
    bm25 = BM25Retriever(corpus)

    # 4. 向量 top-20 与 BM25 top-20
    print("计算向量 / BM25 两路召回...", flush=True)
    top_vec = topk_by_scores(query_emb @ corpus_emb.T, RECALL_N)
    bm25_hits: list[list[int]] = []
    for q in query_texts:
        bm25_hits.append([idx for idx, _ in bm25.search(q, k=RECALL_N)])

    # 5. 诊断互补性
    vec_set = [set(row.tolist()) for row in top_vec]
    bm_set = [set(h) for h in bm25_hits]
    only_bm25 = sum(1 for i, g in enumerate(gold_idx)
                    if g in bm_set[i] and g not in vec_set[i])
    only_vec = sum(1 for i, g in enumerate(gold_idx)
                   if g in vec_set[i] and g not in bm_set[i])
    both = sum(1 for i, g in enumerate(gold_idx)
               if g in vec_set[i] and g in bm_set[i])
    neither = sum(1 for i, g in enumerate(gold_idx)
                  if g not in vec_set[i] and g not in bm_set[i])
    print(f"\n互补性诊断（top-20 池）：")
    print(f"  仅向量命中={only_vec}  仅BM25命中={only_bm25}  双命中={both}  都未命中={neither}")

    # 6. 扫描加权 RRF 配置
    configs = [
        ("朴素RRF(1:1)", 1.0, 1.0, 20),
        ("加权(1.5:1)", 1.5, 1.0, 20),
        ("加权(2:1)", 2.0, 1.0, 20),
        ("加权(3:1)", 3.0, 1.0, 20),
        ("加权(5:1)", 5.0, 1.0, 20),
        ("加权(2:1)+BM25top10", 2.0, 1.0, 10),
        ("加权(2:1)+BM25top5", 2.0, 1.0, 5),
        ("加权(2:1)+BM25top3", 2.0, 1.0, 3),
    ]

    print("\n" + "=" * 70)
    print("RRF 调优结果（融合后取 top-20 计算指标）")
    print("=" * 70)
    print(f"{'配置':<22}{'R@1':>7}{'R@5':>7}{'R@10':>7}{'MRR':>8}{'nDCG':>8}")
    print("-" * 70)

    base_vec = metric_of([row.tolist() for row in top_vec], gold_idx)
    print(f"{'[基线] 纯向量':<22}{base_vec['recall@1']:>7}{base_vec['recall@5']:>7}"
          f"{base_vec['recall@10']:>7}{base_vec['mrr@10']:>8}{base_vec['ndcg@10']:>8}")

    best = None
    for name, w_vec, w_bm25, bm25_k in configs:
        ranked_lists = []
        for i in range(len(query_texts)):
            vec = list(top_vec[i])
            bm = bm25_hits[i][:bm25_k]
            rrf: dict[int, float] = {}
            for rank, idx in enumerate(vec):
                rrf[idx] = rrf.get(idx, 0.0) + w_vec / (RRF_K + rank + 1)
            for rank, idx in enumerate(bm):
                rrf[idx] = rrf.get(idx, 0.0) + w_bm25 / (RRF_K + rank + 1)
            fused = sorted(rrf.items(), key=lambda x: -x[1])
            ranked_lists.append([idx for idx, _ in fused[:RECALL_N]])
        m = metric_of(ranked_lists, gold_idx)
        print(f"{name:<22}{m['recall@1']:>7}{m['recall@5']:>7}"
              f"{m['recall@10']:>7}{m['mrr@10']:>8}{m['ndcg@10']:>8}")
        if best is None or m["ndcg@10"] > best[1]["ndcg@10"]:
            best = (name, m, dict(w_vec=w_vec, w_bm25=w_bm25, bm25_k=bm25_k))
    print("=" * 70)

    print(f"\n最优配置: {best[0]}  ->  {json.dumps(best[1], ensure_ascii=False)}")

    out = {
        "complementarity": {"only_vector": only_vec, "only_bm25": only_bm25,
                            "both": both, "neither": neither},
        "baseline_vector": base_vec,
        "best_config": best[0],
        "best_params": best[2],
        "best_metrics": best[1],
    }
    (DATA_DIR / "rrf_tuning_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {DATA_DIR / 'rrf_tuning_results.json'}")


if __name__ == "__main__":
    main()
