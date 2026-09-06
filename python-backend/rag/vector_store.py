"""
向量库模块 — ChromaDB 封装

职责：向量的持久化存储 + 向量相似度检索（cosine）。
embedding 由上层 Embedder 提供，做到「存储」与「向量化」解耦。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .embedding import Embedder


class VectorStore:
    """ChromaDB 向量库封装"""

    def __init__(
        self,
        persist_dir: str,
        embedder: Optional[Embedder] = None,
        collection_name: str = "ecommerce_knowledge",
    ) -> None:
        self.persist_dir = str(persist_dir)
        self.embedder = embedder or Embedder()
        self.collection_name = collection_name
        self._client = None

    def _get_client(self):
        if self._client is None:
            import chromadb

            Path(self.persist_dir).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self.persist_dir)
        return self._client

    def _collection(self):
        client = self._get_client()
        return client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def reset(self) -> None:
        """清空重建集合（重新索引时使用）"""
        client = self._get_client()
        try:
            client.delete_collection(self.collection_name)
        except Exception:  # noqa: BLE001
            pass
        self._client = None

    def count(self) -> int:
        return self._collection().count()

    def get_metadata(self) -> dict:
        """读取 collection 元数据（含 embedding backend 等）"""
        return self._collection().metadata or {}

    def set_metadata(self, metadata: dict) -> None:
        """写入 collection 元数据，用于 embedding backend 一致性校验"""
        self._collection().modify(metadata=metadata)

    def upsert(self, ids: list[str], documents: list[str], metadatas: list[dict]) -> None:
        """向量化并写入（若有 embedding 则直接复用，避免重复计算）"""
        if not ids:
            return
        embeddings = self.embedder.embed_documents(documents)
        self._collection().upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )

    def query(self, query_text: str, k: int = 10) -> list[dict]:
        """
        向量相似度检索，返回 [{id, doc, metadata, distance}, ...]
        distance 为 cosine 距离（越小越相关）。
        """
        if self.count() == 0:
            return []
        q_emb = self.embedder.embed_query(query_text)
        res = self._collection().query(query_embeddings=[q_emb], n_results=min(k, self.count()))

        out: list[dict] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, doc in enumerate(docs):
            out.append(
                {
                    "id": ids[i] if i < len(ids) else None,
                    "doc": doc,
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": dists[i] if i < len(dists) else 1.0,
                }
            )
        return out
