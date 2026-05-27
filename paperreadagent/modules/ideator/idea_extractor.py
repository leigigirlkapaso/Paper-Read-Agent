"""
modules/ideator/idea_extractor.py
IdeaExtractor — 将笔记拆分为独立 idea，各自 embedding，支持 idea 级语义搜索。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_FLASH_MODEL = "deepseek-v4-flash"
_MAX_IDEA_CHARS = 5000


class IdeaExtractor:
    """LLM 驱动的笔记 idea 提取器 + embedding + 缓存。

    每条笔记 → flash LLM 提取 1-N 个独立 idea →
    每个 idea 独立 embedding → 缓存到 ideator_note_ideas。
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
            [{idea_index, content, embedding: list[float]}, ...]
        """
        # 1. 查缓存
        cached = self._data.get_note_ideas(note_source, note_id)
        if cached:
            return cached

        # 2. flash LLM 提取 idea
        try:
            idea_texts = await self._extract_via_llm(note_text)
        except Exception:
            logger.warning(
                "[IdeaExtractor] LLM 提取失败，回退整条笔记",
                exc_info=True,
            )
            idea_texts = [note_text[:_MAX_IDEA_CHARS]]

        if not idea_texts:
            idea_texts = [note_text[:_MAX_IDEA_CHARS]]

        # 3. 每个 idea 独立 embedding
        ideas = []
        for idx, idea_text in enumerate(idea_texts):
            emb = []
            try:
                emb = await self._core_llm.embed(
                    idea_text[:_MAX_IDEA_CHARS], module="ideator",
                )
            except Exception:
                logger.warning(
                    "[IdeaExtractor] idea embedding 失败",
                    exc_info=True,
                )
            ideas.append({
                "note_source": note_source,
                "note_id": note_id,
                "idea_index": idx,
                "content": idea_text,
                "embedding": emb,
            })

        # 4. 写缓存
        try:
            self._data.insert_note_ideas(note_source, note_id, ideas)
        except Exception:
            logger.warning("[IdeaExtractor] idea 缓存写入失败", exc_info=True)

        return ideas

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
