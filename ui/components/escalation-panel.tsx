"use client";

import { useState, useEffect } from "react";
import { AlertTriangle, Clock, Package, User, X } from "lucide-react";

type EscalationTicket = {
  id: string;
  reason: string;
  order_number: string;
  customer_name: string;
  return_case_id: string;
  product_name: string;
  created_at: string;
  status: string;
};

type EscalationInfo = {
  flag: boolean;
  reason: string;
  order_number: string;
  customer_name: string;
  return_case_id: string;
  product_name: string;
};

type Props = {
  escalation: EscalationInfo | null;
  onClose: () => void;
};

export function EscalationPanel({ escalation, onClose }: Props) {
  const [tickets, setTickets] = useState<EscalationTicket[]>([]);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    fetch("/api/escalations")
      .then((r) => r.json())
      .then((data) => setTickets(data.tickets || []))
      .catch(() => {});
  }, [escalation]);

  if (!escalation?.flag && tickets.length === 0) return null;

  if (collapsed) {
    return (
      <div
        className="fixed bottom-4 right-4 bg-orange-500 text-white rounded-full px-4 py-2 shadow-lg cursor-pointer hover:bg-orange-600 transition-colors flex items-center gap-2 z-50"
        onClick={() => setCollapsed(false)}
      >
        <AlertTriangle className="h-4 w-4" />
        <span className="text-sm font-medium">
          {tickets.length} 个待处理工单
        </span>
      </div>
    );
  }

  return (
    <div className="fixed bottom-4 right-4 w-80 max-h-[60vh] bg-white border border-orange-200 rounded-xl shadow-xl z-50 overflow-hidden">
      <div className="bg-orange-500 text-white px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <AlertTriangle className="h-4 w-4" />
          <span className="font-semibold text-sm">
            人工接管工单 ({tickets.length})
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setCollapsed(true)}
            className="text-white/80 hover:text-white"
          >
            <span className="text-xs">折叠</span>
          </button>
          <button onClick={onClose} className="text-white/80 hover:text-white">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div className="overflow-y-auto max-h-[calc(60vh-48px)]">
        {tickets.map((ticket) => (
          <div
            key={ticket.id}
            className="p-3 border-b border-gray-100 hover:bg-orange-50 transition-colors"
          >
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-mono text-orange-600">
                #{ticket.id}
              </span>
              <span className="text-xs text-gray-400 flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {ticket.created_at.slice(11, 16)}
              </span>
            </div>
            <p className="text-sm text-gray-800 mb-2">{ticket.reason}</p>
            <div className="flex flex-wrap gap-2 text-xs text-gray-500">
              {ticket.order_number && (
                <span className="bg-gray-100 px-2 py-0.5 rounded flex items-center gap-1">
                  <Package className="h-3 w-3" />
                  {ticket.order_number}
                </span>
              )}
              {ticket.customer_name && (
                <span className="bg-gray-100 px-2 py-0.5 rounded flex items-center gap-1">
                  <User className="h-3 w-3" />
                  {ticket.customer_name}
                </span>
              )}
              {ticket.product_name && (
                <span className="bg-gray-100 px-2 py-0.5 rounded">
                  {ticket.product_name}
                </span>
              )}
            </div>
            <button
              onClick={() => {
                // 模拟人工接管
                const updated = tickets.map((t) =>
                  t.id === ticket.id ? { ...t, status: "processing" } : t
                );
                setTickets(updated.filter((t) => t.status === "pending"));
              }}
              className="mt-2 w-full text-center text-xs bg-orange-500 text-white rounded-lg py-1.5 hover:bg-orange-600 transition-colors"
            >
              接入处理
            </button>
          </div>
        ))}

        {tickets.length === 0 && (
          <div className="p-6 text-center text-gray-400 text-sm">
            暂无待处理工单
          </div>
        )}
      </div>
    </div>
  );
}
