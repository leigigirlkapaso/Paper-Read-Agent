"""
utils/ai_style_critique.py
"AI 味" self-critique + de-AI rewrite for LLM-generated academic prose.

critique_ai_style: scores text 0-100 for AI-ness across 5 dimensions, flags the
  worst sentences (excerpt + dimension + reason + fix), and gives global tips.
rewrite_deai: rewrites the text to reduce AI-ness while preserving every datum.

Both take an `llm` object exposing
  async achat(user_prompt, system_prompt=None, *, max_tokens=...) -> (str, dict)
(satisfied by core.llm and utils.llm_client.LLMClient). Borrowed from LitKB.
"""
from __future__ import annotations

import json
import logging

from utils.json_utils import clean_json

logger = logging.getLogger(__name__)

_DIMENSIONS = ["套路化结构", "空泛措辞", "缺具体数据", "缺个人视角", "过度对仗"]

_CRITIQUE_SYSTEM = """\
你是中文学术写作的"AI 味"审稿人。给定一段 LLM 生成的学术分析文本，诊断它有多像"AI 生成的套话"，并指出具体问题。

# 硬性输出要求
- 纯 raw JSON，第一个字符是 {，无 markdown 围栏，无 <think>
- overall_score: 0-100 整数（越高越像 AI 套话）
- level: 低|中|高
- dimension_scores: 必须含 5 个维度，各 0-100：套路化结构 / 空泛措辞 / 缺具体数据 / 缺个人视角 / 过度对仗
- flagged: 列出 AI 味最重的 3-8 个句子，每条 {excerpt(原文片段), dimension(5 维度之一), reason, fix(建议改法)}
- suggestions: 3-4 条全局去 AI 味建议
- 诚实：若文本本就具体扎实，给低分，不要硬挑

# 输出格式（严格 JSON）
{"overall_score": 72, "level": "高",
 "dimension_scores": {"套路化结构": 0, "空泛措辞": 0, "缺具体数据": 0, "缺个人视角": 0, "过度对仗": 0},
 "flagged": [{"excerpt": "...", "dimension": "空泛措辞", "reason": "...", "fix": "..."}],
 "suggestions": ["...", "...", "..."]}"""

_REWRITE_SYSTEM = """\
你是中文学术写作润色专家。给定一段"AI 味重"的分析文本和它的诊断，重写它以降低 AI 味，使其更像真人研究者写的。

# 铁律
- 保留所有具体数据、事实、技术细节 —— 一个数字都不许改/删/编
- 只改文风：去套话、补具体、加判断视角、破对仗腔
- 不许为了"不像 AI"而牺牲准确性或编造内容
- 输出纯改写后的 markdown 正文，不要解释、不要前后言"""


async def critique_ai_style(text: str, llm) -> dict:
    """Diagnose AI-ness. Returns the spec JSON dict, or {"error", "overall_score": None}
    on empty text / LLM failure / parse failure (never raises)."""
    if not text or not text.strip():
        return {"error": "空文本，无法诊断", "overall_score": None}
    try:
        raw, _usage = await llm.achat(
            user_prompt=f"待诊断文本：\n\n{text}",
            system_prompt=_CRITIQUE_SYSTEM,
            max_tokens=4000,
        )
        parsed = json.loads(clean_json(raw))
        if not isinstance(parsed, dict) or "overall_score" not in parsed:
            return {"error": "解析失败：返回结构不符", "overall_score": None}
        return parsed
    except Exception as exc:
        logger.warning("[AICritique] failed: %s", exc)
        return {"error": f"诊断失败：{exc}", "overall_score": None}


async def rewrite_deai(text: str, critique: dict, llm) -> str:
    """Rewrite to reduce AI-ness, preserving all data. Returns '' on empty/failure."""
    if not text or not text.strip():
        return ""
    suggestions = (critique or {}).get("suggestions") or []
    critique_summary = "；".join(str(s) for s in suggestions) if suggestions else "（无具体诊断，按铁律通用去 AI 味）"
    try:
        user_prompt = f"# 原文\n{text}\n\n# 诊断建议（参考）\n{critique_summary}"
        rewritten, _usage = await llm.achat(
            user_prompt=user_prompt,
            system_prompt=_REWRITE_SYSTEM,
            max_tokens=8000,
        )
        return (rewritten or "").strip()
    except Exception as exc:
        logger.warning("[AICritique] rewrite failed: %s", exc)
        return ""
