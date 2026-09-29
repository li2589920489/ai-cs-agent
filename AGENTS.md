# AGENTS.md — 项目编码规则（AI 每次动手前必读）

> 用途：对抗「上下文腐化」。对话一长 AI 会忘规则、被过时文档带偏，把关键事实与约束写死在这里，每次改动前先读。
> 配套文档：`需求分析文档.md`（要什么）→ `技术方案文档.md`（怎么做）→ `DESIGN.md`（架构）
> 维护约定：核心事实变更后，同步更新本文件的「事实红线」一节。

---

## 0. 项目速览

AI 电商客服系统：**6 Agent 协作**（Triage 分诊 + 订单/商品/售后/政策 4 业务 Agent + 人工兜底）+ **RAG 检索** + **MCP 标准化** + **Human-in-the-Loop**。

| 层 | 技术 |
|---|---|
| Agent 框架 | OpenAI Agents SDK（主）；LangGraph（独立模块，见红线 2） |
| 后端 | FastAPI + Uvicorn（Python 3.10 — 本地 `.venv` 3.10.11、CI `python-version: "3.10"`、生产镜像 `python:3.13-slim`） |
| 前端 | Next.js 15 + React + TailwindCSS（npm，锁文件 package-lock.json） |
| LLM | DeepSeek `deepseek-chat`（OpenAI 兼容） |
| 向量库 | **自建 numpy 后端**（见红线 1） |
| 数据库 | SQLite（`tenant_id` 字段预留，见红线 3） |
| 来源 | Fork 自 openai/openai-cs-agents-demo（README:12 已声明） |

---

## 1. 目录结构（★ 标注为遗留，勿改勿引）

```
ai-cs-agent/
├── python-backend/                  # 后端（工作目录）
│   ├── main.py                      # FastAPI 入口（/api/chat /api/chat/stream /api/knowledge /api/escalations /health）
│   ├── chat_service.py              # 共享 run_chat() / run_chat_stream()：消息→Triage→Agent→回复+trace；会话 _sessions 在此
│   ├── server.py                    # ★ ChatKit 桥接（死代码：全仓无 import，唯一入口是 uvicorn main:app）
│   ├── mcp_server.py                # FastMCP Server（4 工具）
│   ├── after_sales_graph.py         # LangGraph 售后状态机（独立，见红线 2）
│   ├── knowledge_store.py           # SQLite 知识库（多租户字段）
│   ├── order_store.py               # 订单 / 退货工单持久化 + 退货状态机（幂等约束）
│   ├── auth.py                      # API Key → tenant_id / 坐席角色鉴权
│   ├── memory_store.py              # ChatKit Store 实现（会话记忆，见红线 4）
│   ├── doc_importer.py              # PDF/Word/TXT 导入 + LLM 提取
│   ├── demo_rag.py                  # RAG 独立验证脚本
│   ├── download_models.py           # BGE 模型下载（本地化）
│   ├── eval_routing.py              # Triage 路由评测（80 条 → 98.75%）
│   ├── eval_retrieval.py            # RAG 公开基准评测（EcomRetrieval）
│   ├── eval_real_corpus.py          # RAG 自有语料评测（90 query）
│   ├── eval_prompt.py               # Prompt 对抗性评测（26 条 → 88.46%）
│   ├── eval_rrf_tuning.py           # RRF 调参实验（推翻混合检索）
│   ├── taobao_adapter.py / taobao_webhook.py / mock_taobao.py   # 淘宝接入（Mock）
│   ├── douyin_adapter.py / douyin_webhook.py / mock_douyin.py   # 抖店接入（Mock）
│   ├── ecommerce/                   # 电商业务（主战场）
│   │   ├── agents.py                #   6 Agent 定义 + handoff 关系
│   │   ├── context.py               #   共享上下文 ECommerceAgentContext（17 字段）
│   │   ├── tools.py                 #   13 个业务工具（含 RAG 接入）
│   │   ├── guardrails.py            #   2 个 Input Guardrail + 2 个 verdict 工具
│   │   └── demo_data.py             #   模拟商品/订单/优惠券/政策
│   ├── rag/                         # 完整 RAG 包（9 个 .py）
│   │   ├── __init__.py              #   单例入口 get_pipeline / warmup / mark_pipeline_stale
│   │   ├── pipeline.py              #   检索管线（默认 use_hybrid=True，业务层传 False 走纯向量）
│   │   ├── chunking.py              #   语义分块 256/50
│   │   ├── embedding.py             #   BGE 向量化（可降级）
│   │   ├── numpy_store.py           #   ★ 现役向量库（sqlite 持久化 + numpy 矩阵 cosine）
│   │   ├── vector_store.py          #   兼容层（继承 NumpyVectorStore，接口不变）
│   │   ├── bm25.py                  #   BM25 关键词检索（仅作降级兜底，不参与默认融合）
│   │   ├── reranker.py              #   bge-reranker 精排
│   │   └── indexer.py               #   索引构建（含知识库指纹判断）
│   ├── tests/                       # pytest（conftest.py 把 DB 与 RAG 索引目录都指向临时目录）
│   ├── scripts/                     # import_real_products.py 等导入/验证脚本
│   ├── airline/                     # ★ 原示例遗留（airline 场景），非电商业务，勿改勿引
│   ├── data/                        # models/(BGE 模型) eval/(评测集) knowledge.db orders.db
│   ├── Dockerfile / requirements.txt / requirements-dev.txt / .env.example / .dockerignore
├── ui/                              # Next.js 前端
│   ├── app/page.tsx                 # 3 Tab 布局（客服对话/知识库/坐席工作台）
│   ├── components/                  # chat-panel / knowledge-panel / agent-workspace / escalation-panel / guardrails 等
│   └── lib/                         #   api.ts / sse.ts（SSE 解帧）/ chat-stream.ts（进度态）/ types.ts / utils.ts
├── docs/                            # 演示截图 + 评测报告 + 部署指南
├── docker-compose.yml               # backend / frontend / nginx 三 service
├── nginx.conf                       # 反向代理（/api/ 含流式三指令）
├── AGENTS.md / README.md / DESIGN.md / CHANGELOG.md / 技术方案文档.md / 需求分析文档.md
└── LICENSE                          # MIT（基于 openai/openai-cs-agents-demo，勿删声明）
```

---

## 2. 事实红线（最容易写错，改动前务必核对）

> 这些是「表述/改动」时的高危点。文档或代码里写错，性质会从「未做」变成「不实」。

1. **向量库是自建 numpy 后端，不是 ChromaDB**。现役实现 `rag/numpy_store.py`，`vector_store.py` 仅是兼容层。但 `requirements.txt` 仍列 `chromadb`、`.env.example` 仍写 `CHROMA_PERSIST_DIR`——**这些都是遗留/滞后，不要据此改回 ChromaDB**。

2. **LangGraph 售后状态机未接入主链路**。`after_sales_graph.py` 独立跑通（5 节点），但全仓无任何模块 import 它；线上售后走 Agents SDK 的 `after_sales_agent`（`ecommerce/agents.py`）。禁止写「售后已由 LangGraph 编排」，正确口径是「独立验证的原型，未接入 Agent 链路」。（注意：`order_store.py` 的退货状态机是独立于 LangGraph 的另一件事，属主链路，别混淆。）

3. **多租户 = 字段 + 查询层过滤 + API Key 鉴权，非完整隔离**。知识库表 `tenant_id` + 查询层 `WHERE tenant_id=?` 过滤（`knowledge_store.py`）+ `auth.py` 的 `get_current_tenant`（api_key sha256 → tenant_id）已实现；订单/退货表同样带 `tenant_id` 字段（默认 `'default'`）。但会话/工单为内存态、未按租户隔离。禁止写「已实现完整多租户隔离 / RBAC」。

4. **会话是内存态，非跨会话长期记忆**。`chat_service.py` 的 `_sessions` 字典 + 30min TTL；`memory_store.py` 是 ChatKit `Store` 实现。禁止写「长期记忆机制」。

5. **鉴权已做但未全覆盖**。知识库接口走 `get_current_tenant`（`auth.py`）；工单全部接口（`GET /api/escalations` 列表、`{id}/messages` 读取、`accept/reply/close`）走 `require_agent_role`（`main.py:232`）。**唯一例外是 `GET /api/chat/poll`（`main.py:296`）仍无鉴权**——买家轮询接口，加鉴权需前端同步携带会话令牌，已决策本轮不改，作为**已知限制**保留（`tests/test_api_contracts.py` 有特征化用例锁定现状）。另：API Key 静态无轮换。

6. **平台接入仅 Mock**。淘宝/抖店只有适配层 + webhook + mock，无真实店铺资质。

7. **评测数字口径**（禁止改动或另造）：Triage 98.75%（79/80）；RAG 纯向量 R@10 75.0% / MRR 52.1% / nDCG 57.6%；自有语料纯向量 97.33% / 混合 100%；Prompt 88.46%（26 条）；6 Agent / 13 function_tool + 2 verdict + 4 MCP；Context 17 字段；**1328 chunks**（1321 product + 7 policy）；镜像 2.6 GB。
   - **Triage 98.75% 的测量口径**：该值是**单次实测**，`easy` 组会漂移。6 个 Agent 均未设 `temperature`（走 DeepSeek 默认值，非确定）→ 复跑实测 **96.25% / 97.50%**，三次错题集**完全不重叠**。故该数字**不是**稳定准确率，文档中应写为「单次实测 98.75%，实测区间 96.25%–98.75%」。要稳定数字须先固定 `temperature=0`。详见 `分流评测报告.md` 开头。

---

## 3. 编码规范

- **Python 3.10**（本地 `.venv` 3.10.11、CI 3.10、镜像 `python:3.13-slim`）；依赖装进 `.venv`，不污染全局环境。
- **类型注解**：pydantic 模型用 `BaseModel`，函数参数/返回尽量加注解（项目现状即是）。
- **命名**：变量/函数 snake_case，类 PascalCase。
- **注释**：中文注释与 docstring 允许（项目现状即是），关键逻辑写清楚。
- **小步改动**：一次只改一个可验证增量；改完立即跑对应验证（见 §5），验证通过才提交。
- **不改 `airline/`**：属原示例遗留（航空场景），清理走独立任务，日常改动勿碰。（原 ui 下两个无引用的死组件已删除，勿再引入。）
- **多租户字段**：新增查询/写入一律保留 `tenant_id` 字段透传，不得删除。

---

## 4. 依赖白名单

**已在 requirements.txt（允许使用）**：`openai-agents` `openai-chatkit` `pydantic` `fastapi` `uvicorn` `python-dotenv` `pypdf` `python-docx` `numpy` `sentence-transformers` `rank-bm25` `jieba` `fastmcp` `langchain-core` `langchain-text-splitters` `langgraph`

**测试依赖（`requirements-dev.txt`，不进生产镜像）**：`pytest` `httpx`

**遗留（勿据其改动）**：`chromadb`——实际向量库已换 numpy 后端，此依赖未在使用路径上，但暂不删（避免连锁）。

**铁律**：新增任何第三方库，必须先向维护者说明用途与理由，得到确认后再写进 requirements.txt；禁止擅自 `pip install`、禁止擅改已有依赖版本。

---

## 5. 命令速查（Windows，在 python-backend 目录下）

```bash
# 后端启动
.venv\Scripts\uvicorn main:app --reload          # 或 activate 后：uvicorn main:app --reload

# 前端（另开终端，在 ui/ 下）
npm run dev:next                                  # 只起 Next.js dev server（推荐）
# npm run dev 会并用 concurrently 同时起前后端；其 dev:server 写死 .venv\Scripts\python.exe，仅 Windows 可用
# 锁文件为 package-lock.json（npm），pnpm-lock.yaml 已移除

# 测试（conftest.py 自动把 DB 与 RAG 索引目录指向临时目录，不写 data/*.db）
.venv\Scripts\python -m pytest tests\ -v
# 裸 `pytest` 也能跑（conftest 已把 python-backend/ 插进 sys.path）；但推荐 `python -m pytest`

# 独立验证（不启动全服务）
.venv\Scripts\python demo_rag.py                  # RAG 管线
.venv\Scripts\python after_sales_graph.py         # LangGraph 状态机
.venv\Scripts\python mcp_server.py                # MCP Server（stdio）

# 评测（四套，全部可复跑）
.venv\Scripts\python eval_routing.py              # Triage 98.75%
.venv\Scripts\python eval_retrieval.py            # RAG 公开基准
.venv\Scripts\python eval_real_corpus.py          # 自有语料
.venv\Scripts\python eval_prompt.py               # Prompt 对抗性

# 容器化
docker compose up -d --build                      # 在项目根目录（backend / frontend / nginx）
```

---

## 6. API 约定

| 路径 | 说明 | 鉴权 |
|---|---|---|
| `POST /api/chat` | 核心对话，返回 `reply` + `agent_trace` + `escalation` + `session_id` | 无 |
| `POST /api/chat/stream` | **SSE 流式对话**：5 类帧 `delta` / `trace` / `escalation` / `error` / `done`；`delta` 带 `mid`（消息标识）、`done` 带权威 `reply` | 无 |
| `GET /api/chat/poll?session_id=` | 买家轮询坐席回复 | **无**（已知限制，见红线 5） |
| `GET/POST/PUT/DELETE /api/knowledge` | 知识库 CRUD（`main.py:320-352`） | 读匿名 / 写 `get_current_tenant` |
| `POST /api/knowledge/import`、`/import-doc` | CSV / PDF·Word 批量导入 | `get_current_tenant` |
| `POST /api/knowledge/reindex` | 重建 RAG 向量索引（批量导入后立即生效） | `get_current_tenant` |
| `GET /api/escalations` | 工单列表（含 pending 计数） | `require_agent_role` |
| `GET /api/escalations/{id}/messages` | 工单对话记录 | `require_agent_role` |
| `POST /api/escalations/{id}/accept\|reply\|close` | 坐席工单操作 | `require_agent_role` |
| `GET /health` | 健康检查，返回 6 agents | 无 |

---

## 7. 禁止事项（负面清单）

| 不做 | 理由 |
|---|---|
| ❌ 加 Agent / 加工具 | 6 Agent + 13 function_tool + 4 MCP 叙事已足够，边际趋零 |
| ❌ 重构代码架构 | 需求已满足，重构无外部增量 |
| ❌ 换框架 / 再造 LangChain 系统 | 只增复杂度，不增可验证性 |
| ❌ 加回 BM25 混合检索 | 已实测推翻（BM25 增量 3.4% 却污染 27.8%） |
| ❌ 把向量库改回 ChromaDB | 见红线 1，Windows 下 hnsw 损坏是历史教训 |
| ❌ 改动 `airline/` | 属遗留清理任务，非日常改动 |
| ❌ 删 LICENSE / Fork 声明 | 诚信红线，README:12 已声明 |
| ❌ 擅自新增依赖 | 见 §4 铁律 |
| ❌ 写不实表述 | 见红线 1-6，「未做」不得写成「已做」 |
| ❌ 在项目文档/代码夹带个人私人物料 | 仓库只写技术内容；个人材料、面向评审的叙述、第二人称方案书一律不入仓 |
