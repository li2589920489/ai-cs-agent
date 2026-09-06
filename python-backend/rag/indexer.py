"""
索引构建器 — 从业务数据源构建 RAG 索引

数据源：
    1. 商品知识库（SQLite，knowledge_store）→ 商品主信息 + FAQ 两条线
    2. 店铺政策（demo_data.STORE_POLICIES）→ 每条政策一个 chunk（长内容再分块）

chunk 携带 metadata，用于检索结果溯源（回答「这条信息来自哪个商品/政策」）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .chunking import split_text
from .pipeline import RAGPipeline

DEFAULT_PERSIST_DIR = Path(__file__).resolve().parent.parent / "data" / "chroma_rag"
DEFAULT_COLLECTION = "ecommerce_knowledge"


def _chunk_id(text: str) -> str:
    """基于内容哈希生成稳定 chunk id，便于增量更新"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def build_chunks() -> list[dict]:
    """从业务数据源组装待索引的 chunk 列表"""
    import knowledge_store
    from ecommerce.demo_data import STORE_POLICIES

    chunks: list[dict] = []

    # 1. 商品知识
    for item in knowledge_store.list_knowledge():
        name = item.get("name", "")
        pid = item.get("product_id", "")
        base_meta = {"type": "product", "product_id": pid, "name": name}

        # 商品主信息 chunk
        main_text = (
            f"商品：{name}（{pid}）\n"
            f"介绍：{item.get('description', '')}\n"
            f"卖点：{'；'.join(item.get('selling_points', []))}\n"
            f"规格：{'；'.join(item.get('specs', []))}"
        )
        chunks.append({"text": main_text, "metadata": dict(base_meta, field="main")})

        # FAQ 每条独立成 chunk，提升问答精确召回
        for faq in item.get("faq", []):
            if faq.strip():
                chunks.append(
                    {
                        "text": f"【{name}】常见问题：{faq.strip()}",
                        "metadata": dict(base_meta, field="faq"),
                    }
                )

    # 2. 店铺政策（长文本先分块）
    for policy_name, content in STORE_POLICIES.items():
        meta = {"type": "policy", "policy_name": policy_name}
        pieces = split_text(content, chunk_size=300, chunk_overlap=40) or [content]
        for piece in pieces:
            chunks.append({"text": f"【{policy_name}】\n{piece.strip()}", "metadata": meta})

    return chunks


def build_index(
    persist_dir: str = str(DEFAULT_PERSIST_DIR),
    collection_name: str = DEFAULT_COLLECTION,
    reset: bool = True,
) -> RAGPipeline:
    """构建完整索引并返回管线实例"""
    chunks = build_chunks()
    pipeline = RAGPipeline(persist_dir=persist_dir, collection_name=collection_name)
    n = pipeline.index(chunks, reset=reset)
    print(f"[RAG] 索引构建完成：{n} 个 chunk → {pipeline.describe()}")
    return pipeline


def load_pipeline(
    persist_dir: str = str(DEFAULT_PERSIST_DIR),
    collection_name: str = DEFAULT_COLLECTION,
) -> RAGPipeline:
    """加载已建好的索引（若为空或 embedding backend 变化则自动重建）"""
    pipeline = RAGPipeline(persist_dir=persist_dir, collection_name=collection_name)
    current_backend = pipeline.embedder.backend

    if pipeline.vector_store.count() > 0:
        stored_backend = pipeline.vector_store.get_metadata().get("embedding_backend")
        if stored_backend == current_backend:
            # 复用现有索引，重建内存侧的 BM25 与文本表
            docs = pipeline.vector_store._collection().get(include=["documents", "metadatas"])
            texts = docs.get("documents") or []
            metas = docs.get("metadatas") or []
            pipeline._texts = texts
            pipeline._metas = [dict(m) if m else {} for m in metas]
            from .bm25 import BM25Retriever

            pipeline._bm25 = BM25Retriever(texts)
            return pipeline
        print(f"[RAG] embedding backend 变更（{stored_backend} → {current_backend}），重建索引")

    return build_index(persist_dir=persist_dir, collection_name=collection_name, reset=True)


if __name__ == "__main__":
    # 独立运行：python -m rag.indexer
    p = build_index()
    for r in p.search("退货政策是什么", k=3):
        print("=" * 60)
        print(f"[{r['metadata'].get('type')}] {r['text'][:120]}")
        print(f"score={r['score']}")
