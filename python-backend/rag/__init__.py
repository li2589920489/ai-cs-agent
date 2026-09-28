"""
RAG 模块 — 完整检索增强生成技术栈

分层设计（每层可独立替换，符合工程化可插拔原则）：

    文档解析(doc_importer) → 分块(chunking) → Embedding(bge-small-zh)
          ↓                                             ↓
    BM25 关键词检索(bm25)                     numpy 向量库(vector_store)
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
import threading

# 禁用 HF 新 Xet 存储后端（与 hf-mirror 镜像不兼容会导致 401），回退传统 HTTP 下载
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# 国内默认走 hf-mirror 镜像加速（已设置 HF_ENDPOINT 则尊重用户配置）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from .embedding import Embedder
from .reranker import Reranker
from .pipeline import RAGPipeline
from .indexer import build_index, load_pipeline

__all__ = [
    "Embedder", "Reranker", "RAGPipeline", "build_index", "load_pipeline",
    "get_pipeline", "mark_pipeline_stale", "reset_pipeline", "warmup",
]

# 进程内管线单例：首次取用时加载本地 embedding 模型（耗时），后续复用
_pipeline_singleton = None
_pipeline_stale = False
# 重建只允许一个执行者：并发请求同时发现「过期」时，若各自跑一遍 load_pipeline，
# 会出现 N 次全量重向量化（上千 chunk、数十秒）
_pipeline_lock = threading.Lock()


def get_pipeline():
    """获取 RAG 管线单例；被标记过期时自动重载。

    重载走 load_pipeline —— 它会比对知识库指纹，决定「复用现有索引」还是「重建」，
    因此知识库写入后无需手工干预即可让新内容进入检索范围。

    并发保护：快路径（单例已就绪且未过期）不加锁直接返回；只有确实需要加载/重建时
    才进临界区，并用双重检查避免重复执行。
    """
    global _pipeline_singleton, _pipeline_stale
    if _pipeline_singleton is not None and not _pipeline_stale:
        return _pipeline_singleton
    with _pipeline_lock:
        if _pipeline_singleton is None or _pipeline_stale:
            _pipeline_singleton = load_pipeline()
            _pipeline_stale = False
    return _pipeline_singleton


def warmup() -> None:
    """启动期预热：提前加载 embedding / reranker 模型并建好索引。

    为什么需要：模型加载与索引重建耗时约 27s，若等到首个请求才做，该请求会同步阻塞
    事件循环（uvicorn 单进程下会卡住所有并发请求），实测冷启动首个请求要 70.9s。

    为什么用 use_rerank=True 而不是 False：运行时真实检索路径是「纯向量 + 重排」
    （见 ecommerce/tools.py::_rag_search），reranker 是懒加载的，这里不触发它，
    第一个真实提问仍要再付一次模型加载时间。

    失败不阻断启动 —— 退化为首请求懒加载，与改动前行为一致。
    """
    try:
        pipeline = get_pipeline()
        pipeline.search("预热查询", k=1, use_hybrid=False, use_rerank=True)
        reranker_state = "可用" if pipeline.reranker.available else "不可用"
        msg = f"[RAG] 预热完成：{pipeline.size} chunks（reranker {reranker_state}）"
    except Exception as e:  # noqa: BLE001
        msg = f"[RAG] 预热失败（不阻断启动，首个请求会退化为懒加载）：{e}"
    # flush=True 是必须的：uvicorn 日志走 stderr（行缓冲），而 print 走 stdout 且重定向到
    # 文件时是块缓冲 —— 不 flush 的话这条验收标志可能延迟几十秒才落盘，让"等日志"的检查误判超时。
    print(msg, flush=True)


def mark_pipeline_stale() -> None:
    """标记管线需要重载（知识库写入后调用）。

    运行中的服务持有管线单例，不重载便不会感知知识库变化——这是「新增知识检索不到」
    的根因；标记本身很轻，真正的重建延后到下次检索时按指纹判断。
    """
    global _pipeline_stale
    _pipeline_stale = True


def reset_pipeline() -> None:
    """丢弃管线单例（reindex 接口用：确保下次取用时重新加载磁盘上的新索引）"""
    global _pipeline_singleton, _pipeline_stale
    _pipeline_singleton = None
    _pipeline_stale = False
