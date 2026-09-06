"""
RAG 检索管线 — 混合检索 + RRF 融合 + Re-ranking

检索流程（体现「检索优化」的核心设计）：
    1. 向量检索（语义）  → top M
    2. BM25 检索（关键词）→ top M
    3. RRF(Reciprocal Rank Fusion) 融合两者排序，兼顾语义与精确匹配
    4. Cross-Encoder 重排，进一步精排
    5. 返回 top k（含来源元数据，支持引用溯源）
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .bm25 import BM25Retriever
from .embedding import Embedder
from .reranker import Reranker
from .vector_store import VectorStore

# RRF 常数，业界常用 60
RRF_K = 60
# 混合检索的召回宽度与重排宽度
RECALL_N = 20
RERANK_N = 8


class RAGPipeline:
    """一站式 RAG 检索管线：index() 建索引，search() 混合检索"""

    def __init__(
        self,
        persist_dir: str,
        collection_name: str = "ecommerce_knowledge",
        embedder: Optional[Embedder] = None,
        reranker: Optional[Reranker] = None,
    ) -> None:
        self.embedder = embedder or Embedder()
        self.reranker = reranker or Reranker()
        self.vector_store = VectorStore(persist_dir, self.embedder, collection_name)
        self._texts: list[str] = []
        self._metas: list[dict] = []
        self._bm25: Optional[BM25Retriever] = None

    # ---------- 索引 ----------

    def index(self, chunks: list[dict], reset: bool = False) -> int:
        """
        建立索引。
        chunks: [{"text": str, "metadata": dict}, ...]
        reset=True 时清空重建向量库。
        """
        if reset:
            self.vector_store.reset()

        self._texts = [c["text"] for c in chunks]
        self._metas = []
        ids: list[str] = []
        for i, c in enumerate(chunks):
            meta = dict(c.get("metadata") or {})
            meta["_idx"] = i  # 注入下标，便于 RRF 融合时对齐
            self._metas.append(meta)
            ids.append(f"c{i}")

        self.vector_store.upsert(ids=ids, documents=self._texts, metadatas=self._metas)
        # 记录当前 embedding backend，供下次加载时校验一致性
        self.vector_store.set_metadata({"embedding_backend": self.embedder.backend})
        self._bm25 = BM25Retriever(self._texts)
        return len(self._texts)

    # ---------- 检索 ----------

    def search(
        self,
        query: str,
        k: int = 5,
        use_hybrid: bool = True,
        use_rerank: bool = True,
    ) -> list[dict]:
        """
        混合检索，返回 [{text, metadata, score}, ...]
        score 为融合/重排后的相关度（越大越相关）。
        """
        if not self._texts or not self._bm25:
            return []

        if not use_hybrid:
            return self._vector_only(query, k)

        # 1. 向量检索 + BM25 检索（各召回 RECALL_N 条）
        vec_results = self.vector_store.query(query, k=RECALL_N)
        bm25_results = self._bm25.search(query, k=RECALL_N)

        # 2. RRF 融合
        fused = self._rrf_fuse(vec_results, bm25_results)
        if not fused:
            return []

        # 3. 取融合后的候选文本，交给重排器精排
        candidates = [(idx, self._texts[idx], self._metas[idx]) for idx, _ in fused[:RERANK_N]]
        if use_rerank and self.reranker.available:
            ranked = self.reranker.rerank(query, [c[1] for c in candidates])
            # 重排结果按文本对齐回原元数据
            score_map = {text: score for text, score in ranked}
            ordered = sorted(
                candidates, key=lambda c: score_map.get(c[1], 0.0), reverse=True
            )
            return [
                {"text": t, "metadata": m, "score": round(score_map.get(t, 0.0), 4)}
                for _, t, m in ordered[:k]
            ]

        # 不重排：直接按 RRF 分数返回
        return [
            {"text": self._texts[idx], "metadata": self._metas[idx], "score": round(s, 4)}
            for idx, s in fused[:k]
        ]

    def _vector_only(self, query: str, k: int) -> list[dict]:
        vec_results = self.vector_store.query(query, k=k)
        return [
            {
                "text": r["doc"],
                "metadata": r["metadata"],
                "score": round(1.0 - r["distance"], 4),  # cosine 距离转相似度
            }
            for r in vec_results
        ]

    def _rrf_fuse(self, vec_results: list[dict], bm25_results: list[tuple[int, float]]) -> list[tuple[int, float]]:
        """
        RRF 融合：score(d) = Σ 1/(K + rank)
        vec_results 已按 distance 升序（最相关在前），bm25_results 已按分数降序。
        返回 [(chunk_idx, rrf_score), ...] 按分数降序。
        """
        rrf: dict[int, float] = {}
        for rank, r in enumerate(vec_results):
            idx = (r["metadata"] or {}).get("_idx")
            if idx is not None:
                rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        for rank, (idx, _score) in enumerate(bm25_results):
            rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        return sorted(rrf.items(), key=lambda x: -x[1])

    # ---------- 元信息 ----------

    @property
    def size(self) -> int:
        return len(self._texts)

    def describe(self) -> dict:
        """返回管线状态，便于诊断与展示"""
        return {
            "collection": self.vector_store.collection_name,
            "persist_dir": self.vector_store.persist_dir,
            "chunks": self.size,
            "embedding_backend": self.embedder.backend,
            "reranker": self.reranker.available,
        }
