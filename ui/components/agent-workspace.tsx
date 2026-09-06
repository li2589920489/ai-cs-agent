"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { Headset, User, Clock, CheckCircle2, XCircle, Send, Package } from "lucide-react";

type Message = {
  role: "user" | "agent";
  content: string;
  time: string;
};

type Ticket = {
  id: string;
  session_id: string;
  reason: string;
  order_number: string;
  customer_name: string;
  return_case_id: string;
  product_name: string;
  created_at: string;
  status: "pending" | "processing" | "closed";
  messages: Message[];
};

export function AgentWorkspace() {
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);

  const selected = tickets.find((t) => t.id === selectedId) || null;

  const loadTickets = useCallback(async () => {
    try {
      const res = await fetch("/api/escalations");
      const data = await res.json();
      setTickets(data.tickets || []);
    } catch (err) {
      console.error("加载工单失败", err);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMessages = useCallback(async (ticketId: string) => {
    try {
      const res = await fetch(`/api/escalations/${ticketId}/messages`);
      const data = await res.json();
      setMessages(data.messages || []);
    } catch (err) {
      console.error("加载对话失败", err);
    }
  }, []);

  // 定时轮询工单列表和当前对话
  useEffect(() => {
    loadTickets();
    const timer = setInterval(loadTickets, 2000);
    return () => clearInterval(timer);
  }, [loadTickets]);

  useEffect(() => {
    if (selectedId) {
      loadMessages(selectedId);
      const timer = setInterval(() => loadMessages(selectedId), 2000);
      return () => clearInterval(timer);
    }
  }, [selectedId, loadMessages]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleAccept = async (ticket: Ticket) => {
    await fetch(`/api/escalations/${ticket.id}/accept`, { method: "POST" });
    setSelectedId(ticket.id);
    loadTickets();
  };

  const handleReply = async () => {
    if (!input.trim() || !selected) return;
    await fetch(`/api/escalations/${selected.id}/reply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: input.trim() }),
    });
    setInput("");
    loadMessages(selected.id);
  };

  const handleClose = async () => {
    if (!selected) return;
    if (!confirm("确定关闭该工单？")) return;
    await fetch(`/api/escalations/${selected.id}/close`, { method: "POST" });
    setSelectedId(null);
    setMessages([]);
    loadTickets();
  };

  return (
    <div className="flex h-full bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
      {/* 左侧：工单列表 */}
      <div className="w-[38%] border-r border-gray-200 flex flex-col">
        <div className="bg-orange-500 text-white h-12 px-4 flex items-center shrink-0">
          <Headset className="h-5 w-5 mr-2" />
          <h2 className="font-semibold text-sm">坐席工作台</h2>
          <span className="ml-auto text-xs font-light">
            待处理 {tickets.filter((t) => t.status === "pending").length}
          </span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="text-center py-10 text-gray-400 text-sm">加载中...</div>
          ) : tickets.length === 0 ? (
            <div className="text-center py-10 text-gray-400 text-sm">
              暂无工单
            </div>
          ) : (
            tickets.map((ticket) => (
              <div
                key={ticket.id}
                onClick={() => setSelectedId(ticket.id)}
                className={`p-3 border-b border-gray-100 cursor-pointer transition-colors ${
                  selectedId === ticket.id ? "bg-orange-50" : "hover:bg-gray-50"
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-mono text-orange-600">
                    #{ticket.id}
                  </span>
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full ${
                      ticket.status === "pending"
                        ? "bg-red-100 text-red-600"
                        : "bg-green-100 text-green-600"
                    }`}
                  >
                    {ticket.status === "pending" ? "待处理" : "处理中"}
                  </span>
                </div>
                <p className="text-sm text-gray-800 mb-1">{ticket.reason}</p>
                <div className="flex flex-wrap gap-2 text-xs text-gray-500">
                  {ticket.customer_name && (
                    <span className="flex items-center gap-1">
                      <User className="h-3 w-3" /> {ticket.customer_name}
                    </span>
                  )}
                  {ticket.order_number && (
                    <span className="flex items-center gap-1">
                      <Package className="h-3 w-3" /> {ticket.order_number}
                    </span>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* 右侧：对话视图 */}
      <div className="flex-1 flex flex-col">
        {!selected ? (
          <div className="flex-1 flex flex-col items-center justify-center text-gray-400">
            <Headset className="h-12 w-12 mb-3 text-orange-300" />
            <p className="text-sm">选择一个工单查看对话</p>
          </div>
        ) : (
          <>
            {/* 工单信息头 */}
            <div className="h-12 px-4 border-b border-gray-200 flex items-center shrink-0">
              <div className="flex-1">
                <span className="text-sm font-medium text-gray-800">
                  {selected.customer_name || "客户"}
                </span>
                <span className="text-xs text-gray-400 ml-2">
                  #{selected.id}
                </span>
              </div>
              {selected.status === "pending" ? (
                <button
                  onClick={() => handleAccept(selected)}
                  className="px-3 py-1.5 bg-orange-500 text-white text-sm rounded-lg hover:bg-orange-600 flex items-center gap-1"
                >
                  <CheckCircle2 className="h-4 w-4" /> 接入
                </button>
              ) : (
                <button
                  onClick={handleClose}
                  className="px-3 py-1.5 bg-gray-100 text-gray-600 text-sm rounded-lg hover:bg-gray-200 flex items-center gap-1"
                >
                  <XCircle className="h-4 w-4" /> 关闭工单
                </button>
              )}
            </div>

            {/* 消息列表 */}
            <div className="flex-1 overflow-y-auto p-4 space-y-3 bg-gray-50">
              {selected.status === "pending" ? (
                <div className="text-center py-10 text-gray-400 text-sm">
                  点击"接入"开始处理该工单
                </div>
              ) : messages.length === 0 ? (
                <div className="text-center py-10 text-gray-400 text-sm">
                  暂无消息，等待客户发送
                </div>
              ) : (
                messages.map((msg, i) => (
                  <div
                    key={i}
                    className={`flex gap-2 ${msg.role === "agent" ? "justify-end" : ""}`}
                  >
                    {msg.role === "user" && (
                      <div className="w-7 h-7 rounded-full bg-gray-300 flex items-center justify-center shrink-0">
                        <User className="h-4 w-4 text-white" />
                      </div>
                    )}
                    <div
                      className={`max-w-[75%] rounded-2xl px-4 py-2.5 text-sm ${
                        msg.role === "agent"
                          ? "bg-orange-500 text-white"
                          : "bg-white border border-gray-200 text-gray-800"
                      }`}
                    >
                      <p className="whitespace-pre-wrap leading-relaxed">{msg.content}</p>
                      <p className="text-[10px] opacity-60 mt-1">{msg.time}</p>
                    </div>
                    {msg.role === "agent" && (
                      <div className="w-7 h-7 rounded-full bg-orange-500 flex items-center justify-center shrink-0">
                        <Headset className="h-4 w-4 text-white" />
                      </div>
                    )}
                  </div>
                ))
              )}
              <div ref={bottomRef} />
            </div>

            {/* 输入区 */}
            {selected.status === "processing" && (
              <div className="border-t border-gray-200 p-3 flex gap-2 shrink-0">
                <input
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleReply()}
                  placeholder="输入回复..."
                  className="flex-1 px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-orange-200"
                />
                <button
                  onClick={handleReply}
                  disabled={!input.trim()}
                  className="px-4 py-2.5 bg-orange-500 text-white rounded-xl hover:bg-orange-600 disabled:opacity-50"
                >
                  <Send className="h-4 w-4" />
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
