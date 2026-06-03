"""
modules/ideator/spark_store.py
SparkStore — 火花 CRUD + embedding 去重 + 质量衰减。
"""

from __future__ import annotations

import json
import logging

from paperreadagent.core.embedding import cosine_similarity, unpack_embedding
from .data_access import DataAccess
from .constants import (
    SPARK_SEED, DEDUP_MERGE_THRESHOLD, DEDUP_FLAG_THRESHOLD,
    QUALITY_USEFUL_DELTA, QUALITY_BAD_DELTA,
    QUALITY_GC_THRESHOLD, QUALITY_GC_AGE_DAYS,
)

logger = logging.getLogger(__name__)


class SparkStore:
    """火花仓库管理。嵌入去重、质量衰减、GC。"""

    def __init__(self, data: DataAccess):
        self.data = data

    def dedup(self, spark_content: str, embedding: list[float]) -> tuple[str, int | None]:
        """返回 (action, merge_target_id)。embedding 为空时跳过向量去重。"""
        if not embedding:
            return ("insert", None)
        existing = self.data.get_existing_sparks()
        if not existing:
            return ("insert", None)

        best_score = 0.0
        best_match = None
        for existing_spark in existing:
            emb_raw = existing_spark.get("embedding", "")
            if not emb_raw:
                continue
            existing_emb = unpack_embedding(emb_raw)
            if not existing_emb:
                continue
            score = cosine_similarity(embedding, existing_emb)
            if score > best_score:
                best_score = score
                best_match = existing_spark

        if best_score >= DEDUP_MERGE_THRESHOLD:
            return ("merge", best_match["id"])
        elif best_score >= DEDUP_FLAG_THRESHOLD:
            return ("insert_flagged", None)
        else:
            return ("insert", None)

    def merge_spark(self, existing_id: int, new_source_refs: list) -> int:
        existing = self.data.get_spark(existing_id)
        if not existing:
            return existing_id

        try:
            old_refs = json.loads(existing.get("source_refs", "[]"))
        except (json.JSONDecodeError, TypeError):
            old_refs = []
        existing_ids = {json.dumps(r, sort_keys=True) for r in old_refs}
        for ref in new_source_refs:
            key = json.dumps(ref, sort_keys=True)
            if key not in existing_ids:
                old_refs.append(ref)

        new_quality = min(existing.get("quality_score", 0.5) + 0.05, 1.0)
        self.data.update_spark(
            existing_id,
            source_refs=old_refs,
            quality_score=new_quality,
        )
        return existing_id

    def save_spark(
        self, content: str, source_type: str, source_refs: list,
        embedding: list[float], quality_score: float,
        core_llm,
        run_id: str | None = None,
        generator_score: float = 0.0,
        metadata: dict | None = None,
        depth_content: str = "",
    ) -> int | None:
        from paperreadagent.core.embedding import pack_embedding

        action, merge_id = self.dedup(content, embedding)
        emb_str = pack_embedding(embedding)

        if action == "merge" and merge_id is not None:
            return self.merge_spark(merge_id, source_refs)

        meta = dict(metadata or {})
        if action == "insert_flagged":
            meta["maybe_duplicate"] = True

        spark_id = self.data.insert_spark(
            content=content,
            status=SPARK_SEED,
            source_type=source_type,
            source_refs=source_refs,
            embedding=emb_str,
            quality_score=quality_score,
            metadata=json.dumps(meta, ensure_ascii=False),
            run_id=run_id or "",
            generator_score=generator_score,
            depth_content=depth_content,
        )

        try:
            self.data._core.knowledge.insert_note(
                source_module="ideator",
                content=f"💡 {content}",
                source_ref=f"spark_{spark_id}",
                content_type="spark",
                tags=["spark", source_type],
                embedding=embedding,
                metadata={"spark_id": spark_id, "source_type": source_type},
            )
        except Exception:
            logger.warning("[SparkStore] core_notes 同步失败", exc_info=True)

        return spark_id

    def apply_feedback(self, spark_id: int, feedback: str) -> None:
        spark = self.data.get_spark(spark_id)
        if not spark:
            return
        current = spark.get("quality_score", 0.5)
        if feedback == "useful":
            new_score = min(current + QUALITY_USEFUL_DELTA, 1.0)
        else:
            new_score = max(current + QUALITY_BAD_DELTA, 0.0)
        self.data.update_spark(spark_id, quality_score=new_score, user_feedback=feedback)

    def deepen_spark(self, spark_id: int, depth_content: str) -> None:
        from datetime import datetime
        self.data.update_spark(
            spark_id,
            status="deep_done",
            depth_content=depth_content,
            deepened_at=datetime.now().isoformat(),
        )

    def update_review_result(self, spark_id: int, *, final_score: float,
                             review_status: str, verdict: str) -> None:
        """记录审查结果，更新火花评分、状态和审查计数。"""
        spark = self.data.get_spark(spark_id)
        if not spark:
            return
        current_count = spark.get("review_count", 0) or 0
        self.data.update_spark(spark_id,
            final_score=final_score,
            review_status=review_status,
            review_count=current_count + 1,
            verdict=verdict,
        )

    def gc_low_quality(self) -> int:
        self.data._core.db.conn.execute(
            f"""DELETE FROM ideator_sparks
               WHERE quality_score < ?
                 AND user_feedback IS NOT NULL
                 AND created_at < datetime('now', '-{QUALITY_GC_AGE_DAYS} days')""",
            (QUALITY_GC_THRESHOLD,),
        )
        self.data._core.db.conn.commit()
        return self.data._core.db.conn.execute("SELECT changes()").fetchone()[0]
