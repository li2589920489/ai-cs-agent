"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Send, Loader2, User, Bot, AlertCircle } from "lucide-react";
import { streamChat, type DeltaFrame, type TraceFrame } from "@/lib/sse";
import { progressFromTrace, normalizeTrace, normalizeEscalation } from "@/lib/chat-stream";

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
};

type Props = {
  onAgentTrace?: (trace: any[]) => void;
  onEscalation?: (info: any) => void;
};

const QUICK_PROMPTS = [
  "查订单TB20260812001",
  "有什么坚果推荐",
  "现在有什么优惠券",
  "我想退货TB20260812001",
];

// 进度文案与轨迹字段的映射统一放在 lib/chat-stream.ts（纯函数，可在 Node 里断言），
// 见该文件头部的说明。

export function ChatPanel({ onAgentTrace, onEscalation }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [manualMode, setManualMode] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  // 首个 delta 到达后置位：此时改用消息气泡展示，隐藏「转圈 + 进度」占位
  const [streamingMsgId, setStreamingMsgId] = useState<string | null>(null);
  const manualCountRef = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, progress]);

  // 组件卸载时中断在途流，避免对已卸载组件 setState
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  // 人工接管模式下轮询坐席回复
  useEffect(() => {
    if (!manualMode || !sessionId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch(`/api/chat/poll?session_id=${sessionId}`);
        const data = await res.json();
        if (cancelled) return;
        if (data.manual && data.messages) {
          const newMsgs = data.messages.slice(manualCountRef.current);
          if (newMsgs.length > 0) {
            setMessages((prev) => [
              ...prev,
              ...newMsgs.map((m: any) => ({
                id: `agent-${m.time}-${Math.random().toString(36).slice(2, 8)}`,
                role: "assistant" as const,
                content: m.content,
                timestamp: new Date(),
              })),
            ]);
            manualCountRef.current = data.messages.length;
          }
          if (data.ticket_status === "closed") {
            setManualMode(false);
            setSessionId(null);
            manualCountRef.current = 0;
          }
        } else {
          // 工单关闭或无人工会话：退出接管模式并清 session，后续消息走新会话
          setManualMode(false);
          setSessionId(null);
          manualCountRef.current = 0;
        }
      } catch {
        // 轮询失败忽略
      }
    };
    poll();
    const timer = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [manualMode, sessionId]);

  /** 把非流式 /api/chat 的返回套用到界面（降级路径与人工接管共用） */
  const applyRestResult = useCallback(
    (data: any) => {
      setMessages((prev) => [
        ...prev,
        {
          id: `rest-${Date.now()}`,
          role: "assistant",
          content: data.reply || "(Agent未返回回复)",
          timestamp: new Date(),
        },
      ]);
      if (data.session_id) setSessionId(data.session_id);
      if (data.agent_trace && onAgentTrace) onAgentTrace(data.agent_trace);

      // /api/chat 返回的 escalation 是 bool，而面板期望 {flag, reason} 对象；
      // 流式帧给的是对象。这里统一收口，两条路径行为一致。
      const esc = data.escalation;
      if (esc && onEscalation) {
        onEscalation(
          typeof esc === "object" ? esc : { flag: !!esc, reason: data.escalation_reason || "" },
        );
      }
      if (data.manual) {
        setManualMode(true);
        manualCountRef.current = 0;
      }
    },
    [onAgentTrace, onEscalation],
  );

  /** 降级：走原有非流式接口。返回是否成功 */
  const fallbackToRest = useCallback(
    async (text: string): Promise<boolean> => {
      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, session_id: sessionId }),
        });
        if (!res.ok) {
          const errText = await res.text();
          throw new Error(errText || `服务器错误 ${res.status}`);
        }
        applyRestResult(await res.json());
        return true;
      } catch (err: any) {
        setError(err.message || "发送失败，请重试");
        return false;
      }
    },
    [sessionId, applyRestResult],
  );

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || loading) return;

    // 人工接管模式下，消息仍需提交到后端，由后端写入工单消息列表，
    // 否则坐席工作台看不到买家的后续消息；后端 /api/chat 的 manual session
    // 分支会返回硬编码提示，前端不再展示它，避免每次输入都重复提示。
    if (manualMode) {
      const sentAt = Date.now();
      setMessages((prev) => [
        ...prev,
        { id: sentAt.toString(), role: "user", content: text, timestamp: new Date() },
      ]);
      setInput("");
      setLoading(true);
      setError(null);
      try {
        await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, session_id: sessionId }),
        });
        // 不展示后端返回的 hardcoded「您已接入人工客服…」提示
      } catch (err: any) {
        setError(err.message || "发送失败，请重试");
      } finally {
        setLoading(false);
      }
      return;
    }

    const userMsg: Message = {
      id: Date.now().toString(),
      role: "user",
      content: text,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);
    setError(null);
    setProgress("正在理解您的问题…");
    setStreamingMsgId(null);

    const controller = new AbortController();
    abortRef.current = controller;

    // 助手消息在首个 delta 到达时才落地，避免出现「空气泡」
    const assistantId = (Date.now() + 1).toString();
    let created = false;
    // 当前正在渲染的助手消息标识。后端每条助手消息带独立的 mid；
    // mid 变化 = 模型开了新的一条消息。初值 undefined 与「旧后端不带 mid」等价，
    // 此时永远不会判定为换消息 → 退化为纯追加（向后兼容）。
    let currentMid: unknown = undefined;

    /**
     * 写入一段 delta。
     *
     * mid 变化意味着上一条是「调工具前的前言」—— 一次 run 里模型可能先吐一句
     * 「I'll route you to our specialist…」再调工具，最后才给正式答复。非流式
     * `run_chat` 对 MessageOutputItem 是**覆盖**语义（只留最后一条），流式若一律
     * 追加就会把英文前言泄漏给用户（实测 4 组查询 3 组复现）。故此处按
     * 「新消息替换、同消息追加」处理，与非流式口径对齐（§4-D15）。
     */
    const pushDelta = (chunk: string, mid: unknown) => {
      if (!created) {
        created = true;
        currentMid = mid;
        setStreamingMsgId(assistantId);
        setMessages((prev) => [
          ...prev,
          { id: assistantId, role: "assistant", content: chunk, timestamp: new Date() },
        ]);
        return;
      }
      if (mid !== currentMid) {
        currentMid = mid;
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, content: chunk } : m)),
        );
        return;
      }
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, content: m.content + chunk } : m)),
      );
    };

    /** 把最终答复落到气泡上（收流时校订；若一个字都没流出则直接落地） */
    const applyFinalReply = (reply: string) => {
      if (created) {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, content: reply } : m)),
        );
      } else {
        created = true;
        setStreamingMsgId(assistantId);
        setMessages((prev) => [
          ...prev,
          { id: assistantId, role: "assistant", content: reply, timestamp: new Date() },
        ]);
      }
    };

    let sawDelta = false;
    let sawEscalation = false;
    let escReason = "";
    // 协议内 error 帧分两类：retryable 交给降级兜底，非 retryable 直接给用户看
    let fatalError: string | null = null;

    try {
      await streamChat(
        { message: text, session_id: sessionId },
        (frame) => {
          switch (frame.event) {
            case "delta": {
              const d = frame.data as DeltaFrame | undefined;
              const chunk = String(d?.text ?? "");
              if (chunk) {
                sawDelta = true;
                pushDelta(chunk, d?.mid);
              }
              break;
            }
            case "trace": {
              const t = frame.data as TraceFrame;
              setProgress(progressFromTrace(t));
              // 逐帧上报，左栏轨迹与流同步增长（轨迹面板是追加语义，不会重复）
              onAgentTrace?.([normalizeTrace(t)]);
              break;
            }
            case "escalation": {
              sawEscalation = true;
              escReason = frame.data?.reason || "";
              break;
            }
            case "error": {
              if (frame.data?.retryable) {
                // 后端已重试 3 次仍失败：留给下面的降级兜底，不在这里报错
                fatalError = null;
              } else {
                fatalError = frame.data?.msg || "生成失败，请重试";
              }
              break;
            }
            case "done": {
              if (frame.data?.session_id) setSessionId(frame.data.session_id);
              // 兜底校订：done 携带的 reply 与非流式 /api/chat 同源（最后一条助手消息），
              // 用它收敛展示文本，即使中间前言仍流了出来也不会残留（§4-D15）
              const finalReply = frame.data?.reply;
              if (typeof finalReply === "string" && finalReply) {
                applyFinalReply(finalReply);
              }
              break;
            }
          }
        },
        { signal: controller.signal },
      );

      // 一个字都没渲染出来 → 降级到非流式接口兜底。
      // 覆盖两种情形：① 后端重试 3 次仍报 retryable 错；② 流正常结束但无文本。
      // 已出字则绝不降级 —— 重试会让用户看到重复内容（D9：仅首 token 前可重试）。
      if (!created) {
        if (fatalError) {
          setError(fatalError);
        } else {
          await fallbackToRest(text);
        }
      }

      // 转人工：流式的 escalation 帧等价于 /api/chat 的 manual 字段
      if (sawEscalation) {
        setManualMode(true);
        manualCountRef.current = 0;
        onEscalation?.({ flag: true, reason: escReason });
      }
    } catch (err: any) {
      // HTTP 层失败（429 / 422 / 网络中断）→ 降级到非流式接口
      if (err?.name === "AbortError") return;
      if (!sawDelta) {
        await fallbackToRest(text);
      } else {
        setError(err?.message || "连接中断，请重试");
      }
    } finally {
      abortRef.current = null;
      setLoading(false);
      setProgress(null);
      setStreamingMsgId(null);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <div className="flex flex-col h-full flex-1 bg-white rounded-xl shadow-sm border border-gray-200">
      {/* 标题栏 */}
      <div className="bg-orange-500 text-white h-12 px-4 flex items-center rounded-t-xl">
        <h2 className="font-semibold text-sm">客户咨询</h2>
        <div className="ml-auto flex items-center gap-2">
          {manualMode ? (
            <span className="text-xs bg-white/20 px-2 py-1 rounded-full flex items-center gap-1">
              <User className="h-3 w-3" /> 人工接管中
            </span>
          ) : (
            <button
              type="button"
              onClick={() => {
                if (loading) return;
                setInput("转人工");
                // 直接触发与点击发送按钮等价的提交流程
                setTimeout(() => {
                  const syntheticEvent = { key: "Enter", shiftKey: false, preventDefault: () => {} } as React.KeyboardEvent;
                  handleKeyDown(syntheticEvent as any);
                }, 0);
              }}
              disabled={loading}
              className="text-xs bg-white/20 hover:bg-white/30 px-3 py-1 rounded-full disabled:opacity-50 transition-colors flex items-center gap-1"
              title="一键发起转人工"
            >
              <User className="h-3 w-3" /> 转人工
            </button>
          )}
        </div>
      </div>

      {/* 消息区 */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-gray-400">
            <Bot className="h-12 w-12 mb-3 text-orange-300" />
            <p className="text-sm">你好！我是淘宝店铺AI客服小智</p>
            <p className="text-xs mt-1">有6个专业Agent随时为你服务</p>
            <div className="flex flex-wrap gap-2 mt-4 justify-center max-w-xs">
              {QUICK_PROMPTS.map((p) => (
                <button
                  key={p}
                  onClick={() => { setInput(p); }}
                  className="text-xs px-3 py-1.5 rounded-full bg-orange-50 text-orange-600 hover:bg-orange-100 transition-colors border border-orange-200"
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`flex gap-3 ${msg.role === "user" ? "justify-end" : ""}`}
          >
            {msg.role === "assistant" && (
              <div className="w-7 h-7 rounded-full bg-orange-100 flex items-center justify-center flex-shrink-0">
                <Bot className="h-4 w-4 text-orange-500" />
              </div>
            )}
            <div
              className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm ${
                msg.role === "user"
                  ? "bg-orange-500 text-white"
                  : "bg-gray-100 text-gray-800"
              }`}
            >
              <p className="whitespace-pre-wrap leading-relaxed">{msg.content}</p>
            </div>
            {msg.role === "user" && (
              <div className="w-7 h-7 rounded-full bg-orange-500 flex items-center justify-center flex-shrink-0">
                <User className="h-4 w-4 text-white" />
              </div>
            )}
          </div>
        ))}
        {/* 等待首字：显示进度提示（trace 帧翻译而来），而不是干转圈 */}
        {loading && !streamingMsgId && (
          <div className="flex gap-3">
            <div className="w-7 h-7 rounded-full bg-orange-100 flex items-center justify-center flex-shrink-0">
              <Bot className="h-4 w-4 text-orange-500" />
            </div>
            <div className="bg-gray-100 rounded-2xl px-4 py-3 flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin text-orange-500" />
              {progress && <span className="text-xs text-gray-500">{progress}</span>}
            </div>
          </div>
        )}
        {error && (
          <div className="flex gap-2 items-center text-red-500 text-sm px-4">
            <AlertCircle className="h-4 w-4" />
            {error}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* 输入区 */}
      <div className="border-t border-gray-200 p-3">
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入你的问题..."
            className="flex-1 px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-orange-200 focus:border-orange-300"
            disabled={loading}
          />
          <button
            onClick={sendMessage}
            disabled={loading || !input.trim()}
            className="px-4 py-2.5 bg-orange-500 text-white rounded-xl hover:bg-orange-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <Send className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
