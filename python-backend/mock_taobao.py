"""模拟淘宝/天猫客服消息推送 — 本地 Mock（含 AES 加密 + 坐席确认流）

模拟淘宝开放平台「消息服务」，向适配层推送「淘宝格式」加密客服消息，验证整条闭环：
    [淘宝加密消息] → taobao_adapter(AES 解密 + 验签) → 6 Agent(run_chat) → 坐席确认队列
    → 坐席调 /taobao/approve → taobao.openim.custmsg.push 模拟发送

两种运行方式：
1. 进程内模式（默认，单命令验证，无需起 webhook 服务）：
       .venv/Scripts/python.exe mock_taobao.py
2. HTTP 模式（模拟真实「平台 → 你的服务器」推送，需先起 taobao_webhook.py:8002）：
       .venv/Scripts/python.exe mock_taobao.py --http

自定义消息：
       .venv/Scripts/python.exe mock_taobao.py --message "帮我查订单 TB20260812001 的物流"

完整坐席确认流（HTTP 模式）：
       # 终端 1：起 webhook
       .venv/Scripts/python.exe -m uvicorn taobao_webhook:app --port 8002
       # 终端 2：推消息 + 看 pending + approve
       .venv/Scripts/python.exe mock_taobao.py --http
       curl http://127.0.0.1:8002/taobao/pending
       curl -X POST http://127.0.0.1:8002/taobao/approve/{conv_id}/{reply_id}
"""

import argparse
import asyncio
import json

import httpx

from taobao_adapter import aes_encrypt, handle_webhook

WEBHOOK_URL = "http://127.0.0.1:8002/taobao/webhook"

# 内置测试消息（from_id / conversation_id / shop_id / 内容）——全部命中项目内置真实数据
DEFAULT_MESSAGES = [
    ("tb_user_001", "tb_conv_001", "tb_shop_demo", "帮我查一下订单 TB20260812001 的物流到哪了"),
    ("tb_user_002", "tb_conv_002", "tb_shop_demo", "良品铺子坚果大礼包多少钱？有什么规格？有货吗？"),
    ("tb_user_003", "tb_conv_003", "tb_shop_demo", "我买的商品坏了，我要赔偿"),
]


def build_taobao_payload(from_id: str, conversation_id: str, shop_id: str, content: str) -> dict:
    """构造淘宝「消息服务」明文 JSON（list 嵌套在 messageList 里），再 AES 加密。"""
    plain_obj = {
        "messageList": [
            {
                "messageType": "ChatMessage",
                "uuid": "mock_uuid_001",
                "fromId": from_id,
                "conversationId": conversation_id,
                "shopId": shop_id,
                "content": content,
                "sendTime": 1694000000,
            }
        ]
    }
    plain_str = json.dumps(plain_obj, ensure_ascii=False)
    encrypted = aes_encrypt(plain_str)
    return {"encrypt": encrypted}


def _print_replies(resp_data: dict) -> None:
    if "pending_replies" not in resp_data:
        print(f"  webhook 返回: code={resp_data.get('code')}, msg={resp_data.get('msg')}")
        if resp_data.get("hint"):
            print(f"  提示: {resp_data['hint']}")
        return
    for r in resp_data["pending_replies"]:
        print("\n" + "-" * 64)
        print(f"买家 from_id: {r.get('from_id')}")
        print(f"会话: {r.get('conversation_id')}")
        print(f"分流结果: {r.get('final_agent')}")
        print(f"是否转人工: {r.get('escalation')}")
        print(f"待审 reply_id: {r.get('reply_id')}")
        print(f"\nAI 生成回复:\n{r.get('reply')}")
        print("-" * 64)


async def run_in_process(messages: list[tuple]) -> None:
    """进程内模式：直接调用适配层 handle_webhook，单命令验证。"""
    for from_id, conv_id, shop_id, content in messages:
        print(f"\n{'#' * 64}\n# 模拟淘宝加密推送：{content}\n{'#' * 64}")
        payload = build_taobao_payload(from_id, conv_id, shop_id, content)
        body = json.dumps(payload).encode("utf-8")
        resp = await handle_webhook(body, {})
        _print_replies(resp)


async def run_http(messages: list[tuple]) -> None:
    """HTTP 模式：POST 到 taobao_webhook.py，模拟真实「平台 → 服务器」推送。"""
    async with httpx.AsyncClient(timeout=120) as client:
        for from_id, conv_id, shop_id, content in messages:
            print(f"\n{'#' * 64}\n# POST 加密客服消息到 {WEBHOOK_URL}：{content}\n{'#' * 64}")
            payload = build_taobao_payload(from_id, conv_id, shop_id, content)
            resp = await client.post(WEBHOOK_URL, json=payload)
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = {"raw": resp.text}
            _print_replies(data)
            if resp.status_code != 200:
                print(f"[WARN] webhook 返回 HTTP {resp.status_code}")


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟淘宝客服消息推送（含 AES 加密 + 坐席确认流）")
    parser.add_argument("--http", action="store_true", help="HTTP 模式（需先起 taobao_webhook.py）")
    parser.add_argument("--message", type=str, default=None, help="自定义单条消息内容")
    parser.add_argument("--conversation", type=str, default="tb_conv_custom", help="自定义会话 ID")
    parser.add_argument("--from-id", type=str, default="tb_user_custom", help="自定义买家 ID")
    parser.add_argument("--shop-id", type=str, default="tb_shop_demo", help="自定义店铺 ID")
    args = parser.parse_args()

    if args.message:
        messages = [(args.from_id, args.conversation, args.shop_id, args.message)]
    else:
        messages = DEFAULT_MESSAGES

    if args.http:
        asyncio.run(run_http(messages))
    else:
        asyncio.run(run_in_process(messages))


if __name__ == "__main__":
    main()