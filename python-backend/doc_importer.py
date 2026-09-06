"""
文档导入模块 — 解析 PDF/Word/TXT，用 DeepSeek 提取商品知识结构化字段

用途：运营上传商品详情文档，系统自动解析 + LLM 提取为结构化知识，批量入库。
"""
from __future__ import annotations

import io
import json
import re

from openai import AsyncOpenAI

DOC_MODEL = "deepseek-chat"

EXTRACT_PROMPT = """你是电商商品知识提取助手。请从下面的文档内容中提取商品知识，输出 JSON 数组。

每个商品对象包含以下字段：
- product_id: 商品ID或编号，没有则用空字符串 ""
- name: 商品名称
- description: 商品介绍（一句话概述）
- selling_points: 卖点列表（字符串数组）
- specs: 规格列表（字符串数组）
- faq: 常见问题列表（字符串数组，每条格式"问题：答案"）

规则：
1. 如果文档包含多个商品，输出多个对象
2. 只输出 JSON 数组，不要任何其他文字或解释
3. 信息不足的字段用空字符串或空数组

文档内容：
{text}"""


def extract_text(filename: str, content: bytes) -> str:
    """根据文件后缀解析文档文本"""
    name = (filename or "").lower()

    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages)

    if name.endswith(".docx"):
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    # txt / md / 其他按文本处理
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def _parse_json(text: str) -> list[dict]:
    """从 LLM 输出中稳健提取 JSON 数组"""
    text = text.strip()
    # 去掉可能的 ```json ... ``` 包裹
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试截取第一个 [ 到最后一个 ]
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            data = json.loads(text[start:end + 1])
        else:
            return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


async def extract_knowledge_with_llm(text: str) -> list[dict]:
    """用 DeepSeek 把文档文本提取为结构化商品知识"""
    if not text.strip():
        return []

    client = AsyncOpenAI()  # 自动读 OPENAI_API_KEY / OPENAI_BASE_URL
    prompt = EXTRACT_PROMPT.format(text=text[:8000])  # 截断防止超长

    resp = await client.chat.completions.create(
        model=DOC_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        response_format={"type": "json_object"},  # 尽量返回 JSON
    )
    content = resp.choices[0].message.content or ""

    items = _parse_json(content)
    # 如果顶层是 {"items": [...]} 形式，取 items
    if not items:
        try:
            obj = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip()))
            if isinstance(obj, dict) and isinstance(obj.get("items"), list):
                items = [d for d in obj["items"] if isinstance(d, dict)]
        except Exception:
            pass
    return items
