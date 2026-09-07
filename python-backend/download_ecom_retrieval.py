"""下载 C-MTEB/EcomRetrieval 数据集并转为 eval_retrieval.py 期望的 parquet 格式

产物（写入 data/eval/ecom_retrieval/）：
  corpus.parquet   : 列 [id, text]（100902 条电商段落）
  queries.parquet  : 列 [id, text]（1000 条查询）
  qrels.parquet    : 列 [query-id, corpus-id, score]（每 query 1 个相关 doc）

用法：
  HF_ENDPOINT=https://hf-mirror.com python download_ecom_retrieval.py
"""
from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

DATA_DIR = Path(__file__).resolve().parent / "data" / "eval" / "ecom_retrieval"

# 确保走 hf-mirror（国内可达）+ 禁用 Xet（规避 Windows Xet 存储空文件问题）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def main() -> None:
    from datasets import load_dataset

    print("== 下载 C-MTEB/EcomRetrieval（hf-mirror）==", flush=True)
    ds = load_dataset("C-MTEB/EcomRetrieval")
    print(f"    splits: {list(ds.keys())}", flush=True)
    for split, d in ds.items():
        print(f"    [{split}] 列={d.column_names} 行={len(d)}", flush=True)

    # C-MTEB 标准三表：corpus / queries / qrels
    corpus = ds["corpus"]
    queries = ds["queries"]
    qrels = ds["qrels"]

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # corpus.parquet: id + text
    print("== 写 corpus.parquet ==", flush=True)
    corpus_df = corpus.to_pandas()
    pq.write_table(pa.Table.from_pandas(corpus_df[["id", "text"]]),
                   DATA_DIR / "corpus.parquet")

    # queries.parquet: id + text
    print("== 写 queries.parquet ==", flush=True)
    queries_df = queries.to_pandas()
    pq.write_table(pa.Table.from_pandas(queries_df[["id", "text"]]),
                   DATA_DIR / "queries.parquet")

    # qrels.parquet: query-id + corpus-id + score（只保留 score>0 的正相关）
    print("== 写 qrels.parquet ==", flush=True)
    qrels_df = qrels.to_pandas()
    qrels_df = qrels_df.rename(columns={"query-id": "query-id", "corpus-id": "corpus-id"})
    pq.write_table(pa.Table.from_pandas(qrels_df), DATA_DIR / "qrels.parquet")

    print(f"\n== 完成 ==\ncorpus={len(corpus_df)}  queries={len(queries_df)}  qrels={len(qrels_df)}")
    print(f"输出目录: {DATA_DIR}")


if __name__ == "__main__":
    main()
