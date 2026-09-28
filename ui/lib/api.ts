// 鉴权凭据：构建期由环境变量注入（NEXT_PUBLIC_* 会进入客户端 bundle）。
// - 坐席 key（role=agent）：人工工单接口 /api/escalations*
// - 租户 key（tenant_id=default）：知识库管理接口 /api/knowledge*
// 生产环境应改为 Next.js Route Handler 在服务端注入，避免凭据下发到浏览器。
export const AGENT_API_KEY = process.env.NEXT_PUBLIC_AGENT_API_KEY ?? "";
export const TENANT_API_KEY = process.env.NEXT_PUBLIC_TENANT_API_KEY ?? "";

export type AuthKind = "agent" | "tenant";

/** 按凭据类型返回请求头；未配置时返回空对象（退化为兼容期行为） */
export function authHeaders(kind: AuthKind): Record<string, string> {
  const key = kind === "agent" ? AGENT_API_KEY : TENANT_API_KEY;
  return key ? { "x-api-key": key } : {};
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * 统一请求封装：自动注入鉴权头 + 校验 res.ok。
 * 后端已给坐席/知识库接口加鉴权，若前端不检查 res.ok，鉴权失败会静默无感。
 */
export async function apiFetch(
  path: string,
  kind: AuthKind,
  init: RequestInit = {},
): Promise<Response> {
  const res = await fetch(path, {
    ...init,
    headers: { ...authHeaders(kind), ...((init.headers as Record<string, string>) || {}) },
  });
  if (!res.ok) {
    throw new ApiError(`${path} → HTTP ${res.status}`, res.status);
  }
  return res;
}

/** 把鉴权错误翻译成用户可读、可自救的提示 */
export function authErrorMessage(err: unknown, kind: AuthKind): string {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      const envName = kind === "agent" ? "NEXT_PUBLIC_AGENT_API_KEY" : "NEXT_PUBLIC_TENANT_API_KEY";
      return `鉴权失败（401）：请在 ui/.env.local 配置 ${envName} 并重启前端`;
    }
    if (err.status === 403) {
      return kind === "agent"
        ? "权限不足（403）：该 API Key 不是坐席角色（需 role=agent）"
        : "权限不足（403）：写操作需要有效的租户 API Key";
    }
    return `请求失败（HTTP ${err.status}）`;
  }
  return "请求失败：无法连接后端服务，请确认后端已启动";
}

// 获取Agent列表，供前端左侧面板使用
export async function fetchBootstrapState() {
  try {
    const res = await fetch(`/api/agents`);
    if (!res.ok) throw new Error(`Agents API error: ${res.status}`);
    return res.json();
  } catch (err) {
    console.error("Error fetching agents:", err);
    return null;
  }
}
