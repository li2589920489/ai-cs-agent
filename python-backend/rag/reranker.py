"""
Re-ranking 重排模块 — 检索结果精排

主方案：BAAI/bge-reranker-base 交叉编码器（Cross-Encoder），
        对 query-doc 逐对打分，比双塔 embedding 更精准。
降级方案：不重排（直接沿用混合检索顺序），保证链路可用。
"""
from __future__ import annotations

import threading
from pathlib import Path

DEFAULT_RERANKER = "BAAI/bge-reranker-base"


class Reranker:
    """交叉编码器重排器：对候选文档重新精排"""

    def __init__(self, model_name: str = DEFAULT_RERANKER, max_length: int = 512) -> None:
        self.model_name = model_name
        self.max_length = max_length
        self._model = None
        self._available = False
        self._loaded = False
        self._lock = threading.Lock()

    def _resolve_model(self) -> str:
        """优先用本地已下载模型（data/models/），否则用 HF 模型名"""
        local = (
            Path(__file__).resolve().parent.parent
            / "data" / "models" / self.model_name.split("/")[-1]
        )
        if (local / "config.json").exists():
            return str(local)
        return self.model_name

    def _load(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(self._resolve_model(), max_length=self.max_length)
                self._available = True
            except Exception as exc:  # noqa: BLE001
                print(f"[RAG] 重排模型加载失败（{exc}），降级为不重排")
                self._available = False
            self._loaded = True

    @property
    def available(self) -> bool:
        self._load()
        return self._available

    def rerank(self, query: str, documents: list[str]) -> list[tuple[str, float]]:
        """
        对候选文档重排，返回 [(文档文本, 分数), ...] 按分数降序。
        降级时返回原顺序、分数为 0。
        """
        if not documents:
            return []
        self._load()
        if not self._available:
            return [(d, 0.0) for d in documents]

        pairs = [[query, d] for d in documents]
        scores = self._model.predict(pairs)
        return sorted(zip(documents, scores), key=lambda x: -x[1])
