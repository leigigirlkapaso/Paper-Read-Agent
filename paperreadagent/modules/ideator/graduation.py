"""graduation.py — Hot/Warm/Cold 3-layer context lifecycle management."""

from __future__ import annotations
import json
import logging

logger = logging.getLogger(__name__)

HOT_MAX = 300_000    # tokens
WARM_MAX = 200_000   # tokens

ROLES = ["gen", "rev1", "rev2", "rev3", "arb1", "arb2"]


class ContextLayer:
    def __init__(self, name: str, max_tokens: int):
        self.name = name
        self.max_tokens = max_tokens
        self.current_tokens = 0

    def usage_pct(self, tokens: int | None = None) -> float:
        t = tokens if tokens is not None else self.current_tokens
        if self.max_tokens == 0:
            return 0.0
        return (t / self.max_tokens) * 100.0

    @property
    def pct(self) -> float:
        return self.usage_pct()


class GraduationManager:
    """Manages hot→warm→cold 3-layer context lifecycle."""

    def __init__(self, db_conn, team_memory):
        self._conn = db_conn
        self._memory = team_memory
        self.layers = {
            "hot": ContextLayer("hot", HOT_MAX),
            "warm": ContextLayer("warm", WARM_MAX),
        }

    def update_layer(self, name: str, tokens: int) -> None:
        if name in self.layers:
            self.layers[name].current_tokens = tokens

    def needs_graduation(self) -> bool:
        return self.layers["hot"].pct >= 50.0

    def needs_hard_compression(self) -> bool:
        total = self.layers["hot"].current_tokens + self.layers["warm"].current_tokens
        return total > (HOT_MAX + WARM_MAX) * 0.85

    def recommend_quota(self) -> dict[str, float]:
        hot_pct = self.layers["hot"].pct
        warm_pct = self.layers["warm"].pct

        if hot_pct > 85 or warm_pct > 85:
            factor = 0.5
        elif hot_pct > 60 or warm_pct > 60:
            factor = 0.7
        elif hot_pct < 30 and warm_pct < 30:
            factor = 1.5
        else:
            factor = 1.0

        return {r: round(factor, 2) for r in ROLES}

    def store_cold_snapshot(self, *, roundtable_id: int, round_number: int,
                            content: str, metadata: dict | None = None) -> int:
        cur = self._conn.execute(
            """INSERT INTO ideator_roundtable_snapshots
               (roundtable_id, message_id, model_name, model_role, round_number,
                prompt_sent, raw_response, tokens_input, tokens_output, tokens_total,
                token_pct_used, compression_triggered, compression_summary, exit_reason)
               VALUES (?, NULL, 'system', 'system', ?, 'graduation_snapshot', ?, 0, 0, 0, 0.0, 0, '', '')""",
            (roundtable_id, round_number,
             json.dumps({"content": content, "metadata": metadata or {}}, ensure_ascii=False)),
        )
        self._conn.commit()
        return cur.lastrowid

    def fetch_cold_snapshot(self, roundtable_id: int, round_number: int) -> dict | None:
        row = self._conn.execute(
            """SELECT * FROM ideator_roundtable_snapshots
               WHERE roundtable_id=? AND round_number=? AND model_name='system'
               ORDER BY id DESC LIMIT 1""",
            (roundtable_id, round_number),
        ).fetchone()
        return dict(row) if row else None

    def report(self) -> str:
        hot = self.layers["hot"]
        warm = self.layers["warm"]
        return (
            f"上下文水位报告\n"
            f"🔥 热层: {hot.pct:.1f}% ({hot.current_tokens} / {HOT_MAX} tokens)\n"
            f"🌤 温层: {warm.pct:.1f}% ({warm.current_tokens} / {WARM_MAX} tokens)\n"
            f"⚠ 需要毕业: {'是' if self.needs_graduation() else '否'}\n"
            f"⛔ 需要硬压缩: {'是' if self.needs_hard_compression() else '否'}"
        )
