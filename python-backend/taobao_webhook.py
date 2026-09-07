"""淘宝/天猫客服消息 webhook 入口 — 独立 FastAPI 服务

接收淘宝开放平台「消息服务」POST 的客服消息（加密 + 签名），转发给适配层处理
（验签 + AES 解密 + 解析 + Agent 回复 + 入坐席确认队列）。
坐席通过 /taobao/approve/{conversation_id}/{reply_id} 一键确认后才真正发出。

启动（在 python-backend 目录下）：
    .venv/Scripts/python.exe -m uvicorn taobao_webhook:app --host 127.0.0.1 --port 8002

验证：
    curl http://127.0.0.1:8002/taobao/health
    # 用 mock_taobao.py 模拟淘宝推送加密客服消息
    # 坐席确认：curl -X POST http://127.0.0.1:8002/taobao/approve/{conv}/{reply_id}
"""

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from taobao_adapter import (
    TAOBAO_MOCK_MODE,
    approve_and_send,
    handle_webhook,
    list_pending,
    reject_reply,
)

app = FastAPI(title="淘宝/天猫客服消息接入", version="1.0.0")


# ==================== 健康检查 ====================

@app.get("/taobao/health")
async def taobao_health() -> dict:
    return {
        "status": "ok",
        "service": "淘宝/天猫客服消息接入",
        "mock_mode": TAOBAO_MOCK_MODE,
        "approve_strategy": "坐席一键确认后发送（Q3 已确认）",
    }


# ==================== 接收消息推送 ====================

@app.post("/taobao/webhook")
async def taobao_webhook_endpoint(request: Request) -> dict:
    """淘宝消息推送回调地址（在淘宝开放平台「应用控制台 → 消息服务」配置为此 URL）。

    注意：回调地址必须 HTTPS + 公网可访问；本地开发可用 frp / ngrok / cpolar 内网穿透。
    """
    body = await request.body()
    headers = dict(request.headers)
    return await handle_webhook(body, headers)


# ==================== 坐席工作台 API ====================

class RejectBody(BaseModel):
    reason: str = ""


@app.get("/taobao/pending")
async def taobao_pending_all() -> dict:
    """坐席查看全部待审回复（工作台首页）。"""
    items = list_pending(None)
    return {
        "count": len(items),
        "items": items,
    }


@app.get("/taobao/pending/{conversation_id}")
async def taobao_pending_by_conv(conversation_id: str) -> dict:
    """坐席查看指定会话的待审回复。"""
    items = list_pending(conversation_id)
    return {
        "conversation_id": conversation_id,
        "count": len(items),
        "items": items,
    }


@app.post("/taobao/approve/{conversation_id}/{reply_id}")
async def taobao_approve(conversation_id: str, reply_id: str) -> dict:
    """坐席一键确认：调用 taobao.openim.custmsg.push 真正发出。"""
    item = await approve_and_send(conversation_id, reply_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"未找到 reply_id={reply_id}")
    return {
        "approved": True,
        "conversation_id": conversation_id,
        "reply_id": reply_id,
        "sent_to": item["open_id"],
        "reply": item["reply"],
    }


@app.post("/taobao/reject/{conversation_id}/{reply_id}")
async def taobao_reject(
    conversation_id: str,
    reply_id: str,
    body: RejectBody = RejectBody(),
) -> dict:
    """坐席拒绝：不发出，从队列移除。"""
    item = reject_reply(conversation_id, reply_id, body.reason)
    if not item:
        raise HTTPException(status_code=404, detail=f"未找到 reply_id={reply_id}")
    return {
        "rejected": True,
        "conversation_id": conversation_id,
        "reply_id": reply_id,
        "reason": body.reason,
    }