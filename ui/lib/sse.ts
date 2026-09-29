/**
 * SSE 客户端 —— POST + ReadableStream 手工解帧。
 *
 * 为什么不用原生 EventSource：
 *   EventSource 只支持 GET，无法携带请求体。而聊天消息必须走 POST body ——
 *   中文进 URL 会被百分号编码膨胀、受长度限制，还会落到访问日志与浏览器历史里。
 *
 * 帧格式与后端 chat_service.run_chat_stream 一一对应
 * （见 SSE流式改造_原子任务_v1.md §4-D8 / §4-D15，共 5 类）：
 *   event: delta       data: {"text": "...", "mid": "..."}   mid = 本条助手消息的标识
 *   event: trace       data: {"type":"handoff"|"tool_call"|"tool_output", ...}
 *   event: escalation  data: {"flag": true, "reason": "..."}
 *   event: error       data: {"msg": "...", "retryable": bool}
 *   event: done        data: {"reply": "...", "session_id": "...", "final_agent": "..."}
 */

/** delta 帧的载荷。`mid` 缺失或为 null 时按「同一条消息」处理（向后兼容旧后端） */
export type DeltaFrame = {
  text: string;
  mid?: string | null;
};

/** trace 帧的载荷（后端只发这三种 type） */
export type TraceFrame = {
  type: "handoff" | "tool_call" | "tool_output";
  /** 发起方 Agent 名；handoff 时为「来源」 */
  agent: string;
  /** 仅 handoff：目标 Agent 名 */
  to?: string | null;
  /** 仅 tool_call：工具名 */
  tool?: string;
};

export type SseFrame = { event: string; data: any };

/** 一次流式调用的结果，供调用方判断是否需要降级到非流式接口 */
export type StreamOutcome = {
  /** 是否收到过任意帧（用于区分「连接成功但无内容」与「根本没连上」） */
  receivedAny: boolean;
  /** 是否已渲染过文本 —— 决定还能不能安全降级重试（已出字再重试会导致内容重复） */
  sawDelta: boolean;
};

/** /api/chat/stream 在建立流之前返回的 HTTP 错误（429 限流 / 422 参数） */
export class SseHttpError extends Error {
  status: number;
  body: string;
  constructor(status: number, body: string) {
    super(`SSE 请求失败（HTTP ${status}）`);
    this.name = "SseHttpError";
    this.status = status;
    this.body = body;
  }
}

/**
 * 解析单个 SSE 帧块（已由 \n\n 切分）。
 *
 * 兼容点：
 * - 以 `:` 开头的是注释行（SSE 心跳常用），忽略；
 * - `data:` 后可能有一个空格（规范允许），要去掉；
 * - JSON 解析失败不抛错，包成 {raw} 交给上层，避免一帧异常打断整条流。
 */
export function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).replace(/^ /, ""));
    }
  }

  if (dataLines.length === 0) return null;

  const text = dataLines.join("\n");
  try {
    return { event, data: JSON.parse(text) };
  } catch {
    return { event, data: { raw: text } };
  }
}

/**
 * 从累积缓冲中切出所有完整帧，返回帧列表与剩余不完整缓冲。
 *
 * 必须缓冲累积：网络分片不保证按帧边界到达，一个长 JSON 可能被切成两半
 * （本地实测：首个 trace 帧与 delta 帧经常落在同一 chunk 里，末尾也会截断）。
 */
export function drainFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = [];
  let rest = buffer.replace(/\r\n/g, "\n");

  let idx: number;
  while ((idx = rest.indexOf("\n\n")) !== -1) {
    const block = rest.slice(0, idx);
    rest = rest.slice(idx + 2);
    const frame = parseFrame(block);
    if (frame) frames.push(frame);
  }
  return { frames, rest };
}

/**
 * 发起流式聊天请求，逐帧回调。
 *
 * 调用方契约：
 * - `onFrame` 里做渲染；抛出的异常会中断整条流（所以不要让渲染错误冒泡）。
 * - 本函数只在「HTTP 层失败」时抛错；协议内的 error 帧会作为普通帧交给 onFrame，
 *   由调用方决定是否降级（D9：仅首 token 之前允许重试）。
 */
export async function streamChat(
  body: { message: string; session_id: string | null },
  onFrame: (frame: SseFrame) => void,
  opts: { signal?: AbortSignal; url?: string } = {},
): Promise<StreamOutcome> {
  const url = opts.url ?? "/api/chat/stream";

  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
    signal: opts.signal,
  });

  if (!res.ok) {
    // 429 / 422 在建立流之前返回 JSON，与 /api/chat 的判错路径一致
    const text = await res.text().catch(() => "");
    throw new SseHttpError(res.status, text);
  }
  if (!res.body) {
    throw new SseHttpError(res.status, "响应无 body，浏览器可能不支持流式读取");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const outcome: StreamOutcome = { receivedAny: false, sawDelta: false };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = drainFrames(buffer);
      buffer = rest;

      for (const frame of frames) {
        if (!outcome.receivedAny) outcome.receivedAny = true;
        if (frame.event === "delta") outcome.sawDelta = true;
        onFrame(frame);
      }
    }

    // 收流后缓冲里若还有无分隔符的尾块，兜底解析一次
    const tail = parseFrame(buffer);
    if (tail) onFrame(tail);
  } finally {
    reader.releaseLock();
  }

  return outcome;
}
