"""
template: semantic_chunker
Sliding-window semantic text splitter for Chinese/English documents.
Source: PaperReadAgent
"""
from __future__ import annotations
import re
from typing import List, Dict


# ── Defaults ──────────────────────────────────────────────────────

_DEFAULT_CHUNK_SIZE = 500        # characters per chunk
_DEFAULT_OVERLAP = 100           # character overlap between adjacent chunks
_MIN_CHUNK_SIZE = 100            # below this, merge with previous chunk

# Semantic boundaries: paragraph breaks first, then sentence endings
_BOUNDARY_PATTERN = re.compile(
    r"\n\n|\n(?=[A-Z一-鿿])|[。！？.!?](?=\s*[A-Z一-鿿])",
)
_SENTENCE_END = re.compile(r"[。！？.!?]$")


# ── Public API ────────────────────────────────────────────────────

def semantic_chunk(
    text: str,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_OVERLAP,
    min_size: int = _MIN_CHUNK_SIZE,
) -> list[dict]:
    """Split long text into overlapping chunks at semantic boundaries.

    Strategy:
    1. Find all boundary positions (paragraph breaks, sentence endings)
    2. Walk through text, cutting at the nearest boundary within [chunk_size]
    3. Slide forward by (chunk_size - overlap) for next chunk
    4. Merge undersized final chunks with previous

    Returns list of dicts: {text, index, byte_start, byte_end, boundary_type}
    """
    if not text or not text.strip():
        return []

    boundaries = _find_boundaries(text, chunk_size)
    chunks = _build_chunks(text, boundaries, chunk_size, overlap, min_size)
    return chunks


def _find_boundaries(text: str, chunk_size: int) -> list[int]:
    """Find all semantic boundary positions in text.
    Includes implicit boundaries every chunk_size as fallback.
    """
    positions: set[int] = {0, len(text)}
    # Paragraph breaks and sentence endings
    for m in _BOUNDARY_PATTERN.finditer(text):
        positions.add(m.end())
    # Add fallback boundaries at regular intervals
    for i in range(chunk_size, len(text), chunk_size):
        positions.add(i)
    return sorted(positions)


def _build_chunks(
    text: str,
    boundaries: list[int],
    chunk_size: int,
    overlap: int,
    min_size: int,
) -> list[dict]:
    """Build chunk list by walking boundaries with sliding window."""
    chunks: list[dict] = []
    start = 0
    step = chunk_size - overlap
    if step <= 0:
        step = chunk_size // 2  # safeguard

    while start < len(text):
        # Find the nearest boundary >= (start + chunk_size)
        target = start + chunk_size
        end = target
        for b in boundaries:
            if b >= target:
                end = b
                break
        if end == target:
            end = min(len(text), target)  # no boundary found, hard cut

        chunk_text = text[start:end].strip()
        if not chunk_text:
            start += step
            continue

        # Detect boundary type
        boundary_type = "semantic"
        if end >= len(text):
            boundary_type = "eof"
        elif _SENTENCE_END.search(text[end-5:end] if end > 5 else text[:end]):
            boundary_type = "sentence"
        elif end == target:
            boundary_type = "fallback"

        chunks.append({
            "text": chunk_text,
            "index": len(chunks),
            "byte_start": start,
            "byte_end": end,
            "boundary_type": boundary_type,
        })

        if end >= len(text):
            break
        start = max(0, end - overlap)

    # Merge undersized tail chunk with previous
    if len(chunks) >= 2 and len(chunks[-1]["text"]) < min_size:
        tail = chunks.pop()
        prev = chunks[-1]
        merged_text = prev["text"] + " " + tail["text"]
        chunks[-1] = {
            "text": merged_text.strip(),
            "index": prev["index"],
            "byte_start": prev["byte_start"],
            "byte_end": tail["byte_end"],
            "boundary_type": "merged",
        }

    return chunks
