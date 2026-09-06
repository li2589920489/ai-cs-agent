"""
Embedding 向量化模块 — 可插拔设计

主方案：BAAI/bge-small-zh-v1.5（中文向量模型，本地 CPU 可跑，约 100MB）
降级方案：ChromaDB 默认 ONNX MiniLM（无额外依赖，但英文模型中文效果弱）

BGE 最佳实践：query 与 document 使用不同的 embedding 方式——
query 需加指令前缀「为这个句子生成表示以用于检索相关文章：」，
document 则不加前缀，以拉开语义空间。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

# BGE 系列模型推荐的 query 指令前缀
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


class Embedder:
    """向量化器：优先本地 BGE 中文模型，失败自动降级到 Chroma 默认 embedding"""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        device: str = "cpu",
        query_instruction: Optional[str] = BGE_QUERY_INSTRUCTION,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.query_instruction = query_instruction
        self._model = None
        self._fallback = None
        self._use_bge = False
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
        """懒加载：只加载一次，线程安全"""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self._resolve_model(), device=self.device)
                self._use_bge = True
            except Exception as exc:  # noqa: BLE001
                print(f"[RAG] 本地 BGE 模型加载失败（{exc}），降级为 Chroma 默认 embedding")
                try:
                    from chromadb.utils import embedding_functions

                    self._fallback = embedding_functions.DefaultEmbeddingFunction()
                except Exception as exc2:  # noqa: BLE001
                    raise RuntimeError(
                        "无法初始化 Embedding：请安装 sentence-transformers "
                        f"或确保 chromadb 可用。原始错误：{exc} | {exc2}"
                    ) from exc
            self._loaded = True

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """向量化文档（不加指令前缀）"""
        self._load()
        if not texts:
            return []
        if self._use_bge:
            return [self._model.encode(t, normalize_embeddings=True).tolist() for t in texts]
        return self._fallback(texts)

    def embed_query(self, text: str) -> list[float]:
        """向量化查询（BGE 加指令前缀）"""
        self._load()
        if self._use_bge:
            q = f"{self.query_instruction}{text}" if self.query_instruction else text
            return self._model.encode(q, normalize_embeddings=True).tolist()
        return self._fallback([text])[0]

    @property
    def backend(self) -> str:
        """返回当前使用的 embedding 后端，便于日志/诊断"""
        self._load()
        return "bge-local" if self._use_bge else "chroma-default"
