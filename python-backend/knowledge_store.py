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

# 数据库路径可被环境变量覆盖：测试指向临时库，避免污染开发库（见 tests/conftest.py）
DB_PATH = Path(
    os.getenv("KNOWLEDGE_DB_PATH")
    or (Path(__file__).resolve().parent / "data" / "knowledge.db")
)


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
            category TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
        """
    )
    # 老库升级：早期版本没有 category 列（导入脚本算出的类目被丢弃，导致无法按类目统计/检索）
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(product_knowledge)").fetchall()}
    if "category" not in cols:
        conn.execute("ALTER TABLE product_knowledge ADD COLUMN category TEXT DEFAULT ''")
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
    if count == 0:
        _seed_default_knowledge(conn)
    _ensure_store_overview(conn)  # 幂等：任何时候启动都补齐 OVERVIEW 行
    conn.close()


def build_store_overview(tenant_id: str = "default", conn: sqlite3.Connection | None = None) -> dict:
    """从真实库统计生成店铺总览文案。

    为什么不再硬编码：demo 期写的「仅 3 大类共 4 件」+「本店不下述美妆/数码/母婴…」清单，
    在真实商品导入后与库内事实正面冲突（库里实际有蓝牙耳机、Dyson 吸尘器、婴儿洗衣液等），
    每回答一次概览类问题就变成主动说谎。改为按 category 实时统计，样例商品只取少量代表，
    从根上避免文案随数据变动而漂移。
    """
    own = conn is None
    if own:
        conn = _get_conn()
    rows = conn.execute(
        """
        SELECT category, name FROM product_knowledge
        WHERE tenant_id = ? AND product_id != 'OVERVIEW'
        """,
        (tenant_id,),
    ).fetchall()
    if own:
        conn.close()

    total = len(rows)
    buckets: dict[str, list[str]] = {}
    for r in rows:
        cat = (r["category"] or "").strip() or "未分类"
        buckets.setdefault(cat, []).append(r["name"] or "")

    if total == 0:
        return {
            "name": "店铺总览",
            "description": "本店铺知识库当前暂无在售商品记录。请如实告知顾客，不要凭训练数据编造品类。",
            "selling_points": "",
            "specs": "",
            "faq": "店铺卖什么：知识库暂无商品记录\n有没有某类商品：请用检索工具确认后再回答",
        }

    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines = [f"本店铺在售商品概览（实时统计，共 {total} 件）："]
    faq_lines: list[str] = []
    for cat, names in ordered:
        sample = "、".join(n[:20] for n in names[:3])
        lines.append(f"【{cat}】{len(names)} 件，例如：{sample}")
        faq_lines.append(f"有没有{cat}：有，{len(names)} 件")
    lines.append(
        "顾客询问具体商品、价格或规格时，请用检索工具查询；"
        "若知识库未收录相关商品或字段，如实告知「知识库暂未收录」，不要凭训练数据编造品类。"
    )

    return {
        "name": "店铺总览",
        "description": "\n".join(lines),
        "selling_points": "\n".join(f"{cat}（{len(names)} 件）" for cat, names in ordered),
        "specs": "\n".join(f"{cat} {len(names)} 件" for cat, names in ordered),
        "faq": "\n".join(faq_lines + ["店铺卖什么：见上方类目统计"]),
    }


def _ensure_store_overview(conn: sqlite3.Connection) -> None:
    """幂等刷新店铺总览行：内容由真实库实时统计生成（见 build_store_overview）。

    注意这里是「存在则更新」而非「存在则跳过」——文案硬编码时代跳过即可保证幂等，
    但改为统计生成后必须每次启动刷新，否则商品的增删不会反映到概览里。

    但「内容没变就不写」是必须的：updated_at 会被 knowledge_signature() 计入向量索引
    指纹（行数:MAX(updated_at)），一次纯粹的时间戳刷新会让索引在每次启动时都被判为
    过期，从而全量重向量化 1300+ chunk（实测每次冷启动多付 40s 以上）。
    """
    ov = build_store_overview(conn=conn)
    new_fields = (ov["name"], ov["description"], ov["selling_points"], ov["specs"], ov["faq"])
    existing = conn.execute(
        "SELECT id, name, description, selling_points, specs, faq FROM product_knowledge "
        "WHERE product_id = 'OVERVIEW' AND tenant_id = 'default' LIMIT 1"
    ).fetchone()
    if existing:
        old_fields = tuple(
            existing[k] for k in ("name", "description", "selling_points", "specs", "faq")
        )
        if old_fields == new_fields:
            return  # 内容未变：不碰 updated_at，保住索引指纹
        conn.execute(
            """
            UPDATE product_knowledge
            SET name = ?, description = ?, selling_points = ?, specs = ?, faq = ?, updated_at = ?
            WHERE id = ?
            """,
            (*new_fields, _now(), existing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO product_knowledge
            (tenant_id, product_id, name, description, selling_points, specs, faq, category, created_at, updated_at)
            VALUES ('default', 'OVERVIEW', ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (ov["name"], ov["description"], ov["selling_points"], ov["specs"], ov["faq"], _now(), _now()),
        )
    conn.commit()


def refresh_store_overview() -> None:
    """按当前库内容刷新店铺总览行。

    批量写入流程（如导入脚本）不经过 init_knowledge_store，需显式调用本函数，
    否则概览会停留在上次启动时的统计快照——件数与类目分布都会失真。
    """
    conn = _get_conn()
    _ensure_store_overview(conn)
    conn.close()


def _seed_default_knowledge(conn: sqlite3.Connection) -> None:
    """首次启动时灌入 4 条商品知识种子。

    店铺总览（product_id='OVERVIEW'）不在此处写死：由 _ensure_store_overview
    按真实库统计生成，避免硬编码的件数与类目清单和导入数据冲突。

    连接所有权：conn 由调用方持有，本函数只 commit、**不 close**。
    """
    # 默认商品知识种子数据
    seeds = [
        {
            "product_id": "P1001",
            "category": "零食",
            "name": "良品铺子坚果大礼包 1200g",
            "description": "6种坚果混装：巴旦木、腰果、夏威夷果、核桃、榛子、开心果，精选大颗坚果，充氮锁鲜。",
            "selling_points": "6种坚果科学配比\n充氮包装锁鲜，口感酥脆\n送礼体面，节日爆款",
            "specs": "经典款（6袋装）\n豪华款（10袋装）",
            "faq": "保质期多久：180天，开封后建议7天内吃完\n是否含糖：原味坚果，无添加糖\n适合送礼吗：礼盒装，适合节日送礼",
        },
        {
            "product_id": "P1002",
            "category": "零食",
            "name": "三只松鼠每日坚果 750g",
            "description": "每日一小袋科学配比，坚果+果干黄金比例，独立包装锁鲜。",
            "selling_points": "每日一小袋，方便携带\n坚果果干科学配比\n独立包装，锁鲜不潮",
            "specs": "30袋装\n15袋装",
            "faq": "每袋多少克：25g/袋\n含哪些果干：蔓越莓、葡萄干、蓝莓干\n适合儿童吗：适合3岁以上儿童食用",
        },
        {
            "product_id": "P1003",
            "category": "家电",
            "name": "小米加湿器 4L 大容量",
            "description": "4L大容量持续加湿16小时，静音设计28dB，缺水自动断电，适合卧室办公室。",
            "selling_points": "4L大容量，16小时长续航\n28dB静音，不影响睡眠\n缺水自动断电，安全省心",
            "specs": "白色 4L\n绿色 4L",
            "faq": "加湿面积多大：适合20-30㎡房间\n需要加纯净水吗：建议加纯净水，避免水垢\n保修多久：整机保修1年",
        },
        {
            "product_id": "P1004",
            "category": "日用品",
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
            (tenant_id, product_id, name, description, selling_points, specs, faq, category, created_at, updated_at)
            VALUES ('default', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                s["product_id"], s["name"], s["description"],
                s["selling_points"], s["specs"], s["faq"], s.get("category", ""), _now(), _now(),
            ),
        )
    conn.commit()
    # 不在此处 close：连接由调用方 init_knowledge_store 持有，它之后还要用同一个
    # 连接执行 _ensure_store_overview(conn) 补齐总览行。
    # 曾在此 close 的后果：空库首次建库时抛出
    # `sqlite3.ProgrammingError: Cannot operate on a closed database`
    # —— 即"全新环境首次启动"（新克隆 / 空 volume 的 Docker 容器 / 新测试临时库）必然崩溃。
    # 本地库已有数据时 count != 0，不会走到这里，因此长期未被发现。


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


def get_knowledge_by_product_id(product_id: str, tenant_id: str = "default") -> dict | None:
    """按业务商品编号查知识条目。

    与 get_knowledge 的区别：那个按数据库主键 id 查；这个按业务编号（如 EC1066 / P1001）查。
    导入的真实商品只有业务编号，主键 id 是导入顺序生成的、调用方无法预知。
    """
    if not product_id:
        return None
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT * FROM product_knowledge
        WHERE product_id = ? AND tenant_id = ? AND product_id != 'OVERVIEW'
        ORDER BY id DESC LIMIT 1
        """,
        (product_id.strip(), tenant_id),
    ).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def list_by_category(category: str, tenant_id: str = "default", limit: int = 5) -> list[dict]:
    """按类目列出商品知识（类目级检索用）"""
    if not category:
        return []
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM product_knowledge
        WHERE tenant_id = ? AND category = ? AND product_id != 'OVERVIEW'
        ORDER BY id DESC LIMIT ?
        """,
        (tenant_id, category.strip(), limit),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def category_stats(tenant_id: str = "default") -> list[dict]:
    """各类目的在售件数（用于概览统计与类目级检索的候选提示）"""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(category), ''), '未分类') AS category, COUNT(*) AS cnt
        FROM product_knowledge
        WHERE tenant_id = ? AND product_id != 'OVERVIEW'
        GROUP BY 1 ORDER BY cnt DESC
        """,
        (tenant_id,),
    ).fetchall()
    conn.close()
    return [{"category": r["category"], "count": r["cnt"]} for r in rows]


def knowledge_signature(tenant_id: str = "default") -> str:
    """知识库指纹（行数 + 最新 updated_at），用于判断向量索引是否已过期。

    为什么用指纹而不是 dirty 标记：一次轻量聚合查询即可，进程重启也不丢状态，
    且批量导入时不会因为逐条写入而反复触发索引重建。
    """
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt, COALESCE(MAX(updated_at), '') AS latest
        FROM product_knowledge WHERE tenant_id = ?
        """,
        (tenant_id,),
    ).fetchone()
    conn.close()
    return f"{row['cnt']}:{row['latest']}"


def create_knowledge(data: dict, tenant_id: str = "default") -> dict:
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO product_knowledge
        (tenant_id, product_id, name, description, selling_points, specs, faq, category, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant_id,
            data.get("product_id", ""),
            data.get("name", ""),
            data.get("description", ""),
            "\n".join(data.get("selling_points", [])) if isinstance(data.get("selling_points"), list) else data.get("selling_points", ""),
            "\n".join(data.get("specs", [])) if isinstance(data.get("specs"), list) else data.get("specs", ""),
            "\n".join(data.get("faq", [])) if isinstance(data.get("faq"), list) else data.get("faq", ""),
            (data.get("category") or "").strip(),
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
                (tenant_id, product_id, name, description, selling_points, specs, faq, category, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    (item.get("product_id") or "").strip(),
                    name,
                    (item.get("description") or "").strip(),
                    "\n".join(item.get("selling_points") or []),
                    "\n".join(item.get("specs") or []),
                    "\n".join(item.get("faq") or []),
                    (item.get("category") or "").strip(),
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
            selling_points = ?, specs = ?, faq = ?, category = ?, updated_at = ?
        WHERE id = ? AND tenant_id = ?
        """,
        (
            field("product_id"), field("name"), field("description"),
            field("selling_points"), field("specs"), field("faq"), field("category"), _now(),
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


_OVERVIEW_KEYWORDS = frozenset(
    [
        "种类", "分类", "类目", "有什么", "有啥", "卖什么", "卖哪些", "经营什么",
        "全部商品", "在售", "都有什么", "店铺", "店面", "类目", "品类",
    ]
)


def _is_overview_query(query: str) -> bool:
    """判断 query 是否是"店铺概览/分类"类问题。命中时优先返回 OVERVIEW 行。"""
    q = (query or "").lower()
    return any(kw in q for kw in _OVERVIEW_KEYWORDS)


def search_knowledge(query: str, tenant_id: str = "default", limit: int = 5) -> list[dict]:
    """关键词搜索商品知识（LIKE 匹配）

    概览类 query（"你们卖什么/有什么商品"等）优先返回店铺总览文档，
    确保 Agent 在工具无相关结果时拿到确定的"只售卖 3 大类"事实，杜绝凭训练数据兜底。
    """
    conn = _get_conn()

    # 概览类 query：先抓 OVERVIEW 行，其余按原 LIKE 逻辑补全
    if _is_overview_query(query):
        overview_row = conn.execute(
            """
            SELECT * FROM product_knowledge
            WHERE tenant_id = ? AND product_id = 'OVERVIEW'
            LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()
        if overview_row:
            like = f"%{query}%"
            extra_rows = conn.execute(
                """
                SELECT * FROM product_knowledge
                WHERE tenant_id = ?
                  AND product_id != 'OVERVIEW'
                  AND (name LIKE ? OR description LIKE ? OR selling_points LIKE ? OR faq LIKE ?)
                ORDER BY id DESC LIMIT ?
                """,
                (tenant_id, like, like, like, like, max(0, limit - 1)),
            ).fetchall()
            conn.close()
            return [_row_to_dict(overview_row)] + [_row_to_dict(r) for r in extra_rows]

    # 普通 query：原 LIKE 逻辑
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
