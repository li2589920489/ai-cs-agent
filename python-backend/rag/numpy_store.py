"""
纯 numpy 后端向量存储 — 替代 chromadb，规避其在某些平台（Windows / 部分 Linux）
上 hnsw segment reader 崩溃的问题。

接口与 ChromaDB 版 VectorStore 完全兼容，便于上层 Pipeline 无感切换。

存储格式（独立 sqlite，不依赖 chromadb schema）：
  chunks(
    id           TEXT PRIMARY KEY,   -- chunk id (e.g. "c0")
    doc          TEXT NOT NULL,      -- 原始文档文本
    metadata_json TEXT NOT NULL,     -- 元数据 (JSON 序列化)
    embedding    BLOB NOT NULL       -- float32 向量 (tobytes)
  )
  collection_meta(key TEXT PRIMARY KEY, value TEXT)  -- 后端一致性校验

检索：embedding 已归一化（BGE normalize=True），用矩阵乘法算 cosine 相似度。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

from .embedding import Embedder


class NumpyVectorStore:
    """纯 numpy 后端向量库（sqlite 持久化 + 内存 cosine 检索）"""

    def __init__(
        self,
        persist_dir: str,
        embedder: Optional[Embedder] = None,
        collection_name: str = "ecommerce_knowledge",
    ) -> None:
        self.persist_dir = str(persist_dir)
        self.embedder = embedder or Embedder()
        self.collection_name = collection_name
        self.db_path = Path(persist_dir) / "numpy_store.db"

        # 内存态：启动时从 sqlite 加载
        self._ids: list[str] = []
        self._docs: list[str] = []
        self._metas: list[dict] = []
        self._embs: Optional[np.ndarray] = None  # (N, dim)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._load_to_memory()

    # ---------- DB 初始化 / 加载 ----------

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id            TEXT PRIMARY KEY,
                    doc           TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    embedding     BLOB NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS collection_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.commit()

    def _load_to_memory(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, doc, metadata_json, embedding FROM chunks ORDER BY rowid"
            ).fetchall()
        if not rows:
            return
        self._ids = [r[0] for r in rows]
        self._docs = [r[1] for r in rows]
        self._metas = [json.loads(r[2]) for r in rows]
        self._embs = np.stack(
            [np.frombuffer(r[3], dtype=np.float32) for r in rows]
        )

    # ---------- 元数据 / 集合操作 ----------

    def count(self) -> int:
        return len(self._ids)

    def reset(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM chunks")
            conn.commit()
        self._ids = []
        self._docs = []
        self._metas = []
        self._embs = None

    def get_metadata(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT key, value FROM collection_meta").fetchall()
        return dict(rows)

    def set_metadata(self, metadata: dict) -> None:
        with sqlite3.connect(self.db_path) as conn:
            for k, v in metadata.items():
                conn.execute(
                    "INSERT OR REPLACE INTO collection_meta(key, value) VALUES (?, ?)",
                    (k, str(v)),
                )
            conn.commit()

    # ---------- 写入 ----------

    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        if not ids:
            return
        # 一次性向量化
        embeddings_list = self.embedder.embed_documents(documents)
        emb_arr = np.asarray(embeddings_list, dtype=np.float32)

        # 写入 sqlite
        with sqlite3.connect(self.db_path) as conn:
            for i, id_ in enumerate(ids):
                conn.execute(
                    "INSERT OR REPLACE INTO chunks(id, doc, metadata_json, embedding) VALUES (?, ?, ?, ?)",
                    (
                        id_,
                        documents[i],
                        json.dumps(metadatas[i], ensure_ascii=False),
                        emb_arr[i].tobytes(),
                    ),
                )
            conn.commit()
        # 刷新内存态
        self._load_to_memory()

    # ---------- 检索 ----------

    def query(self, query_text: str, k: int = 10) -> list[dict]:
        """向量相似度检索（cosine），返回 [{id, doc, metadata, distance}, ...]"""
        if self.count() == 0 or self._embs is None:
            return []
        q_emb = np.asarray(self.embedder.embed_query(query_text), dtype=np.float32)
        # BGE 已 normalize=True，dot product 等价 cosine 相似度
        sims = self._embs @ q_emb  # (N,)
        top_idx = np.argsort(-sims)[: min(k, len(sims))]
        return [
            {
                "id": self._ids[i],
                "doc": self._docs[i],
                "metadata": self._metas[i],
                "distance": float(1.0 - sims[i]),  # cosine distance
            }
            for i in top_idx
        ]