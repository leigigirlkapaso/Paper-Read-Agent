"""
modules/ideator/idea_extractor.py
IdeaExtractor — 将笔记拆分为独立 idea，各自 embedding，支持 idea 级语义搜索。

v2: 长笔记自动 semantic_chunk 后逐块提取，覆盖全量内容。
"""

from __future__ import annotations

import json
import logging

from paperreadagent.core.chunk import semantic_chunk
from paperreadagent.core.embedding import cosine_similarity

logger = logging.getLogger(__name__)

_FLASH_MODEL = "deepseek-v4-flash"
_MAX_IDEA_CHARS = 5000       # max chars per idea text for embedding
_CHUNK_SIZE = 3000           # semantic chunk window
_CHUNK_OVERLAP = 500         # overlap between adjacent chunks
_IDEA_DEDUP_THRESHOLD = 0.90  # cosine threshold to merge duplicate ideas


class IdeaExtractor:
    """LLM 驱动的笔记 idea 提取器 + embedding + 缓存。

    短笔记（≤3000 字）：单次 flash LLM 提取 → embedding → 缓存。
    长笔记：semantic_chunk(3000/500) → 逐块提取 → 去重 → embedding → 缓存。
    后续召回在 idea 级别做语义搜索，MaxSim 聚合回笔记对。
    """

    def __init__(self, *, llm, core_llm, data):
        self._llm = llm          # IdeatorLLM (flash model via model= override)
        self._core_llm = core_llm  # CoreLLM (for embedding)
        self._data = data          # DataAccess

    async def get_or_extract_ideas(
        self, note_source: str, note_id: int, note_text: str,
    ) -> list[dict]:
        """获取笔记的 idea 列表，优先缓存，未命中则 LLM 提取 + embedding + 缓存。

        Returns:
            [{note_source, note_id, idea_index, content, embedding: list[float]}, ...]
        """
        # 1. 查缓存
        cached = self._data.get_note_ideas(note_source, note_id)
        if cached:
            return cached

        # 2. 短笔记走单次路径，长笔记走分块路径
        if len(note_text) <= _CHUNK_SIZE:
            ideas = await self._extract_single(note_source, note_id, note_text)
        else:
            ideas = await self._extract_chunked(note_source, note_id, note_text)

        # 3. 写缓存
        if ideas:
            try:
                self._data.insert_note_ideas(note_source, note_id, ideas)
            except Exception:
                logger.warning("[IdeaExtractor] idea 缓存写入失败", exc_info=True)

        return ideas

    async def _extract_single(
        self, note_source: str, note_id: int, note_text: str,
    ) -> list[dict]:
        """短笔记：单次 flash LLM 提取 + embedding。"""
        try:
            idea_texts = await self._extract_via_llm(note_text)
        except Exception:
            logger.warning("[IdeaExtractor] LLM 提取失败，回退整条笔记", exc_info=True)
            idea_texts = [note_text[:_MAX_IDEA_CHARS]]

        if not idea_texts:
            idea_texts = [note_text[:_MAX_IDEA_CHARS]]

        ideas = []
        for idx, idea_text in enumerate(idea_texts):
            emb = await self._embed_idea(idea_text)
            ideas.append({
                "note_source": note_source,
                "note_id": note_id,
                "idea_index": idx,
                "content": idea_text,
                "embedding": emb,
            })
        return ideas

    async def _extract_chunked(
        self, note_source: str, note_id: int, note_text: str,
    ) -> list[dict]:
        """长笔记：semantic_chunk → 逐块提取 → 去重 → embedding。"""
        chunks = semantic_chunk(
            note_text, chunk_size=_CHUNK_SIZE, overlap=_CHUNK_OVERLAP,
        )
        logger.info(
            f"[IdeaExtractor] Chunked note {note_id}: "
            f"{len(note_text)} chars → {len(chunks)} chunks"
        )

        all_ideas: list[dict] = []  # [{content, embedding}, ...]

        for ch in chunks:
            try:
                idea_texts = await self._extract_via_llm(ch["text"])
            except Exception:
                logger.warning(
                    f"[IdeaExtractor] chunk {ch['index']} LLM 提取失败，跳过",
                    exc_info=True,
                )
                continue

            for idea_text in idea_texts:
                emb = await self._embed_idea(idea_text)
                all_ideas.append({
                    "note_source": note_source,
                    "note_id": note_id,
                    "content": idea_text,
                    "embedding": emb,
                })

        if not all_ideas:
            logger.warning(f"[IdeaExtractor] 全部分块提取失败，回退首段")
            return await self._extract_single(note_source, note_id, note_text)

        # 去重：跨 chunk 的相似 idea 合并（保留首个）
        deduped = self._dedup_ideas(all_ideas, threshold=_IDEA_DEDUP_THRESHOLD)
        # 重新分配 idea_index
        for i, idea in enumerate(deduped):
            idea["idea_index"] = i

        logger.info(
            f"[IdeaExtractor] Note {note_id}: {len(all_ideas)} raw ideas "
            f"→ {len(deduped)} after dedup"
        )
        return deduped

    def _dedup_ideas(self, ideas: list[dict], threshold: float) -> list[dict]:
        """跨 chunk 去重：embedding 余弦相似度 ≥ threshold 视为同一 idea。"""
        if len(ideas) <= 1:
            return ideas

        keep = [ideas[0]]
        for cand in ideas[1:]:
            is_dup = False
            for kept in keep:
                if cand["embedding"] and kept["embedding"]:
                    sim = cosine_similarity(cand["embedding"], kept["embedding"])
                    if sim >= threshold:
                        is_dup = True
                        break
            if not is_dup:
                keep.append(cand)
        return keep

    async def _embed_idea(self, idea_text: str) -> list[float]:
        """对单个 idea 文本做 embedding，失败返回空列表。"""
        try:
            return await self._core_llm.embed(
                idea_text[:_MAX_IDEA_CHARS], module="ideator",
            )
        except Exception:
            logger.warning("[IdeaExtractor] idea embedding 失败", exc_info=True)
            return []

    async def _extract_via_llm(self, note_text: str) -> list[str]:
        """调 flash LLM 提取独立 idea。"""
        prompt = self._core_llm.load_prompt(
            "ideator", "extract_ideas",
            note_text=note_text[:_MAX_IDEA_CHARS],
        )
        raw = await self._llm.chat(
            model_role="idea_extractor",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=2048,
            model=_FLASH_MODEL,
        )
        from paperreadagent.utils.json_utils import clean_json
        raw = clean_json(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[IdeaExtractor] flash LLM JSON 解析失败: %s", raw[:200])
            return []
        return data.get("ideas", [])
