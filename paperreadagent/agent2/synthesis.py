"""
agent2/synthesis.py
Cross-paper review synthesis. After all papers are read (AGENT2), generate one
LLM synthesis of the whole batch — theme, research narrative, commonalities vs
differences, and 2-3 unsolved/diggable problems. Length adapts to paper count.

Replaces the previous static "综合总结" placeholder in the final report.
Borrowed from LitKB's collection-overview pattern.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PER_PAPER_SUMMARY_CHARS = 1500   # truncate each paper's summary to control tokens
_MAX_PAPERS_FOR_SYNTHESIS = 60    # cap papers fed to one synthesis call (avoid context overflow on large batches)

_SYSTEM_PROMPT = "你是资深科研综述写作者，擅长把一组论文归纳为有洞察的整体综述。"

_USER_TEMPLATE = """\
给定一组论文的标题与精读分析，写一篇整体综述。

目标篇幅：约 {target_chars} 字（本批共 {n_papers} 篇论文，据此调整深度）。
研究主题背景：{topic}

要求：
1. 明确这批论文整体研究什么主题
2. 概括研究脉络：主要方法 / 核心结论 / 当前共识
3. 指出论文间的共同点与分歧点
4. 列出 2-3 个未解决/可深挖的问题（具体、可执行，供后续研究选题）
5. 全部基于提供的论文，不要编造；引用具体论文标题
6. 用中文，Markdown 格式，不要前言/结语客套

# 论文列表
{papers_block}"""


def _compute_synthesis_budget(n_papers: int) -> tuple[int, int]:
    """Return (target_chars, max_tokens) tiered by paper count."""
    if n_papers <= 3:
        return 400, 1200
    if n_papers <= 8:
        return 800, 2000
    if n_papers <= 20:
        return 1500, 3500
    if n_papers <= 50:
        return 2500, 6000
    return 4000, 8000


def _build_papers_block(papers_with_summaries: list) -> str:
    blocks = []
    for paper, summary in papers_with_summaries:
        s = (summary or "").strip()
        if len(s) > _PER_PAPER_SUMMARY_CHARS:
            s = s[:_PER_PAPER_SUMMARY_CHARS] + " …（截断）"
        blocks.append(f"### {paper.title}\n{s}")
    return "\n\n".join(blocks)


async def generate_synthesis(cfg: dict, papers_with_summaries: list, llm) -> str:
    """Generate a cross-paper synthesis. Returns '' on empty input or LLM failure
    (caller falls back to the static placeholder — never breaks the report)."""
    if not papers_with_summaries:
        return ""

    n = len(papers_with_summaries)
    # 大批次防上下文溢出：按相关性取 top-N 篇喂给综述（其余不影响报告正文）
    selected = papers_with_summaries
    if n > _MAX_PAPERS_FOR_SYNTHESIS:
        selected = sorted(
            papers_with_summaries,
            key=lambda ps: getattr(ps[0], "relevance_score", 0.0) or 0.0,
            reverse=True,
        )[:_MAX_PAPERS_FOR_SYNTHESIS]
        logger.info("[Synthesis] %d 篇 → 取相关性 top-%d 做综述（防上下文溢出）",
                    n, _MAX_PAPERS_FOR_SYNTHESIS)

    n_used = len(selected)
    target_chars, max_tokens = _compute_synthesis_budget(n_used)
    topic = (cfg.get("research", {}).get("topic") or "").strip()
    papers_block = _build_papers_block(selected)
    user_prompt = _USER_TEMPLATE.format(
        target_chars=target_chars, n_papers=n_used,
        topic=topic, papers_block=papers_block,
    )

    try:
        content, _usage = await llm.achat(
            user_prompt=user_prompt,
            system_prompt=_SYSTEM_PROMPT,
            max_tokens=max_tokens,
        )
        return (content or "").strip()
    except Exception as exc:
        logger.warning("[Synthesis] generation failed (%d papers): %s", n_used, exc)
        return ""
