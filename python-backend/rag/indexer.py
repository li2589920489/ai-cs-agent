"""
索引构建器 — 从业务数据源构建 RAG 索引

数据源：
    1. 商品知识库（SQLite，knowledge_store）→ 商品主信息 + FAQ 两条线
    2. 店铺政策（demo_data.STORE_POLICIES）→ 每条政策一个 chunk（长内容再分块）

chunk 携带 metadata，用于检索结果溯源（回答「这条信息来自哪个商品/政策」）。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .chunking import split_text
from .pipeline import RAGPipeline

# 持久化目录支持环境变量覆盖。pytest 的 conftest.py 借此把索引指向临时目录——
# 否则测试会用「只有 5 行 demo 的临时知识库」去覆盖开发用的 data/chroma_rag/，
# 把 1328 chunk 的索引打回 28 个（知识库指纹不匹配会触发全量重建）。
DEFAULT_PERSIST_DIR = Path(
    os.getenv("RAG_PERSIST_DIR")
    or (Path(__file__).resolve().parent.parent / "data" / "chroma_rag")
)
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
        category = (item.get("category") or "").strip()
        base_meta = {"type": "product", "product_id": pid, "name": name, "category": category}

        # 商品主信息 chunk —— 逐字段判空/去重后再拼装。
        # 真实导入商品存在「字段缺失」的常态场景（description 直接复用标题、specs/faq 无来源留空），
        # 若原样拼接，同一标题会在 chunk 内重复三遍：既稀释语义向量，也让召回结果没有信息量。
        desc = (item.get("description") or "").strip()
        selling_points = [s.strip() for s in item.get("selling_points", []) if s.strip()]
        specs = [s.strip() for s in item.get("specs", []) if s.strip()]

        parts = [f"商品：{name}（{pid}）"]
        if desc and desc != name:
            parts.append(f"介绍：{desc}")
        if category:
            parts.append(f"类目：{category}")
        # 卖点若与标题完全同源（无分隔符的堆砌标题，分词结果就是标题本身），同样属于
        # 零信息量重复 —— 跳过，避免 chunk 里标题出现第二次
        if selling_points and not (len(selling_points) == 1 and selling_points[0] == name):
            parts.append(f"卖点：{'；'.join(selling_points)}")
        if specs:
            parts.append(f"规格：{'；'.join(specs)}")

        chunks.append({"text": "\n".join(parts), "metadata": dict(base_meta, field="main")})

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


def _knowledge_signature() -> str:
    """当前知识库指纹；读取失败返回 'unknown'"""
    try:
        import knowledge_store

        return knowledge_store.knowledge_signature()
    except Exception as e:  # noqa: BLE001
        print(f"[RAG] 读取知识库指纹失败：{e}")
        return "unknown"


def build_index(
    persist_dir: str = str(DEFAULT_PERSIST_DIR),
    collection_name: str = DEFAULT_COLLECTION,
    reset: bool = True,
) -> RAGPipeline:
    """构建完整索引并返回管线实例（同时记录知识库指纹，供后续判断索引是否过期）"""
    chunks = build_chunks()
    pipeline = RAGPipeline(persist_dir=persist_dir, collection_name=collection_name)
    n = pipeline.index(chunks, reset=reset)
    pipeline.vector_store.set_metadata({"knowledge_sig": _knowledge_signature()})
    print(f"[RAG] 索引构建完成：{n} 个 chunk → {pipeline.describe()}")
    return pipeline


def load_pipeline(
    persist_dir: str = str(DEFAULT_PERSIST_DIR),
    collection_name: str = DEFAULT_COLLECTION,
) -> RAGPipeline:
    """加载索引；为空、embedding backend 变化或知识库已变更时自动重建。

    第三个判据（知识库指纹）是必需的：此前只在「向量库为空 / backend 变化」时重建，
    导致经前端或 API 新增的知识永远检索不到——SQLite 里有、向量库里没有。
    """
    pipeline = RAGPipeline(persist_dir=persist_dir, collection_name=collection_name)
    current_backend = pipeline.embedder.backend

    if pipeline.vector_store.count() > 0:
        meta = pipeline.vector_store.get_metadata()
        stored_backend = meta.get("embedding_backend")
        stored_sig = meta.get("knowledge_sig")
        current_sig = _knowledge_signature()

        backend_ok = stored_backend == current_backend
        # 老索引没有指纹字段（stored_sig 为 None）→ 视为过期，重建一次把指纹写上
        sig_ok = stored_sig is not None and stored_sig == current_sig

        if backend_ok and sig_ok:
            # 复用现有索引 — NumpyVectorStore 在 __init__ 已把数据加载到内存，
            # 只需重建 BM25 与文本/元数据引用
            n = pipeline.vector_store.count()
            pipeline._texts = list(pipeline.vector_store._docs)
            pipeline._metas = [dict(m) if m else {} for m in pipeline.vector_store._metas]
            from .bm25 import BM25Retriever

            pipeline._bm25 = BM25Retriever(pipeline._texts)
            print(f"[RAG] 复用已有索引：{n} 个 chunk, backend={current_backend}, knowledge_sig={current_sig}")
            return pipeline

        if not backend_ok:
            print(f"[RAG] embedding backend 变更（{stored_backend} → {current_backend}），重建索引")
        else:
            print(f"[RAG] 知识库已变更（{stored_sig} → {current_sig}），重建索引")

    return build_index(persist_dir=persist_dir, collection_name=collection_name, reset=True)


if __name__ == "__main__":
    # 独立运行：python -m rag.indexer
    p = build_index()
    for r in p.search("退货政策是什么", k=3):
        print("=" * 60)
        print(f"[{r['metadata'].get('type')}] {r['text'][:120]}")
        print(f"score={r['score']}")
