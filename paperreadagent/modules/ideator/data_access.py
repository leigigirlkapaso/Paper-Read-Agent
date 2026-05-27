"""
modules/ideator/data_access.py
DataAccess — 统一数据适配层，桥接 legacy DB + Core DB。
"""

from __future__ import annotations

import json
import logging

from paperreadagent.core import Core

logger = logging.getLogger(__name__)


class DataAccess:
    """ideator 统一数据访问。对上层屏蔽 legacy/core 双句柄。"""

    def __init__(self, core: Core):
        self._core = core
        if core.legacy_db is None:
            raise RuntimeError("Core.legacy_db 未注入，请确保 web/app.py 已桥接")
        self._legacy = core.legacy_db

    # ── 论文层（走 legacy）──────────────────────

    def get_paper(self, paper_id: int) -> dict | None:
        return self._legacy.get_paper(paper_id)

    def get_paper_by_arxiv_id(self, arxiv_id: str) -> dict | None:
        """通过 arxiv_id 精确查找论文。"""
        row = self._legacy.conn.execute(
            "SELECT * FROM papers WHERE arxiv_id = ? LIMIT 1", (arxiv_id,),
        ).fetchone()
        return self._legacy.dict_row(row) if row else None

    def get_papers_by_session(self, session_id: int) -> list[dict]:
        return self._legacy.get_session_papers(session_id)

    def get_paper_summaries(self, paper_id: int) -> list[dict]:
        return self._legacy.get_paper_summaries(paper_id)

    def get_user_note(self, paper_id: int) -> dict | None:
        return self._legacy.get_note(paper_id)

    def search_papers(self, query: str, project_id: int | None = None) -> list[dict]:
        return self._legacy.search_papers(query, project_id)

    def get_all_notes(self, project_id: int | None = None) -> list[dict]:
        return self._legacy.get_all_notes(project_id)

    def get_all_papers_with_notes(self, project_id: int | None = None) -> list[dict]:
        """返回有笔记的论文列表（用于跨笔记联想）。"""
        notes = self._legacy.get_all_notes(project_id)
        papers = {}
        for note in notes:
            paper_id = note.get("paper_id")
            if paper_id and paper_id not in papers:
                paper = self._legacy.get_paper(paper_id)
                if paper:
                    paper["_note"] = note.get("content", "")
                    papers[paper_id] = paper
        return list(papers.values())

    def get_cross_project_graph(self) -> dict:
        return self._legacy.get_cross_project_graph()

    # ── 知识层（走 core）──────────────────────

    def search_core_notes(
        self, embedding: list[float], *, top_k: int = 5, min_similarity: float = 0.3
    ) -> list[dict]:
        return self._core.knowledge.search_by_embedding(
            embedding, top_k=top_k, min_similarity=min_similarity,
        )

    def find_contradictions(
        self, embedding: list[float], *, top_k: int = 5
    ) -> list[dict]:
        return self._core.knowledge.find_contradictions(embedding, top_k=top_k)

    def get_notes_by_module(
        self, source_module: str, *, content_type: str | None = None, limit: int = 50
    ) -> list[dict]:
        return self._core.knowledge.get_notes_by_module(
            source_module, content_type=content_type, limit=limit,
        )

    def get_recent_insights(self, limit: int = 10) -> list[dict]:
        """获取系统中最新的可挖掘内容（core_notes + legacy notes）。
        只取 literature 模块的笔记，排除 thinker 闲聊和 ideator 火花。"""
        results = []
        for ctype in ("insight", "note", "spark", "hypothesis", "resolution"):
            rows = self._core.db.conn.execute(
                """SELECT id, source_module, source_ref, content, content_type,
                          tags, metadata, created_at
                   FROM core_notes
                   WHERE content_type = ? AND source_module = 'literature'
                   ORDER BY created_at DESC LIMIT ?""",
                (ctype, limit),
            ).fetchall()
            for r in rows:
                results.append(dict(r))
        # 从 legacy 笔记合并
        try:
            legacy_notes = self._legacy.get_all_notes()
            for note in (legacy_notes or []):
                if note.get("content"):
                    results.append({
                        "id": note.get("id"),
                        "content": note.get("content", ""),
                        "created_at": note.get("created_at", ""),
                        "source_module": "literature",
                        "content_type": "note",
                    })
        except Exception:
            logger.debug("[DataAccess] get_recent_insights failed", exc_info=True)
        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return results[:limit]

    # ── 火花专用 ──────────────────────────────

    def get_existing_sparks(self, limit: int = 200) -> list[dict]:
        rows = self._core.db.conn.execute(
            """SELECT * FROM ideator_sparks
               WHERE embedding != ''
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return self._core.db.dict_rows(rows)

    def find_similar_sparks_by_embedding(
        self, embedding: list[float], *, top_k: int = 3,
        min_similarity: float = 0.60,
    ) -> list[dict]:
        """通过 embedding 向量查找语义相似的火花。"""
        from paperreadagent.core.embedding import cosine_similarity, unpack_embedding
        existing = self.get_existing_sparks(limit=200)
        if not existing or not embedding:
            return []

        scored = []
        for s in existing:
            emb = unpack_embedding(s.get("embedding", ""))
            if not emb:
                continue
            sim = cosine_similarity(embedding, emb)
            if sim >= min_similarity:
                scored.append({
                    "id": s["id"],
                    "content": s.get("content", "")[:500],
                    "quality_score": s.get("quality_score", 0),
                    "status": s.get("status", ""),
                    "source_type": s.get("source_type", ""),
                    "similarity": round(sim, 4),
                })

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:top_k]

    def insert_spark(self, **fields) -> int:
        allowed = ("content", "status", "source_type", "source_refs",
                    "embedding", "quality_score", "metadata",
                    "run_id", "generator_score", "final_score",
                    "review_status", "review_count", "depth_content")
        vals = {k: fields.get(k) for k in allowed}
        defaults = {
            "status": "seed", "source_refs": "[]", "quality_score": 0.5,
            "metadata": "{}", "run_id": "", "generator_score": 0.0,
            "final_score": 0.0, "review_status": "pending", "review_count": 0,
            "depth_content": "",
        }
        for k, default in defaults.items():
            if vals[k] is None:
                vals[k] = default

        cursor = self._core.db.conn.execute(
            """INSERT INTO ideator_sparks
               (content, status, source_type, source_refs, embedding, quality_score, metadata,
                run_id, generator_score, final_score, review_status, review_count, depth_content)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vals["content"], vals["status"], vals["source_type"],
             json.dumps(vals["source_refs"], ensure_ascii=False)
                if isinstance(vals["source_refs"], list) else vals["source_refs"],
             vals["embedding"], vals["quality_score"], vals["metadata"],
             vals["run_id"], vals["generator_score"], vals["final_score"],
             vals["review_status"], vals["review_count"], vals["depth_content"]),
        )
        self._core.db.conn.commit()
        return cursor.lastrowid

    def insert_cross_link(self, **fields) -> int:
        cursor = self._core.db.conn.execute(
            """INSERT INTO ideator_cross_links
               (source_a_type, source_a_id, source_b_type, source_b_id,
                link_type, relevance_score, reasoning, spark_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (fields["source_a_type"], fields["source_a_id"],
             fields["source_b_type"], fields["source_b_id"],
             fields["link_type"], fields.get("relevance_score", 0.0),
             fields.get("reasoning", ""), fields.get("spark_id")),
        )
        self._core.db.conn.commit()
        return cursor.lastrowid

    def update_spark(self, spark_id: int, **fields) -> None:
        allowed = {"status", "quality_score", "depth_content", "user_feedback",
                    "source_refs", "metadata", "deepened_at", "embedding",
                    "final_score", "review_status", "review_count", "verdict"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = []
        for k in updates:
            v = updates[k]
            if k in ("source_refs", "metadata") and isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            vals.append(v)
        vals.append(spark_id)
        self._core.db.conn.execute(
            f"UPDATE ideator_sparks SET {set_clause} WHERE id = ?", vals,
        )
        self._core.db.conn.commit()

    def get_spark(self, spark_id: int) -> dict | None:
        row = self._core.db.conn.execute(
            "SELECT * FROM ideator_sparks WHERE id = ?", (spark_id,),
        ).fetchone()
        return self._core.db.dict_row(row)

    def list_sparks(
        self, *, status: str | None = None, source_type: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM ideator_sparks WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        sql += " ORDER BY quality_score DESC, created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._core.db.conn.execute(sql, params).fetchall()
        return self._core.db.dict_rows(rows)

    # ── 召回权重 ──────────────────────────────

    def get_recall_weights(self) -> list[dict]:
        """返回所有召回路径的权重。"""
        rows = self._core.db.conn.execute(
            "SELECT * FROM ideator_recall_weights"
        ).fetchall()
        return self._core.db.dict_rows(rows)

    def get_recall_weight(self, source_type: str) -> dict | None:
        row = self._core.db.conn.execute(
            "SELECT * FROM ideator_recall_weights WHERE source_type = ?",
            (source_type,),
        ).fetchone()
        return self._core.db.dict_row(row)

    def update_recall_weight(
        self, *, source_type: str, weight: float,
        useful_inc: int = 0, noise_inc: int = 0,
    ) -> None:
        self._core.db.conn.execute(
            """UPDATE ideator_recall_weights
               SET weight = ?,
                   useful_count = useful_count + ?,
                   noise_count = noise_count + ?,
                   updated_at = datetime('now')
               WHERE source_type = ?""",
            (weight, useful_inc, noise_inc, source_type),
        )
        self._core.db.conn.commit()

    # ── Idea 级 embedding ──────────────────────────────

    def get_note_ideas(self, note_source: str, note_id: int) -> list[dict]:
        """获取笔记的缓存 idea 列表（含已解包的 embedding）。"""
        from paperreadagent.core.embedding import unpack_embedding
        rows = self._core.db.conn.execute(
            """SELECT id, note_source, note_id, idea_index, content, embedding
               FROM ideator_note_ideas
               WHERE note_source = ? AND note_id = ?
               ORDER BY idea_index""",
            (note_source, note_id),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["embedding"] = unpack_embedding(d.get("embedding", ""))
            results.append(d)
        return results

    def insert_note_ideas(
        self, note_source: str, note_id: int, ideas: list[dict],
    ) -> None:
        """批量写入笔记的 idea 列表。UNIQUE 约束下重复执行视为 no-op。"""
        from paperreadagent.core.embedding import pack_embedding
        for idea in ideas:
            emb_str = pack_embedding(idea.get("embedding", []))
            self._core.db.conn.execute(
                """INSERT OR IGNORE INTO ideator_note_ideas
                   (note_source, note_id, idea_index, content, embedding)
                   VALUES (?, ?, ?, ?, ?)""",
                (note_source, note_id, idea["idea_index"],
                 idea["content"], emb_str),
            )
        self._core.db.conn.commit()

    def search_similar_ideas(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        min_similarity: float = 0.3,
        exclude_note_source: str = "",
        exclude_note_id: int = 0,
    ) -> list[dict]:
        """在 idea 级别搜索语义相似的 idea，返回带 parent note 信息的结果。"""
        return self._search_ideas(
            embedding, top_k=top_k, min_similarity=min_similarity,
            exclude_note_source=exclude_note_source,
            exclude_note_id=exclude_note_id,
            contradictions=False,
        )

    def search_contradictory_ideas(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        exclude_note_source: str = "",
        exclude_note_id: int = 0,
    ) -> list[dict]:
        """在 idea 级别搜索语义矛盾（方向相反）的 idea。"""
        return self._search_ideas(
            embedding, top_k=top_k, min_similarity=None,
            exclude_note_source=exclude_note_source,
            exclude_note_id=exclude_note_id,
            contradictions=True,
        )

    def _search_ideas(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        min_similarity: float | None = 0.3,
        exclude_note_source: str = "",
        exclude_note_id: int = 0,
        contradictions: bool = False,
    ) -> list[dict]:
        """在 ideator_note_ideas 中做余弦相似度搜索，分组聚合到 parent note。"""
        from paperreadagent.core.embedding import unpack_embedding, cosine_similarity
        rows = self._core.db.conn.execute(
            """SELECT id, note_source, note_id, idea_index, content, embedding
               FROM ideator_note_ideas
               WHERE embedding != ''
               ORDER BY id""",
        ).fetchall()

        scored = []
        for r in rows:
            if exclude_note_source and exclude_note_id:
                if (r["note_source"] == exclude_note_source
                        and r["note_id"] == exclude_note_id):
                    continue
            emb = unpack_embedding(r["embedding"])
            if not emb:
                continue
            sim = cosine_similarity(embedding, emb)
            if contradictions:
                sim = -abs(sim)
            elif min_similarity is not None and sim < min_similarity:
                continue
            d = dict(r)
            d["_similarity"] = sim
            scored.append(d)

        scored.sort(key=lambda x: x["_similarity"], reverse=not contradictions)
        return scored[:top_k]

    # ── 圆桌讨论 ─────────────────────────────────────────

    def insert_roundtable(self, spark_id: int) -> int:
        cur = self._core.db.conn.execute(
            "INSERT INTO ideator_roundtables (spark_id) VALUES (?)", (spark_id,)
        )
        self._core.db.conn.commit()
        return cur.lastrowid

    _ROUNDTABLE_COLS = {"status", "round_count", "closed_at"}

    def update_roundtable(self, rt_id: int, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in self._ROUNDTABLE_COLS}
        if not allowed:
            return
        sets = [f"{k}=?" for k in allowed]
        vals = list(allowed.values()) + [rt_id]
        self._core.db.conn.execute(
            f"UPDATE ideator_roundtables SET {','.join(sets)} WHERE id=?", vals
        )
        self._core.db.conn.commit()

    def get_roundtable(self, rt_id: int) -> dict | None:
        row = self._core.db.conn.execute(
            "SELECT * FROM ideator_roundtables WHERE id=?", (rt_id,)
        ).fetchone()
        return self._core.db.dict_row(row)

    _RT_MSG_COLS = {"roundtable_id", "round_number", "sender_type", "sender_name",
                     "sender_role", "message_type", "content", "word_count",
                     "mentioned_by", "parent_id", "metadata"}

    def insert_roundtable_message(self, **fields) -> int:
        allowed = {k: v for k, v in fields.items() if k in self._RT_MSG_COLS}
        if not allowed:
            return -1
        keys = list(allowed.keys())
        placeholders = ",".join(["?"] * len(keys))
        vals = list(allowed.values())
        cur = self._core.db.conn.execute(
            f"INSERT INTO ideator_roundtable_messages ({','.join(keys)}) VALUES ({placeholders})", vals
        )
        self._core.db.conn.commit()
        return cur.lastrowid

    def get_roundtable_messages(self, rt_id: int, since_round: int = 0) -> list[dict]:
        rows = self._core.db.conn.execute(
            "SELECT * FROM ideator_roundtable_messages WHERE roundtable_id=? AND round_number>=? ORDER BY id",
            (rt_id, since_round)
        ).fetchall()
        return self._core.db.dict_rows(rows)

    def insert_snapshot(self, **fields) -> int:
        keys = list(fields.keys())
        placeholders = ",".join(["?"] * len(keys))
        vals = list(fields.values())
        cur = self._core.db.conn.execute(
            f"INSERT INTO ideator_roundtable_snapshots ({','.join(keys)}) VALUES ({placeholders})", vals
        )
        self._core.db.conn.commit()
        return cur.lastrowid

    # ── 团队记忆 ─────────────────────────────────────────

    def insert_team_memory(self, **fields) -> int:
        cur = self._core.db.conn.execute(
            """INSERT INTO ideator_team_memory
               (roundtable_id, spark_id, memory_type, content, metadata, round_number)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (fields["roundtable_id"], fields["spark_id"], fields["memory_type"],
             fields["content"], fields.get("metadata", "{}"),
             fields.get("round_number", 0)),
        )
        self._core.db.conn.commit()
        return cur.lastrowid

    def get_team_memory(self, *, spark_id: int, memory_type: str | None = None) -> list[dict]:
        if memory_type:
            rows = self._core.db.conn.execute(
                "SELECT * FROM ideator_team_memory WHERE spark_id=? AND memory_type=? ORDER BY created_at",
                (spark_id, memory_type),
            ).fetchall()
        else:
            rows = self._core.db.conn.execute(
                "SELECT * FROM ideator_team_memory WHERE spark_id=? ORDER BY memory_type, created_at",
                (spark_id,),
            ).fetchall()
        return self._core.db.dict_rows(rows)

    def get_roundtable_snapshots(self, rt_id: int, round_number: int | None = None) -> list[dict]:
        if round_number is not None:
            rows = self._core.db.conn.execute(
                "SELECT * FROM ideator_roundtable_snapshots WHERE roundtable_id=? AND round_number=?",
                (rt_id, round_number),
            ).fetchall()
        else:
            rows = self._core.db.conn.execute(
                "SELECT * FROM ideator_roundtable_snapshots WHERE roundtable_id=?",
                (rt_id,),
            ).fetchall()
        return self._core.db.dict_rows(rows)
