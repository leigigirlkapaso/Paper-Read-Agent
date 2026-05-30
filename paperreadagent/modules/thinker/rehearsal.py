"""
modules/thinker/rehearsal.py
RehearsalEngine — academic presentation rehearsal lifecycle management.

Four phases: preparing → presenting → qa → summarizing → completed
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class RehearsalEngine:
    """Manages the full lifecycle of an academic presentation rehearsal."""

    def __init__(self, core):
        self._core = core
        self._llm = core.llm
        self._db = core.db

    # ── CRUD ───────────────────────────────────────────────────

    async def create(
        self,
        *,
        title: str,
        question_list_source: str,
        question_list_content: str,
    ) -> int:
        """Create a new rehearsal session. Returns the new row ID."""
        conn = self._db.conn
        cur = conn.execute(
            """INSERT INTO thinker_rehearsals
               (title, question_list_source, question_list_content)
               VALUES (?, ?, ?)""",
            (title, question_list_source, question_list_content),
        )
        conn.commit()
        rid = cur.lastrowid
        logger.info(f"[Rehearsal] Created rehearsal #{rid}: {title}")
        return rid

    async def get_rehearsal(self, rehearsal_id: int) -> dict | None:
        """Get rehearsal details, parsing JSON fields into Python objects."""
        row = self._db.conn.execute(
            "SELECT * FROM thinker_rehearsals WHERE id = ?", (rehearsal_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        for field in ("summary_grammar_corrections", "summary_suggestions"):
            try:
                result[field] = json.loads(result[field])
            except (json.JSONDecodeError, TypeError):
                result[field] = []
        return result

    async def list_rehearsals(self, q: str = "") -> list[dict]:
        """List rehearsal history, with optional full-text search."""
        sql = "SELECT id, title, status, created_at, summary_briefing FROM thinker_rehearsals WHERE 1=1"
        params: list = []
        if q:
            sql += " AND (title LIKE ? OR summary_briefing LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        sql += " ORDER BY created_at DESC LIMIT 50"
        rows = self._db.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    async def delete_rehearsal(self, rehearsal_id: int) -> None:
        """Delete a rehearsal record."""
        self._db.conn.execute(
            "DELETE FROM thinker_rehearsals WHERE id = ?", (rehearsal_id,)
        )
        self._db.conn.commit()
        logger.info(f"[Rehearsal] Deleted rehearsal #{rehearsal_id}")

    # ── Status transitions ─────────────────────────────────────

    async def update_status(self, rehearsal_id: int, status: str) -> None:
        """Update rehearsal phase."""
        valid = {"preparing", "presenting", "qa", "summarizing", "completed"}
        if status not in valid:
            raise ValueError(f"Invalid status: {status}, valid values: {valid}")
        self._db.conn.execute(
            """UPDATE thinker_rehearsals
               SET status = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (status, rehearsal_id),
        )
        self._db.conn.commit()

    # ── Transcript accumulation ─────────────────────────────────

    async def append_presentation_transcript(
        self, rehearsal_id: int, text: str
    ) -> None:
        """Append text to the presentation transcript (server-side concatenation)."""
        self._db.conn.execute(
            """UPDATE thinker_rehearsals
               SET presentation_transcript = presentation_transcript || ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (text, rehearsal_id),
        )
        self._db.conn.commit()

    async def append_qa_turn(
        self, rehearsal_id: int, question: str, answer: str
    ) -> None:
        """Append one Q&A turn to the Q&A transcript."""
        entry = f"\n\n[Q · 🤖] {question}\n[A · 🎤] {answer}\n"
        self._db.conn.execute(
            """UPDATE thinker_rehearsals
               SET qa_transcript = qa_transcript || ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (entry, rehearsal_id),
        )
        self._db.conn.commit()

    # ── Summary storage ────────────────────────────────────────

    async def save_summary(
        self,
        rehearsal_id: int,
        *,
        briefing: str,
        grammar_corrections: list[dict],
        suggestions: list[dict],
    ) -> None:
        """Save the LLM-generated three-part summary and mark as completed."""
        self._db.conn.execute(
            """UPDATE thinker_rehearsals
               SET summary_briefing = ?,
                   summary_grammar_corrections = ?,
                   summary_suggestions = ?,
                   status = 'completed',
                   updated_at = datetime('now')
               WHERE id = ?""",
            (
                briefing,
                json.dumps(grammar_corrections, ensure_ascii=False),
                json.dumps(suggestions, ensure_ascii=False),
                rehearsal_id,
            ),
        )
        self._db.conn.commit()
        logger.info(f"[Rehearsal] Summary saved for rehearsal #{rehearsal_id}")

    # ── Audio path ─────────────────────────────────────────────

    async def set_audio_path(self, rehearsal_id: int, path: str) -> None:
        """Record the full audio file storage path."""
        self._db.conn.execute(
            "UPDATE thinker_rehearsals SET full_audio_path = ? WHERE id = ?",
            (path, rehearsal_id),
        )
        self._db.conn.commit()

    # ── LLM audience question selection ────────────────────────

    async def next_question(
        self,
        rehearsal_id: int,
        presentation_text: str,
        previous_qa: list[tuple[str, str]],
    ) -> str:
        """
        LLM picks the next question from the question bank.

        Returns the question text, or empty string on failure.
        """
        row = self._db.conn.execute(
            "SELECT question_list_content FROM thinker_rehearsals WHERE id = ?",
            (rehearsal_id,),
        ).fetchone()
        if not row:
            return ""

        questions_md = row["question_list_content"]

        try:
            prompt = self._llm.load_prompt(
                "thinker", "rehearsal_audience",
                questions_md=questions_md,
                presentation_text=presentation_text,
                previous_qa=previous_qa,
            )
        except Exception:
            logger.warning(
                "[Rehearsal] Failed to load rehearsal_audience prompt, using inline fallback",
                exc_info=True,
            )
            prompt = _build_audience_prompt_inline(questions_md, presentation_text, previous_qa)

        try:
            response, _ = await self._llm.achat(
                user_prompt=prompt,
                module="thinker",
                purpose="rehearsal_question",
            )
        except Exception as e:
            logger.error(f"[Rehearsal] LLM question selection failed: {e}")
            return ""

        return response.strip()

    # ── LLM summary generation ─────────────────────────────────

    async def generate_summary(
        self,
        rehearsal_id: int,
        presentation_text: str,
        qa_text: str,
    ) -> dict:
        """
        Generate the rehearsal summary (three parts: briefing, grammar corrections, suggestions).

        Returns: {"briefing": str, "grammar_corrections": list, "suggestions": list}
        """
        try:
            prompt = self._llm.load_prompt(
                "thinker", "rehearsal_summary",
                presentation_text=presentation_text,
                qa_text=qa_text,
            )
        except Exception:
            logger.warning(
                "[Rehearsal] Failed to load rehearsal_summary prompt, using inline fallback",
                exc_info=True,
            )
            prompt = _build_summary_prompt_inline(presentation_text, qa_text)

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                raw, _ = await self._llm.achat(
                    user_prompt=prompt,
                    module="thinker",
                    purpose="rehearsal_summary",
                )
                result = _parse_summary_json(raw)
                return result
            except Exception as e:
                logger.warning(
                    f"[Rehearsal] Summary generation attempt {attempt + 1} failed: {e}"
                )
                if attempt < max_attempts - 1:
                    continue
                return {
                    "briefing": "Summary generation failed. Please try generating the summary again.",
                    "grammar_corrections": [],
                    "suggestions": [],
                }

        return {"briefing": "", "grammar_corrections": [], "suggestions": []}


# ── Helper functions ─────────────────────────────────────────────

def _parse_summary_json(raw: str) -> dict:
    """Parse LLM summary JSON, tolerating markdown code block wrapping."""
    import re as _re

    cleaned = _re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    match = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
    if match:
        cleaned = match.group(0)
    data = json.loads(cleaned)
    return {
        "briefing": data.get("briefing", ""),
        "grammar_corrections": data.get("grammar_corrections", []),
        "suggestions": data.get("suggestions", []),
    }


def _build_audience_prompt_inline(
    questions_md: str,
    presentation_text: str,
    previous_qa: list[tuple[str, str]],
) -> str:
    """Fallback prompt builder when jinja2 template is not yet available."""
    asked_section = ""
    if previous_qa:
        asked_section = "## Questions already asked\n"
        for q, a in previous_qa:
            asked_section += f"- Q: {q}\n  A: {a}\n"
    else:
        asked_section = "(No questions asked yet)\n"

    return f"""You are a professional academic conference attendee in a Q&A session.

## Full Presentation Transcript
{presentation_text}

## Question Bank
{questions_md}

{asked_section}
## Your Task

Pick ONE question from the Question Bank. Selection strategy:
1. Do NOT go in order — skip questions with low answer value or weak connection to the presentation
2. Prioritize: questions directly related to core contributions, ones that expose potential logical gaps, cross-category connections
3. You may ask a follow-up based on the last answer (doesn't need to be verbatim from the bank, but must be a natural extension)
4. Avoid asking consecutive questions from the same category
5. If all high-quality questions are exhausted, reply "NO_MORE_QUESTIONS"

Output ONLY the question text itself. No prefix, numbering, or explanation."""


def _build_summary_prompt_inline(presentation_text: str, qa_text: str) -> str:
    """Fallback summary prompt builder."""
    return f"""You are a senior academic presentation coach. Generate a structured summary report based on the following rehearsal transcript.

## Presentation Transcript
{presentation_text}

## Q&A Transcript
{qa_text}

## Output Format

Output strictly as JSON, no extra text:

```json
{{
  "briefing": "Presentation briefing (in Chinese, 300-500 chars): overview, Q&A performance, overall assessment",
  "grammar_corrections": [
    {{
      "id": 1,
      "original": "original sentence",
      "corrected": "corrected version",
      "note": "explanation of the correction"
    }}
  ],
  "suggestions": [
    {{
      "id": 1,
      "category": "内容建议|表达建议|优化示范",
      "issue": "problem description",
      "suggestion": "specific suggestion",
      "example": "improved example (empty string if not applicable)"
    }}
  ]
}}
```

## Requirements
- briefing: Objective evaluation, highlights both strengths and weaknesses
- grammar_corrections: Only fix clear grammar/word choice errors, not style preferences. Include original quote for each. Empty array if no errors.
- suggestions: Categorized as 内容建议 (content), 表达建议 (delivery), or 优化示范 (example rewrite). Each must be specific and actionable."""
