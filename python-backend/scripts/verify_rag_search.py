"""
真实检索验证 — 用 sqlite + Embedder + numpy 算 cosine，
绕开 chromadb 1.5.9 hnsw segment reader 损坏问题。

背景：
    chromadb 1.5.9 在 Windows 上 build_index 后，hnsw segment 文件未正确
    生成，导致 collection.count() / .get() / .query() 全部触发
    "Error loading hnsw index"。

    但 chromadb.sqlite3 里数据完整 (embedding_metadata 表含全部
    chroma:document + name + type + product_id 等)。

    本脚本直接读 sqlite + 重新 embed 算 cosine，验证 9 个真实 query 的
    top-3 召回效果。

用法:
    python scripts/verify_rag_search.py
"""
from __future__ import annotations

import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rag.embedding import Embedder  # noqa: E402

SQLITE_PATH = ROOT / "data" / "chroma_rag" / "chroma.sqlite3"

QUERIES = [
    ("良品铺子坚果大礼包 1200g", "demo P1001 应第一"),
    ("三只松鼠每日坚果 750g", "demo P1002 应第一"),
    ("维达抽纸 3层 120抽", "demo P1004 应第一"),
    ("小米加湿器 4L", "demo P1003 应第一"),
    ("坚果大礼包", "零食类真实商品 EC*"),
    ("电饭煲", "家电类真实商品 EC*"),
    ("抽纸卫生纸", "日用品类真实商品 EC*"),
    ("退货政策是什么", "policy chunk 应第一"),
    ("七天无理由退货运费", "policy chunk 应召回"),
]


def load_chunks_from_sqlite() -> tuple[list[str], list[dict]]:
    """从 chromadb sqlite 读所有 chunk (document + metadata)

    chromadb 1.5.9 sqlite schema:
      embeddings: id, segment_id, embedding_id, seq_id, created_at
      embedding_metadata: id, key, string_value, int_value, float_value, bool_value
    """
    conn = sqlite3.connect(str(SQLITE_PATH))
    rows = conn.execute(
        """
        SELECT e.embedding_id,
               MAX(CASE WHEN em.key='chroma:document' THEN em.string_value END) AS document,
               MAX(CASE WHEN em.key='name' THEN em.string_value END) AS name,
               MAX(CASE WHEN em.key='product_id' THEN em.string_value END) AS product_id,
               MAX(CASE WHEN em.key='policy_name' THEN em.string_value END) AS policy_name,
               MAX(CASE WHEN em.key='type' THEN em.string_value END) AS type
        FROM embeddings e
        JOIN embedding_metadata em ON e.id = em.id
        GROUP BY e.embedding_id
        """
    ).fetchall()
    conn.close()

    docs: list[str] = []
    metas_out: list[dict] = []
    for row in rows:
        _, document, name, product_id, policy_name, mtype = row
        docs.append(document or "")
        metas_out.append({
            "type": mtype,
            "name": name,
            "product_id": product_id,
            "policy_name": policy_name,
        })
    return docs, metas_out


def cosine_top_k(
    query_emb: np.ndarray,
    doc_embs: np.ndarray,
    k: int = 3,
) -> list[tuple[int, float]]:
    """cosine 相似度 top-k，返回 [(doc_idx, score), ...]"""
    q = query_emb / (np.linalg.norm(query_emb) + 1e-8)
    d = doc_embs / (np.linalg.norm(doc_embs, axis=1, keepdims=True) + 1e-8)
    sims = d @ q
    top_idx = np.argsort(-sims)[:k]
    return [(int(i), float(sims[i])) for i in top_idx]


def main():
    print("=" * 60)
    print("真实检索验证 — sqlite + Embedder + numpy")
    print("=" * 60)

    print("\n[Step 1] 从 chromadb.sqlite3 读所有 chunk ...")
    docs, metas = load_chunks_from_sqlite()
    print(f"    共 {len(docs)} chunks")
    type_cnt = Counter(m["type"] for m in metas)
    print("    type 分布:")
    for t, n in type_cnt.items():
        print(f"      {t}: {n}")

    print("\n[Step 2] 加载 Embedder (bge-small-zh) ...")
    embedder = Embedder()

    print("\n[Step 3] 算所有 chunks 的 embedding (一次性, ~30s) ...")
    t0 = time.time()
    doc_embs_list = embedder.embed_documents(docs)
    doc_embs = np.asarray(doc_embs_list, dtype=np.float32)
    elapsed = time.time() - t0
    print(f"    完成 shape={doc_embs.shape}, 耗时={elapsed:.1f}s")

    print("\n[Step 4] 算 9 个 query 的 embedding ...")
    query_texts = [q[0] for q in QUERIES]
    q_embs = []
    for q in query_texts:
        emb = embedder.embed_query(q)
        q_embs.append(np.asarray(emb, dtype=np.float32))

    print("\n[Step 5] numpy 算 cosine top-3:")
    for (query, expected), q_emb in zip(QUERIES, q_embs):
        top = cosine_top_k(q_emb, doc_embs, k=3)
        print(f"\n>>> {query}  (期望: {expected})")
        for rank, (idx, score) in enumerate(top, 1):
            m = metas[idx]
            label = m.get("name") or m.get("policy_name") or "?"
            doc_preview = (docs[idx] or "")[:80].replace("\n", " ")
            print(f"  [{rank}] [{m['type']}] score={score:.4f} | {label}")
            print(f"       {doc_preview}")

    print("\n" + "=" * 60)
    print("验证完成。注: chromadb 1.5.9 hnsw 读端损坏，本脚本绕开 chromadb，" + "直接用 bge embedder + numpy 算 cosine 验证检索逻辑。")
    print("=" * 60)


if __name__ == "__main__":
    main()