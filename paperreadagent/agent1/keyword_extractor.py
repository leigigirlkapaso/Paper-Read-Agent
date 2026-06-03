"""
agent1/keyword_extractor.py
AGENT1-A：从用户的研究构想中提取 arxiv 检索关键词。

输入：research.topic（自然语言描述的研究方向）
输出：
  - keywords: List[str]  —— 单个关键词（英文）
  - queries:  List[str]  —— 用于 arxiv 检索的组合查询串（三层递进）

三层查询策略
  - 宽泛层（broad）：2 个词的 OR 组合 → 高召回
  - 中间层（mid）  ：2~3 个词的 AND 组合 → 平衡
  - 精准层（precise）：3+ 个词的 AND 组合 → 高精度
"""

from __future__ import annotations

import json
import logging
import re

from utils.llm_client import LLMClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是一位经验丰富的学术搜索专家，熟悉 arxiv 的检索规则。
用户会给你一段研究构想，你需要：

1. 提取 6~10 个英文关键词（单词或短语），覆盖核心概念、方法、应用场景。

2. 生成三个层次共 10~15 条 arxiv 检索查询串：
   - 宽泛层（broad，3~4 条）：只用 2 个词，用 OR 连接，目标是高召回
     示例：all:"human activity recognition" OR all:"HAR"
   - 中间层（mid，4~6 条）：2~3 个词用 AND 连接，覆盖不同维度组合
     示例：all:"human activity recognition" AND all:"wearable sensors"
   - 精准层（precise，3~5 条）：3 个以上词用 AND 连接，定向精准
     示例：all:"human activity recognition" AND all:"missing sensor" AND all:robustness

   重要原则：
   - 每层的查询之间要有多样性，不要重复相同的词组合
   - 宽泛层务必确保至少能在 arxiv 上检索到论文（不要用太生僻的词）
   - 优先使用 all: 字段（覆盖标题+摘要+全文），而非 abs: 或 ti:
   - 短语需加双引号，单词不加

请严格以 JSON 格式输出，不要有任何额外文字：
{
  "keywords": ["kw1", "kw2", ...],
  "queries": {
    "broad":   ["query1", "query2", ...],
    "mid":     ["query3", "query4", ...],
    "precise": ["query5", "query6", ...]
  }
}
"""


def extract_keywords(topic: str, llm: LLMClient) -> dict:
    """
    从研究构想中提取关键词和三层检索 query。

    Args:
        topic: 用户填写的研究方向文本
        llm:   LLMClient 实例

    Returns:
        {
          "keywords": [...],
          "queries": [...],   # 扁平化后的全部 query，宽泛→中间→精准排列
          "queries_by_tier": {"broad": [...], "mid": [...], "precise": [...]}
        }
    """
    logger.info("[AGENT1-A] 开始提取关键词（三层策略）...")
    user_prompt = f"以下是用户的研究构想：\n\n{topic.strip()}"

    raw, _usage = llm.chat(user_prompt=user_prompt, system_prompt=_SYSTEM_PROMPT)
    result = _parse_json_response(raw)

    total_q = len(result["queries"])
    logger.info(
        f"[AGENT1-A] 提取完成：{len(result['keywords'])} 个关键词，"
        f"{total_q} 条检索 query（"
        f"宽泛={len(result['queries_by_tier']['broad'])} / "
        f"中间={len(result['queries_by_tier']['mid'])} / "
        f"精准={len(result['queries_by_tier']['precise'])}）"
    )
    return result


def _parse_json_response(raw: str) -> dict:
    """解析 LLM 返回的 JSON，兼容旧格式（queries 为数组）和新格式（queries 为对象）。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    try:
        data = json.loads(cleaned)
        if not isinstance(data.get("keywords"), list):
            raise ValueError("LLM 返回的 keywords 字段不是数组")
        queries_raw = data.get("queries", {})

        # 兼容旧格式：queries 直接是数组
        if isinstance(queries_raw, list):
            flat = queries_raw
            by_tier = {"broad": flat, "mid": [], "precise": []}
        else:
            broad   = queries_raw.get("broad", [])
            mid     = queries_raw.get("mid", [])
            precise = queries_raw.get("precise", [])
            # 排列顺序：宽泛 → 中间 → 精准（保证宽泛层先执行）
            flat    = broad + mid + precise
            by_tier = {"broad": broad, "mid": mid, "precise": precise}

        return {
            "keywords":       data["keywords"],
            "queries":        flat,
            "queries_by_tier": by_tier,
        }
    except Exception as e:
        logger.error(f"[AGENT1-A] JSON 解析失败，原始输出：\n{raw}\n错误：{e}")
        return _fallback_extract(raw)


def _fallback_extract(raw: str) -> dict:
    """JSON 解析失败时的降级处理：逐行扫描引号内的短语。"""
    tokens = re.findall(r'"([^"]{3,60})"', raw)
    keywords = [t for t in tokens if len(t.split()) <= 5][:8]
    queries = [t for t in tokens if len(t.split()) > 1][:5]
    if not queries:
        queries = [" AND ".join(keywords[:3])] if keywords else ["machine learning"]
    return {
        "keywords":        keywords,
        "queries":         queries,
        "queries_by_tier": {"broad": queries, "mid": [], "precise": []},
    }
