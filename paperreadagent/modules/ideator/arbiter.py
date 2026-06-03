"""arbiter.py — Arbiter: graduation decisions, context control, quota, tool authorization."""

from __future__ import annotations
import json
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader as JFSL

logger = logging.getLogger(__name__)

_JINJA_ENV = Environment(
    loader=JFSL(Path(__file__).parent / "prompts"),
    autoescape=False,
)

ROLES = ["gen", "rev1", "rev2", "rev3", "arb1", "arb2"]
DEFAULT_QUOTAS = {"gen": 2000, "rev1": 800, "rev2": 800, "rev3": 800, "arb1": 500, "arb2": 500}


class Arbiter:
    """Chief arbiter — graduation, context regulation, quotas, tool authorization."""

    def __init__(self, *, llm, graduation, tool_registry, team_memory):
        self._llm = llm
        self._graduation = graduation
        self._tool_registry = tool_registry
        self._team_memory = team_memory
        self._current_quotas = dict(DEFAULT_QUOTAS)
        self._recall_count = 0
        self._max_recalls = 2

    def reset_for_new_team(self) -> None:
        """Reset per-roundtable state before a new roundtable session."""
        self._recall_count = 0

    def calculate_round_quotas(self, hot_pct: float, warm_pct: float) -> dict[str, int]:
        """Calculate per-agent word quotas based on context watermark."""
        factor = 1.0
        if hot_pct > 85 or warm_pct > 85:
            factor = 0.5
        elif hot_pct > 60 or warm_pct > 60:
            factor = 0.7
        elif hot_pct < 30 and warm_pct < 30:
            factor = 1.5

        return {r: max(100, int(q * factor)) for r, q in DEFAULT_QUOTAS.items()}

    def evaluate_tool_request(self, role: str, tool_name: str, reason: str) -> dict:
        """Evaluate an agent's request for a tool they don't normally have access to."""
        if self._tool_registry.can_call(role, tool_name):
            return {"approved": True, "reason": "authorized_by_default"}

        if tool_name == "trigger_recall" and role in ("gen", "rev1", "rev2", "rev3"):
            if self._recall_count >= self._max_recalls:
                return {"approved": False, "reason": f"Maximum incremental recalls ({self._max_recalls}) reached"}
            self._recall_count += 1
            self._tool_registry.grant_tool(role, tool_name, reason=reason, duration_rounds=1)
            return {"approved": True, "reason": "arbiter_approved_incremental_recall", "recall_count": self._recall_count}

        return {"approved": False, "reason": f"Role {role} not authorized for {tool_name}"}

    def can_trigger_recall(self) -> bool:
        return self._recall_count < self._max_recalls

    async def execute_graduation(self, *, roundtable_id: int, spark_id: int,
                                  round_number: int, round_content: str,
                                  existing_memories: str) -> dict:
        """Execute graduation cycle: LLM decides what to keep/compress/store."""

        prompt = self._load_graduation_prompt(
            hot_pct=self._graduation.layers['hot'].pct,
            warm_pct=self._graduation.layers['warm'].pct,
            round_content=round_content,
            existing_memories=existing_memories,
        )

        result = await self._llm.chat(
            model_role="arbiter_1",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        raw = result[0] if isinstance(result, tuple) else result
        from paperreadagent.utils.json_utils import clean_json
        raw = clean_json(raw)
        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[Arbiter] 毕业决策 JSON 解析失败", exc_info=True)
            decision = {"verdict": "parse_error", "hot_keep": round_content}

        # Persist cold snapshot
        self._graduation.store_cold_snapshot(
            roundtable_id=roundtable_id, round_number=round_number,
            content=round_content,
        )

        # Write structured memories
        for mtype in ("consensus", "disagreement", "decision", "assumption", "open_question"):
            items = decision.get(f"{mtype}s", decision.get(mtype, [])) if mtype != "consensus" else decision.get("consensus", [])
            if isinstance(items, list):
                for item in items:
                    content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                    self._team_memory.write(
                        roundtable_id=roundtable_id, spark_id=spark_id,
                        memory_type=mtype, content=content,
                        round_number=round_number,
                    )

        return decision

    def _load_graduation_prompt(self, *, hot_pct: float, warm_pct: float,
                                 round_content: str, existing_memories: str) -> str:
        tpl = _JINJA_ENV.get_template("arbiter_graduation.jinja2")
        return tpl.render(
            hot_pct=f"{hot_pct:.1f}",
            warm_pct=f"{warm_pct:.1f}",
            round_content=round_content,
            existing_memories=existing_memories,
        )
