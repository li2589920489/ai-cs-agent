"""模拟抖店客服消息推送 — 本地 Mock

模拟抖店开放平台，向适配层推送「抖店格式」的客服消息，验证整条闭环：
    [抖店格式消息] → douyin_adapter(验签+解析) → 6 Agent(run_chat) → 回复

两种运行方式：
1. 进程内模式（默认，单命令验证，无需起 webhook 服务）：
       .venv/Scripts/python.exe mock_douyin.py
2. HTTP 模式（模拟真实「平台 → 你的服务器」推送，需先起 douyin_webhook.py:8001）：
       .venv/Scripts/python.exe mock_douyin.py --http

自定义消息：
       .venv/Scripts/python.exe mock_douyin.py --message "帮我查订单 TB20260812001 的物流"
"""

import argparse
import asyncio
import json

import httpx

# 与 douyin_adapter.TAG_CUSTOMER_SERVICE 保持一致（客服消息 tag，占位值）
TAG_CUSTOMER_SERVICE = "200"

WEBHOOK_URL = "http://127.0.0.1:8001/douyin/webhook"

# 内置测试消息（open_id, conversation_id, 内容）——全部命中项目内置真实数据
DEFAULT_MESSAGES = [
    ("dy_user_001", "conv_001", "帮我查一下订单 TB20260812001 的物流到哪了"),
    ("dy_user_002", "conv_002", "良品铺子坚果大礼包多少钱？有什么规格？有货吗？"),
    ("dy_user_003", "conv_003", "我买的商品坏了，我要赔偿"),
]


def build_douyin_payload(open_id: str, conversation_id: str, content: str) -> list[dict]:
    """构造抖店「消息推送」格式的客服消息（数组，每项含 tag / msg_id / data）"""
    return [{
        "tag": TAG_CUSTOMER_SERVICE,
        "msg_id": "mock_msg_001",
        "data": {
            "from_user_open_id": open_id,
            "conversation_id": conversation_id,
            "content": content,
            "msg_type": "text",
        },
    }]


def _print_replies(replies: list[dict]) -> None:
    if not replies:
        print("(无客服消息被处理)")
        return
    for r in replies:
        print("\n" + "-" * 64)
        print(f"买家 open_id: {r.get('open_id')}")
        print(f"分流结果: {r.get('final_agent')}")
        print(f"是否转人工: {r.get('escalation')}")
        print("Agent 轨迹:")
        for t in r.get("trace", []):
            if t.get("type") == "handoff":
                print(f"  - handoff: {t['from']} -> {t['to']}")
            elif t.get("type") == "tool_call":
                print(f"  - 工具调用: {t['tool']} (by {t['agent']})")
        print(f"\n回复:\n{r.get('reply')}")
        print("-" * 64)


async def run_in_process(messages: list[tuple]) -> None:
    """进程内模式：直接调用适配层 handle_webhook，单命令验证"""
    from douyin_adapter import handle_webhook

    for open_id, conv_id, content in messages:
        print(f"\n{'#' * 64}\n# 模拟抖店推送客服消息：{content}\n{'#' * 64}")
        payload = json.dumps(build_douyin_payload(open_id, conv_id, content), ensure_ascii=False)
        resp = await handle_webhook(payload.encode("utf-8"), {})
        _print_replies(resp.get("replies", []))


async def run_http(messages: list[tuple]) -> None:
    """HTTP 模式：POST 到 douyin_webhook.py，模拟真实「平台 → 服务器」推送"""
    async with httpx.AsyncClient(timeout=120) as client:
        for open_id, conv_id, content in messages:
            print(f"\n{'#' * 64}\n# POST 客服消息到 {WEBHOOK_URL}：{content}\n{'#' * 64}")
            payload = build_douyin_payload(open_id, conv_id, content)
            resp = await client.post(WEBHOOK_URL, json=payload)
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = {"raw": resp.text}
            _print_replies(data.get("replies", []))
            if resp.status_code != 200:
                print(f"[WARN] webhook 返回 HTTP {resp.status_code}")


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟抖店客服消息推送")
    parser.add_argument("--http", action="store_true", help="HTTP 模式（需先起 douyin_webhook.py）")
    parser.add_argument("--message", type=str, default=None, help="自定义单条消息内容")
    parser.add_argument("--conversation", type=str, default="conv_custom", help="自定义会话 ID")
    args = parser.parse_args()

    if args.message:
        messages = [("dy_user_custom", args.conversation, args.message)]
    else:
        messages = DEFAULT_MESSAGES

    if args.http:
        asyncio.run(run_http(messages))
    else:
        asyncio.run(run_in_process(messages))


if __name__ == "__main__":
    main()
