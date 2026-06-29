"""
agent2/paper_reader.py
AGENT2：对单篇已下载的论文进行独立精读与结构化总结。

- 每篇文献独立调用一次 LLM（控制 token 消耗）
- 使用 async 接口，配合 parallel_runner 并发执行
- 支持断点续传：若 summary 文件已存在则直接读取，不重复调用
- Phase 1：数据库缓存（prompt-aware），通过 db 参数可选启用
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent1.arxiv_searcher import PaperMeta
from utils.llm_client import LLMClient
from utils.pdf_parser import parse_pdf, _ARXIV_ID_PATTERN, _try_pmc_xml

if TYPE_CHECKING:
    from db.database import Database

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = "你是一位专业的学术论文分析助理，严格按照用户要求的格式输出结构化总结。"


async def read_paper(
    paper: PaperMeta,
    pdf_path: Path,
    summary_dir: Path,
    summary_prompt: str,
    topic: str,
    llm: LLMClient,
    max_chars: int = 110000,
    db: "Database | None" = None,
    session_id: int = 0,
) -> str:
    """
    精读单篇论文，返回 Markdown 格式的总结字符串。

    Args:
        paper:          论文元信息
        pdf_path:       已下载的 PDF 文件路径
        summary_dir:    单篇总结的保存目录（断点续传用）
        summary_prompt: 来自 config.yaml 的总结 prompt
        topic:          用户研究构想（注入给 LLM 作为背景）
        llm:            LLMClient 实例（使用异步接口）
        max_chars:      送入 LLM 的最大 PDF 字符数
        db:             数据库实例（可选，启用 prompt-aware 缓存）
        session_id:     当前会话 ID

    Returns:
        Markdown 格式总结文本
    """
    safe_id = paper.arxiv_id.replace("/", "_")

    # ── 计算 prompt 指纹（Phase 2: prompt-aware cache） ────────
    prompt_hash = _compute_prompt_hash(summary_prompt, llm.model_name, llm.temperature, max_chars, topic)
    prompt_short = prompt_hash[:8]

    # 文件缓存路径含 prompt 短哈希，实现多版本共存
    # 也检查旧格式（无哈希），跳过旧缓存避免误用
    legacy_path = summary_dir / f"{safe_id}.md"
    summary_path = summary_dir / f"{safe_id}_{prompt_short}.md"

    # 清理旧格式缓存
    if legacy_path.exists():
        try:
            legacy_path.unlink()
            logger.info(f"[AGENT2] 清理旧格式缓存: {legacy_path.name}")
        except OSError as e:
            logger.warning(f"[AGENT2] 无法删除旧缓存: {e}")

    # ── 断点续传（文件级缓存，prompt-aware） ───────────────────
    if summary_path.exists() and summary_path.stat().st_size > 50:
        logger.info(f"[AGENT2] 已有缓存 (prompt={prompt_short})，跳过: {paper.arxiv_id}")
        try:
            cached_content = summary_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"[AGENT2] 缓存文件损坏，将重新生成: {e}")
            summary_path.unlink(missing_ok=True)
        else:
            if db and session_id:
                await _update_db_summary_status(db, session_id, paper.arxiv_id, "cached")
                # 同步写回 DB summaries 表（修复缓存命中时 DB 无内容的问题）
                paper_rows0 = await asyncio.to_thread(
                    lambda: db.conn.execute(
                        "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
                        (session_id, paper.arxiv_id),
                    ).fetchone()
                )
                if paper_rows0:
                    await asyncio.to_thread(
                        lambda: db.save_summary(
                            paper_id=paper_rows0["id"],
                            prompt_hash=prompt_hash,
                            model_name=llm.model_name,
                            temperature=llm.temperature,
                            max_chars=max_chars,
                            content=cached_content,
                            pdf_text_hash="cached",
                        )
                    )
            return cached_content

    # ── PDF 解析 ──────────────────────────────────────────────
    pmc_text = None
    if paper.source_platform == "pmc" or (paper.arxiv_id or "").startswith(("pmcid_", "pmid_")):
        pmcid = _extract_pmcid(paper)
        if pmcid:
            pmc_text = _try_pmc_xml(pmcid)
            if pmc_text:
                logger.info(f"[AGENT2] PMC XML fetched for {pmcid}")

    if pmc_text:
        pdf_text = pmc_text
    else:
        try:
            arxiv_id = paper.arxiv_id if (paper.arxiv_id and _ARXIV_ID_PATTERN.search(paper.arxiv_id)) else ""
            pdf_text = await asyncio.wait_for(
                asyncio.to_thread(parse_pdf, pdf_path, max_chars, arxiv_id=arxiv_id), timeout=120
            )
        except Exception as e:
            logger.error(f"[AGENT2] PDF 解析失败 ({paper.arxiv_id}): {e}")
            fallback = _make_fallback_summary(paper, error=str(e))
            if db and session_id:
                await _update_db_summary_status(db, session_id, paper.arxiv_id, "failed")
            return fallback

    # ── 构建 Prompt ───────────────────────────────────────────
    user_prompt = _build_user_prompt(paper, pdf_text, summary_prompt, topic)
    pdf_text_hash = _compute_pdf_hash(pdf_text)

    # ── 数据库缓存检查（精确匹配：prompt + PDF 内容） ─────────
    if db and session_id:
        paper_rows = await asyncio.to_thread(
            lambda: db.conn.execute(
                "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
                (session_id, paper.arxiv_id),
            ).fetchone()
        )
        if paper_rows:
            paper_db_id = paper_rows["id"]
            cached = await asyncio.to_thread(
                db.get_cached_summary, paper_db_id, prompt_hash, pdf_text_hash
            )
            if cached:
                logger.info(f"[AGENT2] 数据库缓存命中: {paper.arxiv_id}")
                await _update_db_summary_status(db, session_id, paper.arxiv_id, "cached")
                # 同步写回文件缓存
                _save_summary(summary_path, cached)
                return cached

    # ── 调用 LLM（异步）附带重试机制 ──────────────────────────────────────
    max_retries = 2
    raw_summary = None
    last_error = None

    token_usage = None
    for attempt in range(max_retries + 1):
        try:
            raw_summary, token_usage = await llm.achat(
                user_prompt=user_prompt,
                system_prompt=_SYSTEM_PROMPT,
            )
            break
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                logger.warning(f"[AGENT2] LLM 调用失败 ({paper.arxiv_id})，重试 {attempt + 1}/{max_retries}...")
                await asyncio.sleep(3.0 * (attempt + 1))

    if raw_summary is None:
        logger.error(f"[AGENT2] 彻底失败 ({paper.arxiv_id})，重试 {max_retries} 次: {last_error}")
        fallback = _make_fallback_summary(paper, error=str(last_error))
        if db and session_id:
            await _update_db_summary_status(db, session_id, paper.arxiv_id, "failed")
        return fallback

    # ── 包装为 Markdown 卡片 ──────────────────────────────────
    md_card = _wrap_as_card(paper, raw_summary)
    _save_summary(summary_path, md_card)

    # ── 保存到数据库 ──────────────────────────────────────────
    if db and session_id:
        paper_rows2 = await asyncio.to_thread(
            lambda: db.conn.execute(
                "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
                (session_id, paper.arxiv_id),
            ).fetchone()
        )
        if paper_rows2:
            await asyncio.to_thread(
                lambda: db.save_summary(
                    paper_id=paper_rows2["id"],
                    prompt_hash=prompt_hash,
                    model_name=llm.model_name,
                    temperature=llm.temperature,
                    max_chars=max_chars,
                    content=md_card,
                    pdf_text_hash=pdf_text_hash,
                    token_count=token_usage.total_tokens if token_usage else None,
                )
            )
            await asyncio.to_thread(
                lambda: db.update_paper(
                    paper_rows2["id"],
                    summary_status="success",
                    summary_path=str(summary_path),
                )
            )

    logger.info(f"[AGENT2] 总结完成: {paper.title[:60]}...")
    return md_card


# ── 内部工具函数 ───────────────────────────────────────────────

# Version tag for the fact-card / self-check block. Bumping this (or editing
# the block below) changes _compute_prompt_hash, invalidating cached summaries
# so papers are re-read with the new output requirements.
_PROMPT_VERSION = "v2-factcard"

_FACTCARD_SELFCHECK_BLOCK = """

─────────────────────────────────────
【附加输出要求 — 以下两节必须在上述分析之后输出】

## 关键数据卡
从论文中提取可直接复用的精确事实，供后续研究 idea 生成与综述写作使用。
硬约束（仅适用于本节，不影响上面的分析散文）：
- 每条必须含：具体数值 + 单位 + 测试条件/数据集
- 严禁模糊词："较高""较大""有所提升""显著优于"等一律不写
- 没有具体数据支撑的结论不要写进本节
- 列表化，每条 ≤ 一行
示例：
- RLBench 10 任务平均成功率 78.3%（基线 Diffusion Policy 52.1%）
- 推理延迟 23ms/帧（NVIDIA A100，batch=1）

## 未提取自检
列出你在论文里看到、但本次分析没有深入提取的内容（自检漏读）：
- 格式：[Fig./Table/章节] — 展示了什么 + 为何未提取
示例：
- Fig.7 — 展示了 attention 可视化，但未提取具体 attention 分布数据
- 附录 C — 含完整超参表，本次未逐项记录
若确无遗漏，写"无明显遗漏"。

---

## 结构化抽取（机器读取，必填）

请在文末另起一节，输出严格 JSON，包在 <JSON>...</JSON> 标签之间：

<JSON>
{
  "problem": "<1-2 句研究问题>",
  "methods": ["<方法/技术名>", "..."],
  "datasets": ["<数据集名>", "..."],
  "metrics": [{"name":"<指标名>", "value":"<数值+单位>", "condition":"<在什么数据集/任务/条件下>"}],
  "baselines": ["<对照方法名>", "..."],
  "limitations": ["<作者明说的局限>", "..."],
  "contributions": ["<主要贡献>", "..."]
}
</JSON>

# 硬性约束
- 标签 <JSON>...</JSON> 完整闭合，内部是 raw JSON（无 ```json 围栏，无注释）
- 列表项软上限：methods/datasets/baselines ≤5，limitations/contributions ≤4，metrics ≤6
- 抽不到就给空数组 []（不要编造、不要写"暂无"占位）
- metrics 必须三元组齐全；只有数字没有 condition 等于无效——宁缺勿凑
- 字符串用论文原文术语，不要中文翻译/改写（除非论文本就是中文）
─────────────────────────────────────"""


def _build_user_prompt(
    paper: PaperMeta,
    pdf_text: str,
    summary_prompt: str,
    topic: str,
) -> str:
    # 判断是否为本地手动放入的文献（缺少在线元信息）
    is_local = paper.arxiv_url == "" and paper.authors == ["未知"]

    meta_lines = [
        f"- **标题**：{paper.title}",
        f"- **作者**：{', '.join(paper.authors[:5])}{'等' if len(paper.authors) > 5 else ''}",
        f"- **发表时间**：{paper.published}",
    ]
    if paper.arxiv_url:
        meta_lines.append(f"- **arxiv 链接**：{paper.arxiv_url}")

    if is_local:
        meta_lines.append(
            "- **说明**：该文献为本地手动导入文件，上述元信息（标题/作者/时间）"
            "可能不准确，请从论文全文首页提取真实的标题、作者和发表时间，"
            "并在分析结果的开头注明真实元信息。"
        )

    return (
        f"## 用户研究构想（背景信息，供关联分析使用）\n{topic.strip()}\n\n"
        f"## 论文元信息\n"
        + "\n".join(meta_lines)
        + f"\n\n## 论文全文（Markdown 格式）\n{pdf_text}\n\n"
        f"## 分析要求\n{summary_prompt.strip()}"
        + _FACTCARD_SELFCHECK_BLOCK
    )


def _wrap_as_card(paper: PaperMeta, summary: str) -> str:
    """将 LLM 输出包装为统一格式的 Markdown 卡片。"""
    authors_str = ", ".join(paper.authors[:5])
    if len(paper.authors) > 5:
        authors_str += " 等"

    link = paper.arxiv_url or paper.pdf_url or ""
    title_str = f"[{paper.title}]({link})" if link else paper.title

    return (
        f"### {title_str}\n\n"
        f"**作者**：{authors_str}　"
        f"**发表时间**：{paper.published}　"
        f"**相关性评分**：{paper.relevance_score:.2f}\n\n"
        f"{summary.strip()}\n"
    )


def _make_fallback_summary(paper: PaperMeta, error: str) -> str:
    """处理失败时生成占位摘要卡片。"""
    link = paper.arxiv_url or paper.pdf_url or ""
    title_str = f"[{paper.title}]({link})" if link else paper.title

    return (
        f"### {title_str}\n\n"
        f"**作者**：{', '.join(paper.authors[:3])}　"
        f"**发表时间**：{paper.published}\n\n"
        f"> ⚠️ 本文处理失败，错误信息：{error}\n\n"
        f"**摘要（原文）**：{(paper.abstract or '')[:500]}...\n"
    )


def _save_summary(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _compute_prompt_hash(summary_prompt: str, model: str, temperature: float, max_chars: int, topic: str = "") -> str:
    # _FACTCARD_SELFCHECK_BLOCK is folded in directly, so ANY edit to the block
    # auto-invalidates cached summaries (no need to remember bumping _PROMPT_VERSION).
    components = f"{summary_prompt}|{model}|{temperature}|{max_chars}|{topic}|{_PROMPT_VERSION}|{_FACTCARD_SELFCHECK_BLOCK}"
    return hashlib.sha256(components.encode()).hexdigest()


def _compute_pdf_hash(pdf_text: str) -> str:
    return hashlib.sha256(pdf_text.encode()).hexdigest()


def _extract_pmcid(paper: PaperMeta) -> str | None:
    """从 PaperMeta 中提取 PMCID。

    支持格式：
    - arxiv_id = "pmcid_PMC123456" → "PMC123456"
    - doi = "PMC123456" → "PMC123456"
    """
    if paper.arxiv_id:
        if paper.arxiv_id.startswith("pmcid_"):
            return paper.arxiv_id[6:]  # strip "pmcid_" prefix
    if paper.doi:
        if paper.doi.startswith("PMC"):
            return paper.doi
    return None


async def _update_db_summary_status(
    db: "Database", session_id: int, arxiv_id: str, status: str
) -> None:
    await asyncio.to_thread(
        db.conn.execute,
        "UPDATE papers SET summary_status = ? WHERE session_id = ? AND arxiv_id = ?",
        (status, session_id, arxiv_id),
    )
    await asyncio.to_thread(db.conn.commit)
