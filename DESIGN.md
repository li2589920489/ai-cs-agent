# AI 电商智能客服助手 — 详细设计方案

> 版本：v1.1 | 2026-09-17 | 基于 openai-cs-agents-demo 改造
> 关联文档：《需求分析文档.md》v1.0、《技术方案文档.md》v1.0、`AGENTS.md`

---

## 一、产品定位

一个面向淘宝风格电商场景的**多 Agent 协作智能客服系统**。6 个专业 AI Agent 各司其职，通过 handoff 接力机制协同处理用户请求。常规问题由 AI 自主解决，复杂/敏感问题自动转人工。

### 核心价值
- **对用户**：7×24 即时响应，订单、商品、售后一站式解决
- **对商家**：减少重复性客服工作量，人工只需处理复杂案例
- **对开发者**：体现多 Agent 架构、RAG、Human-in-the-Loop、生产级工程能力

---

## 二、系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    前端 (Next.js 15)                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ Agent 监控面板 │  │ 客户咨询界面  │  │ 坐席工作台    │  │
│  │  - Agent卡片  │  │  - 对话窗口  │  │  - 待处理列表 │  │
│  │  - 运行日志   │  │  - 快捷指令  │  │  - 一键接管   │  │
│  │  - 安全护栏   │  │  - 会话保持  │  │  - 对话记录   │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
├─────────────────────────────────────────────────────────┤
│                  后端 (FastAPI + Uvicorn)                │
│  ┌──────────────────────────────────────────────────┐   │
│  │   /api/chat  REST 接口（鉴权 + 限流 + 会话管理）  │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │       6 Agent 智能体 (OpenAI Agents SDK)          │   │
│  │   Triage → 订单详情/商品知识/售后退换货/店铺政策   │   │
│  │   → Human Escalation                             │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │        RAG 管线（BGE 向量化 + numpy 检索 + 重排）  │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │              数据层                               │   │
│  │   SQLite 知识库 | numpy 向量后端 | 租户凭据 | 内存会话/工单 │   │
│  └──────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────┤
│                   LLM (DeepSeek API)                    │
└─────────────────────────────────────────────────────────┘
```

---

## 三、Agent 体系

### 3.1 Agent 总览

| Agent | 职能 | 工具数 | Handoff 目标 |
|-------|------|--------|-------------|
| Triage Agent | 意图识别 + 路由分发 | 0 | 其余 5 个 |
| 订单详情 Agent | 订单状态 + 物流轨迹 + 历史订单 + 物流异常 | 3 | 售后退换货, Triage |
| 商品知识 Agent | 商品搜索 + 详情 + 库存 + 知识库检索 | 4 | 店铺政策, Triage |
| 售后退换货 Agent | 退货/退款/取消 + 自动转人工 | 4 | 店铺政策, 人工, Triage |
| 店铺政策 Agent | 政策问答 + 优惠券（RAG 增强） | 3 | 人工, Triage |
| Human Escalation Agent | 生成对话摘要 + 标记转接 | 1 | Triage |

> 业务工具去重后共 **13 个 function_tool**（`tools.py`），另有 2 个 Guardrail verdict 工具和 4 个 MCP 工具。

### 3.2 Agent 详细设计

#### Triage Agent（分流中枢）
- **定位**：不做任何业务处理，仅做意图分类
- **路由规则**：
  - 订单/物流 → 订单详情 Agent
  - 商品/搜索 → 商品知识 Agent
  - 退货/退款 → 售后退换货 Agent
  - 优惠券/促销/政策/会员 → 店铺政策 Agent
  - 投诉/转人工/索赔/12315 → Human Escalation Agent
- **复合意图路由**：一句话含多领域诉求时按**主导诉求**路由，接收方处理完整诉求（不再二次 handoff）

#### 订单详情 Agent（订单物流）
- **工具**：`order_status_tool` / `list_customer_orders_tool` / `tracking_lookup_tool`
- **上下文**：写入 `order_number`, `customer_name`, `account_id`, `order_status`, `order_items`
- **降级策略**：物流异常 → 建议转售后；物流超 48h 未更新主动提示核查
- **硬约束**：不复述用户消息中无法核实的虚构前提

#### 商品知识 Agent（商品咨询）
- **工具**：`knowledge_search_tool` / `product_info_tool` / `search_products_tool` / `inventory_check_tool`
- **上下文**：写入 `product_id`, `product_name`, `selected_sku`
- **硬约束**：禁止编造商品信息，工具返回为空时明确告知「知识库暂未收录」

#### 售后退换货 Agent（售后处理）
- **工具**：`initiate_return_tool` / `cancel_order_tool` / `faq_lookup_tool` / `escalate_to_human_tool`
- **自动转人工条件**：食品类已签收退货、超 7 天退货、金额争议
- **上下文**：写入 `return_case_id`, `return_status`, `escalation_flag`

#### 店铺政策 Agent（政策问答 + 优惠券）
- **工具**：`faq_lookup_tool`（RAG 优先 + 规则兜底）/ `rag_search_tool`（纯 RAG）/ `coupon_lookup_tool`
- **知识库**：退货政策、退款时效、发货时效、物流查询、优惠券规则、会员权益、常见售后
- **数据**：4 张优惠券（NEW10/FULL200/FULL500/FULL1000）

#### Human Escalation Agent
- **工具**：`escalate_to_human_tool`
- **输出**：结构化摘要包含转接原因、关联订单号、客户名、退货单号、涉及商品
- **硬约束**：调工具后不再生成额外文本（避免重复回复骚扰）

### 3.3 Handoff 流转机制

```
Triage → [订单详情 | 商品知识 | 售后退换货 | 店铺政策 | Human Escalation]
订单详情 → 售后退换货 → Human Escalation
商品知识 → 店铺政策 → Human Escalation
售后退换货 → [店铺政策 | Human Escalation]
店铺政策 → Human Escalation
Human Escalation → Triage (返回)
```

- 每次 handoff 携带完整 `ECommerceAgentContext`
- 接收方无需重复询问已获取的信息
- 循环上限：`max_turns=10`
- Handoff 工具名统一英文 `tool_name_override`（避免中文名清洗后冲突）

---

## 四、数据模型

### 4.1 Agent 共享上下文（17 字段）

| 字段 | 类型 | 含义 | 写入方 |
|------|------|------|--------|
| `customer_name` | str? | 客户名 | 订单详情/售后退换货 |
| `account_id` | str? | 账户 ID | 订单详情/店铺政策 |
| `order_number` | str? | 订单号 | 订单详情/售后退换货 |
| `order_status` | str? | 订单状态 | 订单详情 |
| `tracking_number` | str? | 运单号 | 订单详情 |
| `order_items` | list? | 订单商品列表 | 订单详情 |
| `product_id` | str? | 商品 ID | 商品知识 |
| `product_name` | str? | 商品名 | 商品知识 |
| `selected_sku` | str? | 选中规格 | 商品知识 |
| `return_case_id` | str? | 退货单号 | 售后退换货 |
| `return_status` | str? | 退货状态 | 售后退换货 |
| `refund_amount` | str? | 退款金额 | 售后退换货 |
| `available_coupons` | list? | 可用优惠券 | 店铺政策 |
| `applied_coupon` | str? | 已用优惠券 | 店铺政策 |
| `escalation_flag` | bool | 需转人工 | 售后退换货/人工 |
| `escalation_reason` | str? | 转接原因 | 售后退换货/人工 |
| `internal_notes` | str? | 内部备注（不展示前端） | 内部 |

### 4.2 数据

| 数据 | 数量 | 说明 |
|------|------|------|
| 模拟商品 | 4 件 | 坚果 ×2、加湿器、抽纸，含价格/规格/库存/评价 |
| 模拟订单 | 3 笔 | 已发货/已签收×2，含完整物流信息 |
| 优惠券 | 4 张 | 新用户/满 200/满 500/满 1000 |
| 店铺政策 | 7 条 | 退货/退款/发货/物流/优惠券/会员/售后 |
| **真实语料** | **1328 chunks** | 1305 商品 + 7 政策（商品侧含 FAQ 拆分共 1321 chunk），经 `import_real_products.py` 导入 |

### 4.3 会话管理

- 前端通过 `session_id` 参数保持对话上下文
- 后端维护 `_sessions` 字典，30 分钟 TTL（`chat_service.py`）
- 每次请求自动清理过期会话
- 生产环境应替换为 Redis

---

## 五、接口设计

### 5.1 POST /api/chat
```
请求：{ "message": "查订单TB20260812001", "session_id": "abc123" }
响应：
{
  "reply": "订单状态...",
  "agent_trace": [{ "type": "handoff", "from": "Triage Agent", ... }],
  "final_agent": "订单详情 Agent",
  "session_id": "abc123"
}
```
- 输入校验：空消息拦截，超 1000 字拦截
- 频率限制：内存滑动窗口 60s/30 次
- 错误处理：ModelBehaviorError / MaxTurnsExceeded / 通用异常
- Agent 循环上限：10 轮

### 5.2 GET /api/agents
```
响应：{ "agents": [...], "current_agent": "Triage Agent", "context": {} }
```
- Guardrail 名称已映射为中文

### 5.3 GET /health
```
响应：{ "status": "healthy", "service": "AI电商客服智能体", "agents": "6" }
```

### 5.4 知识库 / 工单接口（鉴权）

见《技术方案文档.md》第四章——知识库接口走 `get_current_tenant`（租户鉴权），工单接口走 `require_agent_role`（坐席角色）。

---

## 六、安全设计

### 6.1 输入护栏（2 个 input_guardrail）

- **内容相关性检测**（Relevance Guardrail）：非电商话题自动拦截
- **越狱攻击检测**（Jailbreak Guardrail）：提取系统 prompt、SQL 注入、代码注入自动拦截
- 每个 Agent 均配置这 2 个 guardrail
- 实现方式：function-tool 提交结论（规避 DeepSeek 对 output_type JSON Schema 结构化输出的不稳定）

### 6.2 接口安全

- 租户鉴权：api_key sha256 哈希 + `get_current_tenant`（`auth.py`）
- 坐席鉴权：`require_agent_role`（工单操作限定 role='agent'）
- CORS：环境变量 `FRONTEND_URL` 控制允许域
- 输入校验：消息长度限制 1000 字
- 频率限制：内存滑动窗口 60s/30 次
- Agent 循环上限：`max_turns=10`

### 6.3 当前缺口

- ⚠️ API Key 为静态（无轮换/过期机制）
- ⚠️ 无请求日志持久化
- ⚠️ 频率限制为内存态（多实例不共享，生产需 Redis）

---

## 七、前端设计

### 7.1 布局
```
┌──────────────┬───────────────────────────┐
│  35% 宽度     │         65% 宽度           │
│ ──────────── │ ───────────────────────── │
│ Agent 监控    │                           │
│  - 6 Agent   │     客户咨询界面           │
│  - 运行日志   │     - 欢迎语 + 快捷指令    │
│  - 安全护栏   │     - 对话气泡            │
│  - 上下文     │     - 输入框 + 发送按钮    │
└──────────────┴───────────────────────────┘
```
- 三栏 keep-mounted 布局（Agent 监控 / 客户咨询 / 坐席工作台）
- 主题色：橙色 (#f97316)，贴近淘宝视觉
- Agent 卡片：当前活跃高亮 + "运行中"徽章

### 7.2 技术栈
- Next.js 15 + React + TailwindCSS + shadcn/ui
- lucide-react 图标库
- 无第三方 Chat UI 依赖（自行实现聊天组件）

### 7.3 当前缺口
- ⚠️ Agent 运行日志未实时同步（需刷新页面才能看到新日志）
- ⚠️ 上下文面板数据更新滞后
- ⚠️ 无移动端适配

---

## 八、部署架构

```
开发环境：
  后端: uvicorn main:app --host 0.0.0.0 --port 8000
  前端: npx next start --port 3000
  Next.js 代理: /api/* → http://127.0.0.1:8000/api/*

容器化（已实现）：
  Dockerfile（CPU torch + 国内源，镜像 6GB → 2.6GB）
  docker-compose.yml（前端 + 后端 + 模型卷挂载）

生产环境（规划，未上线）：
  前端: Nginx 静态文件 + 反向代理
  后端: Gunicorn + Uvicorn workers
  会话/限流: Redis
  数据库: PostgreSQL
```

---

## 九、已知问题 & Roadmap

### 当前限制

| 优先级 | 问题 | 方案 |
|--------|------|------|
| P1 | Agent 日志不实时同步 | 前端加 SSE /api/chat 的变化 |
| P1 | API Key 静态无轮换 | 接入正式身份体系（OAuth/JWT） |
| P2 | 上下文面板数据滞后 | 在 `/api/chat` 响应中携带最新上下文 |
| P2 | 无 Token 用量统计 | 响应中加 `token_usage` 字段 |
| P2 | 频率限制内存态 | 生产迁 Redis 令牌桶 |
| P3 | 会话/工单内存态丢数据 | 生产迁 Redis + PostgreSQL |
| P3 | 无 A/B 评估框架 | 加测试集 + 准确率对比 |
| P3 | 无请求日志持久化 | 加 trace 落库 |

### 已解决

| 问题 | 解决方案 |
|------|---------|
| 无人工接管后台 UI | 新增坐席工作台 `agent-workspace.tsx`（248 行） |
| 无频率限制 | 内存滑动窗口 60s/30 次 |
| 零鉴权 | `auth.py`（api_key sha256 + 租户/坐席两档角色） |
| ChromaDB hnsw 崩溃 | 自建 numpy 向量后端（接口兼容） |
| URL 硬编码 localhost:8000 | 改用相对路径 + Next.js 代理 |
| ChatKit CDN 被拦截 | 自行实现聊天组件 |
| CORS 锁死 | 环境变量控制 |
| 无会话状态 | session_id + _sessions 字典 + 30min TTL |
| Agent 无限循环 | max_turns=10 |
| Guardrails 英文显示 | 映射为中文名 |
| 死代码 chatkit 端点 | 全部移除 |

---

## 十、技术亮点

1. **6 Agent 多智能体协作**：Triage 分流 + Handoff 接力，非简单串行
2. **Human-in-the-Loop**：复杂/敏感场景自动标记转人工，生成结构化摘要
3. **数据驱动选型**：用 EcomRetrieval 基准实测推翻「混合检索更优」，锁定纯向量 + 重排（R@10 75.0%）
4. **生产级容错**：max_turns 循环上限、ModelBehaviorError 降级、会话过期清理
5. **安全护栏**：2 个 input_guardrail（相关性 + 越狱），每个 Agent 独立配置
6. **自研向量后端**：ChromaDB hnsw 崩溃后自建 numpy 后端，规避供应商锁定
7. **模型无关**：DeepSeek API 可平滑切换 GPT/Claude

---

## 十一、项目结构

```
ai-cs-agent/
├── python-backend/
│   ├── main.py                  # FastAPI 入口 + /api/chat + /api/agents + /health
│   ├── auth.py                  # 租户鉴权（api_key sha256 + 租户/坐席角色）
│   ├── chat_service.py          # 共享 run_chat()：消息→Triage→Agent→回复+trace
│   ├── knowledge_store.py       # SQLite 知识库（多租户 tenant_id 过滤）
│   ├── doc_importer.py          # PDF/Word/TXT 文档导入 + LLM 提取
│   ├── memory_store.py          # ChatKit 会话存储实现
│   ├── mcp_server.py            # FastMCP Server（4 个 MCP 工具）
│   ├── after_sales_graph.py     # LangGraph 售后状态机（独立原型，未接入主链路）
│   ├── eval_routing.py          # Triage 路由评测（80 条）
│   ├── eval_retrieval.py        # RAG 检索评测
│   ├── eval_rrf_tuning.py       # RRF 调参实验（推翻混合检索）
│   ├── download_models.py       # BGE 模型下载（本地化）
│   ├── restore_models.py        # 从 HF 缓存恢复 BGE 模型
│   ├── taobao_adapter.py        # 淘宝/天猫客服消息适配层（OAuth + AES + 签名）
│   ├── taobao_webhook.py        # 淘宝消息推送 FastAPI 服务（8002）
│   ├── mock_taobao.py           # 淘宝消息推送 Mock
│   ├── rag/                     # 完整 RAG 包
│   │   ├── chunking.py          #   语义分块
│   │   ├── embedding.py         #   BGE 向量化（可降级）
│   │   ├── bm25.py              #   BM25 关键词检索（兜底）
│   │   ├── numpy_store.py       #   numpy 向量后端（sqlite + cosine）
│   │   ├── vector_store.py      #   兼容层（封装 numpy_store）
│   │   ├── reranker.py          #   bge-reranker 精排
│   │   ├── pipeline.py          #   检索管线（默认纯向量 + 重排）
│   │   └── indexer.py           #   索引构建（backend 自动重建）
│   ├── ecommerce/
│   │   ├── agents.py            # 6 Agent 定义 + handoff 关系
│   │   ├── context.py           # ECommerceAgentContext（17 字段）
│   │   ├── tools.py             # 13 个业务工具函数（含 RAG 接入）
│   │   ├── demo_data.py         # 模拟商品/订单/物流/优惠券/政策
│   │   └── guardrails.py        # 2 个 input_guardrail（function-tool 实现）
│   ├── tests/
│   │   └── test_smoke.py        # smoke 测试（health + 鉴权 + Triage/RAG）
│   ├── data/
│   │   ├── models/              # BGE 模型（本地，bind mount 挂载）
│   │   ├── chroma_rag/          # 向量数据目录（numpy 后端，目录名保留历史）
│   │   ├── eval/                # 评测数据集 + 结果 JSON
│   │   ├── auth.db              # 租户凭据（api_key 哈希）
│   │   └── knowledge.db         # SQLite 知识库
│   ├── .env                     # API Key 配置
│   ├── Dockerfile               # 容器化（CPU torch + 国内源）
│   ├── requirements.txt         # 运行依赖
│   └── requirements-dev.txt     # 测试依赖（pytest + httpx）
├── ui/
│   ├── app/
│   │   ├── page.tsx             # 主页面：双面板布局
│   │   ├── layout.tsx           # 根布局
│   │   └── globals.css          # 全局样式
│   ├── components/
│   │   ├── chat-panel.tsx       # 自研聊天组件
│   │   ├── agent-panel.tsx      # Agent 监控面板
│   │   ├── agents-list.tsx      # Agent 卡片网格
│   │   ├── agent-workspace.tsx  # 坐席工作台（人工接管）
│   │   ├── knowledge-panel.tsx  # 知识库管理后台
│   │   ├── runner-output.tsx    # 运行日志
│   │   ├── conversation-context.tsx  # 上下文显示
│   │   ├── guardrails.tsx       # 安全护栏面板
│   │   └── ui/                  # shadcn/ui 组件
│   ├── lib/
│   │   ├── api.ts               # API 调用封装
│   │   ├── types.ts             # TypeScript 类型
│   │   └── utils.ts             # 工具函数
│   ├── next.config.mjs          # Next.js 配置（/api/* 代理）
│   └── package.json
├── docs/
│   ├── screenshots/             # 核心流程截图
│   ├── 真实query评测报告.md     # 真实语料评测
│   ├── 真实商品导入工程.md      # 1305 商品导入过程
│   └── prompt_对抗性评测.md     # Prompt 评测
├── .github/workflows/ci.yml     # CI（smoke 测试）
├── docker-compose.yml           # 一键编排 + 模型卷挂载
├── 抖音接入方案.md              # 抖店开放平台接入设计 + Mock 联调
├── 淘宝接入方案.md              # 淘宝 TOP API 接入设计 + Mock 联调
├── DESIGN.md                    # 本文件
├── 技术方案文档.md              # 技术实现方案
├── 需求分析文档.md              # 业务需求分析
├── AGENTS.md                    # 编码规则（事实红线）
├── 分流评测报告.md              # Triage 98.75% 评测过程
├── RAG检索评测报告.md           # RAG 检索评测过程
├── README.md                    # 项目门面
└── LICENSE                      # MIT License（基于 openai/openai-cs-agents-demo）
```
