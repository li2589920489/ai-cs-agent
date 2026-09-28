"""
自有场景真实 RAG 评测 — 1300 EC* 商品 + 7 店铺政策
====================================================
对比 EcomRetrieval 标准基准（100k corpus）结果，证明 RAG 在"自有场景"下
的检索质量可与公开基准对齐，且实际服务的就是这一份语料。

模式（复用 eval_retrieval.py 同款核心算法）：
- vector            : 纯向量（默认运行时路径）
- hybrid            : 向量 + BM25，RRF 融合
- hybrid_rerank     : 混合 + bge-reranker 精排

输入:
  data/eval/real_query_eval/query_eval_set.json   (构造好的评测集)
  data/eval/real_query_eval/sub_corpus.json        (1300 EC* 子集)
  data/chroma_rag/numpy_store.db                   (1327 chunks 实际索引)

输出:
  data/eval/real_query_eval/eval_results.json

用法:
  python eval_real_corpus.py                        # 默认测 3 种模式
  python eval_real_corpus.py --modes vector,hybrid_rerank
  python eval_real_corpus.py --limit-queries 50     # smoke
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

# 与 eval_retrieval.py / rag 保持一致
RRF_K = 60
RECALL_N = 20
RERANK_N = 20

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "eval" / "real_query_eval"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_eval_set() -> tuple[list[str], list[str], list[int], list[str]]:
    """读 query_eval_set.json + sub_corpus.json，返回 (corpus, query_texts, gold_idx, query_ids)

    corpus: 1320 product + 7 policy = 1327 chunks（按 numpy_store 的实际顺序）
    gold_idx: 每个 query 对应的相关 doc 在 corpus 里的下标
    """
    with open(DATA_DIR / "query_eval_set.json", encoding="utf-8") as f:
        eval_set = json.load(f)
    with open(DATA_DIR / "sub_corpus.json", encoding="utf-8") as f:
        sub_corpus = json.load(f)

    # 7 条 policy 也算 corpus（policy chunk 算命中的话也算召回）
    # 但 EcomRetrieval qrels 是按 EC* id 标的，policy 不会出现在 qrels 里
    # 所以评测只对 product 子集做"召回率"判定

    # 构造 corpus-id -> idx 映射
    sub_corpus_id_to_idx = {r["id"]: i for i, r in enumerate(sub_corpus)}

    # 拿 numpy_store 实际 chunks 顺序（确保下标对得上 numpy_store._metas）
    # 实际做法：直接用 load_pipeline() 返回的 corpus（顺序就是 numpy_store 顺序）
    # 这里我们用 numpy_store 加载 1320 EC* + 7 policy
    query_texts: list[str] = []
    query_ids: list[str] = []
    gold_idx: list[int] = []
    for case in eval_set:
        query_texts.append(case["query"])
        query_ids.append(case["query_id"])
        # 投影：sub_corpus 是按 EC{id} 顺序，gold_corpus_id 是原始 EcomRetrieval corpus-id
        gold_idx.append(sub_corpus_id_to_idx.get(case["gold_corpus_id"], -1))

    return sub_corpus, query_texts, gold_idx, query_ids


def get_numpy_store_corpus() -> tuple[list[str], list[dict]]:
    """从 numpy_store.db 读真实 chunks（1320 product + 7 policy）"""
    from rag import load_pipeline

    p = load_pipeline()
    return p.vector_store._docs, p.vector_store._metas


def compute_metrics(ranked_indices: list[list[int]], gold_idx: list[int],
                    top_ks: list[int] = (1, 5, 10)) -> dict:
    """复用 eval_retrieval.py 的指标计算（单相关 doc）"""
    m = len(ranked_indices)
    hits = {k: 0 for k in top_ks}
    rr_sum = 0.0
    ndcg_sum = 0.0
    found = 0

    for ranked, g in zip(ranked_indices, gold_idx):
        if g < 0:
            continue  # qrels 不在子集里的 query，跳过
        try:
            rank = ranked.index(g) + 1
        except ValueError:
            rank = 10**9
        if rank <= 10:
            found += 1
            rr_sum += 1.0 / rank
            ndcg_sum += 1.0 / math.log2(rank + 1)
        for k in top_ks:
            if rank <= k:
                hits[k] += 1

    return {
        **{f"recall@{k}": round(hits[k] / m, 4) for k in top_ks},
        "mrr@10": round(rr_sum / m, 4),
        "ndcg@10": round(ndcg_sum / m, 4),
        "hit@10_rate": round(found / m, 4),
        "query_count": m,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="vector,hybrid,hybrid_rerank",
                    help="vector / hybrid / hybrid_rerank")
    ap.add_argument("--limit-queries", type=int, default=0)
    ap.add_argument("--rerank-top", type=int, default=RERANK_N)
    args = ap.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    use_rerank = "hybrid_rerank" in modes

    print("== 加载自有场景语料（numpy_store.db）==", flush=True)
    actual_docs, actual_metas = get_numpy_store_corpus()
    print(f"    numpy_store corpus: {len(actual_docs)} chunks", flush=True)
    print(f"    类别分布: {sum(1 for m in actual_metas if m.get('type') == 'product')} product, "
          f"{sum(1 for m in actual_metas if m.get('type') == 'policy')} policy", flush=True)

    print("== 加载 query 评测集 ==", flush=True)
    sub_corpus, query_texts, gold_idx, query_ids = load_eval_set()
    print(f"    query_eval_set: {len(query_texts)} 条", flush=True)

    # 把 sub_corpus 的 id → numpy_store 下标映射起来
    # numpy_store 的 chunks 顺序可能和 sub_corpus 不一致，需要重新构造 gold_idx
    # numpy_store 的 meta 含 product_id (EC*) 或 policy_name
    product_id_to_npy_idx = {}
    for i, m in enumerate(actual_metas):
        if m.get("type") == "product" and m.get("product_id"):
            # EC{id} -> npy idx
            product_id_to_npy_idx[m["product_id"]] = i
        elif m.get("type") == "policy" and m.get("policy_name"):
            product_id_to_npy_idx[m["policy_name"]] = i

    # 重写 gold_idx：以 numpy_store 顺序为准
    new_gold_idx = []
    valid_count = 0
    for case_idx, case in enumerate(json.loads((DATA_DIR / "query_eval_set.json").read_text(encoding="utf-8"))):
        ec_id = case["gold_corpus_id"]
        ec_product_id = f"EC{ec_id}"
        npy_idx = product_id_to_npy_idx.get(ec_product_id, -1)
        new_gold_idx.append(npy_idx)
        if npy_idx >= 0:
            valid_count += 1

    print(f"    gold 在 numpy_store 中有效命中: {valid_count}/{len(new_gold_idx)}", flush=True)
    gold_idx = new_gold_idx

    # 如果有 -1（numpy_store 没有），过滤掉
    valid_mask = [i for i, g in enumerate(gold_idx) if g >= 0]
    query_texts = [query_texts[i] for i in valid_mask]
    query_ids = [query_ids[i] for i in valid_mask]
    gold_idx = [gold_idx[i] for i in valid_mask]
    print(f"    有效 query 数（gold 在 numpy_store 里）: {len(query_texts)}", flush=True)

    if args.limit_queries > 0:
        query_texts = query_texts[: args.limit_queries]
        query_ids = query_ids[: args.limit_queries]
        gold_idx = gold_idx[: args.limit_queries]
        print(f"    [smoke] 只测前 {args.limit_queries} 条", flush=True)

    # ---------- 2. 加载 BGE 模型 ----------
    print("== 加载 BGE 模型 ==", flush=True)
    from rag.embedding import Embedder, BGE_QUERY_INSTRUCTION
    from sentence_transformers import SentenceTransformer

    embedder = Embedder()
    model_path = embedder._resolve_model()
    print(f"    模型路径: {model_path}", flush=True)
    model = SentenceTransformer(model_path, device="cpu")

    # ---------- 3. Embedding（带缓存）----------
    corpus_cache = CACHE_DIR / "real_corpus_emb.npy"
    query_cache = CACHE_DIR / "real_query_emb.npy"

    if corpus_cache.exists():
        print("== 命中 corpus embedding 缓存 ==", flush=True)
        corpus_emb = np.load(corpus_cache)
    else:
        print(f"== 向量化 corpus（{len(actual_docs)} 条）==", flush=True)
        t0 = time.time()
        corpus_emb = model.encode(actual_docs, normalize_embeddings=True,
                                  show_progress_bar=False, convert_to_numpy=True)
        corpus_emb = corpus_emb.astype(np.float32)
        np.save(corpus_cache, corpus_emb)
        print(f"    完成 ({time.time()-t0:.1f}s), shape={corpus_emb.shape}", flush=True)

    if query_cache.exists():
        print("== 命中 query embedding 缓存 ==", flush=True)
        query_emb_all = np.load(query_cache)
    else:
        print(f"== 向量化 queries（{len(query_texts)} 条）==", flush=True)
        t0 = time.time()
        query_emb_all = model.encode(
            [BGE_QUERY_INSTRUCTION + q for q in query_texts],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        ).astype(np.float32)
        np.save(query_cache, query_emb_all)
        print(f"    完成 ({time.time()-t0:.1f}s), shape={query_emb_all.shape}", flush=True)

    query_emb = query_emb_all[: len(query_texts)]

    # ---------- 4. BM25 ----------
    bm25 = None
    if any("hybrid" in m for m in modes):
        print("== 构建 BM25 索引 ==", flush=True)
        from rag.bm25 import BM25Retriever

        t0 = time.time()
        bm25 = BM25Retriever(actual_docs)
        print(f"    完成 ({time.time()-t0:.1f}s)", flush=True)

    # ---------- 5. reranker ----------
    reranker = None
    if use_rerank:
        print("== 加载 bge-reranker-base ==", flush=True)
        from rag.reranker import Reranker

        reranker = Reranker()
        print(f"    reranker available = {reranker.available}", flush=True)

    # ---------- 6. 逐模式检索 ----------
    def topk_by_scores(scores: np.ndarray, k: int) -> np.ndarray:
        idx = np.argpartition(-scores, kth=min(k - 1, scores.shape[1] - 1), axis=1)[:, :k]
        rows = np.arange(scores.shape[0])[:, None]
        topk_scores = scores[rows, idx]
        order = np.argsort(-topk_scores, axis=1)
        return idx[rows, order]

    results: dict[str, dict] = {}

    def run_mode(name: str) -> None:
        print(f"\n== 评测模式: {name} ==", flush=True)
        t0 = time.time()
        ranked_lists: list[list[int]] = []

        if name == "vector":
            top_vec = topk_by_scores(query_emb @ corpus_emb.T, RECALL_N)
            ranked_lists = [row.tolist() for row in top_vec]

        elif name == "hybrid":
            top_vec = topk_by_scores(query_emb @ corpus_emb.T, RECALL_N)
            for i, q in enumerate(query_texts):
                vec = list(top_vec[i])
                bm = bm25.search(q, k=RECALL_N)
                rrf: dict[int, float] = {}
                for rank, idx in enumerate(vec):
                    rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
                for rank, (idx, _s) in enumerate(bm):
                    rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
                fused = sorted(rrf.items(), key=lambda x: -x[1])
                ranked_lists.append([idx for idx, _ in fused[:RECALL_N]])

        elif name == "hybrid_rerank":
            top_vec = topk_by_scores(query_emb @ corpus_emb.T, RECALL_N)
            for i, q in enumerate(query_texts):
                vec = list(top_vec[i])
                bm = bm25.search(q, k=RECALL_N)
                rrf: dict[int, float] = {}
                for rank, idx in enumerate(vec):
                    rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
                for rank, (idx, _s) in enumerate(bm):
                    rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
                fused = sorted(rrf.items(), key=lambda x: -x[1])
                cand = fused[: args.rerank_top]
                cand_idx = [idx for idx, _ in cand]
                cand_texts = [actual_docs[idx] for idx in cand_idx]
                reranked = reranker.rerank(q, cand_texts)
                text_to_idx = {t: i for i, t in zip(cand_idx, cand_texts)}
                ordered_idx = [text_to_idx[t] for t, _ in reranked]
                seen = set(ordered_idx)
                ordered_idx += [idx for idx, _ in fused if idx not in seen]
                ranked_lists.append(ordered_idx[:RECALL_N])

        m = compute_metrics(ranked_lists, gold_idx)
        m["elapsed_s"] = round(time.time() - t0, 1)
        results[name] = m
        print(f"    {json.dumps(m, ensure_ascii=False)}", flush=True)

    for mode in modes:
        run_mode(mode)

    # ---------- 7. 对比表 ----------
    print("\n" + "=" * 78, flush=True)
    print("自有场景检索质量对比（1300 EC* + 7 policy 真实语料）", flush=True)
    print("=" * 78, flush=True)
    header = (f"{'模式':<16}{'Recall@1':>9}{'Recall@5':>9}{'Recall@10':>10}"
              f"{'MRR@10':>9}{'nDCG@10':>9}{'耗时':>8}")
    print(header, flush=True)
    print("-" * 78, flush=True)
    name_map = {
        "vector": "纯向量",
        "hybrid": "混合(RRF)",
        "hybrid_rerank": "混合+重排",
    }
    for mode, m in results.items():
        label = name_map.get(mode, mode)
        print(f"{label:<16}{m['recall@1']:>9}{m['recall@5']:>9}{m['recall@10']:>10}"
              f"{m['mrr@10']:>9}{m['ndcg@10']:>9}{m['elapsed_s']:>7}s", flush=True)
    print("=" * 78, flush=True)

    # 与公开基准对比
    print("\n[对比] EcomRetrieval 100k corpus 标准基准（已有数据）：", flush=True)
    print("    纯向量       Recall@10=0.7500  MRR@10=0.5210  nDCG@10=0.5760", flush=True)
    print("    混合+重排    Recall@10=0.7380  MRR@10=0.5200  nDCG@10=???", flush=True)

    out_path = DATA_DIR / "eval_results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}", flush=True)


if __name__ == "__main__":
    main()