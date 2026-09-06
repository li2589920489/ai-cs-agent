"""
商品知识库存储层 — SQLite 实现

用于运营/管理员管理商品知识（介绍/卖点/规格/FAQ），
商品知识 Agent 从知识库检索商品知识回答用户。
预留 tenant_id 字段支持多租户，后续可迁移 PostgreSQL。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "knowledge.db"


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接，自动建表"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            product_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            selling_points TEXT DEFAULT '',
            specs TEXT DEFAULT '',
            faq TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
        """
    )
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """把数据库行转为字典，文本字段转结构化"""
    d = dict(row)
    d["selling_points"] = [s for s in d.get("selling_points", "").split("\n") if s.strip()]
    d["specs"] = [s for s in d.get("specs", "").split("\n") if s.strip()]
    d["faq"] = [s for s in d.get("faq", "").split("\n") if s.strip()]
    return d


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_knowledge_store() -> None:
    """初始化数据库并灌入默认商品知识（首次启动）"""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM product_knowledge").fetchone()[0]
    if count > 0:
        conn.close()
        return

    # 默认商品知识种子数据
    seeds = [
        {
            "product_id": "P1001",
            "name": "良品铺子坚果大礼包 1200g",
            "description": "6种坚果混装：巴旦木、腰果、夏威夷果、核桃、榛子、开心果，精选大颗坚果，充氮锁鲜。",
            "selling_points": "6种坚果科学配比\n充氮包装锁鲜，口感酥脆\n送礼体面，节日爆款",
            "specs": "经典款（6袋装）\n豪华款（10袋装）",
            "faq": "保质期多久：180天，开封后建议7天内吃完\n是否含糖：原味坚果，无添加糖\n适合送礼吗：礼盒装，适合节日送礼",
        },
        {
            "product_id": "P1002",
            "name": "三只松鼠每日坚果 750g",
            "description": "每日一小袋科学配比，坚果+果干黄金比例，独立包装锁鲜。",
            "selling_points": "每日一小袋，方便携带\n坚果果干科学配比\n独立包装，锁鲜不潮",
            "specs": "30袋装\n15袋装",
            "faq": "每袋多少克：25g/袋\n含哪些果干：蔓越莓、葡萄干、蓝莓干\n适合儿童吗：适合3岁以上儿童食用",
        },
        {
            "product_id": "P1003",
            "name": "小米加湿器 4L 大容量",
            "description": "4L大容量持续加湿16小时，静音设计28dB，缺水自动断电，适合卧室办公室。",
            "selling_points": "4L大容量，16小时长续航\n28dB静音，不影响睡眠\n缺水自动断电，安全省心",
            "specs": "白色 4L\n绿色 4L",
            "faq": "加湿面积多大：适合20-30㎡房间\n需要加纯净水吗：建议加纯净水，避免水垢\n保修多久：整机保修1年",
        },
        {
            "product_id": "P1004",
            "name": "维达抽纸 3层 120抽×24包",
            "description": "原生木浆，3层加厚，无荧光剂，母婴可用，柔软亲肤。",
            "selling_points": "原生木浆，无荧光剂\n3层加厚，不易破\n母婴可用，安全放心",
            "specs": "3层120抽×24包",
            "faq": "一箱多少包：24包\n是否含荧光剂：不含，母婴可用\n纸张尺寸：180mm×120mm",
        },
    ]
    for s in seeds:
        conn.execute(
            """
            INSERT INTO product_knowledge
            (tenant_id, product_id, name, description, selling_points, specs, faq, created_at, updated_at)
            VALUES ('default', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                s["product_id"], s["name"], s["description"],
                s["selling_points"], s["specs"], s["faq"], _now(), _now(),
            ),
        )
    conn.commit()
    conn.close()


def list_knowledge(tenant_id: str = "default") -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM product_knowledge WHERE tenant_id = ? ORDER BY id DESC", (tenant_id,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_knowledge(knowledge_id: int, tenant_id: str = "default") -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM product_knowledge WHERE id = ? AND tenant_id = ?",
        (knowledge_id, tenant_id),
    ).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def create_knowledge(data: dict, tenant_id: str = "default") -> dict:
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO product_knowledge
        (tenant_id, product_id, name, description, selling_points, specs, faq, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant_id,
            data.get("product_id", ""),
            data.get("name", ""),
            data.get("description", ""),
            "\n".join(data.get("selling_points", [])) if isinstance(data.get("selling_points"), list) else data.get("selling_points", ""),
            "\n".join(data.get("specs", [])) if isinstance(data.get("specs"), list) else data.get("specs", ""),
            "\n".join(data.get("faq", [])) if isinstance(data.get("faq"), list) else data.get("faq", ""),
            now, now,
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return get_knowledge(new_id, tenant_id)


def bulk_import(items: list[dict], tenant_id: str = "default") -> dict:
    """批量导入商品知识，返回成功/失败统计"""
    conn = _get_conn()
    now = _now()
    success = 0
    failed = 0
    errors: list[str] = []

    for idx, item in enumerate(items, start=1):
        name = (item.get("name") or "").strip()
        if not name:
            failed += 1
            errors.append(f"第{idx}行：商品名为空，跳过")
            continue
        try:
            conn.execute(
                """
                INSERT INTO product_knowledge
                (tenant_id, product_id, name, description, selling_points, specs, faq, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    (item.get("product_id") or "").strip(),
                    name,
                    (item.get("description") or "").strip(),
                    "\n".join(item.get("selling_points") or []),
                    "\n".join(item.get("specs") or []),
                    "\n".join(item.get("faq") or []),
                    now, now,
                ),
            )
            success += 1
        except Exception as e:
            failed += 1
            errors.append(f"第{idx}行({name})：{e}")

    conn.commit()
    conn.close()
    return {"success": success, "failed": failed, "errors": errors[:20]}


def update_knowledge(knowledge_id: int, data: dict, tenant_id: str = "default") -> dict | None:
    conn = _get_conn()
    existing = conn.execute(
        "SELECT * FROM product_knowledge WHERE id = ? AND tenant_id = ?",
        (knowledge_id, tenant_id),
    ).fetchone()
    if not existing:
        conn.close()
        return None

    def field(key):
        val = data.get(key)
        if val is None:
            return existing[key]
        if isinstance(val, list):
            return "\n".join(val)
        return val

    conn.execute(
        """
        UPDATE product_knowledge SET
            product_id = ?, name = ?, description = ?,
            selling_points = ?, specs = ?, faq = ?, updated_at = ?
        WHERE id = ? AND tenant_id = ?
        """,
        (
            field("product_id"), field("name"), field("description"),
            field("selling_points"), field("specs"), field("faq"), _now(),
            knowledge_id, tenant_id,
        ),
    )
    conn.commit()
    conn.close()
    return get_knowledge(knowledge_id, tenant_id)


def delete_knowledge(knowledge_id: int, tenant_id: str = "default") -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM product_knowledge WHERE id = ? AND tenant_id = ?",
        (knowledge_id, tenant_id),
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def search_knowledge(query: str, tenant_id: str = "default", limit: int = 5) -> list[dict]:
    """关键词搜索商品知识（LIKE 匹配）"""
    conn = _get_conn()
    like = f"%{query}%"
    rows = conn.execute(
        """
        SELECT * FROM product_knowledge
        WHERE tenant_id = ?
          AND (name LIKE ? OR description LIKE ? OR selling_points LIKE ? OR faq LIKE ?)
        ORDER BY id DESC LIMIT ?
        """,
        (tenant_id, like, like, like, like, limit),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]
