"""
agent2/parallel_runner.py
AGENT2 并发调度器：使用 asyncio.Semaphore 控制并发数，
对所有已下载论文并行调用 paper_reader，并显示进度。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

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
    max_chars: int = 110000,
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
    from agent2.pipeline import run_pipelined

    logger.info(
        f"[AGENT2] 启动两阶段管道精读：{len(papers)} 篇，"
        f"LLM 并发={max_concurrent}"
    )

    return await run_pipelined(
        papers=papers,
        pdf_dir=pdf_dir,
        summary_dir=summary_dir,
        summary_prompt=summary_prompt,
        topic=topic,
        llm=llm,
        max_llm_concurrent=max_concurrent,
        max_parse_workers=8,
        max_chars=max_chars,
        db=db,
        session_id=session_id,
    )
