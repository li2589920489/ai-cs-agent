# RAG 技术栈说明

> 本文档是「完整 RAG 技术栈」落地的说明，介绍文档解析、分块、Embedding、混合检索、重排、Agent 工具接入的端到端方案。

## 一、架构总览

```
文档解析( pypdf / python-docx )
        ↓
语义分块( RecursiveCharacterTextSplitter，chunk=256，overlap=50 )
        ↓
Embedding( BAAI/bge-small-zh-v1.5，本地中文向量，512维 )
        ↓
向量库( ChromaDB，cosine 距离，HNSW 索引 )
   +
BM25 关键词检索( rank-bm25 + jieba 分词 )
        ↓
混合检索 + RRF 融合( Reciprocal Rank Fusion，k=60 )
        ↓
Re-ranking( BAAI/bge-reranker-base 交叉编码器 )
        ↓
组装上下文 → 交给 Agent 的 LLM 生成回答（可溯源）
```

## 二、技术选型与理由

| 层 | 选型 | 理由 |
|---|---|---|
| 文档解析 | pypdf + python-docx | 覆盖 PDF/Word/TXT，兼容 GBK/UTF-8 |
| 分块 | langchain-text-splitters 递归分块 | 按「段落→换行→中文句末标点→字符」逐级切分，语义不割裂 |
| Embedding | BAAI/bge-small-zh-v1.5 | 中文效果好，~100MB 本地可跑（4GB 显存/CPU 均可），query 加指令前缀 |
| 向量库 | ChromaDB | 轻量、持久化、cosine 距离、HNSW 索引，适合中小规模知识库 |
| 关键词检索 | rank-bm25 + jieba | 稀疏检索补向量检索的精确匹配盲区（如商品 ID、专有名词） |
| 融合 | RRF | 无需调权、对分数尺度不敏感，业界标准融合算法 |
| 重排 | bge-reranker-base（Cross-Encoder） | 双塔 embedding 粗排后，交叉编码器逐对精排，显著提升 top-k 精度 |

**核心设计原则：每层可插拔、可降级。** Embedding 加载失败自动降级 Chroma 默认模型；重排模型加载失败自动跳过重排；保证链路在任何环境都可用。

## 三、检索流程（体现「检索优化」）

1. **双路召回**：向量检索（语义）+ BM25（关键词）各召回 top20
2. **RRF 融合**：`score(d) = Σ 1/(60 + rank)`，融合出兼顾语义与精确匹配的候选集
3. **精排**：Cross-Encoder 对 top8 候选逐对打分重排
4. **溯源**：每个 chunk 携带 `{type, product_id, policy_name}` 元数据，回答可回溯到知识来源

## 四、如何运行验证

```bash
cd python-backend
# 1. 独立验证 RAG 管线（构建索引 + 检索）
.venv/Scripts/python demo_rag.py

# 2. 验证售后 LangGraph 状态机
.venv/Scripts/python after_sales_graph.py

# 3. 启动 MCP Server（stdio，供 MCP 客户端接入）
.venv/Scripts/python mcp_server.py

# 4. 启动完整后端（RAG 已接入 Agent 工具）
.venv/Scripts/python -m uvicorn main:app --reload
```

## 五、MCP 架构说明

`mcp_server.py` 用 FastMCP 把知识库检索 / 订单查询封装为**标准化 MCP 工具**，实现「工具层与 Agent 编排解耦、可复用」。任何 MCP 客户端（Claude Desktop、其他 Agent 框架）都能通过统一协议调用，无需关心底层是向量库还是 SQLite。

## 六、模块在系统中的位置

**RAG 模块：**
> 电商客服知识库完整 RAG 检索管线：文档解析 → 语义分块 → BGE-small-zh 向量化 + ChromaDB 存储 → BM25+向量混合检索（RRF 融合）→ bge-reranker 交叉编码器重排，检索结果携带来源元数据支持溯源；各层可插拔、可降级，保障链路稳定。

**MCP 模块：**
> 用 FastMCP 将知识库检索 / 订单查询封装为标准化 MCP Server，Agent 通过 MCP 协议统一调用，实现工具层与编排层解耦、跨框架复用。

**LangGraph 模块：**
> 用 LangGraph 将售后处理建模为显式状态机（受理 → 查单 → 判责 → 退货/退款/人工兜底），条件路由表达业务分支，状态可持久化、可插人工审批节点。

**Docker 模块：**
> 后端 FastAPI 服务容器化（Dockerfile + docker-compose 一键编排，卷持久化向量库与知识库，含健康检查）。

## 七、当前验证状态（2026-09-01 实测）

| 模块 | 状态 | 证据 |
|---|---|---|
| RAG 全链路 | ✅ 通过 | 23 个 chunk 建索引，`embedding_backend=bge-local` + `reranker=True`，5 条查询（退货政策/坚果退货/优惠券叠加/商品卖点/物流异常）全部命中正确来源 |
| BGE 中文模型 | ✅ 本地加载 | `data/models/bge-small-zh-v1.5`（91MB）+ `bge-reranker-base`（1.06GB），离线可用 |
| LangGraph 状态机 | ✅ 通过 | 节点 validate→classify→process_return/process_refund/escalate，3 类售后场景路由正确 |
| MCP Server | ✅ 通过 | 4 工具就绪：search_knowledge / search_policy / get_order / search_products |
| Agent 工具接入 | ✅ 已修复 | rag_search/knowledge_search 已接完整 RAG，server.py 移除失效 agent 引用 |
| Docker | ✅ 通过 | 镜像 2.6GB（CPU torch，无 CUDA 依赖），容器 `healthy`，`/health` 返回正常 |

**Docker 落地的三个关键决策：**
1. **镜像加速器**：国内直连 Docker Hub 超时，配置 registry-mirror（DaoCloud 等 3 源）+ 基础镜像走 DaoCloud，构建 22s 拉取完成。
2. **CPU 版 torch**：Linux 上 `torch` 默认 CUDA 版会拉 2GB+ 的 nvidia 依赖，改为先装 CPU 版（191MB），镜像从 ~6GB 压到 2.6GB，契合轻量化定位。
3. **pip 国内源**：`PIP_INDEX_URL` 指向清华源，依赖下载稳定 6-8MB/s。
4. **模型与镜像解耦**：BGE 模型不进镜像（`.dockerignore` 排除 `data/`），改用 bind mount `./python-backend/data/models:/app/data/models:ro` 只读挂载，容器内实测 `embedding_backend=bge-local` + `reranker=True`，镜像保持 2.6GB 瘦身、模型独立更新无需重建镜像。
