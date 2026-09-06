"use client";

import { useState, useRef, useEffect } from "react";
import { Send, Loader2, User, Bot, AlertCircle } from "lucide-react";

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

export function ChatPanel({ onAgentTrace, onEscalation }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [manualMode, setManualMode] = useState(false);
  const manualCountRef = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

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
          }
        } else {
          setManualMode(false);
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

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || loading) return;

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

      const data = await res.json();

      const aiMsg: Message = {
        id: (Date.now() + 1).toString(),
        role: "assistant",
        content: data.reply || "(Agent未返回回复)",
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, aiMsg]);

      // 保持会话
      if (data.session_id) setSessionId(data.session_id);

      // 通知父组件Agent轨迹
      if (data.agent_trace && onAgentTrace) {
        onAgentTrace(data.agent_trace);
      }

      // 通知父组件转接状态
      if (data.escalation?.flag && onEscalation) {
        onEscalation(data.escalation);
      }

      // 人工接管模式：开始轮询坐席回复
      if (data.manual) {
        setManualMode(true);
        manualCountRef.current = 0;
      }
    } catch (err: any) {
      setError(err.message || "发送失败，请重试");
    } finally {
      setLoading(false);
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
        {loading && (
          <div className="flex gap-3">
            <div className="w-7 h-7 rounded-full bg-orange-100 flex items-center justify-center">
              <Bot className="h-4 w-4 text-orange-500" />
            </div>
            <div className="bg-gray-100 rounded-2xl px-4 py-3">
              <Loader2 className="h-4 w-4 animate-spin text-orange-500" />
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
