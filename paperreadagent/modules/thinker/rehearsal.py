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

    # Valid forward transitions: current → {allowed next states}
    _TRANSITIONS: dict[str, set[str]] = {
        "preparing": {"presenting", "qa"},
        "presenting": {"qa", "preparing"},
        "qa": {"summarizing", "preparing"},
        "summarizing": {"completed", "qa"},  # qa fallback on failure
        "completed": set(),
    }

    async def update_status(self, rehearsal_id: int, status: str, *, force: bool = False) -> None:
        """Update rehearsal phase. Enforces valid forward transitions unless force=True."""
        valid = {"preparing", "presenting", "qa", "summarizing", "completed"}
        if status not in valid:
            raise ValueError(f"Invalid status: {status}, valid values: {valid}")

        if not force:
            row = self._db.conn.execute(
                "SELECT status FROM thinker_rehearsals WHERE id = ?", (rehearsal_id,)
            ).fetchone()
            if row:
                current = row["status"]
                allowed = self._TRANSITIONS.get(current, set())
                if status not in allowed and current != status:
                    raise ValueError(
                        f"Invalid transition: {current} → {status}. Allowed: {allowed}"
                    )

        self._db.conn.execute(
            """UPDATE thinker_rehearsals
               SET status = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (status, rehearsal_id),
        )
        self._db.conn.commit()

    async def validate_rehearsal(self, rehearsal_id: int) -> dict | None:
        """Check rehearsal exists and return it, or None if not found.
        Use before any mutation to prevent silent data loss on invalid IDs."""
        row = self._db.conn.execute(
            "SELECT id, status FROM thinker_rehearsals WHERE id = ?", (rehearsal_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── Transcript accumulation ─────────────────────────────────

    async def append_presentation_transcript(
        self, rehearsal_id: int, text: str
    ) -> None:
        """Append text to the presentation transcript (server-side concatenation).

        Uses BEGIN IMMEDIATE to prevent lost updates from concurrent chunk writes.
        """
        self._db.conn.execute("BEGIN IMMEDIATE")
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
        """Append one Q&A turn to the Q&A transcript.

        Uses BEGIN IMMEDIATE to prevent lost updates from concurrent writes.
        """
        entry = f"\n\n[Q · 🤖] {question}\n[A · 🎤] {answer}\n"
        self._db.conn.execute("BEGIN IMMEDIATE")
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
                    max_tokens=32768,  # 6-dim briefing + corrections + suggestions
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
    """Parse LLM summary JSON, tolerating markdown code block wrapping.
    Includes type validation to prevent DB write failures from LLM hallucinations."""
    import re as _re

    cleaned = _re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    match = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
    if match:
        cleaned = match.group(0)
    data = json.loads(cleaned)

    # Validate types — LLMs sometimes hallucinate arrays where strings are expected
    briefing = data.get("briefing", "")
    if not isinstance(briefing, str):
        briefing = str(briefing)  # Convert list/dict to string representation

    corrections = data.get("grammar_corrections", [])
    if not isinstance(corrections, list):
        corrections = []

    suggestions = data.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []

    # Ensure each correction/suggestion item is a dict, then normalize fields
    corrections_raw = [c for c in corrections if isinstance(c, dict)]
    suggestions_raw = [s for s in suggestions if isinstance(s, dict)]

    corrections = []
    for c in corrections_raw:
        lang = str(c.get("language", "")).lower()
        c["language"] = "en" if lang.startswith("en") else "zh"
        corrections.append(c)

    suggestions = []
    for s in suggestions_raw:
        cat = str(s.get("category", ""))
        if "结构" in cat:
            s["category"] = "结构建议"
        elif "表达" in cat or "delivery" in cat.lower():
            s["category"] = "表达建议"
        elif "示范" in cat or "rewrite" in cat.lower() or "优化" in cat:
            s["category"] = "优化示范"
        else:
            s["category"] = "内容建议"
        suggestions.append(s)

    return {
        "briefing": briefing,
        "grammar_corrections": corrections,
        "suggestions": suggestions,
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
    """Fallback summary prompt builder — mirrors rehearsal_summary.jinja2 structure."""
    return f"""You are a senior academic presentation coach. Generate a thorough, brutally honest evaluation of this rehearsal.

## Presentation Transcript
{presentation_text}

## Q&A Transcript
{qa_text}

## Output Format

Output strictly as JSON, no extra text:

```json
{{
  "briefing": "Multi-section Markdown report in Chinese. Structure (each section starts with ### heading):\n\n### 🔑 核心发现\n3-5 bullet points of the most critical, actionable insights. Prioritize what the presenter is LEAST aware of.\n\n### 📊 内容评估\nLogic flow, argument strength, structure, evidence quality, technical depth vs clarity. Be specific about strengths AND weaknesses.\n\n### 💬 Q&A 表现\nAnswer quality, handling strategy, patterns of strength/weakness, specific improvement directions.\n\n### ✅ 改进清单\nPrioritized checkboxes: - [ ] 🔴 高优先 (must fix), - [ ] 🟡 中优先 (should fix), - [ ] 🟢 低优先 (nice to have). Each item must be specific and testable.",
  "grammar_corrections": [
    {{
      "id": 1,
      "language": "zh or en",
      "original": "exact original sentence",
      "corrected": "corrected version",
      "note": "explanation in Chinese"
    }}
  ],
  "suggestions": [
    {{
      "id": 1,
      "category": "内容建议|表达建议|结构建议|优化示范",
      "issue": "problem description in Chinese",
      "suggestion": "specific actionable suggestion in Chinese",
      "example": "improved example text, or empty string"
    }}
  ]
}}
```

## Requirements
- **briefing**: All in Chinese. Be honest — generic praise is useless. Every criticism must include a suggested fix. Separate sections with ### headings. Handle edge cases: if transcript is short, focus on what CAN be evaluated.
- **grammar_corrections**: Bilingual — fix BOTH Chinese and English errors. Chinese: 搭配不当, 语序, 用词. English: grammar, tense, articles. Each entry MUST include exact original quote. Empty array if no errors.
- **suggestions**: Four categories — 内容建议 (arguments/evidence/data), 表达建议 (phrasing/clarity/conciseness), 结构建议 (organization/flow/transitions/time allocation), 优化示范 (before/after rewrite). Each must be specific and actionable. Provide concrete examples wherever possible.
- **Prioritize**: Rank issues by impact. The improvement checklist must guide the presenter on what to fix first."""
