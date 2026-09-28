"""Step 1 (改造版): 基于 1300 EC* 自建 query 评测集。

背景: EcomRetrieval 1000 qrels 是基于 100k corpus 标注的，投影到 1300 EC* 子集后
       命中 0 条（子集偏差 — qrels 的"正例"刚好不在这 1.3% 子集里）。

改造: 从 1300 EC* 的标题自动生成"自然问句"作为 query 集，每条 query 的 ground truth
      就是原 EC* corpus-id。再混入一批"未收录 query"测 RAG 是否编造。

构造的 query 类型:
  Q1: 直接 query     - 用 EC* 标题里的关键词组合（"维达抽纸 3层 120抽"）
  Q2: 自然修饰 query - 在标题前加"有没有""请问"等口语词
  Q3: 价格/数量 query - 提取数字+品类词作 query（"维达 3层 120抽"）
  Q4: 未收录 query   - 笔记本/手机/口红等 EC* 没有的品类词（期望不召回）

输出:
  data/eval/real_query_eval/sub_corpus.json
  data/eval/real_query_eval/query_eval_set.json  (query + gold_corpus_id + 类型)
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "eval" / "real_query_eval"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 输入
ECOM_DIR = ROOT / "data" / "eval" / "ecom_retrieval"
CORPUS_JSON = OUT_DIR / "sub_corpus.json"
QUERY_SET_JSON = OUT_DIR / "query_eval_set.json"

# 输出配额
NUM_Q1 = 30  # 直接 query
NUM_Q2 = 30  # 自然修饰 query
NUM_Q3 = 15  # 价格/数量 query
NUM_Q4 = 15  # 未收录 query

# 未收录品类（必须 EC* 没有）
UNSEEN_CATEGORIES = [
    ("笔记本电脑", "笔记本"),
    ("手机", "手机"),
    ("口红色号", "口红"),
    ("洗衣机", "洗衣机"),
    ("空调", "空调"),
    ("电冰箱", "冰箱"),
    ("电视机", "电视"),
    ("洗发水", "洗发水"),
    ("面霜", "面霜"),
    ("香水", "香水"),
    ("耳机", "耳机"),
    ("照相机", "相机"),
    ("手表", "手表"),
    ("皮鞋", "皮鞋"),
    ("羽绒服", "羽绒服"),
]

random.seed(42)


def extract_keywords(title: str, max_kw: int = 4) -> list[str]:
    """从 title 提取 1~4 个关键词 — 用 regex 抓品牌 + 品类词"""
    # 去掉数字、标点
    cleaned = re.sub(r"[【】\[\]()()（）+\-/、，；。|]", " ", title)
    # 切词（简单按空格/中文标点）
    tokens = [t for t in re.split(r"[\s,，;;]+", cleaned) if 2 <= len(t) <= 8]
    # 优先返回含数字/字母的 token（如 "3层" "120抽"）
    tokens.sort(key=lambda x: (0 if re.search(r"\d", x) else 1, -len(x)))
    return tokens[:max_kw]


def make_q1_direct(title: str) -> str:
    """直接 query: 取标题前 12 字"""
    return title[:12].strip()


def make_q2_natural(title: str) -> str:
    """自然修饰 query"""
    kws = extract_keywords(title)
    if not kws:
        return title[:10]
    # 随机挑修饰前缀
    prefix = random.choice(["有没有", "请问", "想要", "想买", "找一下"])
    return f"{prefix}{kws[0]}"


def make_q3_price(title: str) -> str:
    """价格/数量 query: 含数字的关键词组合"""
    kws = extract_keywords(title, max_kw=3)
    digit_kws = [k for k in kws if re.search(r"\d", k)]
    if digit_kws:
        return digit_kws[0]
    return kws[0] if kws else title[:8]


def main() -> None:
    # 1) 读 EcomRetrieval 完整 corpus，按 corpus-id 反查 text
    import pandas as pd

    corpus_df = pd.read_parquet(ECOM_DIR / "corpus.parquet")
    corpus_df["id"] = corpus_df["id"].astype(int)
    corpus_df["text"] = corpus_df["text"].astype(str)
    print(f"[1] EcomRetrieval corpus: {len(corpus_df)} 条")

    # 2) 拿 1300 EC* 的原始 corpus-id
    import knowledge_store

    all_products = knowledge_store.list_knowledge()
    ec_products = [r for r in all_products if (r.get("product_id") or "").startswith("EC")]
    ec_corpus_ids = sorted(int(p["product_id"][2:]) for p in ec_products)
    print(f"[2] 1300 EC* 原始 corpus-id: {len(ec_corpus_ids)} 条 (范围 {ec_corpus_ids[0]} - {ec_corpus_ids[-1]})")

    # 3) 投影到 EcomRetrieval corpus
    sub_corpus = corpus_df[corpus_df["id"].isin(set(ec_corpus_ids))].copy()
    print(f"[3] sub_corpus: {len(sub_corpus)} 条 (覆盖率 {len(sub_corpus)/len(ec_corpus_ids)*100:.1f}%)")

    # 保存 sub_corpus
    sub_records = [{"id": str(r["id"]), "text": r["text"]} for _, r in sub_corpus.iterrows()]
    with open(CORPUS_JSON, "w", encoding="utf-8") as f:
        json.dump(sub_records, f, ensure_ascii=False, indent=2)
    print(f"    [产物] sub_corpus -> {CORPUS_JSON.name}")

    # 4) 自建 query 集
    sub_corpus_dict = {int(r["id"]): r["text"] for _, r in sub_corpus.iterrows()}
    sub_corpus_ids_list = list(sub_corpus_dict.keys())

    eval_set = []

    # Q1 直接 query - 从每个商品各取 1 条
    sample_n = min(NUM_Q1, len(sub_corpus_ids_list))
    sample_ids = random.sample(sub_corpus_ids_list, sample_n)
    for cid in sample_ids:
        title = sub_corpus_dict[cid]
        eval_set.append({
            "query_id": f"q1_{cid}",
            "query": make_q1_direct(title),
            "gold_corpus_id": str(cid),
            "type": "Q1_direct",
            "gold_text": title,
        })

    # Q2 自然修饰 query
    sample_ids = random.sample(sub_corpus_ids_list, min(NUM_Q2, len(sub_corpus_ids_list)))
    for cid in sample_ids:
        title = sub_corpus_dict[cid]
        eval_set.append({
            "query_id": f"q2_{cid}",
            "query": make_q2_natural(title),
            "gold_corpus_id": str(cid),
            "type": "Q2_natural",
            "gold_text": title,
        })

    # Q3 价格/数量 query
    sample_ids = random.sample(sub_corpus_ids_list, min(NUM_Q3, len(sub_corpus_ids_list)))
    for cid in sample_ids:
        title = sub_corpus_dict[cid]
        eval_set.append({
            "query_id": f"q3_{cid}",
            "query": make_q3_price(title),
            "gold_corpus_id": str(cid),
            "type": "Q3_price",
            "gold_text": title,
        })

    # Q4 未收录 query - 期望召回为空（幻觉率评估用）
    for cat_name, kw in UNSEEN_CATEGORIES[:NUM_Q4]:
        eval_set.append({
            "query_id": f"q4_{kw}",
            "query": f"你们有{kw}卖吗" if random.random() < 0.5 else kw,
            "gold_corpus_id": None,
            "type": "Q4_unseen",
            "gold_text": None,
        })

    print(f"[4] 自建 query: {len(eval_set)} 条 (Q1={NUM_Q1}, Q2={NUM_Q2}, Q3={NUM_Q3}, Q4={NUM_Q4})")

    with open(QUERY_SET_JSON, "w", encoding="utf-8") as f:
        json.dump(eval_set, f, ensure_ascii=False, indent=2)
    print(f"    [产物] query_eval_set -> {QUERY_SET_JSON.name}")

    # 5) 报告
    print("\n=== 评测集构造完成 ===")
    print(f"  corpus:  {len(sub_records)} 条 (1300 EC*)")
    print(f"  queries: {len(eval_set)} 条 (含 Q1/Q2/Q3 正例 75 条 + Q4 未收录 15 条)")
    print(f"  Q4 期望: 召回率 0%（不应在 EC* 语料中召回任何商品）")


if __name__ == "__main__":
    main()