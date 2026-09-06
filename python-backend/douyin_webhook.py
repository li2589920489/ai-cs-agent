"""抖店客服消息 webhook 入口 — 独立 FastAPI 服务

接收抖店开放平台「消息推送」POST 的客服消息，转发给适配层处理（验签 + 解析 + Agent 回复 + 回发）。

启动（在 python-backend 目录下）：
    .venv/Scripts/python.exe -m uvicorn douyin_webhook:app --host 127.0.0.1 --port 8001

验证：
    curl http://127.0.0.1:8001/douyin/health
    # 用 mock_douyin.py 模拟抖店推送客服消息
"""

from fastapi import FastAPI, Request

from douyin_adapter import DOUYIN_MOCK_MODE, handle_webhook

app = FastAPI(title="抖店客服消息接入", version="1.0.0")


@app.get("/douyin/health")
async def douyin_health() -> dict:
    return {
        "status": "ok",
        "service": "抖店客服消息接入",
        "mock_mode": DOUYIN_MOCK_MODE,
    }


@app.post("/douyin/webhook")
async def douyin_webhook(request: Request) -> dict:
    """抖店消息推送回调地址（在抖店开放平台「应用后台-消息推送地址」配置为此 URL）"""
    body = await request.body()
    headers = dict(request.headers)
    return await handle_webhook(body, headers)
