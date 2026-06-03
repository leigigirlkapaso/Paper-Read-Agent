"""
modules/thinker/resolutions.py
ResolutionTracker — 承诺追踪。检测未完成承诺、追问执行情况。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from paperreadagent.core import Core
from .constants import RESOLUTION_PENDING, QUESTION_TYPE_RESOLUTION_FOLLOWUP

logger = logging.getLogger(__name__)


class ResolutionTracker:
    """管理用户承诺的生命周期：提取 → 追问 → 完成/放弃。"""

    def __init__(self, core: Core):
        self.core = core

    async def check_daily_resolutions(self) -> list[dict]:
        """调度器定时调用（每天一次）。检查 pending 和 in_progress 的承诺。"""
        now = datetime.now(timezone.utc).isoformat()

        rows = self.core.db.conn.execute(
            """SELECT r.*, c.id as conv_id
               FROM thinker_resolutions r
               JOIN thinker_conversations c ON r.conversation_id = c.id
               WHERE r.status IN ('pending','in_progress')
                 AND c.status = 'active'
               ORDER BY r.created_at ASC
               LIMIT 100""",
        ).fetchall()

        generated: list[dict] = []
        for row in rows:
            r = dict(row)
            asked_at = r.get("asked_at")
            should_ask = True

            if asked_at:
                try:
                    last_asked = datetime.fromisoformat(asked_at)
                    elapsed = (datetime.now(timezone.utc) - last_asked).total_seconds()
                    if elapsed < 86400:
                        should_ask = False
                except (ValueError, TypeError):
                    logger.warning("[ResolutionTracker] asked_at 日期解析失败: %s", asked_at, exc_info=True)

            if not should_ask:
                continue

            status_label = "进行中" if r["status"] == "in_progress" else "未开始"
            question = f"嘿，之前你许了个承诺——「{r['content']}」（{status_label}），现在是什么情况？"

            cursor = self.core.db.conn.execute(
                """INSERT INTO thinker_pending_questions
                   (conversation_id, question, question_type)
                   VALUES (?, ?, ?)""",
                (r["conv_id"], question, QUESTION_TYPE_RESOLUTION_FOLLOWUP),
            )
            self.core.db.conn.commit()

            self.core.db.conn.execute(
                """UPDATE thinker_resolutions
                   SET asked_at = ?, asked_count = asked_count + 1
                   WHERE id = ?""",
                (now, r["id"]),
            )
            self.core.db.conn.commit()

            generated.append({
                "question_id": cursor.lastrowid,
                "conversation_id": r["conv_id"],
                "resolution_id": r["id"],
                "question": question,
            })

        return generated

    async def mark_fulfilled(self, resolution_id: int) -> None:
        self.core.db.conn.execute(
            "UPDATE thinker_resolutions SET status = 'fulfilled' WHERE id = ?",
            (resolution_id,),
        )
        self.core.db.conn.commit()

    async def mark_abandoned(self, resolution_id: int, reflection: str = "") -> None:
        self.core.db.conn.execute(
            "UPDATE thinker_resolutions SET status = 'abandoned', reflection = ? WHERE id = ?",
            (reflection, resolution_id),
        )
        self.core.db.conn.commit()

    async def mark_in_progress(self, resolution_id: int) -> None:
        self.core.db.conn.execute(
            "UPDATE thinker_resolutions SET status = 'in_progress' WHERE id = ?",
            (resolution_id,),
        )
        self.core.db.conn.commit()

    async def mark_done(self, resolution_id: int) -> None:
        self.core.db.conn.execute(
            "UPDATE thinker_resolutions SET status = 'done' WHERE id = ?",
            (resolution_id,),
        )
        self.core.db.conn.commit()

    async def mark_cancelled(self, resolution_id: int, reflection: str = "") -> None:
        self.core.db.conn.execute(
            "UPDATE thinker_resolutions SET status = 'cancelled', reflection = ? WHERE id = ?",
            (reflection, resolution_id),
        )
        self.core.db.conn.commit()

    async def get_pending(self) -> list[dict]:
        """获取所有未完成的承诺。"""
        rows = self.core.db.conn.execute(
            """SELECT * FROM thinker_resolutions
               WHERE status IN ('pending','in_progress')
               ORDER BY created_at DESC"""
        ).fetchall()
        return self.core.db.dict_rows(rows)
