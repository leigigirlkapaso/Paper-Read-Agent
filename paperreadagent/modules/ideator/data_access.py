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
    """ideator 统一数据访问。对上层屏蔽 legacy/core 双句柄。

    v2: 火花 embedding 使用 LanceDB ANN 替代暴力搜索去重。
    """

    def __init__(self, core: Core):
        self._core = core
        if core.legacy_db is None:
            raise RuntimeError("Core.legacy_db 未注入，请确保 web/app.py 已桥接")
        self._legacy = core.legacy_db
        self._spark_lance_ready = False
        self._spark_lance_table = None
        self._ensure_spark_lance()

    def _ensure_spark_lance(self) -> None:
        """Initialize LanceDB table for spark embeddings."""
        try:
            import lancedb
            import pyarrow as pa
            from pathlib import Path

            base = Path(__file__).parent.parent.parent.parent
            uri = str(base / "data" / "lancedb")
            Path(uri).mkdir(parents=True, exist_ok=True)
            db = lancedb.connect(uri)

            try:
                self._spark_lance_table = db.open_table("sparks")
            except Exception:
                schema = pa.schema([
                    pa.field("id", pa.int64()),
                    pa.field("vector", pa.list_(pa.float32(), 1024)),
                ])
                self._spark_lance_table = db.create_table("sparks", schema=schema, mode="create")
                logger.info("[DataAccess] LanceDB spark index created")
                # Auto-migrate existing spark embeddings
                self._migrate_sparks_to_lance()

            self._spark_lance_ready = True
        except Exception:
            logger.warning("[DataAccess] LanceDB spark index unavailable", exc_info=True)

    def _migrate_sparks_to_lance(self) -> int:
        """One-time migration of existing spark embeddings to LanceDB."""
        if self._spark_lance_table is None:
            return 0
        try:
            from paperreadagent.core.embedding import unpack_embedding
            rows = self._core.db.conn.execute(
                "SELECT id, embedding FROM ideator_sparks WHERE embedding != '' AND embedding IS NOT NULL"
            ).fetchall()
            if not rows:
                return 0
            # Per-spark upsert: delete by ID before re-adding (atomic at spark level)
            # BUG-094: avoids total data loss on crash mid-migration
            data = []
            batch = []
            for r in rows:
                emb = unpack_embedding(r["embedding"])
                if not emb:
                    continue
                try:
                    self._spark_lance_table.delete(f"id = {r['id']}")
                except Exception:
                    pass
                batch.append({"id": r["id"], "vector": [float(x) for x in emb]})
                if len(batch) >= 500:
                    self._spark_lance_table.add(batch)
                    data.extend(batch)
                    batch = []
            if batch:
                self._spark_lance_table.add(batch)
                data.extend(batch)
            if data:
                logger.info(f"[DataAccess] Migrated {len(data)} spark embeddings to LanceDB")
            return len(data)
        except Exception:
            logger.warning("[DataAccess] Spark migration failed", exc_info=True)
            return 0

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
                        "paper_id": note.get("paper_id", 0),
                        "content": note.get("content", ""),
                        "created_at": note.get("created_at", ""),
                        "source_module": "legacy",
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
        """通过 embedding 向量查找语义相似的火花（LanceDB ANN 搜索）。"""
        if not embedding:
            return []

        # Try LanceDB ANN first
        if self._spark_lance_ready and self._spark_lance_table is not None:
            try:
                query_vec = [float(x) for x in embedding]
                results = self._spark_lance_table.search(query_vec)\
                    .limit(max(top_k, 5))\
                    .to_list()
                spark_ids = [r["id"] for r in results
                             if (1.0 - r.get("_distance", 0) ** 2 / 2.0) >= min_similarity]
                if spark_ids:
                    rows = self._core.db.conn.execute(
                        f"""SELECT id, content, quality_score, status, source_type
                           FROM ideator_sparks WHERE id IN ({','.join('?' for _ in spark_ids)})""",
                        spark_ids,
                    ).fetchall()
                    scored = []
                    for r in rows:
                        match = next((x for x in results if x["id"] == r["id"]), None)
                        sim = 1.0 - (match["_distance"] ** 2 / 2.0) if match else min_similarity
                        scored.append({
                            "id": r["id"], "content": r["content"][:500] if r["content"] else "",
                            "quality_score": r["quality_score"] or 0,
                            "status": r["status"] or "",
                            "source_type": r["source_type"] or "",
                            "similarity": round(sim, 4),
                        })
                    scored.sort(key=lambda x: x["similarity"], reverse=True)
                    return scored[:top_k]
            except Exception:
                logger.warning("[DataAccess] LanceDB spark search failed, fallback", exc_info=True)

        # Fallback: brute-force cosine similarity
        from paperreadagent.core.embedding import cosine_similarity, unpack_embedding
        existing = self.get_existing_sparks(limit=200)
        if not existing:
            return []
        scored = []
        for s in existing:
            emb = unpack_embedding(s.get("embedding", ""))
            if not emb:
                continue
            sim = cosine_similarity(embedding, emb)
            if sim >= min_similarity:
                scored.append({
                    "id": s["id"], "content": s.get("content", "")[:500],
                    "quality_score": s.get("quality_score", 0),
                    "status": s.get("status", ""), "source_type": s.get("source_type", ""),
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
        spark_id = cursor.lastrowid

        # Dual-write to LanceDB for fast ANN dedup
        emb_str = vals.get("embedding", "")
        if emb_str and self._spark_lance_ready:
            self._spark_to_lance(spark_id, emb_str)

        return spark_id

    def _spark_to_lance(self, spark_id: int, embedding) -> None:
        """Write a spark embedding to the LanceDB index."""
        if not self._spark_lance_ready or self._spark_lance_table is None:
            return
        try:
            if isinstance(embedding, str):
                from paperreadagent.core.embedding import unpack_embedding
                emb_list = unpack_embedding(embedding)
            else:
                emb_list = embedding
            if not emb_list:
                return
            # Delete old vector before adding (upsert by delete+add)
            try:
                self._spark_lance_table.delete(f"id = {spark_id}")
            except Exception:
                pass
            self._spark_lance_table.add([{
                "id": spark_id,
                "vector": [float(x) for x in emb_list],
            }])
        except Exception:
            logger.debug(f"[DataAccess] LanceDB spark write failed for id={spark_id}", exc_info=True)

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

        # Sync LanceDB if embedding changed
        if "embedding" in updates:
            emb = updates["embedding"]
            if emb:
                self._spark_to_lance(spark_id, emb)

    def get_spark(self, spark_id: int) -> dict | None:
        row = self._core.db.conn.execute(
            "SELECT * FROM ideator_sparks WHERE id = ?", (spark_id,),
        ).fetchone()
        return self._core.db.dict_row(row)

    def gather_facts_for_spark(
        self, spark_id: int, *, max_papers: int = 8,
    ) -> list[dict]:
        """Collect 7-field extractions for papers directly linked to this spark.

        Sources:
          1. ideator_sparks.source_refs (JSON array of {"type":"paper"/"core_note","id":N})
          2. ideator_cross_links (paper rows on either side of the link)
          3. core_note -> paper indirection via core.knowledge.get_note(note_id).metadata.paper_id

        Returns: list of dicts {paper_id, title, arxiv_id, relevance_score, extraction}
        sorted by relevance_score DESC, capped at max_papers. Papers without
        valid extraction_json are silently dropped.
        Returns [] if spark has no linked papers or no papers have extractions.
        """
        # Per-paper-id score; later writes from cross_links can override defaults
        paper_scores: dict[int, float] = {}

        def _add_paper(pid: int, score: float) -> None:
            if pid is None or pid == 0:
                return
            cur = paper_scores.get(pid)
            if cur is None or score > cur:
                paper_scores[pid] = score

        def _resolve_note_to_paper(nid: int) -> int | None:
            """Resolve a core_note id → paper_id (via metadata.paper_id).
            Returns None if note missing or metadata lacks paper_id."""
            try:
                note = self._core.knowledge.get_note(nid)
            except Exception:
                logger.debug("knowledge.get_note failed for note_id=%s", nid, exc_info=True)
                return None
            if not note:
                return None
            meta = note.get("metadata") or {}
            if not isinstance(meta, dict):
                return None
            pid = meta.get("paper_id")
            return pid if isinstance(pid, int) and pid > 0 else None

        # Path A: spark.source_refs
        spark_row = self._core.db.conn.execute(
            "SELECT id, source_refs FROM ideator_sparks WHERE id = ?", (spark_id,),
        ).fetchone()
        if spark_row is None:
            return []
        spark = self._core.db.dict_row(spark_row)
        raw_refs = spark.get("source_refs")
        if raw_refs:
            try:
                refs = json.loads(raw_refs) if isinstance(raw_refs, str) else raw_refs
            except (TypeError, ValueError):
                refs = []
            if isinstance(refs, list):
                for ref in refs:
                    if not isinstance(ref, dict):
                        continue
                    rtype = ref.get("type")
                    rid = ref.get("id")
                    if rtype == "paper" and isinstance(rid, int):
                        _add_paper(rid, 0.5)
                    elif rtype == "core_note" and isinstance(rid, int):
                        pid = _resolve_note_to_paper(rid)
                        if pid:
                            _add_paper(pid, 0.5)

        # Path B: cross_links
        link_rows = self._core.db.conn.execute(
            "SELECT source_a_type, source_a_id, source_b_type, source_b_id, "
            "relevance_score FROM ideator_cross_links WHERE spark_id = ?",
            (spark_id,),
        ).fetchall()
        for raw in link_rows:
            row = self._core.db.dict_row(raw) if not isinstance(raw, dict) else raw
            score = float(row.get("relevance_score") or 0.5)
            for side in ("a", "b"):
                t = row.get(f"source_{side}_type")
                i = row.get(f"source_{side}_id")
                if not isinstance(i, int):
                    continue
                if t == "paper":
                    _add_paper(i, score)
                elif t == "core_note":
                    pid = _resolve_note_to_paper(i)
                    if pid:
                        _add_paper(pid, score)

        if not paper_scores:
            return []

        # Load papers + filter to those with valid extraction
        results: list[dict] = []
        for pid, score in paper_scores.items():
            paper = self._legacy.get_paper(pid)
            if not paper:
                continue
            raw_ext = paper.get("extraction_json")
            if not raw_ext:
                continue
            try:
                ext = json.loads(raw_ext)
            except Exception:
                logger.debug("extraction_json parse failed for paper_id=%s", pid)
                continue
            if not isinstance(ext, dict):
                continue
            results.append({
                "paper_id": pid,
                "title": paper.get("title", ""),
                "arxiv_id": paper.get("arxiv_id"),
                "relevance_score": score,
                "extraction": ext,
            })

        # Sort DESC by score (stable: same score -> ascending paper_id)
        results.sort(key=lambda r: (-r["relevance_score"], r["paper_id"]))
        return results[:max_papers]

    # ── Roundtable Outlines (Secretary) ────────────────────────

    def insert_outline(
        self, *, rt_id: int, round_number: int, outline_markdown: str,
        facts_block: str = "", model_name: str = "",
        token_usage: dict | None = None,
    ) -> int:
        """Insert a new outline row. Returns the new row id.

        Each call inserts a new row — never updates an existing one.
        get_latest_outline(rt_id) returns the most recent by round_number.
        """
        import json as _json
        token_usage_json = _json.dumps(token_usage or {}, ensure_ascii=False)
        cur = self._core.db.conn.execute(
            """INSERT INTO ideator_roundtable_outlines
               (rt_id, round_number, outline_markdown, facts_block, model_name, token_usage)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rt_id, round_number, outline_markdown, facts_block, model_name, token_usage_json),
        )
        self._core.db.conn.commit()
        return cur.lastrowid

    def get_latest_outline(self, rt_id: int) -> str | None:
        """Get the outline_markdown of the most recent row for this rt_id.
        Returns None if no outline exists for this rt_id."""
        row = self._core.db.conn.execute(
            """SELECT outline_markdown FROM ideator_roundtable_outlines
               WHERE rt_id = ? ORDER BY round_number DESC, id DESC LIMIT 1""",
            (rt_id,),
        ).fetchone()
        return row["outline_markdown"] if row else None

    def get_outline_history(self, rt_id: int) -> list[dict]:
        """Get full version history (used by GET /outline route for round_number,
        and reserved for future time-travel UI)."""
        rows = self._core.db.conn.execute(
            """SELECT id, round_number, outline_markdown, created_at
               FROM ideator_roundtable_outlines WHERE rt_id = ?
               ORDER BY round_number ASC, id ASC""",
            (rt_id,),
        ).fetchall()
        return [dict(r) for r in rows]

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

    # ── 项目书 (project brief) ───────────────────────────

    def insert_project_brief(self, spark_id: int) -> int:
        cur = self._core.db.conn.execute(
            "INSERT INTO ideator_project_briefs (spark_id) VALUES (?)", (spark_id,)
        )
        self._core.db.conn.commit()
        return cur.lastrowid

    _BRIEF_COLS = {"status", "brief_json", "context_sources",
                   "model_name", "token_usage", "error"}

    def update_project_brief(self, brief_id: int, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in self._BRIEF_COLS}
        if not allowed:
            return
        sets = [f"{k}=?" for k in allowed]
        vals = list(allowed.values()) + [brief_id]
        self._core.db.conn.execute(
            f"UPDATE ideator_project_briefs SET {','.join(sets)} WHERE id=?", vals
        )
        self._core.db.conn.commit()

    def get_project_brief(self, brief_id: int) -> dict | None:
        row = self._core.db.conn.execute(
            "SELECT * FROM ideator_project_briefs WHERE id=?", (brief_id,)
        ).fetchone()
        return self._core.db.dict_row(row)

    def list_project_briefs(self, spark_id: int) -> list[dict]:
        rows = self._core.db.conn.execute(
            "SELECT * FROM ideator_project_briefs WHERE spark_id=? ORDER BY id DESC",
            (spark_id,)
        ).fetchall()
        return self._core.db.dict_rows(rows)

    def gather_brief_context(self, spark_id: int) -> dict:
        """Collect everything the project-brief LLM call needs for one spark.

        Returns {spark_content, depth_content, cross_links, team_memory}.
        Raises ValueError if the spark does not exist.
        """
        spark = self.get_spark(spark_id)
        if spark is None:
            raise ValueError(f"spark {spark_id} not found")

        links = self._core.db.conn.execute(
            "SELECT source_a_type, source_a_id, source_b_type, source_b_id, "
            "link_type, reasoning, relevance_score "
            "FROM ideator_cross_links WHERE spark_id=? ORDER BY relevance_score DESC",
            (spark_id,)
        ).fetchall()
        cross_links = self._core.db.dict_rows(links)

        team_memory: list[dict] = []
        rt_id = spark.get("roundtable_id")
        if rt_id:
            mem_rows = self._core.db.conn.execute(
                "SELECT memory_type, content, round_number "
                "FROM ideator_team_memory WHERE spark_id=? ORDER BY id",
                (spark_id,)
            ).fetchall()
            team_memory = self._core.db.dict_rows(mem_rows)

        return {
            "spark_content": spark.get("content", ""),
            "depth_content": spark.get("depth_content", ""),
            "cross_links": cross_links,
            "team_memory": team_memory,
        }
