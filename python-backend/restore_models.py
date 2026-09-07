"""从 HF 缓存 blobs 恢复模型到 data/models/（修复 Xet 存储导致的空 snapshot 文件）

背景：HF 新版 Xet 存储下，snapshots/ 里的文件是 0 字节占位，真实数据在 blobs/ 里，
导致 sentence-transformers 加载时 config.json 为空 → json 解析失败。
本脚本读 trees/<rev>.json 的「文件名 -> blob_id/lfs_sha256」映射，把 blob 复制回
data/models/<model> 的正确路径，从而离线恢复模型、无需重新下载。

用法：
    python restore_models.py            # 恢复 bge-small-zh-v1.5 + bge-reranker-base
    python restore_models.py --model bge-small-zh-v1.5   # 只恢复单个
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HF_HUB = Path.home() / ".cache" / "huggingface" / "hub"
TARGET_ROOT = Path(__file__).resolve().parent / "data" / "models"

# 模型名 -> HF 缓存目录名（models--BAAI--<name>）
MODELS = {
    "bge-small-zh-v1.5": "models--BAAI--bge-small-zh-v1.5",
    "bge-reranker-base": "models--BAAI--bge-reranker-base",
}


def resolve_blob_name(file_meta: dict, blobs_dir: Path) -> str | None:
    """确定某个文件对应的实际 blob 文件名（优先 lfs_sha256，其次 blob_id）。

    处理 Xet 下 LFS 大文件可能存在的 .incomplete 后缀。
    """
    lfs = file_meta.get("lfs_sha256")
    if lfs:
        # 先找完整 blob（lfs_sha256 命名）
        if (blobs_dir / lfs).exists():
            return lfs
        # 再找 .incomplete 文件（下载未完成但大小可能已完整）
        incomplete = sorted(blobs_dir.glob(f"{lfs}.*.incomplete"))
        if incomplete:
            return incomplete[0].name
        return None
    blob_id = file_meta.get("blob_id")
    if blob_id and (blobs_dir / blob_id).exists():
        return blob_id
    return None


def restore_model(model_name: str) -> tuple[int, int, list[str]]:
    """恢复单个模型，返回 (成功数, 失败数, 失败文件列表)。"""
    cache_dir = HF_HUB / MODELS[model_name]
    if not cache_dir.exists():
        print(f"[跳过] {model_name}: 缓存目录不存在 {cache_dir}")
        return 0, 0, []

    # 找到最新的 trees/<rev>.json
    trees_dir = cache_dir / "trees"
    tree_files = sorted(trees_dir.glob("*.json"))
    if not tree_files:
        print(f"[跳过] {model_name}: 无 trees 元数据")
        return 0, 0, []

    tree_path = tree_files[-1]
    rev = tree_path.stem
    snapshot_dir = cache_dir / "snapshots" / rev
    blobs_dir = cache_dir / "blobs"

    with open(tree_path, encoding="utf-8") as f:
        tree = json.load(f)

    files = tree.get("files", {})
    target_dir = TARGET_ROOT / model_name
    target_dir.mkdir(parents=True, exist_ok=True)

    ok, fail = 0, 0
    failed_files = []
    for rel_path, meta in files.items():
        blob_name = resolve_blob_name(meta, blobs_dir)
        if not blob_name:
            fail += 1
            failed_files.append(f"{rel_path} (blob 缺失)")
            continue
        src = blobs_dir / blob_name
        dst = target_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            failed_files.append(f"{rel_path} ({e})")

    print(f"[完成] {model_name}: 恢复 {ok} 个文件, 失败 {fail} 个 → {target_dir}")
    for f in failed_files:
        print(f"      ✗ {f}")
    return ok, fail, failed_files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, default=None,
                        help="只恢复指定模型（默认全部）")
    args = parser.parse_args()

    if args.model:
        if args.model not in MODELS:
            print(f"未知模型: {args.model}，可选: {list(MODELS)}")
            sys.exit(1)
        restore_model(args.model)
    else:
        for name in MODELS:
            restore_model(name)


if __name__ == "__main__":
    main()
