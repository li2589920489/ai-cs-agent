"""抖店客服消息接入适配层

职责：把抖店「消息推送」的入站格式翻译成内部消息，复用共享的 run_chat() 生成回复，
再把回复封装成抖店「发送客服消息」的出站格式。

设计原则：
1. 不侵入现有 Agent —— run_chat() 来自 chat_service，与 main.py 的 /api/chat 共用同一份逻辑。
2. 协议层与业务层解耦 —— 验签 / 解析 / 回发集中在本文件，业务 Agent 不感知渠道。
3. Mock 可切换 —— DOUYIN_MOCK_MODE=1 时验签放行、回发改为打印，便于无资质本地联调。

真实接入注意（见 抖音接入方案.md 第五节）：
- 需要企业资质 + 抖店店铺 + 应用审核 + 公网地址。
- 本文件的 tag 常量、验签算法、发送接口字段为「示意」，落地时以抖店开放平台最新文档为准。
"""

import json
import os

from chat_service import run_chat

# ==================== 配置 ====================

# Mock 模式：DOUYIN_MOCK_MODE=1 时验签放行、回发改为打印（默认开启，便于本地联调）
DOUYIN_MOCK_MODE = os.getenv("DOUYIN_MOCK_MODE", "1") == "1"

# 抖店消息推送的 tag 常量（示意：真实值需到抖店开放平台「消息订阅」页确认）
TAG_TEST = "0"                 # 平台首次配置时的测试消息
TAG_CUSTOMER_SERVICE = "200"   # 客服消息（占位，以官方文档为准）


# ==================== 协议层：验签 / 解析 / 回发 ====================

def verify_signature(body: bytes, headers: dict, app_secret: str = "") -> bool:
    """验签骨架。真实抖店按官方算法校验签名；Mock 模式直接放行。"""
    if DOUYIN_MOCK_MODE:
        return True
    # TODO: 落地时按抖店「消息推送接入指南」实现签名校验（app_secret + body 的签名算法）
    return True


def is_test_message(body: bytes) -> bool:
    """平台首次配置会 POST 一条测试消息，需原样返回 {"code":0,"msg":"success"}"""
    try:
        payload = json.loads(body.decode("utf-8"))
        items = payload if isinstance(payload, list) else [payload]
        return any(str(m.get("tag", "")) == TAG_TEST for m in items)
    except Exception:  # noqa: BLE001
        return False


def parse_message(body: bytes) -> list[dict]:
    """解析抖店消息推送 body → 提取客服消息列表（open_id / content / conversation_id）"""
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] parse_message: 非 JSON 消息体: {e}")
        return []
    items = payload if isinstance(payload, list) else [payload]

    messages = []
    for m in items:
        tag = str(m.get("tag", ""))
        if tag != TAG_CUSTOMER_SERVICE:
            continue  # 非客服消息（订单/退款/商品等），忽略
        data = m.get("data", {}) or {}
        messages.append({
            "tag": tag,
            "msg_id": m.get("msg_id", ""),
            "open_id": data.get("from_user_open_id") or data.get("open_id") or "",
            "conversation_id": data.get("conversation_id") or data.get("conversation_short_id") or "",
            "content": data.get("content") or data.get("text") or "",
            "msg_type": data.get("msg_type") or "text",
        })
    return messages


async def send_message(open_id: str, conversation_id: str, content: str) -> dict:
    """发送客服消息。Mock 模式打印；真实模式调抖店「发送客服消息」API。"""
    if DOUYIN_MOCK_MODE:
        print(f"\n{'=' * 64}")
        print(f"[抖店·模拟发送] 回复买家 open_id={open_id}")
        print(f"[会话] {conversation_id}")
        print(f"[内容] {content}")
        print(f"{'=' * 64}")
        return {"mock": True, "sent": content}
    # TODO: 落地时调抖店「发送客服消息」API（需 access_token）
    return {"sent": True}


# ==================== 顶层：webhook 处理入口 ====================

async def handle_webhook(body: bytes, headers: dict, app_secret: str = "") -> dict:
    """顶层处理：验签 → 测试消息判断 → 解析 → run_chat → 回发。

    真实模式返回抖店要求的 {"code":0,"msg":"success"}；
    Mock 模式额外携带 replies 便于本地查看效果。
    """
    if not verify_signature(body, headers, app_secret):
        return {"code": 1, "msg": "signature invalid"}

    # 平台首次配置的测试消息，必须原样返回，否则无法启用推送
    if is_test_message(body):
        return {"code": 0, "msg": "success"}

    msgs = parse_message(body)
    replies = []
    for m in msgs:
        result = await run_chat(m["content"], m["conversation_id"] or m["open_id"])
        await send_message(m["open_id"], m["conversation_id"], result["reply"])
        replies.append({
            "open_id": m["open_id"],
            "conversation_id": m["conversation_id"],
            "reply": result["reply"],
            "final_agent": result["final_agent"],
            "trace": result["trace"],
            "escalation": result["escalation"],
        })

    resp = {"code": 0, "msg": "success"}
    if DOUYIN_MOCK_MODE:
        resp["replies"] = replies
    return resp
