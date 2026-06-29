"""
agent1/paper_filter.py
AGENT1-B：使用 LLM 对 arxiv 候选文献进行相关性打分，过滤掉不相关的论文。

策略：
- 每批 batch_size 篇，把「标题 + 摘要」交给 LLM 打分（0.0 ~ 1.0）
- 分数 >= relevance_threshold 的论文保留
- 保留数量上限为 max_download_papers
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from agent1.arxiv_searcher import PaperMeta
from utils.llm_client import LLMClient

logger = logging.getLogger(__name__)

# 顶会/顶刊关键词（小写匹配）
_TOP_VENUES = {
    "neurips", "icml", "iclr", "cvpr", "iccv", "eccv",
    "acl", "emnlp", "naacl", "aaai", "ijcai", "siggraph",
    "chi", "uist", "cscw", "ubicomp", "imwut",
    "nature", "science", "pnas", "cell",
    "tpami", "ijcv", "jmlr", "tvcg",
}

_SYSTEM_PROMPT = """\
你是一位严格的学术文献筛选专家。
用户会给你：
  1. 他的研究构想（Research Topic）
  2. 一批论文（每篇包含编号、标题、摘要，可能附带 venue / citation / code 信息）

你的任务：为每篇论文与研究构想的相关性打分（0.0 ~ 1.0），
  - 1.0 = 高度相关，直接涉及核心问题/方法
  - 0.5 = 部分相关，有参考价值但非核心
  - 0.0 = 不相关

严格以 JSON 数组输出，不要有任何额外文字：
[{"id": 1, "score": 0.9}, {"id": 2, "score": 0.3}, ...]
"""


def filter_papers(
    papers: list[PaperMeta],
    topic: str,
    llm: LLMClient,
    relevance_threshold: float = 0.8,
    max_download_papers: int = 20,
    batch_size: int = 10,
    quality_weight: float = 0.0,
    max_concurrent: int = 200,
) -> list[PaperMeta]:
    """
    对候选论文批量打分，返回相关性达标的论文（按分数降序）。

    Args:
        papers:               arxiv 检索得到的候选列表
        topic:                用户的研究构想文本
        llm:                  LLMClient 实例
        relevance_threshold:  分数低于此值的论文被过滤
        max_download_papers:  最多保留篇数
        batch_size:           每批处理的文献数量
        quality_weight:       质量权重 0~1（0=纯相关性，0.2=轻微考虑引用/顶会/代码）
        max_concurrent:       打分并发上限（批次级别）

    Returns:
        筛选后的 PaperMeta 列表（已填充 relevance_score）
    """
    if not papers:
        return []
    total_batches = (len(papers) + batch_size - 1) // batch_size
    workers = min(max_concurrent, total_batches)
    logger.info(
        f"[AGENT1-B] 开始筛选 {len(papers)} 篇候选文献"
        f"（batch={batch_size}, workers={workers}, qw={quality_weight}）..."
    )

    # 分批并行打分
    batches = []
    for batch_start in range(0, len(papers), batch_size):
        batch = papers[batch_start : batch_start + batch_size]
        batches.append((batch, batch_start))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_score_batch, batch, topic, llm, offset): offset
            for batch, offset in batches
        }
        for f in as_completed(futures):
            f.result()  # 异常已在 _score_batch 内部处理

    # 边界论文批量重试打分：阈值 -0.1 以内所有论文一起送给 LLM 再打 2 次，取最高分
    borderline = [p for p in papers
                  if relevance_threshold - 0.1 <= p.relevance_score < relevance_threshold]
    if borderline:
        logger.info(f"[AGENT1-B] {len(borderline)} 篇边界论文，批量重试打分（最多 2 次）...")
        _retry_borderline_batch(borderline, topic, llm, relevance_threshold)

    # 质量加权混合
    if quality_weight > 0:
        for p in papers:
            qs = _compute_quality_score(p)
            p.relevance_score = p.relevance_score * (1 - quality_weight) + qs * quality_weight
            p.relevance_score = min(p.relevance_score, 1.0)

    # 过滤 + 排序 + 截断
    filtered = [p for p in papers if p.relevance_score >= relevance_threshold]
    filtered.sort(key=lambda p: p.relevance_score, reverse=True)
    filtered = filtered[:max_download_papers]

    logger.info(
        f"[AGENT1-B] 筛选完成：{len(filtered)} 篇达标 "
        f"（阈值={relevance_threshold}，上限={max_download_papers}）"
    )
    return filtered


def _score_batch(
    batch: list[PaperMeta],
    topic: str,
    llm: LLMClient,
    offset: int,
) -> None:
    """对一个批次打分，结果直接写回 PaperMeta.relevance_score。"""
    papers_text = "\n\n".join(
        _format_paper_for_scoring(i + 1, p) for i, p in enumerate(batch)
    )
    user_prompt = (
        f"## 研究构想\n{topic.strip()}\n\n"
        f"## 待评估论文（共 {len(batch)} 篇）\n{papers_text}"
    )

    scored_indices: set[int] = set()
    try:
        raw, _ = llm.chat(user_prompt=user_prompt, system_prompt=_SYSTEM_PROMPT)
        scores = _parse_scores(raw)
        for item in scores:
            idx = item["id"] - 1
            if 0 <= idx < len(batch):
                batch[idx].relevance_score = float(item["score"])
                scored_indices.add(idx)
        logger.info(
            f"[AGENT1-B] 批次 {offset + 1}~{offset + len(batch)} 打分完成"
        )
    except Exception as e:
        logger.error(f"[AGENT1-B] 批次打分失败，未被打分的论文赋予中性分 0.5: {e}")
        for i, p in enumerate(batch):
            if i not in scored_indices:
                p.relevance_score = 0.5


def _retry_borderline_batch(
    borderline: list[PaperMeta], topic: str, llm: LLMClient, threshold: float
) -> None:
    """对一批边界论文批量重试打分最多 2 次，每篇保留最高分。"""
    best_scores = {id(p): p.relevance_score for p in borderline}
    for attempt in range(2):
        _score_batch(borderline, topic, llm, 0)
        for p in borderline:
            if p.relevance_score > best_scores[id(p)]:
                best_scores[id(p)] = p.relevance_score
        if all(s >= threshold for s in best_scores.values()):
            break
    for p in borderline:
        p.relevance_score = best_scores[id(p)]


def _format_paper_for_scoring(idx: int, p: PaperMeta) -> str:
    """格式化学论文评分条目，附带质量元数据。"""
    abstract = (p.abstract or "")[:3000]
    if not p.abstract or not p.abstract.strip():
        lines = [
            f"[{idx}] 标题：{p.title}",
            "摘要：[无摘要可用 — 请仅基于标题与发表场合保守打分；"
            "除非标题明显高度相关或明显不相关，否则建议给 0.4-0.6]",
        ]
    else:
        lines = [f"[{idx}] 标题：{p.title}", f"摘要：{abstract}"]
    if p.venue:
        lines.append(f"发表场合：{p.venue}")
    if p.citation_count > 0:
        lines.append(f"引用数：{p.citation_count}")
    if p.code_url:
        lines.append(f"代码：{p.code_url}")
    return "\n".join(lines)


def _compute_quality_score(p: PaperMeta) -> float:
    """根据元数据计算质量分（0~1）。不考虑 LLM 评分，纯客观指标。"""
    score = 0.0
    # 顶会/顶刊
    if p.venue:
        venue_lower = p.venue.lower()
        if any(v in venue_lower for v in _TOP_VENUES):
            score += 0.15
    # 有代码
    if p.code_url:
        score += 0.10
    # 高引用
    if p.citation_count >= 50:
        score += 0.10
    elif p.citation_count >= 10:
        score += 0.05
    return min(score, 1.0)


def _parse_scores(raw: str) -> list[dict]:
    """解析 LLM 返回的 JSON 数组，容忍 markdown 包裹。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    # 有时模型会在数组外包裹额外文字，尝试找到 JSON 数组
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        data = json.loads(cleaned)
        assert isinstance(data, list)
        return data
    except Exception as e:
        logger.error(f"[AGENT1-B] scores JSON 解析失败: {e}\n原始: {raw[:300]}")
        return []
