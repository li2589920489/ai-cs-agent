"""租户鉴权模块 — api_key 哈希存储 + FastAPI 依赖

设计目标（最小后端版，对应优化计划 P0-1 前半）：
- tenants 表存租户凭据：tenant_id + api_key_hash(sha256) + name + role + created_at
- role 分两档：'tenant'（普通租户，只能访问自己 tenant 的知识库）/ 'agent'（坐席，可操作人工工单）
- 无 key 走 default 租户（兼容期：仅读放行，写拒绝）
- 有效 key → 对应 tenant_id；无效 key → 401

安全说明：
- api_key 只存 sha256 哈希，不存明文，避免 DB 泄漏直接暴露密钥
- 鉴权由 FastAPI 依赖层完成（get_current_tenant / require_agent_role），
  本模块只负责存储与查询，不直接暴露业务逻辑
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Header, HTTPException, Request

# 使本模块可独立运行（脚本/测试）时也能读到 .env
load_dotenv()

# 数据库路径可被环境变量覆盖：测试指向临时库，避免污染开发库（见 tests/conftest.py）
DB_PATH = Path(
    os.getenv("AUTH_DB_PATH")
    or (Path(__file__).resolve().parent / "data" / "auth.db")
)


def _hash_key(api_key: str) -> str:
    """api_key 明文 → sha256 十六进制哈希"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接，自动建表"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            tenant_id TEXT PRIMARY KEY,
            api_key_hash TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'tenant',
            created_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    return conn


def init_auth_store() -> None:
    """初始化鉴权存储（建表），首次启动时由 main.py 调用"""
    conn = _get_conn()
    conn.close()


def create_tenant(tenant_id: str, api_key: str, name: str = "", role: str = "tenant") -> bool:
    """创建租户凭据（api_key 存哈希）。tenant_id 或 api_key 冲突时返回 False。"""
    conn = _get_conn()
    h = _hash_key(api_key)
    try:
        conn.execute(
            "INSERT INTO tenants (tenant_id, api_key_hash, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, h, name, role, _now()),
        )
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False
    finally:
        conn.close()
    return ok


def upsert_tenant(tenant_id: str, api_key: str, name: str = "", role: str = "tenant") -> str:
    """插入或更新租户凭据（幂等）。返回 'created' / 'updated' / 'conflict'。

    与 create_tenant 的区别：已存在的 tenant_id 会被更新而不是拒绝——
    这样环境变量可以充当凭据的唯一事实源，便于重复执行与密钥轮换。
    """
    conn = _get_conn()
    h = _hash_key(api_key)
    try:
        row = conn.execute(
            "SELECT tenant_id FROM tenants WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tenants (tenant_id, api_key_hash, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, h, name, role, _now()),
            )
            action = "created"
        else:
            conn.execute(
                "UPDATE tenants SET api_key_hash = ?, name = ?, role = ? WHERE tenant_id = ?",
                (h, name, role, tenant_id),
            )
            action = "updated"
        conn.commit()
    except sqlite3.IntegrityError:
        # 该 api_key 已被其他租户占用（api_key_hash UNIQUE 冲突）
        action = "conflict"
    finally:
        conn.close()
    return action


def ensure_seed_tenants() -> list[dict]:
    """从环境变量幂等 seed 两条内置凭据（启动时调用）。

    - `DEFAULT_TENANT_API_KEY` → 租户 `default`（role='tenant'）：知识库管理面板读写主租户数据
    - `AGENT_API_KEY`          → 租户 `agent`（role='agent'，tenant_id 保持 'agent'）：坐席工作台调人工工单接口

    取舍说明：环境变量为凭据的唯一事实源，已存在的租户按环境变量更新（支持轮换）；
    未配置的环境变量直接跳过，不阻断启动（无凭据时仍可用兼容期行为：GET 放行、写拒绝）。
    """
    specs = [
        ("default", os.getenv("DEFAULT_TENANT_API_KEY", "").strip(), "默认租户", "tenant"),
        ("agent", os.getenv("AGENT_API_KEY", "").strip(), "坐席", "agent"),
    ]
    results: list[dict] = []
    for tenant_id, key, name, role in specs:
        if not key:
            results.append({"tenant_id": tenant_id, "action": "skipped (env 未配置)"})
            continue
        results.append({"tenant_id": tenant_id, "action": upsert_tenant(tenant_id, key, name, role)})
    return results


def get_tenant_by_key(api_key: str) -> dict | None:
    """按 api_key 哈希查租户。返回 {'tenant_id', 'name', 'role'} 或 None。"""
    h = _hash_key(api_key)
    conn = _get_conn()
    row = conn.execute(
        "SELECT tenant_id, name, role FROM tenants WHERE api_key_hash = ?", (h,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {"tenant_id": row["tenant_id"], "name": row["name"], "role": row["role"]}


def get_current_tenant(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> str:
    """解析当前请求的租户（知识库读写接口用）。

    - 无 x-api-key：兼容期走 default 租户，但写操作拒绝（403）
    - 有效 key：返回对应 tenant_id
    - 无效 key：401
    """
    if not x_api_key:
        if request.method != "GET":
            raise HTTPException(status_code=403, detail="写操作需要提供 API Key")
        return "default"
    tenant = get_tenant_by_key(x_api_key)
    if tenant is None:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    return tenant["tenant_id"]


def require_agent_role(
    x_api_key: str | None = Header(default=None),
) -> str:
    """坐席角色校验（人工工单接口用）。

    - 无 key：401
    - 有效 key 但 role != 'agent'：403
    - role == 'agent'：返回 tenant_id
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少坐席 API Key")
    tenant = get_tenant_by_key(x_api_key)
    if tenant is None:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    if tenant["role"] != "agent":
        raise HTTPException(status_code=403, detail="需要坐席角色权限")
    return tenant["tenant_id"]
