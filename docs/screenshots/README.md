# 演示截图索引

13 张截图按演示流程排列，每张对应一个核心场景。

| # | 截图 | 场景说明 | 对应演示路径 |
|---|---|---|---|
| 1 | `demo_00_开场.png` | 演示开始：买家首次咨询 | `买家 → 提问 → Triage 分流` |
| 2 | `demo_01_订单分流.png` | Triage 识别"订单"意图，分流到订单详情 Agent | `订单咨询 → order_status_tool` |
| 3 | `demo_02_商品分流.png` | Triage 识别"商品"意图，分流到商品知识 Agent | `商品咨询 → search_products_tool` |
| 4 | `demo_03_退货政策溯源.png` | RAG 检索命中退货政策，回复携带来源 chunk | `政策问答 → knowledge_search_tool` |
| 5 | `demo_03b_政策RAG检索.png` | RAG 检索过程：候选 chunk 评分与排序 | `RAG 链路透明化` |
| 6 | `demo_04_转人工生成工单.png` | 复杂诉求触发 Human Escalation，自动生成工单 | `转人工 → escalate_to_human_tool` |
| 7 | `demo_05_坐席接管回复.png` | 坐席工作台接管，客服输入回复 | `坐席后台` |
| 8 | `demo_05b_买家收到坐席回复.png` | 买家侧收到坐席回复 | `买家 → 收到坐席回复` |
| 9 | `demo_06_安全护栏拒绝.png` | 输入 Guardrail 拦截越狱/无关问题 | `安全兜底` |
| 10 | `demo_07_知识库导入前.png` | 知识库 CRUD 管理后台空状态 | `知识库管理` |
| 11 | `demo_08_知识库导入后.png` | 知识库导入新文档后状态 | `知识库管理 → 导入` |
| 12 | `frontend-home.png` | Next.js 前端首页（Agent 监控面板） | `前端 → 监控面板` |
| 13 | `frontend-chat-success.png` | Next.js 前端对话成功状态 | `前端 → 买家咨询` |