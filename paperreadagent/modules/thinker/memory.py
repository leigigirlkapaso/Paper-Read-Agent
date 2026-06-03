"""
modules/thinker/memory.py
MemoryPipeline — 记忆编码、检索、重排、注入。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from paperreadagent.core import Core
from .constants import (
    MEMORY_TYPE_INSIGHT,
    MEMORY_TYPE_RESOLUTION,
    MEMORY_TYPE_SPARK,
)

logger = logging.getLogger(__name__)

_CANDIDATE_SNIPPET_LEN = 800
_RERANK_CACHE: dict[str, tuple[float, str]] = {}
_RERANK_CACHE_TTL = 30


class MemoryPipeline:
    """记忆管道：检索 → 重排 → 注入。编码逻辑在 ChatEngine 中。"""

    def __init__(self, core: Core):
        self.core = core

    # ── 检索 ──────────────────────────────────────────────

    async def retrieve(self, user_message: str) -> list[dict]:
        """多路并行召回原始候选记忆。返回含 _source 标记的列表。"""
        emb = await self.core.llm.embed(user_message[:3000], module="thinker")

        task_names = ["semantic", "resolutions", "recent", "profile", "sparks"]
        coros = [
            self._retrieve_semantic(emb),
            self._retrieve_resolutions(),
            self._retrieve_recent_insights(),
            self._retrieve_profile(),
            self._retrieve_sparks(),
        ]
        gathered = await asyncio.gather(*coros, return_exceptions=True)
        results = {}
        for name, result in zip(task_names, gathered):
            if isinstance(result, Exception):
                logger.warning(f"[MemoryPipeline] 召回路径 {name} 失败", exc_info=True)
                results[name] = []
            else:
                results[name] = result

        merged = []
        seen_content = set()
        for source, items in results.items():
            for item in items:
                key = item.get("content", "")[:80]
                if key in seen_content:
                    continue
                seen_content.add(key)
                item["_source"] = source
                merged.append(item)

        return merged

    async def _retrieve_semantic(self, emb: list[float]) -> list[dict]:
        if not emb:
            return []
        return self.core.knowledge.search_by_embedding(
            emb, source_module="thinker", top_k=5, min_similarity=0.3,
        )

    async def _retrieve_resolutions(self) -> list[dict]:
        rows = self.core.db.conn.execute(
            """SELECT id, content, status, deadline FROM thinker_resolutions
               WHERE status IN ('pending','in_progress')
               ORDER BY created_at DESC LIMIT 3"""
        ).fetchall()
        return [
            {"id": f"res_{r['id']}", "content": f"[{r['status']}] {r['content']}",
             "type": "resolution", "status": r["status"], "deadline": r["deadline"]}
            for r in rows
        ]

    async def _retrieve_recent_insights(self) -> list[dict]:
        rows = self.core.db.conn.execute(
            """SELECT mi.id, cn.content FROM thinker_memory_index mi
               JOIN core_notes cn ON mi.core_note_id = cn.id
               WHERE mi.memory_type = ?
               ORDER BY cn.created_at DESC LIMIT 3""",
            (MEMORY_TYPE_INSIGHT,),
        ).fetchall()
        return [
            {"id": f"recent_{r['id']}", "content": r["content"][:_CANDIDATE_SNIPPET_LEN],
             "type": "insight"}
            for r in rows
        ]

    async def _retrieve_profile(self) -> list[dict]:
        row = self.core.db.conn.execute(
            "SELECT * FROM thinker_user_profile WHERE id = 1"
        ).fetchone()
        if not row:
            return []
        r = dict(row)
        parts = []
        domains = json.loads(r.get("research_domains", "[]"))
        if domains:
            parts.append(f"研究领域: {', '.join(domains)}")
        goals = json.loads(r.get("long_term_goals", "[]"))
        if goals:
            parts.append(f"长期目标: {', '.join(goals)}")
        style = r.get("thinking_style", "")
        if style:
            parts.append(f"思维偏好: {style}")
        if not parts:
            return []
        return [{"id": "profile", "content": " | ".join(parts), "type": "profile"}]

    async def _retrieve_sparks(self) -> list[dict]:
        rows = self.core.db.conn.execute(
            """SELECT mi.id, cn.content FROM thinker_memory_index mi
               JOIN core_notes cn ON mi.core_note_id = cn.id
               WHERE mi.memory_type = ?
               ORDER BY mi.importance DESC LIMIT 2""",
            (MEMORY_TYPE_SPARK,),
        ).fetchall()
        return [
            {"id": f"spark_{r['id']}", "content": r["content"][:_CANDIDATE_SNIPPET_LEN],
             "type": "spark"}
            for r in rows
        ]

    # ── 重排 ──────────────────────────────────────────────

    async def rerank(self, user_message: str, candidates: list[dict]) -> list[dict]:
        """LLM 重排候选记忆，返回 top 5-6 附带 relevance + reason。"""
        if len(candidates) <= 6:
            result = [dict(c) for c in candidates]
            for c in result:
                c.setdefault("relevance", "medium")
                c.setdefault("reason", "")
            return result

        cache_key = str(hash(user_message + json.dumps([c.get("id") for c in candidates], sort_keys=True)))
        now = time.time()
        if cache_key in _RERANK_CACHE:
            ts, cached = _RERANK_CACHE[cache_key]
            if now - ts < _RERANK_CACHE_TTL:
                return json.loads(cached)

        prompt = self.core.llm.load_prompt(
            "thinker", "memory_rerank",
            user_message=user_message,
            candidates=[
                {"id": c.get("id", i), "type": c.get("type", ""),
                 "content": c.get("content", "")[:_CANDIDATE_SNIPPET_LEN]}
                for i, c in enumerate(candidates)
            ],
        )

        try:
            raw, _ = await self.core.llm.achat(
                user_prompt=prompt, module="thinker", purpose="memory_rerank",
            )
            result = json.loads(raw)
            if isinstance(result, list):
                _RERANK_CACHE[cache_key] = (now, json.dumps(result, ensure_ascii=False))
                return result
        except Exception:
            logger.warning("[MemoryPipeline] 重排失败，回退原序", exc_info=True)

        result = [dict(c) for c in candidates[:6]]
        for c in result:
            c.setdefault("relevance", "medium")
            c.setdefault("reason", "")
        return result

    # ── 注入 ──────────────────────────────────────────────

    def inject(self, ranked: list[dict]) -> str:
        """将排序后的记忆片段拼接为 System Prompt 追加文本。"""
        if not ranked:
            return ""
        lines = ["\n\n## 相关记忆（小思记得的内容）\n"]
        for m in ranked:
            content = m.get("content", "")[:800]
            reason = m.get("reason", "")
            type_label = m.get("type", "")
            if type_label == "resolution":
                prefix = "  [未完成]"
            elif type_label == "profile":
                prefix = "  [画像]"
            elif type_label == "spark":
                prefix = "  [念头]"
            else:
                prefix = "  [相关]"
            line = f"{prefix} {content}"
            if reason:
                line += f"（{reason}）"
            lines.append(line)
        return "\n".join(lines)

    async def index_note(self, note_id: int, memory_type: str) -> None:
        """将一条 core_note 注册到 memory_index（供 ChatEngine 编码阶段调用）。"""
        emb_raw = self.core.db.conn.execute(
            "SELECT embedding FROM core_notes WHERE id = ?", (note_id,)
        ).fetchone()
        embedding = emb_raw["embedding"] if emb_raw else ""
        self.core.db.conn.execute(
            """INSERT OR IGNORE INTO thinker_memory_index
               (core_note_id, memory_type, embedding) VALUES (?, ?, ?)""",
            (note_id, memory_type, embedding),
        )
        self.core.db.conn.commit()
