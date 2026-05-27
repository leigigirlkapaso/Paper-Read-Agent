"""
modules/thinker/knowledge_linker.py
KnowledgeLinker — 基于 embedding 的跨会话知识关联。
"""

from __future__ import annotations

import asyncio
import logging

from paperreadagent.core import Core
from paperreadagent.core.embedding import pack_embedding, unpack_embedding

logger = logging.getLogger(__name__)


class KnowledgeLinker:
    """知识关联引擎。将对话消息与 core_notes 中的笔记建立语义连接。"""

    def __init__(self, core: Core):
        self.core = core

    async def embed_message(self, message_id: int) -> list[float]:
        """为一条消息生成 embedding 并存储，返回 embedding 向量。"""
        row = self.core.db.conn.execute(
            "SELECT content FROM thinker_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if not row or not row["content"]:
            return []

        embedding = await self.core.llm.embed(
            row["content"], module="thinker"
        )

        self.core.db.conn.execute(
            "UPDATE thinker_messages SET embedding = ? WHERE id = ?",
            (pack_embedding(embedding), message_id),
        )
        self.core.db.conn.commit()
        return embedding

    async def _get_embedding(self, message_id: int) -> list[float]:
        """Get embedding for a message, computing it if needed."""
        row = self.core.db.conn.execute(
            "SELECT embedding FROM thinker_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if row:
            emb = unpack_embedding(row["embedding"])
            if emb:
                return emb
        return await self.embed_message(message_id)

    async def find_related_notes(
        self, message_id: int, *, top_k: int = 5, min_similarity: float = 0.3,
        query_embedding: list[float] | None = None,
    ) -> list[dict]:
        """查找与指定消息语义相似的 core_notes 笔记。"""
        query_emb = query_embedding or await self._get_embedding(message_id)
        if not query_emb:
            return []

        return self.core.knowledge.search_by_embedding(
            query_emb, top_k=top_k, min_similarity=min_similarity,
        )

    async def find_contradictions(
        self, message_id: int, *, top_k: int = 3,
        query_embedding: list[float] | None = None,
    ) -> list[dict]:
        """查找与指定消息观点相反的已有笔记。"""
        query_emb = query_embedding or await self._get_embedding(message_id)
        if not query_emb:
            return []

        return self.core.knowledge.find_contradictions(
            query_emb, top_k=top_k,
        )

    async def link_message_to_knowledge(self, message_id: int) -> list[dict]:
        """一键关联：embedding 计算一次，并行查相关笔记和矛盾。"""
        query_emb = await self._get_embedding(message_id)
        if not query_emb:
            return []

        raw = await asyncio.gather(
            self.find_related_notes(message_id, query_embedding=query_emb),
            self.find_contradictions(message_id, query_embedding=query_emb),
            return_exceptions=True,
        )
        related = raw[0] if not isinstance(raw[0], Exception) else []
        contradictions = raw[1] if not isinstance(raw[1], Exception) else []

        results = []
        for r in related:
            r["_type"] = "related"
            results.append(r)
        for c in contradictions:
            c["_type"] = "contradiction"
            results.append(c)

        return sorted(results, key=lambda x: abs(x.get("_similarity", 0)), reverse=True)
