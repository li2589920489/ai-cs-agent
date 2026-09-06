"use client";

import { useCallback, useEffect, useState } from "react";
import { AgentPanel } from "@/components/agent-panel";
import { ChatPanel } from "@/components/chat-panel";
import { EscalationPanel } from "@/components/escalation-panel";
import { KnowledgePanel } from "@/components/knowledge-panel";
import { AgentWorkspace } from "@/components/agent-workspace";
import type { Agent, AgentEvent, GuardrailCheck, EventType } from "@/lib/types";
import { fetchBootstrapState } from "@/lib/api";

export default function Home() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [currentAgent, setCurrentAgent] = useState<string>("");
  const [guardrails, setGuardrails] = useState<GuardrailCheck[]>([]);
  const [context, setContext] = useState<Record<string, any>>({});
  const [escalation, setEscalation] = useState<any>(null);
  const [tab, setTab] = useState<"chat" | "knowledge" | "workspace">("chat");

  useEffect(() => {
    (async () => {
      const bootstrap = await fetchBootstrapState();
      if (!bootstrap) return;
      if (bootstrap.current_agent) setCurrentAgent(bootstrap.current_agent);
      if (Array.isArray(bootstrap.agents)) setAgents(bootstrap.agents);
      if (bootstrap.context) setContext(bootstrap.context);
    })();
  }, []);

  const handleAgentTrace = useCallback((trace: any[]) => {
    if (!trace || trace.length === 0) return;

    const newEvents: AgentEvent[] = trace.map((t, i) => ({
      id: `${Date.now()}-${i}`,
      type: t.type as EventType,
      agent: t.agent || t.from || "",
      content: t.type === "handoff" ? `${t.from} ${String.fromCharCode(8594)} ${t.to}` : (t.tool || t.result || ""),
      metadata: t.type === "handoff"
        ? { source_agent: t.from, target_agent: t.to }
        : (t.type === "tool_call" ? { tool_name: t.tool, tool_args: {} as Record<string, any> } : { tool_result: t.result || "" }),
      timestamp: new Date(),
    }));

    setEvents((prev) => [...prev, ...newEvents]);

    // 更新当前活跃 Agent
    const lastHandoff = [...trace].reverse().find(t => t.type === "handoff");
    if (lastHandoff) {
      setCurrentAgent(lastHandoff.to);
    } else if (trace.length > 0) {
      const firstAgent = trace[0].agent || trace[0].from;
      if (firstAgent) setCurrentAgent(firstAgent);
    }
  }, []);

  return (
    <div className="flex flex-col h-screen bg-gray-100">
      {/* 顶部 Tab 切换 */}
      <div className="flex gap-2 px-2 pt-2">
        <button
          onClick={() => setTab("chat")}
          className={`px-4 py-2 text-sm rounded-t-lg transition-colors ${
            tab === "chat"
              ? "bg-white text-orange-600 font-medium border border-b-0 border-gray-200"
              : "bg-transparent text-gray-500 hover:bg-gray-200"
          }`}
        >
          客服对话
        </button>
        <button
          onClick={() => setTab("knowledge")}
          className={`px-4 py-2 text-sm rounded-t-lg transition-colors ${
            tab === "knowledge"
              ? "bg-white text-orange-600 font-medium border border-b-0 border-gray-200"
              : "bg-transparent text-gray-500 hover:bg-gray-200"
          }`}
        >
          知识库管理
        </button>
        <button
          onClick={() => setTab("workspace")}
          className={`px-4 py-2 text-sm rounded-t-lg transition-colors ${
            tab === "workspace"
              ? "bg-white text-orange-600 font-medium border border-b-0 border-gray-200"
              : "bg-transparent text-gray-500 hover:bg-gray-200"
          }`}
        >
          坐席工作台
        </button>
      </div>

      <main className={`${tab === "chat" ? "flex" : "hidden"} flex-1 gap-2 p-2 min-h-0`}>
        <AgentPanel
          agents={agents}
          currentAgent={currentAgent}
          events={events}
          guardrails={guardrails}
          context={context}
        />
        <ChatPanel onAgentTrace={handleAgentTrace} onEscalation={setEscalation} />
        <EscalationPanel escalation={escalation} onClose={() => setEscalation(null)} />
      </main>
      <main className={`${tab === "knowledge" ? "flex-1" : "hidden"} p-2 min-h-0`}>
        <KnowledgePanel />
      </main>
      <main className={`${tab === "workspace" ? "flex-1" : "hidden"} p-2 min-h-0`}>
        <AgentWorkspace />
      </main>
    </div>
  );
}
