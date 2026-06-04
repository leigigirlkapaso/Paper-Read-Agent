"""
agent1/keyword_extractor.py
AGENT1-A：从用户的研究构想中提取 arxiv 检索关键词。

输入：research.topic（自然语言描述的研究方向）
输出：
  - keywords: List[str]  —— 英文学术关键词
  - queries:  List[str]  —— 用于检索的查询串（角度×维度矩阵）

策略（v2）：
  1. 拆解 3~4 个研究角度
  2. 中→英文关键词映射
  3. 每个角度 × 4 维度（宽泛召回 / 方法聚焦 / 竞争方案 / 评估范式）生成 query
"""

from __future__ import annotations

import json
import logging
import re

from utils.llm_client import LLMClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是一位经验丰富的学术搜索专家，熟悉 arxiv 的检索规则。

用户会给你一段研究构想（可能中英混杂），你需要：

## 第一步：拆解研究角度
将研究构想拆分为 3~4 个独立的研究角度（sub-angle）。每个角度应该覆盖一个不同的子方向或技术维度。
例如："基于LLM的触觉渲染系统" 可拆为：
  - haptic rendering pipeline（触觉渲染管道）
  - LLM-driven semantic parsing（LLM语义解析）
  - vibrotactile actuator design（振动执行器设计）
  - user perception evaluation（用户感知评估）

## 第二步：提取关键词
为每个角度提取 3~5 个英文学术关键词。确保：
  - 中文概念已正确映射为英文学术术语（如"触觉"→haptic，"足部"→foot/plantar）
  - 包含同义词和替代表述（如"vibrotactile"和"vibration feedback"）
  - 优先使用学界通用术语，避免生僻缩写

## 第三步：生成四维查询
对每个研究角度，严格按照以下四个维度各生成 1 条 arxiv 查询串：

1. **宽泛召回（broad）**：2~3 个词用 OR 连接，高召回。all: 字段。
   示例：all:"haptic rendering" OR all:"vibrotactile display"

2. **方法聚焦（method）**：3~4 个词用 AND 连接，找同类技术方案。all: 字段。
   示例：all:"haptic" AND all:"foot" AND all:"virtual reality" AND all:rendering

3. **竞争方案（competitor）**：搜索替代方法、并行技术路线。OR 为主，覆盖不同技术路径。
   示例：all:"electrotactile" OR all:"surface haptics" OR all:"ultrasonic"

4. **评估范式（evaluation）**：搜索实验设计、用户研究、数据集、评价指标。AND 连接。
   示例：all:"haptic" AND all:"user study" AND all:"presence" AND all:walking

重要原则：
- 每个角度 4 条 query，3 个角度 = 12 条，4 个角度 = 16 条
- 不同角度间的 query 不要重复相同的词组合
- 优先使用 all: 字段（覆盖标题+摘要+全文）
- 短语需加双引号，单词不加
- 确保每条 query 简洁有力，不是长句

请严格以 JSON 格式输出，不要有任何额外文字：
{
  "keywords": ["english", "keyword", "list"],
  "angles": [
    {
      "angle": "angle description in English",
      "broad": ["query1"],
      "method": ["query1"],
      "competitor": ["query1"],
      "evaluation": ["query1"]
    }
  ]
}
"""


def extract_keywords(topic: str, llm: LLMClient) -> dict:
    """
    从研究构想中提取关键词和多维检索 query。

    Args:
        topic: 用户填写的研究方向文本
        llm:   LLMClient 实例

    Returns:
        {
          "keywords": [...],
          "queries": [...],   # 扁平化后的全部 query，宽泛→方法→竞争→评估排列
          "queries_by_tier": {"broad": [...], "method": [...], "competitor": [...], "evaluation": [...]}
        }
    """
    logger.info("[AGENT1-A] 开始提取关键词（角度×维度矩阵）...")
    user_prompt = f"以下是用户的研究构想：\n\n{topic.strip()}"

    raw, _usage = llm.chat(user_prompt=user_prompt, system_prompt=_SYSTEM_PROMPT)
    result = _parse_json_response(raw)

    total_q = len(result["queries"])
    n_angles = len(result.get("angles", []))
    b = len(result["queries_by_tier"]["broad"])
    m = len(result["queries_by_tier"]["method"])
    c = len(result["queries_by_tier"]["competitor"])
    e = len(result["queries_by_tier"]["evaluation"])
    logger.info(
        f"[AGENT1-A] 提取完成：{n_angles} 个角度，{len(result['keywords'])} 个关键词，"
        f"{total_q} 条 query（broad={b} / method={m} / competitor={c} / evaluation={e}）"
    )
    return result


def _parse_json_response(raw: str) -> dict:
    """解析 LLM 返回的 JSON，兼容 v1（三层）和 v2（角度×维度）格式。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    try:
        data = json.loads(cleaned)
        keywords = _extract_keywords(data)
        by_tier, flat = _extract_queries(data)
        return {
            "keywords": keywords,
            "queries": flat,
            "queries_by_tier": by_tier,
        }
    except Exception as e:
        logger.error(f"[AGENT1-A] JSON 解析失败，原始输出：\n{raw}\n错误：{e}")
        return _fallback_extract(raw)


def _extract_keywords(data: dict) -> list[str]:
    """提取关键词，兼容 v1（keywords 数组）和 v2（可能分散在 angles 中）。"""
    kw = data.get("keywords", [])
    if isinstance(kw, list) and kw:
        return kw
    # 兜底：从 angles 中提取
    kw_set = set()
    for angle in data.get("angles", []):
        for kw_text in angle.get("keywords", []):
            kw_set.add(kw_text.strip())
    return list(kw_set)


def _extract_queries(data: dict) -> tuple[dict, list]:
    """提取 query，兼容 v1（queries 对象含 broad/mid/precise）和 v2（angles 数组）。"""
    # ── v2 格式：angles 数组 ──
    angles = data.get("angles", [])
    if angles:
        by_tier: dict[str, list[str]] = {
            "broad": [], "method": [], "competitor": [], "evaluation": [],
        }
        for angle in angles:
            for dim in ("broad", "method", "competitor", "evaluation"):
                qs = angle.get(dim, [])
                if isinstance(qs, list):
                    by_tier[dim].extend(qs)
        flat = by_tier["broad"] + by_tier["method"] + by_tier["competitor"] + by_tier["evaluation"]
        return by_tier, flat

    # ── v1 格式：queries 对象（broad/mid/precise）──
    queries_raw = data.get("queries", {})
    if isinstance(queries_raw, list):
        return (
            {"broad": queries_raw, "method": [], "competitor": [], "evaluation": []},
            queries_raw,
        )
    broad = queries_raw.get("broad", [])
    mid = queries_raw.get("mid", [])
    precise = queries_raw.get("precise", [])
    by_tier = {
        "broad": broad,
        "method": mid,
        "competitor": precise,
        "evaluation": [],
    }
    flat = broad + mid + precise
    return by_tier, flat


def _fallback_extract(raw: str) -> dict:
    """JSON 解析失败时的降级处理：逐行扫描引号内的短语。"""
    tokens = re.findall(r'"([^"]{3,60})"', raw)
    keywords = [t for t in tokens if len(t.split()) <= 5][:8]
    queries = [f'all:"{t}"' for t in tokens if len(t.split()) > 1][:5]
    if not queries:
        queries = [" AND ".join(keywords[:3])] if keywords else ["machine learning"]
    return {
        "keywords": keywords,
        "queries": queries,
        "queries_by_tier": {
            "broad": queries, "method": [], "competitor": [], "evaluation": [],
        },
    }
