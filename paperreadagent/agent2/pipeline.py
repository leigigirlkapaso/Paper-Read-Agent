"""
agent2/pipeline.py
Two-stage AGENT2 pipeline: Parse Pool → asyncio.Queue → LLM Pool.

Separates PDF parsing (CPU/IO-bound, ThreadPoolExecutor via asyncio.to_thread)
from LLM reading (API-bound, asyncio.Semaphore) so parse never blocks LLM slots.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent1.arxiv_searcher import PaperMeta
from agent2.paper_reader import (
    _compute_prompt_hash,
    _build_user_prompt,
    _wrap_as_card,
    _make_fallback_summary,
    _save_summary,
    _compute_pdf_hash,
    _extract_pmcid,
    _update_db_summary_status,
)
from agent2.paper_reader import _SYSTEM_PROMPT
from utils.llm_client import LLMClient
from utils.pdf_parser import parse_pdf, _ARXIV_ID_PATTERN, _try_pmc_xml
from utils.extraction_parser import parse_extraction

if TYPE_CHECKING:
    from db.database import Database

logger = logging.getLogger(__name__)


async def _backfill_extraction_from_cache(db, paper_id: int, cached_markdown: str) -> None:
    """Cache-hit branches: if the cached markdown carries a <JSON> block AND the
    current papers.extraction_json is NULL, backfill it. Best-effort, never raises."""
    try:
        extraction = parse_extraction(cached_markdown)
        if not extraction:
            return
        existing = await asyncio.to_thread(
            lambda: db.conn.execute(
                "SELECT extraction_json FROM papers WHERE id = ?", (paper_id,)
            ).fetchone()
        )
        if existing and existing["extraction_json"]:
            return  # already populated; don't overwrite
        await asyncio.to_thread(
            lambda: db.update_paper(
                paper_id,
                extraction_json=json.dumps(extraction, ensure_ascii=False),
            )
        )
    except Exception:
        logger.warning("[Pipeline] cache-hit extraction backfill failed", exc_info=True)


# ═══════════════════════════════════════════════════════════════════
# Stage helpers
# ═══════════════════════════════════════════════════════════════════

async def _prepare_paper_text(
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
    """Check cache or parse PDF. Returns pdf_text (or cached markdown summary).

    Cache hit → returns the cached markdown summary (starts with "### ").
    Cache miss → parses PDF and returns the raw text.
    Never returns empty string — falls back to abstract snippet on parse failure.

    Mirrors the cache-check + parse logic from paper_reader.read_paper()
    lines 59-132, but stops before the LLM call.
    """
    safe_id = paper.arxiv_id.replace("/", "_")

    # ── Compute prompt fingerprint ─────────────────────────────
    prompt_hash = _compute_prompt_hash(
        summary_prompt, llm.model_name, llm.temperature, max_chars, topic
    )
    prompt_short = prompt_hash[:8]

    legacy_path = summary_dir / f"{safe_id}.md"
    summary_path = summary_dir / f"{safe_id}_{prompt_short}.md"

    # Clean legacy cache format
    if legacy_path.exists():
        try:
            legacy_path.unlink()
        except OSError:
            pass

    # ── File-level cache check ─────────────────────────────────
    if summary_path.exists() and summary_path.stat().st_size > 50:
        try:
            cached = summary_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning(f"[Pipeline] 缓存文件损坏: {paper.arxiv_id}")
            summary_path.unlink(missing_ok=True)
        else:
            logger.info(f"[Pipeline] 缓存命中 (file): {paper.arxiv_id}")
            if db and session_id:
                await _update_db_summary_status(db, session_id, paper.arxiv_id, "cached")
            return cached  # markdown summary → _do_llm_read will pass through

    # Note: DB cache check requires pdf_text_hash (computed after parsing),
    # so it lives in _do_llm_read() instead. File cache handles pre-parse hits.

    # ── PDF parse ──────────────────────────────────────────────
    pmc_text = None
    if paper.source_platform == "pmc" or (paper.arxiv_id or "").startswith(
        ("pmcid_", "pmid_")
    ):
        pmcid = _extract_pmcid(paper)
        if pmcid:
            pmc_text = _try_pmc_xml(pmcid)

    if pmc_text:
        pdf_text = pmc_text
        logger.info(f"[Pipeline] PMC XML 解析完成: {paper.arxiv_id}")
    else:
        try:
            arxiv_id = (
                paper.arxiv_id
                if (paper.arxiv_id and _ARXIV_ID_PATTERN.search(paper.arxiv_id))
                else ""
            )
            pdf_text = await asyncio.wait_for(
                asyncio.to_thread(parse_pdf, pdf_path, max_chars, arxiv_id=arxiv_id),
                timeout=120,
            )
        except asyncio.TimeoutError:
            logger.error(f"[Pipeline] PDF 解析超时 ({paper.arxiv_id})")
            pdf_text = f"## 标题\n{paper.title}\n\n## 摘要\n{(paper.abstract or '无摘要')[:5000]}"
        except Exception as e:
            logger.error(f"[Pipeline] PDF 解析失败 ({paper.arxiv_id}): {e}")
            pdf_text = f"## 标题\n{paper.title}\n\n## 摘要\n{(paper.abstract or '无摘要')[:5000]}\n\n> 全文解析失败: {e}"

    return pdf_text


async def _do_llm_read(
    paper: PaperMeta,
    pdf_text: str,
    summary_prompt: str,
    topic: str,
    llm: LLMClient,
    summary_dir: Path,
    max_chars: int = 110000,
    db: "Database | None" = None,
    session_id: int = 0,
) -> str:
    """Run LLM reading on parsed/cached text. Returns markdown summary.

    If pdf_text starts with "### " (cached summary from file/DB),
    returns it as-is without re-calling LLM.
    """
    # ── Cache hit passthrough ──────────────────────────────────
    if pdf_text.strip().startswith("### "):
        if db and session_id:
            paper_rows = await asyncio.to_thread(
                lambda: db.conn.execute(
                    "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
                    (session_id, paper.arxiv_id),
                ).fetchone()
            )
            if paper_rows:
                await _backfill_extraction_from_cache(db, paper_rows["id"], pdf_text)
        return pdf_text

    # ── Build prompt + hashes ──────────────────────────────────
    prompt_hash = _compute_prompt_hash(
        summary_prompt, llm.model_name, llm.temperature, max_chars, topic
    )
    pdf_text_hash = _compute_pdf_hash(pdf_text)
    prompt_short = prompt_hash[:8]
    safe_id = paper.arxiv_id.replace("/", "_")
    summary_path = summary_dir / f"{safe_id}_{prompt_short}.md"

    # ── DB cache check (requires pdf_text_hash, now available) ─
    if db and session_id:
        paper_rows = await asyncio.to_thread(
            lambda: db.conn.execute(
                "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
                (session_id, paper.arxiv_id),
            ).fetchone()
        )
        if paper_rows:
            cached = await asyncio.to_thread(
                db.get_cached_summary, paper_rows["id"], prompt_hash, pdf_text_hash
            )
            if cached:
                logger.info(f"[Pipeline] 缓存命中 (db): {paper.arxiv_id}")
                await _update_db_summary_status(db, session_id, paper.arxiv_id, "cached")
                _save_summary(summary_path, cached)
                await _backfill_extraction_from_cache(db, paper_rows["id"], cached)
                return cached

    user_prompt = _build_user_prompt(paper, pdf_text, summary_prompt, topic)

    # ── LLM call with retry ────────────────────────────────────
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
                logger.warning(
                    f"[Pipeline] LLM 重试 {attempt + 1}/{max_retries} ({paper.arxiv_id})..."
                )
                await asyncio.sleep(3.0 * (attempt + 1))

    if raw_summary is None:
        logger.error(
            f"[Pipeline] LLM 彻底失败 ({paper.arxiv_id}): {last_error}"
        )
        fallback = _make_fallback_summary(paper, error=str(last_error))
        if db and session_id:
            await _update_db_summary_status(db, session_id, paper.arxiv_id, "failed")
        return fallback

    # ── Wrap + save ────────────────────────────────────────────
    md_card = _wrap_as_card(paper, raw_summary)
    _save_summary(summary_path, md_card)

    # ── DB persistence ─────────────────────────────────────────
    if db and session_id:
        paper_rows = await asyncio.to_thread(
            lambda: db.conn.execute(
                "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
                (session_id, paper.arxiv_id),
            ).fetchone()
        )
        if paper_rows:
            await asyncio.to_thread(
                lambda: db.save_summary(
                    paper_id=paper_rows["id"],
                    prompt_hash=prompt_hash,
                    model_name=llm.model_name,
                    temperature=llm.temperature,
                    max_chars=max_chars,
                    content=md_card,
                    pdf_text_hash=pdf_text_hash,
                    token_count=token_usage.total_tokens if token_usage else None,
                )
            )
            # Parse structured extraction (best-effort; None on any failure)
            extraction = parse_extraction(raw_summary)
            extraction_json_str = (
                json.dumps(extraction, ensure_ascii=False) if extraction else None
            )
            await asyncio.to_thread(
                lambda: db.update_paper(
                    paper_rows["id"],
                    summary_status="success",
                    summary_path=str(summary_path),
                    extraction_json=extraction_json_str,
                )
            )

    logger.info(f"[Pipeline] 总结完成: {paper.title[:60]}...")
    return md_card


# ═══════════════════════════════════════════════════════════════════
# Main pipeline orchestrator
# ═══════════════════════════════════════════════════════════════════

async def run_pipelined(
    papers: list[PaperMeta],
    pdf_dir: Path,
    summary_dir: Path,
    summary_prompt: str,
    topic: str,
    llm: LLMClient,
    max_llm_concurrent: int = 100,
    max_parse_workers: int = 8,
    max_chars: int = 110000,
    db: "Database | None" = None,
    session_id: int = 0,
) -> list[tuple[PaperMeta, str]]:
    """Two-stage pipeline: Parse Pool → asyncio.Queue → LLM Pool.

    Stage 1 — Parse Pool:
        Parses PDFs concurrently (asyncio.Semaphore for parse concurrency).
        Cache hits skip parsing entirely. Results fed into async queue.

    Stage 2 — LLM Pool:
        Multiple workers consume from queue, each guarded by LLM semaphore.
        Cache hits pass through instantly (no API call).

    Args:
        papers:              Papers to process
        pdf_dir:             Directory containing downloaded PDFs
        summary_dir:         Directory for summary cache files
        summary_prompt:      LLM summary prompt from config
        topic:               User's research topic
        llm:                 LLMClient instance
        max_llm_concurrent:  Max concurrent LLM API calls
        max_parse_workers:   Max concurrent PDF parses
        max_chars:           Max characters for PDF text
        db:                  Optional Database instance
        session_id:          Current session ID

    Returns:
        List of (PaperMeta, summary_markdown) in input order
    """
    if not papers:
        return []

    summary_dir.mkdir(parents=True, exist_ok=True)

    # ── Build pdf_path lookup map ──────────────────────────────
    pdf_map: dict[str, Path] = {}
    if pdf_dir.exists():
        for f in pdf_dir.glob("*.pdf"):
            pdf_map[f.stem] = f

    n_papers = len(papers)

    # ── Queue connecting stages ────────────────────────────────
    parse_queue: asyncio.Queue = asyncio.Queue(maxsize=max_llm_concurrent * 2)

    # ── Results collection ─────────────────────────────────────
    results: dict[str, str] = {}  # arxiv_id → summary markdown

    # ── Stage 2: LLM consumer workers ──────────────────────────
    llm_sem = asyncio.Semaphore(max_llm_concurrent)

    async def _llm_worker(worker_id: int) -> None:
        """Consume (paper, pdf_text) from queue, run LLM, store result."""
        while True:
            item = await parse_queue.get()
            try:
                if item is None:  # sentinel → stop
                    return
                paper, pdf_text = item
                async with llm_sem:
                    summary = await _do_llm_read(
                        paper=paper,
                        pdf_text=pdf_text,
                        summary_prompt=summary_prompt,
                        topic=topic,
                        llm=llm,
                        summary_dir=summary_dir,
                        max_chars=max_chars,
                        db=db,
                        session_id=session_id,
                    )
                results[paper.arxiv_id] = summary
                done = len(results)
                if done % 5 == 0 or done == n_papers:
                    logger.info(f"[Pipeline] LLM 进度 {done}/{n_papers}")
            except Exception as e:
                logger.error(f"[Pipeline] LLM worker {worker_id} 异常: {e}")
            finally:
                parse_queue.task_done()

    # Start LLM workers (1 per concurrency slot)
    n_workers = min(n_papers, max_llm_concurrent)
    llm_workers = [
        asyncio.create_task(_llm_worker(i)) for i in range(n_workers)
    ]
    logger.info(
        f"[Pipeline] 启动两阶段管道: {n_papers} 篇, "
        f"parse={max_parse_workers}w, llm={n_workers}w"
    )

    # ── Stage 1: Parse producer — concurrent parse, enqueue as ready ─
    parse_sem = asyncio.Semaphore(max_parse_workers)

    async def _parse_one(paper: PaperMeta) -> tuple[PaperMeta, str]:
        """Parse one paper and return (paper, pdf_text)."""
        stem = paper.arxiv_id.replace("/", "_")
        pdf_path = pdf_map.get(stem, pdf_dir / f"{stem}.pdf")
        async with parse_sem:
            try:
                pdf_text = await _prepare_paper_text(
                    paper=paper,
                    pdf_path=pdf_path,
                    summary_dir=summary_dir,
                    summary_prompt=summary_prompt,
                    topic=topic,
                    llm=llm,
                    max_chars=max_chars,
                    db=db,
                    session_id=session_id,
                )
            except Exception as e:
                logger.error(f"[Pipeline] Parse 失败 ({paper.arxiv_id}): {e}")
                pdf_text = _make_fallback_summary(paper, error=f"Parse: {e}")
            return (paper, pdf_text)

    # Fan out all parses, enqueue as they complete
    parse_tasks = [asyncio.create_task(_parse_one(p)) for p in papers]
    for coro in asyncio.as_completed(parse_tasks):
        paper, pdf_text = await coro
        await parse_queue.put((paper, pdf_text))

    # ── All papers enqueued — signal workers to stop ───────────
    for _ in llm_workers:
        await parse_queue.put(None)

    # ── Wait for workers to drain the queue ────────────────────
    await asyncio.gather(*llm_workers)

    # ── Build ordered output ───────────────────────────────────
    ordered: list[tuple[PaperMeta, str]] = []
    for p in papers:
        summary = results.get(p.arxiv_id)
        if summary is None:
            summary = _make_fallback_summary(p, error="Pipeline processing missed")
            logger.warning(f"[Pipeline] 论文遗漏，使用占位摘要: {p.arxiv_id}")
        ordered.append((p, summary))

    logger.info(f"[Pipeline] 管道完成: {len(ordered)} 篇")
    return ordered
