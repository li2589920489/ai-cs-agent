"""
模型下载脚本 — 从 hf-mirror 下载 BGE 模型到本地

绕过 huggingface_hub 的 Xet/缓存兼容问题（国内镜像下会写出 0 字节文件），
直接用 urllib 下载到 data/models/，供 Embedder/Reranker 本地加载。

用法：python download_models.py
"""
from __future__ import annotations

import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "data", "models")
BASE_URL = "https://hf-mirror.com"

# 每个模型需要下载的文件（相对路径）
MODEL_FILES: dict[str, list[str]] = {
    "BAAI/bge-small-zh-v1.5": [
        "config.json",
        "model.safetensors",
        "sentence_bert_config.json",
        "config_sentence_transformers.json",
        "modules.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.txt",
        "special_tokens_map.json",
        "1_Pooling/config.json",
    ],
    "BAAI/bge-reranker-base": [
        "config.json",
        "pytorch_model.bin",
        "tokenizer.json",
        "tokenizer_config.json",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
    ],
}


def _download(url: str, dest: str) -> bool:
    """下载单个文件，带进度显示；已存在且非空则跳过"""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"  下载 {os.path.basename(dest)} ...", flush=True)
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(
                    f"\r    {pct:3d}% ({done // 1048576}MB / {total // 1048576}MB)  ",
                    end="",
                    flush=True,
                )
    print()
    os.replace(tmp, dest)
    return True


def main() -> None:
    for model, files in MODEL_FILES.items():
        local_dir = os.path.join(DEST, model.split("/")[-1])
        print(f"==> {model}  →  {local_dir}")
        for f in files:
            url = f"{BASE_URL}/{model}/resolve/main/{f}"
            _download(url, os.path.join(local_dir, f))
    print("\n✅ 模型下载完成")


if __name__ == "__main__":
    main()
