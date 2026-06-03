"""
agent2/parallel_runner.py
AGENT2 并发调度器：使用 asyncio.Semaphore 控制并发数，
对所有已下载论文并行调用 paper_reader，并显示进度。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from tqdm.asyncio import tqdm as async_tqdm

from agent1.arxiv_searcher import PaperMeta
from agent2.paper_reader import read_paper
from utils.llm_client import LLMClient

logger = logging.getLogger(__name__)


async def run_parallel(
    papers: list[PaperMeta],
    pdf_dir: Path,
    summary_dir: Path,
    summary_prompt: str,
    topic: str,
    llm: LLMClient,
    max_concurrent: int = 100,
    max_chars: int = 60000,
    db: "Database | None" = None,
    session_id: int = 0,
) -> list[tuple[PaperMeta, str]]:
    """
    并发精读所有论文，返回 (PaperMeta, markdown_summary) 列表。

    Args:
        papers:         已筛选并下载的论文元信息列表
        pdf_dir:        PDF 存放目录
        summary_dir:    单篇总结缓存目录
        summary_prompt: 来自 config.yaml 的总结 prompt
        topic:          用户研究构想
        llm:            LLMClient 实例
        max_concurrent: 最大并发数（防止 Rate Limit）
        max_chars:      单篇最大 PDF 字符数

    Returns:
        [(PaperMeta, summary_str), ...]，顺序与 papers 一致
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    summary_dir.mkdir(parents=True, exist_ok=True)

    # 扫描目录建立 stem→path 映射
    pdf_map: dict[str, Path] = {}
    if pdf_dir.exists():
        for f in pdf_dir.glob("*.pdf"):
            pdf_map[f.stem] = f

    async def _bounded_read(paper: PaperMeta) -> tuple[PaperMeta, str]:
        stem = paper.arxiv_id.replace("/", "_")
        pdf_path = pdf_map.get(stem, pdf_dir / f"{stem}.pdf")
        async with semaphore:
            summary = await read_paper(
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
        return (paper, summary)

    logger.info(
        f"[AGENT2] 启动并发精读：{len(papers)} 篇，最大并发={max_concurrent}"
    )

    tasks = [_bounded_read(p) for p in papers]

    results: list[tuple[PaperMeta, str]] = []
    for coro in async_tqdm.as_completed(
        tasks,
        total=len(tasks),
        desc="精读论文",
        unit="篇",
    ):
        try:
            result = await asyncio.wait_for(coro, timeout=300)
            results.append(result)
        except asyncio.TimeoutError:
            logger.error("[AGENT2] 单篇论文超时（300s），跳过")
        except Exception as e:
            logger.error(f"[AGENT2] 任务异常: {e}")

    # 恢复原始顺序（as_completed 是乱序的）
    paper_to_summary = {r[0].arxiv_id: r[1] for r in results}
    ordered = []
    for p in papers:
        summary = paper_to_summary.get(p.arxiv_id)
        if summary is None:
            from agent2.paper_reader import _make_fallback_summary
            summary = _make_fallback_summary(p, error="超时或处理异常")
            logger.warning(f"[AGENT2] 论文处理失败，使用占位摘要: {p.arxiv_id}")
        ordered.append((p, summary))

    logger.info("[AGENT2] 所有论文精读完成")
    return ordered
