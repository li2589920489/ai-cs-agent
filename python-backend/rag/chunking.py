"""
文本分块模块 — 语义感知的递归分块

主方案：langchain 的 RecursiveCharacterTextSplitter（按换行→中文标点→字符逐级切分）
降级方案：内置简易句子切分器（不依赖 langchain）
"""
from __future__ import annotations

# 中文友好的分隔符优先级：段落 → 换行 → 句末标点 → 分句标点 → 字符
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]


def split_text(text: str, chunk_size: int = 256, chunk_overlap: int = 50) -> list[str]:
    """把长文本切成有重叠的 chunk（chunk_overlap 保证上下文不割裂）"""
    if not text or not text.strip():
        return []
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=_SEPARATORS,
            keep_separator=True,
        )
        return [c.strip() for c in splitter.split_text(text) if c.strip()]
    except Exception:  # noqa: BLE001
        return _fallback_split(text, chunk_size, chunk_overlap)


def _fallback_split(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """内置降级切分器：按句子累积到 chunk_size"""
    sentences: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？；\n":
            sentences.append(buf)
            buf = ""
    if buf.strip():
        sentences.append(buf)

    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if len(cur) + len(s) > chunk_size and cur:
            chunks.append(cur)
            # 保留 overlap：取上一 chunk 尾部内容
            cur = cur[-chunk_overlap:] if chunk_overlap > 0 else ""
        cur += s
    if cur.strip():
        chunks.append(cur)
    return chunks
