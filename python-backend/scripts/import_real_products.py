"""
真实商品数据导入脚本 — 老板视角的工程链路

模拟"电商老板拿到一份原始淘宝商品标题库（parquet），通过清洗、字段映射、
入库到 ai-cs-agent 商品知识库"的完整流程。

数据源: data/eval/ecom_retrieval/corpus.parquet
        (C-MTEB/EcomRetrieval 公开基准，100902 条真实淘宝商品标题)

完整链路:
  1. 加载 parquet 原始数据
  2. 文本清洗 (去空/去重/长度过滤)
  3. 关键词分类 (零食/家电/日用品)
  4. 字段映射 (id+text → knowledge_store schema)
  5. 质量过滤 + 配额抽取 (零食500+家电300+日用品500)
  6. 批量入库 (knowledge_store.bulk_import)
  7. 输出报告 (清洗报告 / 原始样本 / 清洗后样本)
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import knowledge_store  # noqa: E402

# 数据源
CORPUS_PATH = ROOT / "data" / "eval" / "ecom_retrieval" / "corpus.parquet"

# 输出
OUT_DIR = ROOT / "data"
RAW_SAMPLE_PATH = OUT_DIR / "real_products_raw_sample.json"
CLEANED_SAMPLE_PATH = OUT_DIR / "real_products_cleaned.json"
REPORT_PATH = OUT_DIR / "import_report.json"

# 分类关键词 (强特征词)
CATEGORY_KEYWORDS = {
    "零食": (
        r"坚果|饼干|薯片|糖果|巧克力|果干|点心|卤味|肉脯|蜜饯|海苔|瓜子|"
        r"花生|果冻|面包|蛋糕|方便面|膨化|饮料|果汁|奶茶|奶粉|咖啡|茶|酒|"
        r"酱油|醋|糖|盐|调料|味精|鸡精|油|米|面|麦片|燕麦|汤圆|水饺|包子|"
        r"馒头|豆浆|豆腐|酸奶|牛奶|火腿|香肠|腊肉|辣条|锅巴|麻花|酥饼|"
        r"月饼|糕点|曲奇|威化|冰淇淋|雪糕|泡芙|布丁|蛋卷|桃酥|凤梨酥|"
        r"粽子|元宵|八宝|罐头|果脯|蜜枣|葡萄干|蔓越莓|蓝莓|草莓|果酱|"
        r"蜂蜜|花茶|果茶|冰糖|红糖|白糖|食盐|鱼|虾|蟹|贝|海参|鲍鱼|燕窝"
    ),
    "家电": (
        r"加湿|电饭煲|电水壶|电风扇|台灯|插座|空调|冰箱|洗衣机|热水器|"
        r"吸尘器|净化器|吹风|剃须|微波炉|烤箱|榨汁机|咖啡机|扫地机|"
        r"豆浆机|音响|电视|音箱|手机|平板|相机|电脑|耳机|充电|电池|"
        r"电磁炉|开关|电灯|灯|电热|智能|遥控|蓝牙|路由器|鼠标|键盘|"
        r"显示器|打印|扫描|投影|耳麦|麦克风|摄像头|监控|门铃|门锁|"
        r"报警|烟雾|传感|电动|遥控器|适配|接线|插头|电板|净水|饮水机"
    ),
    "日用品": (
        r"抽纸|卫生纸|洗发|沐浴|洗衣|牙膏|牙刷|毛巾|纸巾|湿巾|洗衣液|"
        r"洗洁精|垃圾袋|保鲜膜|收纳|拖把|扫把|肥皂|香皂|洗手液|消毒液|"
        r"杀虫|蚊香|花露|浴巾|浴袍|枕套|被套|床单|地毯|窗帘|门垫|"
        r"砧板|保鲜盒|储物|晾衣|衣架|挂钩|抹布|钢丝球|海绵|刷|扫|"
        r"簸箕|畚斗|压缩|桌布|围裙|袖套|鞋垫|袜|拖鞋|被|枕|垫|"
        r"毯|床|柜|桌|椅|凳|镜|架|篮|筒|刀|叉|勺|餐"
    ),
}

# 配额
QUOTAS = {"零食": 500, "家电": 300, "日用品": 500}

# 长度阈值
MIN_LEN = 10
MAX_LEN = 60  # 太长往往是堆砌关键词

# 质量黑名单：违禁品类 + 功效宣称
# 取舍：语料取自公开检索基准的真实淘宝标题，含「印度神油」「排毒燃脂」等违规表述，
# 演示时被检索出来是负分项，若真上线则属平台违规风险。
# 注意不要把「成人」当作独立关键词——它大量出现在「成人中老年」「儿童成人」等正常语境，
# 单独匹配会造成约 25 条误报；这里只匹配明确的违禁/功效表述。
BLOCKLIST_PATTERN = re.compile(
    r"印度神油|成人用品|情趣用品|催情|迷药|春药|伟哥|壮阳|延时喷剂|增粗|增大膏|"
    r"燃脂|减肥(药|茶|贴|腰带|果|代餐)|排毒|瘦身|瘦腿|瘦脸|丰胸|消脂|泻药"
)


def cleanup_existing() -> int:
    """幂等：清空之前导入的 EC* 商品，避免重复入库"""
    conn = knowledge_store._get_conn()
    cur = conn.execute("DELETE FROM product_knowledge WHERE product_id LIKE 'EC%'")
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        print(f"    [清理] 删除已有 EC* 数据: {deleted} 条")
    return deleted


def load_raw_corpus() -> pd.DataFrame:
    """Step 1: 加载 parquet 原始数据"""
    print("[Step 1] 加载 corpus.parquet...")
    df = pd.read_parquet(CORPUS_PATH)
    df["id"] = df["id"].astype(str)
    df["text"] = df["text"].astype(str)
    print(f"    原始数据: {len(df)} 条")
    return df


def clean_text(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Step 2: 数据清洗 — 去空/去重/长度过滤

    返回 (清洗后数据, 各阶段计数)。计数显式返回而非事后反推，
    避免流程增删字段时统计公式失准。
    """
    print("[Step 2] 数据清洗...")
    stats: dict = {"raw_count": len(df)}

    # 去空
    df = df[df["text"].str.strip().astype(bool)].copy()
    stats["after_drop_empty"] = len(df)
    stats["dropped_empty"] = stats["raw_count"] - stats["after_drop_empty"]
    print(f"    去空: {stats['raw_count']} -> {stats['after_drop_empty']} (丢 {stats['dropped_empty']})")

    # 去重 (按 text)
    df = df.drop_duplicates(subset=["text"], keep="first").copy()
    stats["after_dedup"] = len(df)
    stats["dropped_duplicate"] = stats["after_drop_empty"] - stats["after_dedup"]
    print(f"    去重: {stats['after_drop_empty']} -> {stats['after_dedup']} (丢 {stats['dropped_duplicate']})")

    # 长度过滤
    df = df[df["text"].str.len().between(MIN_LEN, MAX_LEN)].copy()
    stats["after_length_filter"] = len(df)
    stats["dropped_by_length"] = stats["after_dedup"] - stats["after_length_filter"]
    print(
        f"    长度过滤({MIN_LEN}-{MAX_LEN}字符): {stats['after_dedup']} -> "
        f"{stats['after_length_filter']} (丢 {stats['dropped_by_length']})"
    )

    return df, stats


def filter_blocklist(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Step 2.5: 质量过滤 — 剔除违禁品类与功效宣称标题

    返回 (保留数据, 被剔除记录)。剔除记录写进报告，使「过滤了多少、过滤了哪些」可追溯。
    """
    print("[Step 2.5] 质量过滤 (违禁品类/功效宣称)...")
    hit_mask = df["text"].str.contains(BLOCKLIST_PATTERN, na=False)
    blocked = df[hit_mask].copy()
    kept = df[~hit_mask].copy()
    print(f"    命中黑名单: {len(blocked)} 条 -> 剔除后 {len(kept)} 条")
    for _, r in blocked.head(10).iterrows():
        print(f"      x {str(r['text'])[:40]}")
    return kept, [{"id": r["id"], "text": str(r["text"])[:60]} for _, r in blocked.iterrows()]


def classify_category(df: pd.DataFrame) -> pd.DataFrame:
    """Step 3: 关键词分类 — 零食/家电/日用品"""
    print("[Step 3] 关键词分类...")
    records = []
    for _, row in df.iterrows():
        text = row["text"]
        scores = {}
        for cat, kw_pattern in CATEGORY_KEYWORDS.items():
            scores[cat] = len(re.findall(kw_pattern, text))
        best_cat = max(scores, key=scores.get)
        if scores[best_cat] == 0:
            continue  # 不命中任何类目，丢弃
        records.append({
            "id": row["id"],
            "text": text,
            "category": best_cat,
            "category_score": scores[best_cat],
        })

    df_classified = pd.DataFrame(records)
    print(f"    分类后总数: {len(df_classified)}")
    print("    各类目分布:")
    for cat in ["零食", "家电", "日用品"]:
        n = int((df_classified["category"] == cat).sum())
        print(f"      {cat}: {n} 条")

    return df_classified


def extract_selling_points(text: str) -> list[str]:
    """从标题里提取卖点关键词作为 selling_points

    简单策略: 用顿号/逗号/空格分词，保留长度 >= 2 的词作为卖点
    (真实工程: title 是堆砌关键词的，分词结果就是卖点)
    """
    parts = re.split(r"[、,,。;; /|]+", text)
    parts = [p.strip() for p in parts if len(p.strip()) >= 2]
    return parts[:3]  # 最多 3 条


def map_fields(classified_df: pd.DataFrame) -> list[dict]:
    """Step 4: 字段映射 — id+text -> knowledge_store schema"""
    print("[Step 4] 字段映射...")
    items = []
    for _, row in classified_df.iterrows():
        item = {
            "_id": row["id"],
            "_category": row["category"],
            "product_id": f"EC{row['id']}",  # EC 前缀避免和 P1001~P1004 冲突
            "name": row["text"],
            "description": row["text"],  # description 暂用标题本身 (真实场景: title 拼凑)
            "selling_points": extract_selling_points(row["text"]),
            "specs": [],  # 无来源，留空 (真实工程字段缺失场景)
            "faq": [],  # 无来源，留空 (真实工程字段缺失场景)
        }
        items.append(item)
    print(f"    映射后: {len(items)} 条")
    return items


def sample_by_quota(items: list[dict]) -> list[dict]:
    """Step 5: 配额抽取 — 各类按 QUOTAS 取"""
    print("[Step 5] 配额抽取...")
    by_cat = defaultdict(list)
    for it in items:
        by_cat[it["_category"]].append(it)

    sampled = []
    for cat, quota in QUOTAS.items():
        pool = by_cat.get(cat, [])
        if len(pool) >= quota:
            chosen = pool[:quota]
            print(f"    {cat}: 池={len(pool)}，取={quota}")
        else:
            chosen = pool
            print(f"    {cat}: 池={len(pool)}，取={len(pool)} (不足配额)")
        sampled.extend(chosen)

    print(f"    抽取后: {len(sampled)} 条")
    return sampled


def bulk_import_items(sampled: list[dict]) -> dict:
    """Step 6: 批量入库 — 只剥离内部辅助字段，业务字段（含类目）全部落库"""
    print("[Step 6] 批量入库...")
    items_for_import = []
    for it in sampled:
        items_for_import.append({
            "product_id": it["product_id"],
            "name": it["name"],
            "description": it["description"],
            "selling_points": it["selling_points"],
            "specs": it["specs"],
            "faq": it["faq"],
            # 类目此前只落在报告里、入库时被剥掉，导致概览统计与类目级检索都无从下手
            "category": it["_category"],
        })

    result = knowledge_store.bulk_import(items_for_import)
    print(f"    成功={result['success']}, 失败={result['failed']}")
    if result["errors"]:
        print(f"    前 5 条错误: {result['errors'][:5]}")
    return result


def save_samples(raw_df: pd.DataFrame, classified_df: pd.DataFrame, sampled: list[dict]) -> None:
    """保存样本文件（清洗后的示例产物）"""
    raw_sample = raw_df.head(10).to_dict("records")
    with open(RAW_SAMPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(raw_sample, f, ensure_ascii=False, indent=2)
    print(f"    [产物] 原始样本(前10): {RAW_SAMPLE_PATH.name}")

    cleaned_sample = []
    for it in sampled[:10]:
        cleaned_sample.append({
            "product_id": it["product_id"],
            "name": it["name"],
            "description": it["description"],
            "selling_points": it["selling_points"],
            "specs": it["specs"],
            "faq": it["faq"],
            "category": it["_category"],
        })
    with open(CLEANED_SAMPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(cleaned_sample, f, ensure_ascii=False, indent=2)
    print(f"    [产物] 清洗后样本(前10): {CLEANED_SAMPLE_PATH.name}")


def build_report(
    raw_df: pd.DataFrame,
    clean_stats: dict,
    blocked_rows: list[dict],
    classified_df: pd.DataFrame,
    sampled: list[dict],
    import_result: dict,
    cleanup_deleted: int,
) -> dict:
    """Step 7: 输出报告"""
    print("[Step 7] 输出报告...")

    by_category = defaultdict(int)
    for it in sampled:
        by_category[it["_category"]] += 1

    report = {
        "data_source": {
            "path": str(CORPUS_PATH),
            "raw_total": len(raw_df),
            "dataset": "C-MTEB/EcomRetrieval (真实淘宝商品 query-corpus 检索基准)",
        },
        "cleaning": {
            **clean_stats,
            "filtered_by_blocklist": len(blocked_rows),
            "after_blocklist_filter": clean_stats["after_length_filter"] - len(blocked_rows),
            "after_classify": len(classified_df),
            "min_len": MIN_LEN,
            "max_len": MAX_LEN,
        },
        "blocklist_filter": {
            "pattern": BLOCKLIST_PATTERN.pattern,
            "filtered_count": len(blocked_rows),
            "filtered_samples": blocked_rows[:10],
        },
        "classification": {
            "by_category_after_classify": {
                cat: int((classified_df["category"] == cat).sum())
                for cat in ["零食", "家电", "日用品"]
            },
            "quotas": QUOTAS,
        },
        "sampling": {
            "total_sampled": len(sampled),
            "by_category": dict(by_category),
        },
        "field_mapping_strategy": {
            "product_id": "EC{原 id} (前缀避免与 demo P1001-P1004 冲突)",
            "name": "原始标题",
            "description": "原始标题 (title 拼凑是真实电商场景)",
            "selling_points": "从标题分词提取 (强特征词)",
            "specs": "无来源，留空 (字段缺失是真实工程常态)",
            "faq": "无来源，留空 (字段缺失是真实工程常态)",
            "category": "标题关键词分类 (零食/家电/日用品)，落库供概览统计与类目级检索",
        },
        "import": {
            "cleanup_deleted_existing": cleanup_deleted,
            "success": import_result["success"],
            "failed": import_result["failed"],
            "first_errors": import_result["errors"][:5],
        },
        "knowledge_db_after": {
            "total_products": len(knowledge_store.list_knowledge()),
            "ec_count": sum(
                1 for r in knowledge_store.list_knowledge()
                if (r.get("product_id") or "").startswith("EC")
            ),
        },
    }

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"    [产物] 报告: {REPORT_PATH.name}")
    return report


def main():
    print("=" * 60)
    print("真实商品数据导入 — 老板视角的工程链路")
    print("=" * 60)

    # 0. 幂等清理
    print("[Step 0] 清理已有 EC* 数据 (幂等)...")
    cleanup_deleted = cleanup_existing()

    # 1. 加载
    raw_df = load_raw_corpus()

    # 2. 清洗
    cleaned_df, clean_stats = clean_text(raw_df)

    # 2.5 质量过滤（违禁品类 / 功效宣称）
    cleaned_df, blocked_rows = filter_blocklist(cleaned_df)

    # 3. 分类
    classified_df = classify_category(cleaned_df)

    # 4. 字段映射
    items = map_fields(classified_df)

    # 5. 配额抽取
    sampled = sample_by_quota(items)

    # 保存样本
    save_samples(raw_df, classified_df, sampled)

    # 6. 批量入库
    import_result = bulk_import_items(sampled)

    # 6.5 刷新店铺总览：OVERVIEW 由真实库统计生成，批量写入后必须重算，否则概览仍是旧快照
    knowledge_store.refresh_store_overview()
    print("    [刷新] 店铺总览已按新数据重算")

    # 7. 报告
    report = build_report(
        raw_df, clean_stats, blocked_rows, classified_df, sampled, import_result, cleanup_deleted
    )

    print()
    print("=" * 60)
    print("完成。汇总:")
    print(f"  原始:      {len(raw_df)} 条")
    print(f"  质量过滤:  剔除 {len(blocked_rows)} 条 (违禁品类/功效宣称)")
    print(f"  清洗后分类: {len(classified_df)} 条")
    print(f"  配额抽取:   {len(sampled)} 条")
    print(f"  入库成功:   {import_result['success']} 条")
    print(f"  入库失败:   {import_result['failed']} 条")
    print(f"  知识库现有: {report['knowledge_db_after']['total_products']} 条 (其中 EC{report['knowledge_db_after']['ec_count']} 条)")
    print("=" * 60)


if __name__ == "__main__":
    main()