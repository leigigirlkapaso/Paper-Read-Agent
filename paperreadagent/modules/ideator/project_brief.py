"""
modules/ideator/project_brief.py
ProjectBriefService — turns a mature spark into a 6-dimension executable
project brief via a single LLM call. On-demand action (like roundtable),
results stored in ideator_project_briefs (one spark -> many briefs).
"""
from __future__ import annotations

import json
import logging

from paperreadagent.utils.json_utils import clean_json
from paperreadagent.modules.ideator.ideator_llm import IdeatorLLM
from paperreadagent.modules.ideator.data_access import DataAccess

logger = logging.getLogger(__name__)

_REQUIRED_DIMENSIONS = {
    "feasibility", "theory", "experiment_plan",
    "expected_results", "risk_assessment", "differentiation",
}


class ProjectBriefService:
    """Generate a 6-dimension feasibility brief for a spark."""

    def __init__(self, core, data: DataAccess, *, llm=None):
        self._core = core
        self._data = data
        self._llm = llm if llm is not None else IdeatorLLM(core_llm=core.llm)

    async def generate(self, spark_id: int, *, outline_markdown: str = "") -> int:
        """Generate one project brief. Returns the brief row id.

        Creates a brief row (status='generating'), then fills it in. On
        LLM/parse failure the row is marked status='failed' with the error
        stored — the call does NOT raise (graceful degradation), EXCEPT when
        the spark does not exist (programmer error -> ValueError).

        If outline_markdown is provided (from the secretary's roundtable
        outline), it is injected into the prompt as an additional input
        alongside the standard context sources.
        """
        ctx = self._data.gather_brief_context(spark_id)   # raises if missing

        brief_id = self._data.insert_project_brief(spark_id)
        context_sources = {
            "depth_content": bool(ctx["depth_content"]),
            "cross_links": len(ctx["cross_links"]),
            "team_memory": len(ctx["team_memory"]),
            "outline": bool(outline_markdown),
        }

        try:
            system_prompt = self._llm.load_prompt("ideator", "project_brief_system")
            user_prompt = self._llm.load_prompt(
                "ideator", "project_brief_user",
                spark_content=ctx["spark_content"],
                depth_content=ctx["depth_content"],
                cross_links=ctx["cross_links"],
                team_memory=ctx["team_memory"],
                outline_markdown=outline_markdown,
            )
            raw = await self._llm.chat(
                model_role="project_brief",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.5,
                max_tokens=8000,
            )
            parsed = json.loads(clean_json(raw))
            if not isinstance(parsed, dict) or not (_REQUIRED_DIMENSIONS <= set(parsed)):
                missing = _REQUIRED_DIMENSIONS - set(parsed if isinstance(parsed, dict) else {})
                raise ValueError(f"brief JSON missing dimensions: {missing}")

            model_name = ""
            if hasattr(self._llm, "model_for"):
                try:
                    candidate = self._llm.model_for("project_brief")
                    model_name = candidate if isinstance(candidate, str) else ""
                except Exception:
                    model_name = ""

            self._data.update_project_brief(
                brief_id,
                status="done",
                brief_json=json.dumps(parsed, ensure_ascii=False),
                context_sources=json.dumps(context_sources, ensure_ascii=False),
                model_name=model_name,
            )
        except Exception as exc:
            logger.warning("[ProjectBrief] spark %s failed: %s", spark_id, exc)
            self._data.update_project_brief(
                brief_id,
                status="failed",
                context_sources=json.dumps(context_sources, ensure_ascii=False),
                error=str(exc)[:500],
            )
        return brief_id
