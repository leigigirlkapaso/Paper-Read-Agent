"""
utils/arxiv_downloader.py
负责从 arxiv 或直接 URL 下载 PDF 文件。
- 优先使用 arxiv 官方 PDF 链接
- 支持直接 URL 下载（来自 Semantic Scholar / OpenAlex 开放获取链接）
- 支持断点续传（文件已存在则跳过）
- 下载失败时记录日志，不中断主流程
- Phase 4.1：异步并发下载（aiohttp + Semaphore）
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiohttp
import requests

logger = logging.getLogger(__name__)

_PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; PaperReadAgent/1.0; "
        "+https://github.com/yourname/PaperReadAgent)"
    )
}


# ==============================================================================
# 同步下载（保留向后兼容）
# ==============================================================================

def download_paper(
    arxiv_id: str,
    output_dir: str | Path,
    retries: int = 3,
    timeout: int = 60,
    direct_url: str = "",
) -> Path | None:
    """同步下载单篇论文 PDF。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    clean_id = arxiv_id.strip().split("v")[0]
    filename = f"{clean_id.replace('/', '_')}.pdf"
    dest_path = output_dir / filename

    if dest_path.exists() and dest_path.stat().st_size > 1024:
        logger.info(f"[Downloader] 已存在，跳过: {filename}")
        return dest_path

    if direct_url:
        url = direct_url
    elif _is_arxiv_id(clean_id):
        url = _PDF_URL_TEMPLATE.format(arxiv_id=clean_id)
    else:
        logger.warning(f"[Downloader] 无可用 PDF URL，跳过: {clean_id}")
        return None

    for attempt in range(1, retries + 1):
        try:
            logger.info(f"[Downloader] 下载 {clean_id} (第 {attempt} 次)...")
            resp = requests.get(url, headers=_HEADERS, timeout=timeout, stream=True)
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            if "pdf" not in content_type and "octet-stream" not in content_type:
                first_chunk = next(resp.iter_content(chunk_size=8), b"")
                if not first_chunk.startswith(b"%PDF"):
                    logger.warning(f"[Downloader] 返回内容非 PDF ({content_type})，跳过: {clean_id}")
                    return None
                resp.close()
                resp = requests.get(url, headers=_HEADERS, timeout=timeout, stream=True)
                resp.raise_for_status()

            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            size_kb = dest_path.stat().st_size // 1024
            if size_kb < 1:
                logger.warning(f"[Downloader] 文件过小，可能无效: {filename}")
                dest_path.unlink(missing_ok=True)
                return None

            logger.info(f"[Downloader] 完成: {filename} ({size_kb} KB)")
            return dest_path

        except Exception as e:
            logger.warning(f"[Downloader] 第 {attempt} 次失败 ({clean_id}): {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
            else:
                logger.error(f"[Downloader] 放弃下载: {clean_id}")
                if dest_path.exists():
                    dest_path.unlink()
                return None


def download_papers_batch(
    arxiv_ids: list[str],
    output_dir: str | Path,
    delay: float = 1.0,
    pdf_urls: dict[str, str] | None = None,
) -> dict[str, Path | None]:
    """同步批量下载（串行）。"""
    results: dict[str, Path | None] = {}
    url_map = pdf_urls or {}
    total = len(arxiv_ids)
    for i, arxiv_id in enumerate(arxiv_ids, 1):
        logger.info(f"[Downloader] 批量下载进度 {i}/{total}")
        direct_url = url_map.get(arxiv_id, "")
        results[arxiv_id] = download_paper(arxiv_id, output_dir, direct_url=direct_url)
        if i < total:
            time.sleep(delay)
    return results


# ==============================================================================
# 异步并发下载（Phase 4.1）
# ==============================================================================

async def download_paper_async(
    arxiv_id: str,
    output_dir: Path,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    retries: int = 3,
    timeout: int = 60,
    direct_url: str = "",
) -> Path | None:
    """异步下载单篇 PDF，带信号量限流。"""
    clean_id = arxiv_id.strip().split("v")[0]
    filename = f"{clean_id.replace('/', '_')}.pdf"
    dest_path = output_dir / filename

    # 断点续传
    if dest_path.exists() and dest_path.stat().st_size > 1024:
        logger.debug(f"[AsyncDownloader] 已存在: {filename}")
        return dest_path

    if direct_url:
        url = direct_url
    elif _is_arxiv_id(clean_id):
        url = _PDF_URL_TEMPLATE.format(arxiv_id=clean_id)
    else:
        logger.warning(f"[AsyncDownloader] 无可用 URL: {clean_id}")
        return None

    async with semaphore:
        for attempt in range(1, retries + 1):
            try:
                async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        raise aiohttp.ClientError(f"HTTP {resp.status}")

                    # 验证魔术字节
                    first_bytes = await resp.content.read(8)
                    if not first_bytes.startswith(b"%PDF"):
                        logger.warning(f"[AsyncDownloader] 非 PDF 内容: {clean_id}")
                        return None

                    # 分块写入，避免大文件撑爆内存
                    with open(dest_path, "wb") as f:
                        f.write(first_bytes)
                        while True:
                            chunk = await resp.content.read(65536)  # 64KB
                            if not chunk:
                                break
                            f.write(chunk)

                size_kb = dest_path.stat().st_size // 1024
                if size_kb < 1:
                    dest_path.unlink(missing_ok=True)
                    return None

                logger.info(f"[AsyncDownloader] 完成: {filename} ({size_kb} KB)")
                return dest_path

            except Exception as e:
                logger.warning(f"[AsyncDownloader] 第 {attempt} 次失败 ({clean_id}): {e}")
                if attempt < retries:
                    await asyncio.sleep(2 * attempt)
                else:
                    logger.error(f"[AsyncDownloader] 放弃: {clean_id}")
                    if dest_path.exists():
                        dest_path.unlink()
                    return None


async def download_papers_batch_async(
    arxiv_ids: list[str],
    output_dir: Path,
    max_concurrent: int = 5,
    delay_between: float = 0.5,
    pdf_urls: dict[str, str] | None = None,
) -> dict[str, Path | None]:
    """
    异步并发批量下载。

    Args:
        arxiv_ids:      论文 ID 列表
        output_dir:     保存目录
        max_concurrent: 最大并发连接数
        delay_between:  每批启动之间的延迟（秒）
        pdf_urls:       {arxiv_id: direct_url} 映射

    Returns:
        {arxiv_id: Path or None}
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    url_map = pdf_urls or {}

    semaphore = asyncio.Semaphore(max_concurrent)

    async with aiohttp.ClientSession() as session:
        tasks = []
        for arxiv_id in arxiv_ids:
            direct_url = url_map.get(arxiv_id, "")
            tasks.append(
                download_paper_async(
                    arxiv_id=arxiv_id,
                    output_dir=output_dir,
                    session=session,
                    semaphore=semaphore,
                    direct_url=direct_url,
                )
            )
            # 批次间微延迟，避免瞬间洪峰
            if delay_between > 0:
                await asyncio.sleep(delay_between)

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

    results: dict[str, Path | None] = {}
    for arxiv_id, result in zip(arxiv_ids, results_list):
        if isinstance(result, Exception):
            logger.error(f"[AsyncDownloader] 异常 ({arxiv_id}): {result}")
            results[arxiv_id] = None
        else:
            results[arxiv_id] = result

    return results


# ==============================================================================
# 工具
# ==============================================================================

def _is_arxiv_id(id_str: str) -> bool:
    if id_str.startswith(("s2_", "oa_", "pwc_")):
        return False
    import re
    return bool(re.match(r'^[\d]{4}\.\d{4,5}$', id_str) or
                re.match(r'^[a-z\-]+/\d{7}$', id_str))
