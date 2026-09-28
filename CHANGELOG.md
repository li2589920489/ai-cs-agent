# Changelog

所有对 `ai-cs-agent` 有显著变更的记录。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)（Semantic Versioning）规范。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

---

## [Unreleased]

### 待办

- ~~Human-in-the-Loop 坐席工作台 UI（Next.js 端）~~ ✅ 已完成（`ui/components/agent-workspace.tsx` 295 行）
- 真实店铺接入申请（淘宝企业资质 / 抖店开放平台）
- 模型权重二进制分发（替代本地 `data/models/` .gitignore）

### Added（新增）

- **启动期预热**：`rag.warmup()` + FastAPI `lifespan`（`asyncio.to_thread`），在 uvicorn 开始接受请求前加载 embedding / reranker 并建好索引
- **`python-backend/order_store.py`**：订单 / 退货工单 SQLite 持久化 + 退货状态机（`待审核 → 已通过 → 已寄回 → 已退款`，外加 `已撤销`），用 partial UNIQUE INDEX 保证「同一订单同租户至多一个活跃退货单」的幂等约束
- **自动化测试**：`python-backend/tests/`（`conftest.py` 把 `KNOWLEDGE_DB_PATH` / `AUTH_DB_PATH` / `ORDER_DB_PATH` 指向临时目录；`test_smoke.py` / `test_api_contracts.py` / `test_return_state.py`）
- **CI**：`.github/workflows/ci.yml`（`compileall` 语法检查 + `pytest`，`python-version: "3.10"`，只装精简依赖）
- `python-backend/requirements-dev.txt`（pytest / httpx，不进生产镜像）

### Changed（修改）

- `ecommerce/tools.py`：订单类工具数据源从 `demo_data.MOCK_ORDERS`（内存）切到 `order_store`（落盘），修掉「写落盘、读内存」的不一致；`initiate_return_tool` 增加 `action` 参数（create / cancel / status），**工具总数仍为 13**
- `main.py`：`order_store.init_order_store()` 纳入启动初始化；预热经 `lifespan` 前置
- `knowledge_store.py`：① `_ensure_store_overview()` 内容未变时不再刷 `updated_at`（此前每次冷启动都会让知识库指纹漂移，触发全量重向量化 1300+ chunk）；② `_seed_default_knowledge()` 不再提前 `conn.close()`（此前全新 DB 首次启动必抛 `Cannot operate on a closed database`）
- `rag/__init__.py`：`get_pipeline()` 改为双重检查加锁（`threading.Lock`），并发首问不再重复建索引
- 文档与规则同步：`AGENTS.md`（死组件行 / compose 三 service / 红线 1·5 / chunk 口径 / Python 版本）、`需求分析文档.md`、`docs/真实商品导入工程.md`、`README.md`

### Fixed（修复）

- 冷启动首问 70.9s 同步阻塞 → 预热后首问 <5s
- 全新克隆 / 空 Docker volume 首次启动崩溃（`sqlite3.ProgrammingError`）
- 每次冷启动全量索引重建（知识库指纹漂移）
- `/api/escalations` 列表与 `{id}/messages` 零鉴权 → 均走 `require_agent_role`（仅 `GET /api/chat/poll` 保留无鉴权，作已知限制）
- **`pytest` 会污染开发用向量索引（1328 → 28 chunks）**：`rag/indexer.py` 的持久化目录此前硬编码、无环境变量出口，而 `tests/test_smoke.py::test_rag_retrieval_smoke` 会调 `load_pipeline()`，于是测试用「只有 5 行 demo 的临时知识库」在 `data/chroma_rag/` 里重建了索引。修复：`DEFAULT_PERSIST_DIR` 支持 `RAG_PERSIST_DIR` 覆盖 + `tests/conftest.py` 把索引目录一并指向临时目录
- **CI run #1 退出码 2（收集期 `ModuleNotFoundError: No module named 'main'`）**：CI 步骤用的是裸 `pytest`，其 `sys.path[0]` 是 Scripts/bin 目录、不含 cwd，而 `tests/*.py` 需要 `from main import app` → 三个测试文件全报 ERROR → `Interrupted: 3 errors during collection`。修复：`tests/conftest.py` 显式把 `python-backend/` 插入 `sys.path`（裸 `pytest` 与 `python -m pytest` 都能跑，不再依赖调用方式）；CI 步骤同时改用 `python -m pytest`
- **`test_rag_retrieval_smoke` 的 skip 条件不完整**：原来只判 `data/models/` 下模型文件是否存在，未判 `sentence-transformers` 能否导入；「装了模型但没装依赖」的环境会抛 `RuntimeError: 无法初始化 Embedding` 而不是 skip。修复：`_HAS_RAG = _HAS_MODEL and _HAS_ST`（`importlib.util.find_spec`）

### Verified（验证）

| 项 | 数据 |
|---|---|
| pytest | **22 passed**（smoke / API 契约 / 退货状态机 / 测试隔离回归） |
| CI（GitHub Actions） | run #1 ❌ 退出码 2（裸 `pytest` 收集期 `ModuleNotFoundError`）→ 修复后 run #2 ✅ 全部步骤 success（CI 环境无本地模型，为 21 passed + 1 skipped） |
| 预热日志 | `[RAG] 预热完成：1328 chunks` |
| 索引指纹稳定性 | 修复前连续 3 次冷启动指纹均漂移；修复后不再漂移（复用索引） |
| 测试隔离（A/B 对照） | 置空 `RAG_PERSIST_DIR` 跑一次 pytest → 开发索引 1328 → **28 chunks**（复现污染）；修复后跑 pytest → 索引 chunks 与 mtime **完全不变** |
| 退货状态机 | 幂等（同订单重复申请返回同一 case）+ 撤销回滚（仅「待审核」可撤） |
| 反向对照（负向验证） | 模拟旧实现（只写 Context）查得 `待发货`；走 `cancel_order_tool` 后查得 `已取消` —— 证明断言具备判别力 |
| 文档一致性 | 全仓对已删除 UI 组件的引用为 0；chunk 数现状口径统一为 1328（历史存档数字保留并标注） |

---

## [1.0.0] - 2026-09-07

首次达到功能完整、可稳定运行状态的版本。本次迭代涉及 17 个文件的新增 / 修改 / 删除。

### Added（新增）

#### 核心功能
- **RAG 检索完整链路**：`rag/` 子包 7 个模块（chunking / embedding / bm25 / vector_store / reranker / pipeline / indexer），基于 BGE-small-zh + bge-reranker-base
- **MCP Server 标准化**：`python-backend/mcp_server.py`（4 个工具）
- **LangGraph 售后状态机**：`python-backend/after_sales_graph.py`（受理 → 查单 → 判责 → 退款/退货/人工）
- **Docker 容器化**：`Dockerfile` + `docker-compose.yml`（国内源 + 健康检查）
- **Human-in-the-Loop 兜底**：`ecommerce/guardrails.py`（输入/输出护栏 + 越狱检测）

#### 业务接入
- `python-backend/douyin_adapter.py` / `douyin_webhook.py` / `mock_douyin.py`：抖音客服消息接入适配层 + 本地 Mock
- `python-backend/taobao_adapter.py` / `taobao_webhook.py` / `mock_taobao.py`：淘宝 TOP API 接入适配层（OAuth 2.0 + AES-256-CBC + hmac-sha256 签名 + 坐席确认队列）
- `抖音接入方案.md` / `淘宝接入方案.md`：接入设计文档

#### 工具与脚本
- `python-backend/download_models.py`：BGE 模型本地化下载（避坑 hf-mirror + Xet 损坏）
- `python-backend/download_ecom_retrieval.py`：C-MTEB EcomRetrieval 数据集下载
- `python-backend/restore_models.py`：从 HF Xet 损坏 blob 恢复模型权重

#### 评测
- `eval_routing.py`：Triage 意图路由评测（80 条黄金用例 → 98.75%）
- `eval_retrieval.py`：RAG 检索质量评测（1000 query × 10 万语料）
- `eval_rrf_tuning.py`：混合检索 RRF 调优实验（8 种配置扫描）
- `分流评测报告.md` / `RAG检索评测报告.md` / `评测数据集选型与落地方案.md`
- `data/eval/routing_eval_results.json` / `ecom_retrieval/eval_results.json` / `ecom_retrieval/rrf_tuning_results.json`

#### 文档
- `docs/screenshots/README.md`：13 张演示截图索引

### Changed（修改）

#### 文档数字与代码实测全面对齐
- `README.md`：Triage `240 条` → `80 条`、Recall@10 `73.8%` → `75.0%`、MRR@10 `52.0%` → `52.1%` + 新增 `nDCG@10 57.6%`、工具数 `17` → `13 业务 + 4 Guardrail + 4 MCP = 21`
- `DESIGN.md`：项目结构章节补全所有新增模块，工具数从 12 → 13
- `需求分析文档.md` / `RAG检索评测报告.md` / `分流评测报告.md` / `评测数据集选型与落地方案.md` / `抖音接入方案.md` / `python-backend/RAG技术栈说明.md`：数字与措辞全面校准

#### 工程改造
- `python-backend/ecommerce/tools.py`：提取局部变量小重构（行为不变）
- 全仓库 9 个 .md：移除招聘向措辞（合计 32+ 处）

### Removed（删除）

- `docs/screenshots/offermore-login-qr.png`：无关截图，无文档引用残留

### Fixed（修复）

- `分流评测报告.md`：检索指标口径从已弃用的 `73.8% / 52.0%`（混合+重排）修订为 `75.0% / 52.1%`（纯向量新口径），避免口径自相矛盾

### Verified（验证）

| 项 | 数据 |
|---|---|
| Triage 准确率 | **98.75%**（80 条黄金用例；easy 65/65 + hard 14/15） |
| RAG Recall@10 | **75.0%**（纯向量，已弃用的混合+重排 73.8% 仅做对比展示） |
| RAG MRR@10 | **52.1%** |
| RAG nDCG@10 | **57.6%** |
| 混合检索 vs 纯向量 | 8 种 RRF 配置全部低于纯向量（最高 5:1 加权 nDCG 仍低 3.07pp） |
| 端到端 Mock 联调 | 淘宝链路 6/6 PASS（含 webhook / pending / approve / queue 清空） |
| 敏感信息泄漏 | 0（`.env` / `data/` / `.venv` 全部被 `.gitignore` 屏蔽） |

---

## [0.1.0] - 2026-09-06

项目初始化。从 OpenAI 官方 demo fork 后首次提交（`5e0b1c7 feat: 初始化 ai-cs-agent 项目`）。

### Added
- 基于 OpenAI Agents SDK 的多 Agent 架构
- FastAPI 后端（`main.py` + `server.py`）
- Next.js 15 + Tailwind 前端（`ui/`）
- SQLite 知识库（多租户 `tenant_id` 字段起步）
- 模拟商品/订单/优惠券/政策数据（`ecommerce/demo_data.py`）
- 文档导入（PDF / Word / TXT）

### Changed
- 业务场景从 OpenAI 原 fork 的 `airline/` 航空 demo 改为电商场景（`ecommerce/`）

[Unreleased]: https://github.com/li2589920489/ai-cs-agent/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/li2589920489/ai-cs-agent/releases/tag/v1.0.0
[0.1.0]: https://github.com/li2589920489/ai-cs-agent/releases/tag/v0.1.0
