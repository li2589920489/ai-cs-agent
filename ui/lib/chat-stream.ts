/**
 * 流式帧 → UI 语义的翻译层（纯函数，无 React 依赖）。
 *
 * 单独成模块的原因：
 * 1) 这里的对照表必须与后端逐字对齐（工具名 13 个、Agent 名 6 个），写错不报错、
 *    只会静默退化成「正在查询…」，是最需要被测试覆盖的一环；
 * 2) 抽成纯函数后可在 Node 里直接断言，不依赖浏览器或 next build。
 */

import type { TraceFrame } from "./sse";

/** 工具名 → 进度文案。键取自后端 ecommerce/tools.py 的 13 个 @function_tool */
export const TOOL_PROGRESS: Record<string, string> = {
  search_products_tool: "正在检索商品…",
  knowledge_search_tool: "正在检索知识库…",
  product_info_tool: "正在查询商品详情…",
  inventory_check_tool: "正在核对库存…",
  rag_search_tool: "正在检索店铺资料…",
  order_status_tool: "正在查询订单状态…",
  list_customer_orders_tool: "正在拉取您的订单…",
  tracking_lookup_tool: "正在查询物流轨迹…",
  initiate_return_tool: "正在处理退货申请…",
  cancel_order_tool: "正在处理订单取消…",
  faq_lookup_tool: "正在查找常见问题…",
  coupon_lookup_tool: "正在查询优惠券…",
  escalate_to_human_tool: "正在为您转接人工客服…",
};

/** Agent 名 → 进度文案。键取自后端 ecommerce/agents.py 的 name= 字段 */
export const AGENT_PROGRESS: Record<string, string> = {
  "Triage Agent": "正在为您分诊…",
  "订单详情 Agent": "正在查询订单…",
  "商品知识 Agent": "正在查找商品信息…",
  "售后退换货 Agent": "正在处理售后问题…",
  "店铺政策 Agent": "正在查询店铺政策…",
  "Human Escalation Agent": "正在为您转接人工客服…",
};

/**
 * trace 帧 → 用户可见的进度文案。
 *
 * 这是本架构下流式收益的主要来源：链路是「分诊 → 交接 → 工具取数 → 生成文本」，
 * 实测首字在 2.0–2.9s（多跳架构决定，无法靠流式提前）。若不把中间的 trace 帧
 * 翻译成进度提示，用户在前 3 秒看到的仍是一个转圈图标，流式改造只剩「逐字出现」
 * 这点观感收益。
 */
export function progressFromTrace(t: TraceFrame): string {
  if (t.type === "handoff") {
    if (!t.to) return "正在处理…";
    return AGENT_PROGRESS[t.to] ?? `正在转接${t.to.replace(" Agent", "")}…`;
  }
  if (t.type === "tool_call") {
    return (t.tool && TOOL_PROGRESS[t.tool]) || "正在查询…";
  }
  return "正在整理结果…";
}

/**
 * 流式 trace 帧 → 左栏轨迹面板期望的形状。
 *
 * 两侧字段名不同：流式帧给 `agent`（来源）+ `to`（目标），而轨迹面板读 `from`/`to`
 * （见 app/page.tsx 的 handleAgentTrace）。不转换的话，交接那条事件会渲染成
 * 「undefined → 商品知识 Agent」。
 */
export function normalizeTrace(t: TraceFrame) {
  if (t.type === "handoff") {
    return { type: "handoff", from: t.agent, to: t.to ?? "" };
  }
  if (t.type === "tool_call") {
    return { type: "tool_call", agent: t.agent, tool: t.tool ?? "" };
  }
  return { type: "tool_output", agent: t.agent };
}

/**
 * escalation 帧 → EscalationPanel 期望的载荷。
 *
 * /api/chat（非流式）返回的 `escalation` 是 bool，而面板读 `escalation.flag`；
 * 流式帧给的是 `{flag, reason}` 对象。这里统一收口，保证降级前后行为一致。
 */
export function normalizeEscalation(
  esc: unknown,
  reasonFromRest?: string,
): { flag: boolean; reason: string } | null {
  if (!esc) return null;
  if (typeof esc === "object") {
    const o = esc as { flag?: boolean; reason?: string };
    return { flag: !!o.flag, reason: o.reason ?? "" };
  }
  return { flag: !!esc, reason: reasonFromRest ?? "" };
}
