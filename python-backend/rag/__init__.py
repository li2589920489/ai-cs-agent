"""
RAG 模块 — 完整检索增强生成技术栈

分层设计（每层可独立替换，符合工程化可插拔原则）：

    文档解析(doc_importer) → 分块(chunking) → Embedding(bge-small-zh)
          ↓                                             ↓
    BM25 关键词检索(bm25)                     ChromaDB 向量库(vector_store)
          ↓                                             ↓
              └────── 混合检索 + RRF 融合(pipeline) ──────┘
                              ↓
                      Re-ranking(reranker)
                              ↓
                     组装上下文 → LLM 回答

对外核心类：
    RAGPipeline  — 一站式检索管线（索引 + 混合检索 + 重排）
    Embedder     — 向量化（BAAI/bge-small-zh-v1.5，自动降级）
    Reranker     — 重排（BAAI/bge-reranker-base，自动降级）
"""
import os

# 禁用 HF 新 Xet 存储后端（与 hf-mirror 镜像不兼容会导致 401），回退传统 HTTP 下载
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# 国内默认走 hf-mirror 镜像加速（已设置 HF_ENDPOINT 则尊重用户配置）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from .embedding import Embedder
from .reranker import Reranker
from .pipeline import RAGPipeline
from .indexer import build_index, load_pipeline

__all__ = ["Embedder", "Reranker", "RAGPipeline", "build_index", "load_pipeline"]
