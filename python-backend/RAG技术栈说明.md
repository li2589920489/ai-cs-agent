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
向量库( numpy + sqlite 自建后端，cosine 距离，矩阵乘法检索 )
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
| 向量库 | numpy + sqlite 自建 | ChromaDB 1.5.9 在 Windows 上 hnsw segment reader 崩溃（已知 issue），降级 0.4.24 又被 numpy 2.x/pydantic 2.x 依赖冲突卡住；自建 sqlite + numpy 后端，1300 chunks 检索 < 50ms，去掉 ~100MB 依赖 |
| 关键词检索 | rank-bm25 + jieba | 稀疏检索补向量检索的精确匹配盲区（如商品 ID、专有名词） |
| 融合 | RRF | 无需调权、对分数尺度不敏感，业界标准融合算法 |
| 重排 | bge-reranker-base（Cross-Encoder） | 双塔 embedding 粗排后，交叉编码器逐对精排，显著提升 top-k 精度 |

**核心设计原则：每层可插拔、可降级。** Embedding 加载失败自动降级 chromadb 默认模型；重排模型加载失败自动跳过重排；纯向量检索走 numpy 后端，混合检索保留 BM25 路径可显式启用；保证链路在任何环境都可用。

## 三、检索流程（体现「检索优化」）

1. **运行时默认纯向量 + 重排**：向量检索召回 top M → bge-reranker 重排 → top k
2. **混合检索（保留能力）**：向量 + BM25 各召回 top20 → RRF 融合 → 重排。**仅在显式 `use_hybrid=True` 时启用**（评测脚本用）
3. **精排**：Cross-Encoder 对 top8 候选逐对打分重排
4. **溯源**：每个 chunk 携带 `{type, product_id, policy_name}` 元数据，回答可回溯到知识来源

> **为什么运行时默认纯向量？** RAG 评测（`eval_retrieval.py`）在 EcomRetrieval 上对比纯向量 / BM25 / 混合 / 混合+重排四档，发现混合检索相对纯向量的增量仅 3.4%（34/1000 query）却污染 27.8%（278/1000），混合 nDCG 最高 54.5% < 纯向量 57.6%。结论：纯向量已是更优默认；混合检索作为可降级能力保留，调用方可显式启用。

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
> 电商客服知识库完整 RAG 检索管线：文档解析 → 语义分块 → BGE-small-zh 向量化 + 自建 numpy+sqlite 向量库 → BM25+向量混合检索（RRF 融合，运行时默认关闭，作为可显式启用的能力保留）→ bge-reranker 交叉编码器重排，检索结果携带来源元数据支持溯源；各层可插拔、可降级，保障链路稳定。

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

## 八、运行时策略调整（2026-09-10）

### 1) BM25 降级为兜底

- **过去**：`tools.py:_rag_search()` 写死 `use_hybrid=True`，所有 RAG 工具默认走混合检索
- **现在**：`use_hybrid=False` 是默认；混合检索仅在显式传入时启用（如 `eval_retrieval.py` 的对比实验）
- **依据**：`data/eval/ecom_retrieval/eval_results.json` 实测，纯向量 nDCG@10=57.6% > 混合+重排 54.5%（详见 `eval_retrieval.py` + `eval_rrf_tuning.py` 的对比报告）

### 2) 政策检索：RAG 优先 + 规则兜底

- **过去**：`faq_lookup_tool` 是 15 个关键词的硬编码规则匹配；`rag_search_tool` 是 RAG 版但仅作兜底
- **现在**：`faq_lookup_tool` 默认走 RAG 语义检索（`type_filter="policy"`），命中真实政策 chunk；仅在 RAG 失败/无结果时回退到规则匹配（应对 embedding 模型不可用、向量库为空等异常场景）
- **新增**：`rag_search_tool` 改为纯 RAG 路径，禁用规则匹配兜底，给调用方一个"明确只要语义检索"的选项
- **接口不变**：Agent 工具层（`ecommerce/agents.py` 政策 Agent）无需改调用方式

### 3) MCP 工具同步

- `mcp_server.py` 的 `search_knowledge` / `search_policy` 默认 `use_hybrid=False`，与项目内 Agent 工具链策略一致

### 4) Prompt 同步

- `ecommerce/agents.py` 政策 Agent 的 instructions 改为「默认走 RAG，规则匹配仅作兜底；需要更强语义时可改用 `rag_search_tool`」

## 九、踩坑记录：chromadb hnsw 损坏 → 自建 numpy 后端

### 现象

`build_index` 成功（历史：当时 1327 chunks 入库），但 `count() / get() / query()` 全部抛：
```
Error executing plan: Error sending backfill request to compactor:
Error constructing hnsw segment reader: Error loading hnsw index
```

### 排查

1. 数据完整在 `data/chroma_rag/chroma.sqlite3` 的 `embeddings` / `embedding_metadata` 表中，1327 行全部存在
2. chromadb 1.5.9 是已知有这个 bug（hnsw segment 文件未正确生成），上游在 1.5.x 仍未完全修复
3. 降级 chromadb 0.4.24 修复 hnsw，但被 numpy 2.x / pydantic 2.x 依赖冲突卡住（pip safe-delete 在 Windows 上反复撞文件锁）

### 决策

自建 `rag/numpy_store.py`（NumpyVectorStore）：
- 持久化：独立 sqlite（`numpy_store.db`），schema 干净（`chunks` + `collection_meta` 两张表）
- 检索：BGE 向量已归一化，矩阵乘法等价 cosine 相似度；1300 chunks 检索 < 50ms
- 接口完全兼容原 ChromaDB VectorStore（`count / get_metadata / set_metadata / upsert / query`），上层无感

### 优势

- 不依赖 chromadb 包（虽然仍装着供 embedding.py 兜底用）
- 数据可迁移：原 chromadb.sqlite3 仍保留（历史存档，1327 chunks 在），可通过迁移脚本搬到 numpy_store.db
- 工程价值：**评估 chromadb 在生产环境的稳定性，发现 hnsw 在某些 OS 上崩溃，决策自建后端，规避供应商锁定**

### 保留

- `vector_store.py` 作为兼容层，继承 `numpy_store.NumpyVectorStore`
- `pipeline.py` / `indexer.py` / `mcp_server.py` 不需要改动接口
- `rag/bm25.py` / `rag/reranker.py` 不变
