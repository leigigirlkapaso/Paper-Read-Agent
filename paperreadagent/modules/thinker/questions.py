"""
modules/thinker/questions.py
QuestionGenerator — 不活跃检测 + LLM 生成主动提问。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from paperreadagent.core import Core
from .constants import ROLE_USER, ROLE_AI, QUESTION_TYPE_INACTIVITY

logger = logging.getLogger(__name__)

INACTIVITY_CHECK_LIMIT = 3


class QuestionGenerator:
    """管理主动提问的生成、分发与关闭。"""

    def __init__(self, core: Core):
        self.core = core

    async def check_inactivity(self) -> list[dict]:
        """
        调度器定时调用。单 JOIN 查询避免 N+1 问题。
        """
        cfg = self.core.module_config("thinker")
        timeout_minutes = cfg.get("inactivity_timeout_minutes", 10)
        max_pending = cfg.get("max_pending_questions", 3)

        now = datetime.now(timezone.utc)
        cutoff = now.isoformat()

        active_convs = self.core.db.conn.execute(
            """SELECT c.id, c.snooze_until,
                      (SELECT COUNT(*) FROM thinker_pending_questions q
                       WHERE q.conversation_id = c.id AND q.delivered = 0 AND q.dismissed = 0) as pending_cnt,
                      (SELECT MAX(m.created_at) FROM thinker_messages m
                       WHERE m.conversation_id = c.id) as last_msg_time
               FROM thinker_conversations c
               WHERE c.status = 'active'
                 AND (c.snooze_until IS NULL OR c.snooze_until < ?)
               LIMIT ?""",
            (cutoff, INACTIVITY_CHECK_LIMIT),
        ).fetchall()

        generated: list[dict] = []
        for conv in active_convs:
            conv_id = conv["id"]
            pending_cnt = conv["pending_cnt"] or 0

            if pending_cnt >= max_pending:
                continue

            last_msg_time = conv["last_msg_time"]
            if last_msg_time:
                try:
                    last_time = datetime.fromisoformat(last_msg_time).replace(tzinfo=timezone.utc)
                    elapsed = (now - last_time).total_seconds()
                    if elapsed < timeout_minutes * 60:
                        continue
                except (ValueError, TypeError):
                    logger.warning("[QuestionGenerator] last_msg_time 日期解析失败: %s", last_msg_time, exc_info=True)

            question = await self._generate_question(conv_id)
            if not question:
                continue

            cursor = self.core.db.conn.execute(
                """INSERT INTO thinker_pending_questions
                   (conversation_id, question, question_type)
                   VALUES (?, ?, ?)""",
                (conv_id, question, QUESTION_TYPE_INACTIVITY),
            )
            self.core.db.conn.commit()

            generated.append({
                "question_id": cursor.lastrowid,
                "conversation_id": conv_id,
                "question": question,
            })

        return generated

    async def _generate_question(self, conversation_id: int) -> str:
        """用 LLM 基于对话上下文生成开放性问题。"""
        rows = self.core.db.conn.execute(
            """SELECT role, content FROM thinker_messages
               WHERE conversation_id = ? AND role != 'system'
               ORDER BY created_at DESC LIMIT 8""",
            (conversation_id,),
        ).fetchall()

        recent = []
        for r in reversed(rows):
            speaker = ROLE_USER if r["role"] == "user" else ROLE_AI
            recent.append({"speaker": speaker, "content": r["content"]})

        prompt = self.core.llm.load_prompt(
            "thinker", "question", recent_messages=recent,
        )

        text, _usage = await self.core.llm.achat(
            user_prompt=prompt,
            module="thinker",
            purpose="question_gen",
        )

        return text.strip().strip('"').strip("'")

    async def get_pending_question(self, conversation_id: int) -> dict | None:
        """取最早一条未投递的问题，标记为已投递。"""
        row = self.core.db.conn.execute(
            """SELECT id, question, question_type, generated_at FROM thinker_pending_questions
               WHERE conversation_id = ? AND delivered = 0 AND dismissed = 0
               ORDER BY generated_at ASC LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None

        self.core.db.conn.execute(
            "UPDATE thinker_pending_questions SET delivered = 1, delivered_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row["id"]),
        )
        self.core.db.conn.commit()
        return dict(row)

    async def dismiss_question(self, question_id: int) -> None:
        self.core.db.conn.execute(
            "UPDATE thinker_pending_questions SET dismissed = 1 WHERE id = ?",
            (question_id,),
        )
        self.core.db.conn.commit()
