"""
agent2/paper_reader.py
AGENT2：对单篇已下载的论文进行独立精读与结构化总结。

- 每篇文献独立调用一次 LLM（控制 token 消耗）
- 使用 async 接口，配合 parallel_runner 并发执行
- 支持断点续传：若 summary 文件已存在则直接读取，不重复调用
- Phase 1：数据库缓存（prompt-aware），通过 db 参数可选启用
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent1.arxiv_searcher import PaperMeta
from utils.llm_client import LLMClient
from utils.pdf_parser import parse_pdf

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
    max_chars: int = 60000,
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
                _update_db_summary_status(db, session_id, paper.arxiv_id, "cached")
                # 同步写回 DB summaries 表（修复缓存命中时 DB 无内容的问题）
                paper_rows0 = db.conn.execute(
                    "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
                    (session_id, paper.arxiv_id),
                ).fetchone()
                if paper_rows0:
                    db.save_summary(
                        paper_id=paper_rows0["id"],
                        prompt_hash=prompt_hash,
                        model_name=llm.model_name,
                        temperature=llm.temperature,
                        max_chars=max_chars,
                        content=cached_content,
                        pdf_text_hash="cached",
                    )
            return cached_content

    # ── PDF 解析 ──────────────────────────────────────────────
    try:
        import asyncio
        pdf_text = await asyncio.wait_for(
            asyncio.to_thread(parse_pdf, pdf_path, max_chars), timeout=120
        )
    except Exception as e:
        logger.error(f"[AGENT2] PDF 解析失败 ({paper.arxiv_id}): {e}")
        fallback = _make_fallback_summary(paper, error=str(e))
        if db and session_id:
            _update_db_summary_status(db, session_id, paper.arxiv_id, "failed")
        return fallback

    # ── 构建 Prompt ───────────────────────────────────────────
    user_prompt = _build_user_prompt(paper, pdf_text, summary_prompt, topic)
    pdf_text_hash = _compute_pdf_hash(pdf_text)

    # ── 数据库缓存检查（精确匹配：prompt + PDF 内容） ─────────
    if db and session_id:
        paper_rows = db.conn.execute(
            "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
            (session_id, paper.arxiv_id),
        ).fetchone()
        if paper_rows:
            paper_db_id = paper_rows["id"]
            cached = db.get_cached_summary(paper_db_id, prompt_hash, pdf_text_hash)
            if cached:
                logger.info(f"[AGENT2] 数据库缓存命中: {paper.arxiv_id}")
                _update_db_summary_status(db, session_id, paper.arxiv_id, "cached")
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
            _update_db_summary_status(db, session_id, paper.arxiv_id, "failed")
        return fallback

    # ── 包装为 Markdown 卡片 ──────────────────────────────────
    md_card = _wrap_as_card(paper, raw_summary)
    _save_summary(summary_path, md_card)

    # ── 保存到数据库 ──────────────────────────────────────────
    if db and session_id:
        paper_rows2 = db.conn.execute(
            "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
            (session_id, paper.arxiv_id),
        ).fetchone()
        if paper_rows2:
            db.save_summary(
                paper_id=paper_rows2["id"],
                prompt_hash=prompt_hash,
                model_name=llm.model_name,
                temperature=llm.temperature,
                max_chars=max_chars,
                content=md_card,
                pdf_text_hash=pdf_text_hash,
                token_count=token_usage.total_tokens if token_usage else None,
            )
            db.update_paper(
                paper_rows2["id"],
                summary_status="success",
                summary_path=str(summary_path),
            )

    logger.info(f"[AGENT2] 总结完成: {paper.title[:60]}...")
    return md_card


# ── 内部工具函数 ───────────────────────────────────────────────

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
    components = f"{summary_prompt}|{model}|{temperature}|{max_chars}|{topic}"
    return hashlib.sha256(components.encode()).hexdigest()


def _compute_pdf_hash(pdf_text: str) -> str:
    return hashlib.sha256(pdf_text.encode()).hexdigest()


def _update_db_summary_status(
    db: "Database", session_id: int, arxiv_id: str, status: str
) -> None:
    db.conn.execute(
        "UPDATE papers SET summary_status = ? WHERE session_id = ? AND arxiv_id = ?",
        (status, session_id, arxiv_id),
    )
    db.conn.commit()
