"""
core/embedding.py
文本 embedding 工具函数。向量存 SQLite TEXT（JSON 数组）。
"""

from __future__ import annotations

import json
import math

from .decorators import stable


@stable
def cosine_similarity(a: list[float], b: list[float]) -> float:
    """两个向量的余弦相似度，返回 0~1 之间的值。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@stable
def pack_embedding(vec: list[float]) -> str:
    """将向量序列化为 JSON 字符串。"""
    return json.dumps(vec, ensure_ascii=False)


@stable
def unpack_embedding(raw: str) -> list[float]:
    """从 JSON 字符串还原向量。"""
    if not raw or raw.strip() == "":
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
