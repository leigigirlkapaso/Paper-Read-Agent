"""
core/knowledge.py
KnowledgeLayer — 统一笔记存储与语义搜索。所有模块通过此层存取知识。

v3: 语义分块。长文档自动切分为重叠窗口 chunk，每个 chunk 独立 embedding。
    LanceDB 存储 note_chunks 表（note_id + chunk_index 复合键），搜索时去重归并。
"""

from __future__ import annotations

import logging
from pathlib import Path
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
from .chunk import semantic_chunk

logger = logging.getLogger(__name__)

_DEFAULT_VECTOR_DIM = 1024
_FALLBACK_SEARCH_LIMIT = 1000
_CHUNK_SIZE = 500       # characters per chunk
_CHUNK_OVERLAP = 100    # overlap between adjacent chunks
_CHUNK_TABLE = "note_chunks"


class KnowledgeLayer:
    """
    跨模块知识中心。提供 core_notes 的 CRUD + embedding 语义搜索。

    v2: 底层使用 LanceDB 做 ANN 近似最近邻搜索，O(log n)。
    写入时 SQLite + LanceDB 双写；搜索优先走 LanceDB，失败回退暴力搜索。
    """

    def __init__(self, db: CoreDatabase, *, data_dir: str = ""):
        self._db = db
        self._lance_uri: str = ""
        self._lance_ready: bool = False
        self._vector_dim: int = _DEFAULT_VECTOR_DIM

        # Resolve LanceDB data directory
        if data_dir:
            base = Path(data_dir)
        else:
            base = Path(__file__).parent.parent.parent  # project root
        self._lance_uri = str(base / "data" / "lancedb")
        self._ensure_lance()

    # ── LanceDB lifecycle ────────────────────────────────────────

    def _ensure_lance(self) -> None:
        """Initialize or open LanceDB. Fails silently — search falls back to brute-force."""
        try:
            import lancedb
            import pyarrow as pa

            Path(self._lance_uri).mkdir(parents=True, exist_ok=True)
            self._lance_db = lancedb.connect(self._lance_uri)

            try:
                self._lance_table = self._lance_db.open_table(_CHUNK_TABLE)
                self._lance_ready = True
                logger.info(f"[Knowledge] LanceDB ready: {self._lance_uri}")
            except Exception:
                self._lance_table = None
                self._lance_ready = True
                logger.info(f"[Knowledge] LanceDB connected, table will be created on first write")
        except Exception:
            self._lance_ready = False
            logger.warning("[Knowledge] LanceDB unavailable, falling back to brute-force search",
                           exc_info=True)

    def _ensure_table(self) -> None:
        """Lazy-create the LanceDB note_chunks table."""
        if not self._lance_ready or self._lance_table is not None:
            return
        try:
            import pyarrow as pa

            schema = pa.schema([
                pa.field("note_id", pa.int64()),
                pa.field("chunk_index", pa.int32()),
                pa.field("source_module", pa.string()),
                pa.field("content", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self._vector_dim)),
            ])
            self._lance_table = self._lance_db.create_table(
                _CHUNK_TABLE, schema=schema, mode="create",
            )
            logger.info(f"[Knowledge] LanceDB table '{_CHUNK_TABLE}' created (dim={self._vector_dim})")
        except Exception:
            logger.warning("[Knowledge] Failed to create LanceDB table", exc_info=True)

    # ── CRUD ─────────────────────────────────────────────────────

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
        """插入一条笔记，返回 id。SQLite + LanceDB 双写。"""
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
        note_id = cursor.lastrowid

        # Dual-write to LanceDB: chunk content, embed each chunk
        if embedding:
            self._write_chunks_to_lance(note_id, source_module, content, embedding)
        elif len(content) > _CHUNK_SIZE:
            # Content too large for embedding; store stub for future re-index
            logger.warning(f"[Knowledge] Note {note_id}: content too large ({len(content)} chars), no embedding stored")

        return note_id

    def _write_chunks_to_lance(
        self, note_id: int, source_module: str, content: str, embedding: list[float]
    ) -> None:
        """Write single-chunk embedding to LanceDB (for backward compat — caller already embedded)."""
        if not self._lance_ready:
            return
        try:
            self._ensure_table()
            if self._lance_table is None:
                return

            # Remove old chunks for this note (upsert: delete + re-add)
            try:
                self._lance_table.delete(f"note_id = {note_id}")
            except Exception:
                logger.warning(f"[Knowledge] LanceDB delete failed for note {note_id}", exc_info=True)

            # For notes with pre-computed embedding (single vector), store as chunk 0
            rows = [{
                "note_id": note_id,
                "chunk_index": 0,
                "source_module": source_module,
                "content": content[:2000],
                "vector": [float(x) for x in embedding],
            }]
            self._lance_table.add(rows)
        except Exception:
            logger.warning(f"[Knowledge] LanceDB write failed for note {note_id}", exc_info=True)

    def _write_chunks_to_lance_multi(
        self, note_id: int, source_module: str, content: str, chunk_embeddings: list[list[float]]
    ) -> None:
        """Write multiple chunk embeddings to LanceDB (semantic chunking path)."""
        if not self._lance_ready:
            return
        try:
            self._ensure_table()
            if self._lance_table is None:
                return

            try:
                self._lance_table.delete(f"note_id = {note_id}")
            except Exception:
                logger.warning(f"[Knowledge] LanceDB multi delete failed for note {note_id}", exc_info=True)

            rows = []
            for i, emb in enumerate(chunk_embeddings):
                piece = content[i * _CHUNK_SIZE:(i + 1) * _CHUNK_SIZE + _CHUNK_OVERLAP]
                rows.append({
                    "note_id": note_id,
                    "chunk_index": i,
                    "source_module": source_module,
                    "content": piece[:2000],
                    "vector": [float(x) for x in emb],
                })
            self._lance_table.add(rows)
        except Exception:
            logger.warning(f"[Knowledge] LanceDB multi-chunk write failed for note {note_id}", exc_info=True)

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

    # ── Search ───────────────────────────────────────────────────

    def _search_lance(
        self,
        query_embedding: list[float],
        *,
        source_module: str | None = None,
        top_k: int = 5,
        min_similarity: float | None = None,
        contradictions: bool = False,
    ) -> list[dict] | None:
        """
        LanceDB ANN search. Returns None if LanceDB is unavailable or the table
        doesn't exist, signaling the caller to fall back to brute-force.
        """
        if not self._lance_ready or self._lance_table is None:
            return None

        # L2 distance can't distinguish vector direction — contradictions must use brute-force
        if contradictions:
            return None

        try:
            query_vec = [float(x) for x in query_embedding]
            q = self._lance_table.search(query_vec).limit(max(top_k * 3, 30))

            if source_module:
                # LanceDB SQL-like filter — sanitize single quotes in module name
                safe_module = source_module.replace("'", "''")
                q = q.where(f"source_module = '{safe_module}'", prefilter=True)

            results = q.to_list()

            # Convert L2 distance to cosine similarity for normalized vectors
            # Dedup by note_id: keep the best-matching chunk per note
            best_per_note: dict[int, tuple[float, dict]] = {}
            for r in results:
                distance = r.get("_distance", 0.0)
                sim = 1.0 - (distance ** 2) / 2.0
                if min_similarity is None or sim >= min_similarity:
                    nid = r.get("note_id", r.get("id", 0))  # note_id in chunk table, id in legacy
                    if nid not in best_per_note or sim > best_per_note[nid][0]:
                        best_per_note[nid] = (sim, r)

            scored = sorted(best_per_note.values(), key=lambda x: x[0], reverse=True)

            out = []
            for sim_val, lance_row in scored[:top_k]:
                note = self.get_note(lance_row.get("note_id", lance_row.get("id", 0)))
                if note:
                    note["_similarity"] = sim_val
                    note["_chunk_text"] = lance_row.get("content", "")
                    out.append(note)
            return out
        except Exception:
            logger.warning("[Knowledge] LanceDB search failed, falling back", exc_info=True)
            return None

    def _search_by_embedding(
        self,
        query_embedding: list[float],
        *,
        source_module: str | None = None,
        top_k: int = 5,
        min_similarity: float | None = None,
        contradictions: bool = False,
    ) -> list[dict]:
        """语义搜索 — 优先 LanceDB ANN，失败回退 SQLite 暴力搜索。"""

        # Try LanceDB first
        lance_result = self._search_lance(
            query_embedding,
            source_module=source_module,
            top_k=top_k,
            min_similarity=min_similarity,
            contradictions=contradictions,
        )
        if lance_result is not None:
            return lance_result

        # Fallback: brute-force cosine similarity against all embedded notes
        if source_module:
            rows = self._db.conn.execute(
                "SELECT id, content, source_module, source_ref, content_type, "
                "tags, metadata, embedding, created_at "
                "FROM core_notes WHERE source_module = ? AND embedding != '' "
                "LIMIT ?",
                (source_module, _FALLBACK_SEARCH_LIMIT),
            ).fetchall()
        else:
            rows = self._db.conn.execute(
                "SELECT id, content, source_module, source_ref, content_type, "
                "tags, metadata, embedding, created_at "
                "FROM core_notes WHERE embedding != '' LIMIT ?",
                (_FALLBACK_SEARCH_LIMIT,),
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

    # ── Migration ────────────────────────────────────────────────

    @evolving
    def populate_lance_from_sqlite(self) -> int:
        """
        一次性从 SQLite core_notes 表迁移已有 embedding 到 LanceDB。
        返回成功迁移的向量数量。
        """
        rows = self._db.conn.execute(
            "SELECT id, source_module, content, embedding "
            "FROM core_notes WHERE embedding != '' AND embedding IS NOT NULL"
        ).fetchall()

        if not rows:
            logger.info("[Knowledge] No embeddings to migrate")
            return 0

        self._ensure_table()
        if self._lance_table is None:
            logger.error("[Knowledge] Cannot migrate — LanceDB table unavailable")
            return 0

        try:
            import pyarrow as pa

            # F1: Clear existing data to prevent duplication on restart
            try:
                self._lance_table.delete("note_id >= 0")
            except Exception:
                logger.warning("[Knowledge] LanceDB clear failed during migration", exc_info=True)

            data = []
            batch = []
            for r in rows:
                emb = unpack_embedding(r["embedding"])
                if not emb:
                    continue
                batch.append({
                    "note_id": r["id"],
                    "chunk_index": 0,
                    "source_module": r["source_module"] or "",
                    "content": (r["content"] or "")[:2000],
                    "vector": [float(x) for x in emb],
                })
                if len(batch) >= 500:
                    self._lance_table.add(batch)
                    data.extend(batch)
                    batch = []
            if batch:
                self._lance_table.add(batch)
                data.extend(batch)
                logger.info(f"[Knowledge] Migrated {len(data)} embeddings to LanceDB")
            return len(data)
        except Exception:
            logger.error("[Knowledge] Migration failed", exc_info=True)
            return 0

    # ── Updates & Deletion ───────────────────────────────────────

    @evolving
    def update_note(self, note_id: int, **kwargs) -> None:
        _ALLOWED_COLS = {"content", "content_type", "source_ref", "tags", "metadata", "embedding"}
        sets: list[str] = []
        params: list = []
        for key, val in kwargs.items():
            if key not in _ALLOWED_COLS:
                raise ValueError(f"Cannot update column: {key}")
            col = key
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

        # Sync to LanceDB if embedding changed
        if "embedding" in kwargs:
            if kwargs["embedding"]:
                row = self._db.conn.execute(
                    "SELECT source_module, content FROM core_notes WHERE id = ?", (note_id,)
                ).fetchone()
                if row:
                    self._write_chunks_to_lance(
                        note_id, row["source_module"] or "", row["content"] or "",
                        kwargs["embedding"],
                    )
            else:
                # F4: Remove stale chunks when embedding is cleared
                if self._lance_ready and self._lance_table is not None:
                    try:
                        self._lance_table.delete(f"note_id = {note_id}")
                    except Exception:
                        logger.warning(f"[Knowledge] LanceDB cleanup failed for note {note_id}", exc_info=True)

    @evolving
    def delete_by_module(self, source_module: str) -> int:
        """删除指定模块的所有笔记 + LanceDB 中的向量。"""
        # Get IDs before deleting for LanceDB cleanup
        ids = self._db.conn.execute(
            "SELECT id FROM core_notes WHERE source_module = ?", (source_module,)
        ).fetchall()
        cursor = self._db.conn.execute(
            "DELETE FROM core_notes WHERE source_module = ?", (source_module,)
        )
        self._db.conn.commit()

        # Clean up LanceDB
        if self._lance_ready and self._lance_table is not None and ids:
            try:
                id_list = ", ".join(str(r["id"]) for r in ids)
                self._lance_table.delete(f"note_id IN ({id_list})")
            except Exception:
                logger.warning("[Knowledge] LanceDB cleanup failed", exc_info=True)

        return cursor.rowcount


def _unpack_note(row) -> dict:
    d = row if isinstance(row, dict) else dict(row)
    d["tags"] = _safe_loads(d.get("tags", ""), default=[])
    d["metadata"] = _safe_loads(d.get("metadata", ""), default={})
    return d
