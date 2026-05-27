"""team_memory.py — 9-type structured team memory CRUD for Agent Team."""

from __future__ import annotations

MEMORY_TYPES = frozenset({
    "consensus", "disagreement", "decision", "spark_evolution",
    "evidence", "user_feedback", "open_question", "assumption", "watermark",
})

MEMORY_TYPE_LABELS = {
    "consensus": "共识",
    "disagreement": "分歧",
    "decision": "决策",
    "spark_evolution": "火花演化",
    "evidence": "证据",
    "user_feedback": "用户反馈",
    "open_question": "开放问题",
    "assumption": "假设",
    "watermark": "水位",
}


class TeamMemory:
    """Structured team memory — survives across compression cycles."""

    def __init__(self, db_conn):
        self._conn = db_conn

    def write(self, *, roundtable_id: int, spark_id: int, memory_type: str,
              content: str, round_number: int = 0, metadata: dict | None = None) -> int:
        self._validate_type(memory_type)
        import json
        cur = self._conn.execute(
            """INSERT INTO ideator_team_memory
               (roundtable_id, spark_id, memory_type, content, metadata, round_number)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (roundtable_id, spark_id, memory_type, content,
             json.dumps(metadata or {}, ensure_ascii=False), round_number),
        )
        self._conn.commit()
        return cur.lastrowid

    def read(self, *, spark_id: int, memory_type: str | None = None) -> list[dict]:
        if memory_type:
            self._validate_type(memory_type)
            rows = self._conn.execute(
                "SELECT * FROM ideator_team_memory WHERE spark_id=? AND memory_type=? ORDER BY created_at",
                (spark_id, memory_type),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM ideator_team_memory WHERE spark_id=? ORDER BY memory_type, created_at",
                (spark_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def read_all_types(self, spark_id: int) -> dict[str, list[dict]]:
        result = {}
        for mtype in MEMORY_TYPES:
            result[mtype] = self.read(spark_id=spark_id, memory_type=mtype)
        return result

    def format_for_context(self, spark_id: int) -> str:
        """Format all memories for injection into agent context."""
        all_memories = self.read_all_types(spark_id)
        sections = []
        for mtype in MEMORY_TYPES:
            items = all_memories.get(mtype, [])
            if not items:
                continue
            label = MEMORY_TYPE_LABELS.get(mtype, mtype)
            lines = [f"## {label}"]
            for item in items[-10:]:
                lines.append(f"- {item['content']}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections) if sections else "暂无团队记忆"

    def _validate_type(self, memory_type: str) -> None:
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"Invalid memory_type: {memory_type}. Must be one of {sorted(MEMORY_TYPES)}")
