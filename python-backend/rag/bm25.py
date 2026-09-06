"""
BM25 关键词检索模块 — 稀疏检索，与向量检索互补

主方案：rank_bm25 的 BM25Okapi + jieba 中文分词
降级方案：内置字符 bigram 的简单 TF 打分（不依赖任何库）
"""
from __future__ import annotations


class BM25Retriever:
    """BM25 关键词检索器，与向量检索一起构成混合检索的稀疏侧"""

    def __init__(self, documents: list[str]) -> None:
        self.documents = list(documents)
        self._scores_index = None
        self._use_bm25 = False
        self._build()

    def _tokenize(self, text: str) -> list[str]:
        try:
            import jieba

            return [t for t in jieba.cut(text) if t.strip()]
        except Exception:  # noqa: BLE001
            # 降级：字符 bigram
            t = text.strip()
            return [t[i : i + 2] for i in range(len(t) - 1)] or [t]

    def _build(self) -> None:
        tokenized = [self._tokenize(d) for d in self.documents]
        try:
            from rank_bm25 import BM25Okapi

            self._bm25 = BM25Okapi(tokenized)
            self._use_bm25 = True
        except Exception:  # noqa: BLE001
            self._bm25 = None
            self._tokenized = tokenized

    def search(self, query: str, k: int = 10) -> list[tuple[int, float]]:
        """
        返回 [(文档下标, 归一化分数), ...]，按分数降序。
        归一化到 [0,1] 便于与向量分数做 RRF 融合前的对齐。
        """
        tokens = self._tokenize(query)
        if not tokens:
            return []

        if self._use_bm25:
            scores = self._bm25.get_scores(tokens)
        else:
            # 降级：重叠 token 计数打分
            scores = [sum(1 for t in tokens if t in doc) for doc in self._tokenized]

        if len(scores) == 0 or float(max(scores)) <= 0:
            return []

        max_s = float(max(scores))
        ranked = sorted(
            ((int(i), float(s) / max_s) for i, s in enumerate(scores) if float(s) > 0),
            key=lambda x: -x[1],
        )
        return ranked[:k]
