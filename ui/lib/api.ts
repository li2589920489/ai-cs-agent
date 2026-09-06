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
