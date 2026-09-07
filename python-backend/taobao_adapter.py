"""淘宝/天猫客服消息接入适配层

职责：把淘宝「消息服务」的入站格式（加密 + 签名）翻译成内部消息，复用共享的 run_chat()
生成回复，再把回复放进坐席确认队列（Q3 已确认：坐席一键确认后发送，不直接发出）。
坐席通过 /taobao/approve/{conversation_id}/{reply_id} 接口确认后才真正调
taobao.openim.custmsg.push 发出。

设计原则：
1. 不侵入现有 Agent —— run_chat() 来自 chat_service，与 main.py 的 /api/chat 共用同一份逻辑。
2. 协议层与业务层解耦 —— 验签 / 解密 / 解析 / 回发集中在本文件，业务 Agent 不感知渠道。
3. Mock 可切换 —— TAOBAO_MOCK_MODE=1 时验签放行、加密用对称密钥本地解、回发改为打印。
4. 坐席确认流 —— AI 生成回复不直接发，必须坐席 confirm 后才真发（与产品定位一致）。

真实接入注意（见 淘宝接入方案.md 第五节）：
- 需要企业资质 + 淘宝/天猫店铺 + 自用型/工具型应用审核 + 客服消息权限包 + 公网 HTTPS。
- 本文件的 AES 密钥派生、TOP 签名算法、消息字段名为「示意」，落地时以淘宝开放平台最新
  《消息服务》文档为准（特别是加密 block size / IV 填充 / 签名 canonical 字段顺序）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

from chat_service import run_chat

# ==================== 配置 ====================

# Mock 模式：TAOBAO_MOCK_MODE=1 时验签放行、加密用本地对称密钥、回发改为打印
# （默认开启，便于无资质本地联调）
TAOBAO_MOCK_MODE = os.getenv("TAOBAO_MOCK_MODE", "1") == "1"

# 淘宝消息服务的 tag 常量（示意：真实值需到淘宝开放平台「消息订阅」页确认）
MESSAGE_TYPE_CHAT = "ChatMessage"      # 买家客服消息
MESSAGE_TYPE_TEST = "test"            # 平台首次配置时的测试消息

# Mock 模式下使用的 AES 密钥（base64 编码的 32 字节密钥 = 256 bit）
# 真实模式下，aes_key = base64_decode(app_secret + "=") 是淘宝官方推荐的派生方式
MOCK_AES_KEY_B64 = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # 32 字节 base64
MOCK_AES_KEY = base64.b64decode(MOCK_AES_KEY_B64)


# ==================== OAuth 2.0 Token 管理 ====================

class TaobaoAuth:
    """OAuth 2.0 token 管理 + refresh_token 自动续期。

    真实模式下：
      1. 店铺主在淘宝开放平台扫码授权 → 拿 authorization_code
      2. 用 code + app_key + app_secret 调 https://oauth.taobao.com/token 换 access_token + refresh_token
      3. access_token 过期前用 refresh_token 续期（access_token 7~30 天，refresh_token 更长）

    Mock 模式下：内存里给每个 shop_id 一个固定 fake token，跳过真实换 token。
    """

    def __init__(self, app_key: str = "", app_secret: str = ""):
        self.app_key = app_key or os.getenv("TAOBAO_APP_KEY", "mock_app_key")
        self.app_secret = app_secret or os.getenv("TAOBAO_APP_SECRET", "mock_app_secret")
        self._tokens: dict[str, dict[str, Any]] = {}

    async def exchange_code(self, code: str, shop_id: str) -> dict:
        """授权码换 token（首次接入时调用一次）。Mock 模式直接返回固定 token。"""
        if TAOBAO_MOCK_MODE:
            return self._store_mock_token(shop_id)
        # TODO: 真实模式调 https://oauth.taobao.com/token grant_type=authorization_code
        return {}

    async def get_access_token(self, shop_id: str) -> str:
        """懒获取 + 自动 refresh。"""
        tok = self._tokens.get(shop_id)
        if not tok:
            if TAOBAO_MOCK_MODE:
                self._store_mock_token(shop_id)
                tok = self._tokens[shop_id]
            else:
                raise RuntimeError(f"shop_id={shop_id} 未授权，请先调 exchange_code()")
        if time.time() >= tok["expire"]:
            if TAOBAO_MOCK_MODE:
                # Mock 模式直接续期
                tok["access_token"] = f"mock_token_{uuid.uuid4().hex[:8]}"
                tok["expire"] = time.time() + 86400 * 7
            else:
                # TODO: 真实模式 grant_type=refresh_token
                pass
        return tok["access_token"]

    def _store_mock_token(self, shop_id: str) -> dict:
        self._tokens[shop_id] = {
            "access_token": f"mock_token_{shop_id[:8]}",
            "refresh_token": f"mock_refresh_{shop_id[:8]}",
            "expire": time.time() + 86400 * 7,
        }
        return self._tokens[shop_id]


# 全局单例（真实模式下应在进程启动时注入 app_key/app_secret）
_auth = TaobaoAuth()


def get_auth() -> TaobaoAuth:
    return _auth


# ==================== AES-256-CBC 加解密 ====================

def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    """PKCS#7 padding"""
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16:
        return data
    return data[:-pad_len]


def aes_encrypt(plaintext: str, aes_key: bytes | None = None) -> str:
    """AES-256-CBC 加密（PKCS#7），输出 base64 字符串。

    Mock 模式用本地对称密钥；真实模式 aes_key 派生：base64_decode(app_secret + "=")。
    IV 取密钥的前 16 字节（淘宝官方建议）。
    """
    key = aes_key or MOCK_AES_KEY
    if len(key) < 32:
        # 真实模式派生逻辑：base64_decode(app_secret + "=")，32 字节
        key = (key + b"\x00" * 32)[:32]
    iv = key[:16]

    # 用 pycryptodome（已在 requirements.txt 隐含），无依赖时降级到 cryptography
    try:
        from Crypto.Cipher import AES  # pycryptodome
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        ct = cipher.encrypt(_pkcs7_pad(plaintext.encode("utf-8")))
    except ImportError:
        # 降级：用 cryptography 库
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            cipher = Cipher(algorithms.AES(key[:32]), modes.CBC(iv))
            enc = cipher.encryptor()
            ct = enc.update(_pkcs7_pad(plaintext.encode("utf-8"))) + enc.finalize()
        except ImportError:
            # 终极兜底：Mock 模式下用 base64 假装加密（仅用于本地联调）
            if TAOBAO_MOCK_MODE:
                return base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
            raise
    return base64.b64encode(ct).decode("ascii")


def aes_decrypt(ciphertext_b64: str, aes_key: bytes | None = None) -> str:
    """AES-256-CBC 解密，输入 base64 字符串，返回明文。"""
    key = aes_key or MOCK_AES_KEY
    if len(key) < 32:
        key = (key + b"\x00" * 32)[:32]
    iv = key[:16]
    ct = base64.b64decode(ciphertext_b64)

    try:
        from Crypto.Cipher import AES  # pycryptodome
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        pt = _pkcs7_unpad(cipher.decrypt(ct))
    except ImportError:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            cipher = Cipher(algorithms.AES(key[:32]), modes.CBC(iv))
            dec = cipher.decryptor()
            pt = _pkcs7_unpad(dec.update(ct) + dec.finalize())
        except ImportError:
            # 兜底
            if TAOBAO_MOCK_MODE:
                return base64.b64decode(ciphertext_b64).decode("utf-8")
            raise
    return pt.decode("utf-8")


# ==================== TOP API 签名 ====================

def sign_top(params: dict[str, Any], app_secret: str) -> str:
    """TOP API 签名算法：参数按字典序排序（不含 sign），拼接成 query string，
    两端加 app_secret，做 hmac-sha256，再大写。

    真实淘宝 TOP API 规范：https://open.taobao.com/doc.htm
    """
    items = []
    for k in sorted(params.keys()):
        if k == "sign":
            continue
        v = params[k]
        if v is None:
            continue
        # TOP 规范：value 直接 str()，不做 URL encode（由网关做）
        items.append(f"{k}{v}")
    canonical = "".join(items)
    digest = hmac.new(
        app_secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest.upper()


# ==================== webhook 验签 + 解密 + 解析 ====================

def verify_signature(body: bytes, headers: dict[str, str]) -> bool:
    """淘宝消息推送验签。Mock 模式直接放行。

    真实模式下需按官方文档校验（一般在 header 的 'Authorization' 或自定义字段里
    携带签名，需比对 app_secret + body 的 hmac-sha256）。
    """
    if TAOBAO_MOCK_MODE:
        return True
    # TODO: 落地时按官方《消息服务接入指南》实现签名校验
    return True


def is_test_message(decrypted_body: str) -> bool:
    """平台首次配置会推送一条测试消息，需原样返回 {"code":0,"msg":"success"}。"""
    try:
        payload = json.loads(decrypted_body)
        return payload.get("messageType") == MESSAGE_TYPE_TEST
    except Exception:  # noqa: BLE001
        return False


def parse_message(decrypted_body: str) -> list[dict]:
    """解析淘宝消息推送明文 → 提取买家消息列表。

    明文 JSON 示意：
    {
      "messageList": [
        {"messageType": "ChatMessage", "uuid": "...", "fromId": "buyer_open_id",
         "conversationId": "...", "content": "消息文本", "shopId": "店铺ID",
         "sendTime": 1694000000}
      ]
    }
    """
    try:
        payload = json.loads(decrypted_body)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] parse_message: 非 JSON 明文: {e}")
        return []

    messages = []
    for m in payload.get("messageList", []) or []:
        if m.get("messageType") != MESSAGE_TYPE_CHAT:
            continue  # 非买家消息（订单/退款/系统通知等），忽略
        messages.append({
            "uuid": m.get("uuid", str(uuid.uuid4())),
            "from_id": m.get("fromId", ""),
            "conversation_id": m.get("conversationId") or m.get("fromId", ""),
            "content": m.get("content", ""),
            "shop_id": m.get("shopId", ""),
            "send_time": m.get("sendTime", 0),
        })
    return messages


# ==================== 坐席确认队列 + 回发 ====================

# 内存队列：conversation_id -> [{reply_id, reply, open_id, shop_id, final_agent, trace, escalation, created_at}]
_pending_replies: dict[str, list[dict]] = {}


def enqueue_reply(
    conversation_id: str,
    open_id: str,
    shop_id: str,
    reply: str,
    final_agent: str,
    trace: list,
    escalation: bool,
) -> str:
    """AI 生成回复后入队，等坐席确认。返回 reply_id。"""
    reply_id = str(uuid.uuid4())[:8]
    if conversation_id not in _pending_replies:
        _pending_replies[conversation_id] = []
    _pending_replies[conversation_id].append({
        "reply_id": reply_id,
        "open_id": open_id,
        "shop_id": shop_id,
        "reply": reply,
        "final_agent": final_agent,
        "trace": trace,
        "escalation": escalation,
        "created_at": time.time(),
    })
    return reply_id


def list_pending(conversation_id: str | None = None) -> list[dict]:
    """坐席工作台：查看待审回复列表。conversation_id 为 None 时返回所有。"""
    if conversation_id is None:
        out = []
        for cid, items in _pending_replies.items():
            for it in items:
                out.append({**it, "conversation_id": cid})
        return out
    return [{**it, "conversation_id": conversation_id} for it in _pending_replies.get(conversation_id, [])]


def approve_reply(conversation_id: str, reply_id: str) -> dict | None:
    """坐席确认：从队列取出指定 reply（不真正发送）。

    发送由调用方负责：在 async 上下文（FastAPI endpoint）中调用
    `approve_reply` 后再 `await send_message(...)`，避免 create_task 被取消。
    同步上下文（脚本测试）可调 `approve_reply_sync()`。
    """
    items = _pending_replies.get(conversation_id, [])
    for i, it in enumerate(items):
        if it["reply_id"] == reply_id:
            item = items.pop(i)
            if not items:
                _pending_replies.pop(conversation_id, None)
            return item
    return None


async def approve_and_send(conversation_id: str, reply_id: str) -> dict | None:
    """坐席一键确认（async 版本）：approve + send_message 一次完成。

    必须在 async 上下文（FastAPI endpoint）调用，send_message 会被正确 await。
    """
    item = approve_reply(conversation_id, reply_id)
    if item is None:
        return None
    await send_message(item["open_id"], item["reply"], item["shop_id"])
    return item


def approve_reply_sync(conversation_id: str, reply_id: str) -> dict | None:
    """坐席一键确认（同步版本）：用于脚本测试 / 进程内调用。

    在 async 上下文中请用 `approve_and_send()`，否则 FastAPI 会取消 task。
    """
    import asyncio
    item = approve_reply(conversation_id, reply_id)
    if item is None:
        return None
    asyncio.run(send_message(item["open_id"], item["reply"], item["shop_id"]))
    return item


def reject_reply(conversation_id: str, reply_id: str, reason: str = "") -> dict | None:
    """坐席拒绝：从队列移除，不发出。"""
    items = _pending_replies.get(conversation_id, [])
    for i, it in enumerate(items):
        if it["reply_id"] == reply_id:
            item = items.pop(i)
            if not items:
                _pending_replies.pop(conversation_id, None)
            item["rejected"] = True
            item["reject_reason"] = reason
            return item
    return None


async def send_message(open_id: str, content: str, shop_id: str) -> dict:
    """发送客服消息（回发）。Mock 模式打印；真实模式调 taobao.openim.custmsg.push。"""
    auth = get_auth()
    if TAOBAO_MOCK_MODE:
        print(f"\n{'=' * 64}")
        print(f"[淘宝·模拟发送·坐席已确认] 回复买家 from_id={open_id}")
        print(f"[会话 shop_id={shop_id}] {content}")
        print(f"{'=' * 64}")
        return {"mock": True, "sent": content, "shop_id": shop_id}

    # 真实模式：调 taobao.openim.custmsg.push
    access_token = await auth.get_access_token(shop_id)
    import httpx
    params = {
        "method": "taobao.openim.custmsg.push",
        "app_key": auth.app_key,
        "session": access_token,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "2.0",
        "sign_method": "hmac-sha256",
        "to_user": open_id,
        "content": json.dumps({"text": content}, ensure_ascii=False),
    }
    params["sign"] = sign_top(params, auth.app_secret)
    resp = await httpx.AsyncClient().post(
        "https://eco.taobao.com/router/rest",
        params=params,
    )
    return resp.json()


# ==================== 顶层：webhook 处理入口 ====================

async def handle_webhook(body: bytes, headers: dict[str, str]) -> dict:
    """顶层处理：验签 → 解密 → 测试消息判断 → 解析 → run_chat → 入队待审。

    注意：坐席确认后才会真正发出（与抖音的"自动发"不同，对应方案 Q3 决定）。
    """
    # 1. 验签
    if not verify_signature(body, headers):
        return {"code": 1, "msg": "signature invalid"}

    # 2. 解密（淘宝消息体是密文，明文是 JSON）
    try:
        payload = json.loads(body.decode("utf-8"))
        encrypted = payload.get("encrypt", "")
    except Exception:  # noqa: BLE001
        return {"code": 1, "msg": "invalid body"}

    if not encrypted:
        return {"code": 1, "msg": "missing encrypt field"}

    decrypted = aes_decrypt(encrypted)

    # 3. 测试消息判断
    if is_test_message(decrypted):
        return {"code": 0, "msg": "success"}

    # 4. 解析买家消息
    msgs = parse_message(decrypted)
    enqueued = []
    for m in msgs:
        # 5. 复用现有 Agent 逻辑
        result = await run_chat(m["content"], m["conversation_id"] or m["from_id"])
        # 6. 入队待坐席确认（关键：与抖音的"自动发"不同）
        reply_id = enqueue_reply(
            conversation_id=m["conversation_id"],
            open_id=m["from_id"],
            shop_id=m["shop_id"],
            reply=result["reply"],
            final_agent=result["final_agent"],
            trace=result["trace"],
            escalation=result["escalation"],
        )
        enqueued.append({
            "from_id": m["from_id"],
            "conversation_id": m["conversation_id"],
            "reply_id": reply_id,
            "reply": result["reply"],
            "final_agent": result["final_agent"],
            "escalation": result["escalation"],
        })

    resp = {"code": 0, "msg": "success"}
    if TAOBAO_MOCK_MODE:
        resp["pending_replies"] = enqueued
        resp["hint"] = "坐席通过 POST /taobao/approve/{conversation_id}/{reply_id} 确认后才会真正发出"
    return resp