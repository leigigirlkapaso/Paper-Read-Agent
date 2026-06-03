"""
core/knowledge.py
KnowledgeLayer — 统一笔记存储与语义搜索。所有模块通过此层存取知识。
"""

from __future__ import annotations

import logging
from typing import Optional

import json as _json


def _safe_loads(raw, default=None):
    if default is None:
        default = []
    if not raw:
        return default
    try:
        return _json.loads(raw)
    except (_json.JSONDecodeError, TypeError):
        return default


def _safe_dumps(obj, **kwargs):
    return _json.dumps(obj, ensure_ascii=False, **kwargs)

from .decorators import stable, evolving
from .database import CoreDatabase
from .embedding import pack_embedding, unpack_embedding, cosine_similarity

logger = logging.getLogger(__name__)

_EMBEDDING_SEARCH_LIMIT = 1000


class KnowledgeLayer:
    """
    跨模块知识中心。提供 core_notes 的 CRUD + embedding 相似搜索。

    所有模块通过此层存取笔记、摘要、洞察、承诺等文本内容。
    embedding 基于 content 生成，metadata 为模块自定义 JSON 侧车数据。
    """

    def __init__(self, db: CoreDatabase):
        self._db = db

    @evolving
    def insert_note(
        self,
        *,
        source_module: str,
        content: str,
        source_ref: str = "",
        content_type: str = "note",
        tags: list[str] | None = None,
        metadata: dict | None = None,
        embedding: list[float] | None = None,
    ) -> int:
        """插入一条笔记，返回 id。"""
        cursor = self._db.conn.execute(
            """INSERT INTO core_notes
               (source_module, source_ref, content, embedding, content_type, tags, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                source_module,
                source_ref,
                content,
                pack_embedding(embedding or []),
                content_type,
                _safe_dumps(tags or []),
                _safe_dumps(metadata or {}),
            ),
        )
        self._db.conn.commit()
        return cursor.lastrowid

    @stable
    def get_note(self, note_id: int) -> dict | None:
        row = self._db.conn.execute(
            "SELECT * FROM core_notes WHERE id = ?", (note_id,)
        ).fetchone()
        return _unpack_note(row) if row else None

    @evolving
    def get_notes_by_module(
        self, source_module: str, *, content_type: str | None = None, limit: int = 50
    ) -> list[dict]:
        if content_type:
            rows = self._db.conn.execute(
                """SELECT * FROM core_notes
                   WHERE source_module = ? AND content_type = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (source_module, content_type, limit),
            ).fetchall()
        else:
            rows = self._db.conn.execute(
                "SELECT * FROM core_notes WHERE source_module = ? ORDER BY created_at DESC LIMIT ?",
                (source_module, limit),
            ).fetchall()
        return [_unpack_note(r) for r in rows]

    def _search_by_embedding(
        self,
        query_embedding: list[float],
        *,
        source_module: str | None = None,
        top_k: int = 5,
        min_similarity: float | None = None,
        contradictions: bool = False,
    ) -> list[dict]:
        """Shared embedding search — fetches rows with LIMIT, computes cosine similarity."""
        if source_module:
            rows = self._db.conn.execute(
                "SELECT id, content, source_module, source_ref, content_type, "
                "tags, metadata, embedding, created_at "
                "FROM core_notes WHERE source_module = ? AND embedding != '' "
                "LIMIT ?",
                (source_module, _EMBEDDING_SEARCH_LIMIT),
            ).fetchall()
        else:
            rows = self._db.conn.execute(
                "SELECT id, content, source_module, source_ref, content_type, "
                "tags, metadata, embedding, created_at "
                "FROM core_notes WHERE embedding != '' LIMIT ?",
                (_EMBEDDING_SEARCH_LIMIT,),
            ).fetchall()

        scored: list[tuple[float, dict]] = []
        for r in rows:
            emb = unpack_embedding(r["embedding"])
            if not emb:
                continue
            sim = cosine_similarity(query_embedding, emb)
            if contradictions:
                scored.append((-abs(sim), dict(r)))
            elif min_similarity is None or sim >= min_similarity:
                scored.append((sim, dict(r)))

        scored.sort(key=lambda x: x[0], reverse=not contradictions)
        results = []
        for sim_val, row_dict in scored[:top_k]:
            d = _unpack_note(row_dict)
            d["_similarity"] = sim_val
            results.append(d)
        return results

    @evolving
    def search_by_embedding(
        self,
        query_embedding: list[float],
        *,
        source_module: str | None = None,
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> list[dict]:
        """基于 embedding 余弦相似度搜索相似笔记。"""
        return self._search_by_embedding(
            query_embedding,
            source_module=source_module,
            top_k=top_k,
            min_similarity=min_similarity,
        )

    @evolving
    def find_contradictions(
        self,
        query_embedding: list[float],
        *,
        source_module: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        """找出与查询向量方向相反的已有笔记。"""
        return self._search_by_embedding(
            query_embedding,
            source_module=source_module,
            top_k=top_k,
            contradictions=True,
        )

    @evolving
    def update_note(self, note_id: int, **kwargs) -> None:
        sets: list[str] = []
        params: list = []
        for key, val in kwargs.items():
            col = {
                "content": "content", "content_type": "content_type",
                "source_ref": "source_ref", "tags": "tags",
                "metadata": "metadata", "embedding": "embedding",
            }.get(key, key)
            sets.append(f"{col} = ?")
            if isinstance(val, (list, dict)):
                params.append(_safe_dumps(val))
            else:
                params.append(val)
        if not sets:
            return
        params.append(note_id)
        self._db.conn.execute(
            f"UPDATE core_notes SET {', '.join(sets)} WHERE id = ?", params
        )
        self._db.conn.commit()

    @evolving
    def delete_by_module(self, source_module: str) -> int:
        """删除指定模块的所有笔记，返回删除数量。"""
        cursor = self._db.conn.execute(
            "DELETE FROM core_notes WHERE source_module = ?", (source_module,)
        )
        self._db.conn.commit()
        return cursor.rowcount


def _unpack_note(row) -> dict:
    d = row if isinstance(row, dict) else dict(row)
    d["tags"] = _safe_loads(d.get("tags", ""), default=[])
    d["metadata"] = _safe_loads(d.get("metadata", ""), default={})
    return d
