"""
EcomRetrieval 检索质量评测脚本
================================
在标准中文电商段落检索基准 C-MTEB/EcomRetrieval 上，
对比四种检索模式的 Recall@k / MRR@10 / nDCG@10：

  - vector        : 纯 BGE 向量检索（语义）
  - bm25          : 纯 BM25 关键词检索（稀疏）
  - hybrid        : 向量 + BM25，RRF 融合（混合检索）
  - hybrid_rerank : 混合检索 + bge-reranker 交叉编码器重排

复用本项目 rag 包的同一套组件（BGE-small-zh / BM25 / reranker / RRF k=60），
证明「混合检索 vs 单路」「重排 vs 不重排」的差异，产出可写进简历的硬指标。

用法：
  python eval_retrieval.py                     # 全量：1000 query × 100902 corpus
  python eval_retrieval.py --limit-queries 30  # smoke：只测前 30 条 query
  python eval_retrieval.py --modes vector,bm25,hybrid,hybrid_rerank
  python eval_retrieval.py --no-cache          # 强制重新 embedding

数据集（已下载到 data/eval/ecom_retrieval/）：
  corpus.parquet   : 100902 条电商商品/段落文本（id, text）
  queries.parquet  : 1000 条查询（id, text）
  qrels.parquet    : 1000 条相关性标注（query-id, corpus-id, score=1），每 query 恰好 1 个相关 doc
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

# 与 rag/pipeline.py 保持一致
RRF_K = 60
RECALL_N = 20   # 每路召回宽度
RERANK_N = 20   # 重排候选宽度

# 本地数据目录与缓存目录
DATA_DIR = Path(__file__).resolve().parent / "data" / "eval" / "ecom_retrieval"
CACHE_DIR = DATA_DIR / "cache"


def load_data(data_dir: Path) -> tuple[list, list, dict]:
    """读取 corpus / queries / qrels，返回 (corpus, queries, qrels_map)

    corpus:  list[str]                        文本（下标即 doc idx）
    queries: list[tuple[str, str]]            [(query_id, query_text), ...]
    qrels_map: dict[query_id -> corpus_id]    每个 query 的正确答案（唯一相关 doc）
    """
    corpus_df = pq.read_table(data_dir / "corpus.parquet").to_pandas()
    queries_df = pq.read_table(data_dir / "queries.parquet").to_pandas()
    qrels_df = pq.read_table(data_dir / "qrels.parquet").to_pandas()

    corpus = corpus_df["text"].astype(str).tolist()
    queries = list(zip(queries_df["id"].astype(str), queries_df["text"].astype(str)))

    qrels_map = {
        str(row["query-id"]): str(row["corpus-id"])
        for _, row in qrels_df.iterrows()
    }
    return corpus, queries, qrels_map


def encode_batch(model, texts: list[str], prefix: str | None = None,
                 batch_size: int = 256, label: str = "embed") -> np.ndarray:
    """批量向量化（normalize），带简单进度打印。prefix 用于 BGE query 指令。"""
    out: list[np.ndarray] = []
    n = len(texts)
    t0 = time.time()
    for i in range(0, n, batch_size):
        chunk = texts[i : i + batch_size]
        if prefix:
            chunk = [prefix + t for t in chunk]
        emb = model.encode(chunk, normalize_embeddings=True,
                           show_progress_bar=False, convert_to_numpy=True)
        out.append(emb.astype(np.float32))
        done = min(i + batch_size, n)
        if done % 20000 < batch_size or done == n:
            print(f"    [{label}] {done}/{n}  ({time.time()-t0:.1f}s)", flush=True)
    return np.vstack(out)


def topk_by_scores(scores: np.ndarray, k: int) -> np.ndarray:
    """scores: (num_query, num_corpus)，返回每行 top-k 的列下标，按分数降序。"""
    idx = np.argpartition(-scores, kth=k - 1, axis=1)[:, :k]
    rows = np.arange(scores.shape[0])[:, None]
    topk_scores = scores[rows, idx]
    order = np.argsort(-topk_scores, axis=1)
    return idx[rows, order]


def compute_metrics(ranked_indices: list[list[int]], gold_idx: list[int],
                    top_ks: list[int] = (1, 5, 10)) -> dict:
    """计算检索指标。每个 query 恰好 1 个相关 doc。"""
    m = len(ranked_indices)
    hits = {k: 0 for k in top_ks}
    rr_sum = 0.0
    ndcg_sum = 0.0
    found = 0

    for ranked, g in zip(ranked_indices, gold_idx):
        try:
            rank = ranked.index(g) + 1  # 1-based
        except ValueError:
            rank = 10**9  # 未召回
        if rank <= 10:
            found += 1
            rr_sum += 1.0 / rank
            ndcg_sum += 1.0 / math.log2(rank + 1)  # 单相关 doc，IDCG=1
        for k in top_ks:
            if rank <= k:
                hits[k] += 1

    return {
        **{f"recall@{k}": round(hits[k] / m, 4) for k in top_ks},
        "mrr@10": round(rr_sum / m, 4),
        "ndcg@10": round(ndcg_sum / m, 4),
        "hit@10_rate": round(found / m, 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-queries", type=int, default=0,
                    help="只测前 N 条 query（0=全部），用于 smoke test")
    ap.add_argument("--modes", type=str, default="vector,bm25,hybrid,hybrid_rerank",
                    help="逗号分隔：vector / bm25 / hybrid / hybrid_rerank")
    ap.add_argument("--no-cache", action="store_true", help="忽略缓存，重新 embedding")
    ap.add_argument("--rerank-top", type=int, default=RERANK_N,
                    help="重排候选宽度")
    args = ap.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    use_rerank = "hybrid_rerank" in modes

    # ---------- 1. 加载数据 ----------
    print("== 加载 EcomRetrieval 数据集 ==", flush=True)
    corpus, queries, qrels_map = load_data(DATA_DIR)
    n_corpus = len(corpus)
    n_queries = len(queries)
    print(f"    corpus={n_corpus}  queries={n_queries}  qrels={len(qrels_map)}", flush=True)

    # corpus id -> 下标
    corpus_ids = pq.read_table(DATA_DIR / "corpus.parquet").to_pandas()["id"].astype(str).tolist()
    corpus_id_to_idx = {cid: i for i, cid in enumerate(corpus_ids)}

    # 全量 gold + query text（先不截断，query embedding 始终全量缓存）
    gold_idx: list[int] = []
    query_texts: list[str] = []
    for qid, qtext in queries:
        gold_cid = qrels_map.get(qid)
        gold_idx.append(corpus_id_to_idx.get(gold_cid, -1))
        query_texts.append(qtext)

    # ---------- 2. 加载 BGE 模型（复用 rag 的本地模型定位）----------
    print("== 加载 BGE 模型 ==", flush=True)
    from rag.embedding import Embedder, BGE_QUERY_INSTRUCTION
    from sentence_transformers import SentenceTransformer

    embedder = Embedder()
    model_path = embedder._resolve_model()
    print(f"    模型路径: {model_path}", flush=True)
    model = SentenceTransformer(model_path, device="cpu")

    # ---------- 3. Embedding（带缓存）----------
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    corpus_cache = CACHE_DIR / "ecom_corpus_emb.npy"
    query_cache = CACHE_DIR / "ecom_query_emb.npy"

    if corpus_cache.exists() and not args.no_cache:
        print("== 命中 corpus embedding 缓存 ==", flush=True)
        corpus_emb = np.load(corpus_cache)
    else:
        print(f"== 向量化 corpus（{n_corpus} 条，CPU 需数分钟）==", flush=True)
        corpus_emb = encode_batch(model, corpus, prefix=None, label="corpus")
        np.save(corpus_cache, corpus_emb)

    if query_cache.exists() and not args.no_cache:
        print("== 命中 query embedding 缓存 ==", flush=True)
        query_emb_all = np.load(query_cache)
    else:
        print(f"== 向量化 queries（{n_queries} 条）==", flush=True)
        query_emb_all = encode_batch(model, query_texts,
                                     prefix=BGE_QUERY_INSTRUCTION, label="query")
        np.save(query_cache, query_emb_all)

    # 截断（smoke test 用）；query cache 存的一直是全量，故全量跑也正确
    if args.limit_queries > 0:
        query_texts = query_texts[: args.limit_queries]
        gold_idx = gold_idx[: args.limit_queries]
        print(f"    [smoke] 只测前 {args.limit_queries} 条 query", flush=True)

    query_emb = query_emb_all[: len(query_texts)]
    print(f"    corpus_emb {corpus_emb.shape}  query_emb {query_emb.shape}", flush=True)

    # ---------- 4. 构建 BM25 索引（bm25 / hybrid 模式需要）----------
    bm25 = None
    if any(("hybrid" in m or m == "bm25") for m in modes):
        print("== 构建 BM25 索引（jieba 分词 + BM25Okapi）==", flush=True)
        from rag.bm25 import BM25Retriever
        t0 = time.time()
        bm25 = BM25Retriever(corpus)
        print(f"    BM25 索引完成 ({time.time()-t0:.1f}s)", flush=True)

    # ---------- 5. 加载 reranker（重排用）----------
    reranker = None
    if use_rerank:
        print("== 加载 bge-reranker-base ==", flush=True)
        from rag.reranker import Reranker
        reranker = Reranker()
        print(f"    reranker available = {reranker.available}", flush=True)

    # ---------- 6. 逐模式检索 ----------
    results: dict[str, dict] = {}

    def run_mode(name: str) -> None:
        print(f"\n== 评测模式: {name} ==", flush=True)
        ranked_lists: list[list[int]] = []
        t0 = time.time()

        if name == "vector":
            top_vec = topk_by_scores(query_emb @ corpus_emb.T, RECALL_N)
            ranked_lists = [row.tolist() for row in top_vec]

        elif name == "bm25":
            for i, q in enumerate(query_texts):
                bm = bm25.search(q, k=RECALL_N)
                ranked_lists.append([idx for idx, _ in bm])

        else:
            # hybrid / hybrid_rerank：向量路 + BM25 路，RRF 融合
            top_vec = topk_by_scores(query_emb @ corpus_emb.T, RECALL_N)
            for i, q in enumerate(query_texts):
                vec = list(top_vec[i])
                bm = bm25.search(q, k=RECALL_N)  # [(idx, score), ...]

                rrf: dict[int, float] = {}
                for rank, idx in enumerate(vec):
                    rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
                for rank, (idx, _s) in enumerate(bm):
                    rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
                fused = sorted(rrf.items(), key=lambda x: -x[1])

                if name == "hybrid":
                    ranked_lists.append([idx for idx, _ in fused[:RECALL_N]])
                else:  # hybrid_rerank
                    cand = fused[: args.rerank_top]
                    cand_idx = [idx for idx, _ in cand]
                    cand_texts = [corpus[idx] for idx in cand_idx]
                    reranked = reranker.rerank(q, cand_texts)  # [(text, score), ...]
                    text_to_idx = {t: i for i, t in zip(cand_idx, cand_texts)}
                    ordered_idx = [text_to_idx[t] for t, _ in reranked]
                    seen = set(ordered_idx)
                    ordered_idx += [idx for idx, _ in fused if idx not in seen]
                    ranked_lists.append(ordered_idx[:RECALL_N])

        metrics = compute_metrics(ranked_lists, gold_idx)
        metrics["query_count"] = len(query_texts)
        metrics["elapsed_s"] = round(time.time() - t0, 1)
        results[name] = metrics
        print(f"    {json.dumps(metrics, ensure_ascii=False)}", flush=True)

    for mode in modes:
        run_mode(mode)

    # ---------- 7. 输出对比表 ----------
    print("\n" + "=" * 74, flush=True)
    print("EcomRetrieval 检索质量对比（每 query 1 个相关 doc）", flush=True)
    print("=" * 74, flush=True)
    header = (f"{'模式':<16}{'Recall@1':>9}{'Recall@5':>9}{'Recall@10':>10}"
              f"{'MRR@10':>9}{'nDCG@10':>9}")
    print(header, flush=True)
    print("-" * 74, flush=True)
    name_map = {
        "vector": "纯向量",
        "bm25": "纯BM25",
        "hybrid": "混合(RRF)",
        "hybrid_rerank": "混合+重排",
    }
    for mode, m in results.items():
        label = name_map.get(mode, mode)
        print(f"{label:<16}{m['recall@1']:>9}{m['recall@5']:>9}{m['recall@10']:>10}"
              f"{m['mrr@10']:>9}{m['ndcg@10']:>9}", flush=True)
    print("=" * 74, flush=True)

    out_path = DATA_DIR / "eval_results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}", flush=True)


if __name__ == "__main__":
    main()
