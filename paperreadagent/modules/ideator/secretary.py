"""secretary.py — 圆桌秘书 agent.

每轮讨论结束后，秘书 agent 整理本轮内容到 7 节项目大纲：
1. 研究问题  2. 核心假设  3. 方法设计  4. 实验计划
5. 风险清单  6. 行动项  7. 开放问题/分歧

输入：上一轮大纲 + 本轮 agent reply + facts_block + spark_content
输出：完整重写的大纲 Markdown
"""

from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class SecretaryService:
    """Roundtable secretary — incremental outline maintenance.

    Stateless service: every update() call fetches the previous outline
    from DB, calls LLM to revise it based on the current round's messages,
    writes the new version back, and publishes an SSE event.
    """

    def __init__(self, *, llm, data_access, stream_hub):
        self._llm = llm
        self._data = data_access
        self._stream_hub = stream_hub

    async def update(self, rt_id: int, *, team) -> str | None:
        """Update the outline for this rt_id after a round completes.

        Returns the new outline markdown, or None on failure.
        Best-effort: any failure logs and returns None — roundtable unaffected.
        Publishes 'outline_update' event to hub on success.
        """
        try:
            current_outline = self._data.get_latest_outline(rt_id) or ""
            current_round_msgs = self._get_current_round_msgs(team)
            if not current_round_msgs:
                logger.debug("[Secretary] no model replies this round; skip rt=%s", rt_id)
                return None
            facts_block = getattr(team, "facts_block", "") or ""
            spark_content = getattr(team, "spark_content", "") or ""
            round_number = team.round_number

            new_outline = await self._call_llm(
                current_outline=current_outline,
                current_round_msgs=current_round_msgs,
                facts_block=facts_block,
                spark_content=spark_content,
                round_number=round_number,
            )
            if not new_outline:
                logger.warning("[Secretary] LLM returned empty outline for rt=%s", rt_id)
                return None

            model_name = ""
            if hasattr(self._llm, "model_for"):
                try:
                    cand = self._llm.model_for("secretary")
                    model_name = cand if isinstance(cand, str) else ""
                except Exception:
                    pass

            self._data.insert_outline(
                rt_id=rt_id,
                round_number=round_number,
                outline_markdown=new_outline,
                facts_block=facts_block,
                model_name=model_name,
            )

            # Publish is best-effort: a failure here doesn't undo the DB write.
            # User's page-refresh path (GET /outline) catches up via DB anyway.
            try:
                await self._stream_hub.publish(rt_id, {
                    "type": "outline_update",
                    "rt_id": rt_id,
                    "round_number": round_number,
                    "outline": new_outline,
                })
            except Exception:
                logger.debug(
                    "[Secretary] hub publish failed for rt=%s; DB row already saved",
                    rt_id, exc_info=True,
                )

            return new_outline

        except Exception:
            logger.warning("[Secretary] update failed for rt=%s", rt_id, exc_info=True)
            return None

    def _get_current_round_msgs(self, team) -> list[dict]:
        """Pull only model 'answer' messages from this round.
        Skip user question, interjections, system fallbacks, and prior rounds."""
        round_num = team.round_number
        out = []
        for m in team.messages:
            if m.get("round_number") != round_num:
                continue
            if m.get("sender_type") != "model":
                continue
            if m.get("message_type") != "answer":
                continue
            out.append({
                "seat": m.get("sender_name", "?"),
                "role": m.get("sender_role", "?"),
                "content": m.get("content", ""),
            })
        return out

    async def _call_llm(
        self, *, current_outline, current_round_msgs, facts_block,
        spark_content, round_number,
    ) -> str:
        system_prompt = self._llm.load_prompt("ideator", "secretary_system")
        user_prompt = self._llm.load_prompt(
            "ideator", "secretary_user",
            current_outline=current_outline,
            current_round_msgs=current_round_msgs,
            facts_block=facts_block,
            spark_content=spark_content,
            round_number=round_number,
        )
        raw = await self._llm.chat(
            model_role="secretary",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=8192,
        )
        return self._clean_markdown(raw)

    @staticmethod
    def _clean_markdown(raw: str) -> str:
        """Strip <think> blocks (paired or orphan) and code fences.

        Defense-in-depth: prompt forbids these but LLM may still emit them
        (especially on truncation). We aggressively strip both forms.
        """
        if not raw:
            return ""
        # 1. Strip paired <think>...</think> blocks
        raw = _THINK_RE.sub("", raw).strip()
        # 2. Strip orphan <think> opener up to first markdown header (or EOF)
        #    This catches truncated reasoning blocks where </think> is missing
        raw = re.sub(
            r"^<think>.*?(?=\n#|\Z)", "", raw, flags=re.DOTALL,
        ).strip()
        # 3. Strip code fences (handle leading + scan for last ``` in content)
        if raw.startswith("```"):
            lines = raw.split("\n")
            # Drop opening fence line
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            # Find LAST closing fence (not just the final line) and truncate
            # past it — handles "...\n```\ntrailing chatter" case.
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip() == "```":
                    lines = lines[:i]
                    break
            raw = "\n".join(lines)
        return raw.strip()
