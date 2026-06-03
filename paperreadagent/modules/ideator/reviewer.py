"""
modules/ideator/reviewer.py
SparkReviewer -- dual-review engine + Tier 3 arbitration escalation.
Two independent reviewers evaluate each spark simultaneously.
When they disagree significantly, escalate to Tier 3 arbiter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from paperreadagent.utils.json_utils import clean_json

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    scores: dict  # {"novelty": 0.X, "evidence": 0.X, "feasibility": 0.X}
    verdict: str  # PASS / REVISE / REJECT
    reasoning: str
    reviewer_model: str
    reviewer_role: str  # reviewer_1 / reviewer_2

    @property
    def overall(self) -> float:
        if not self.scores:
            return 0.0
        return sum(self.scores.values()) / len(self.scores)


@dataclass
class ArbitrationResult:
    scores: dict
    verdict: str  # OVERTURN / CONFIRM_R1 / CONFIRM_R2
    reasoning: str
    escalation_reason: str


class SparkReviewer:
    def __init__(self, *, llm, arbitration_cfg: dict):
        self._llm = llm
        self._arbitration_cfg = arbitration_cfg
        from jinja2 import Environment, FileSystemLoader
        self._jinja = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "prompts"),
            autoescape=False,
        )

    async def _call_model(self, model_role: str, messages: list[dict],
                          temperature: float, max_retries: int = 3) -> str:
        """Call LLM with retry on failure. Returns raw response text."""
        last_err = None
        for attempt in range(max_retries):
            try:
                raw = await self._llm.chat(
                    model_role=model_role, messages=messages,
                    temperature=temperature, max_tokens=8192,
                )
                if raw and raw.strip():
                    return raw
            except Exception as e:
                last_err = e
                logger.warning("[Reviewer] LLM call attempt %d failed for %s: %s",
                             attempt + 1, model_role, e)
        if last_err:
            raise last_err
        return ""

    async def _call_reviewer(self, messages: list[dict], role: str) -> ReviewResult:
        for attempt in range(3):
            try:
                raw = await self._call_model(role, messages, temperature=0.3)
                data = json.loads(clean_json(raw))
                return ReviewResult(
                    scores=data["scores"],
                    verdict=data.get("verdict", "PASS"),
                    reasoning=data.get("reasoning", ""),
                    reviewer_model=self._llm.model_for(role),
                    reviewer_role=role,
                )
            except (json.JSONDecodeError, KeyError) as e:
                hint = "你的上一次响应无法解析为 JSON" if isinstance(e, json.JSONDecodeError) else f"缺少必要字段: {e}"
                messages.append({"role": "assistant", "content": "[响应格式错误]"})
                messages.append({"role": "user", "content": f"{hint}。请重新给出只包含有效 JSON 的响应。"})
        # All retries exhausted
        logger.warning("[Reviewer] All retries exhausted for %s, returning REVISE fallback", role)
        return ReviewResult(
            scores={"novelty": 0.5, "evidence": 0.5, "feasibility": 0.5},
            verdict="REVISE", reasoning="审查失败（LLM 响应解析错误）",
            reviewer_model=self._llm.model_for(role), reviewer_role=role,
        )

    async def review_spark(
        self, *, spark_content: str,
        source_a_type: str, source_a_text: str,
        source_b_type: str, source_b_text: str,
        skip_arbitration: bool = True,
    ) -> tuple[ReviewResult, ReviewResult, ArbitrationResult | None]:
        tpl = self._jinja.get_template("review_spark.jinja2")
        prompt = tpl.render(
            spark_content=spark_content,
            source_a_type=source_a_type, source_a_text=source_a_text,
            source_b_type=source_b_type, source_b_text=source_b_text,
        )
        messages = [{"role": "user", "content": prompt}]

        r1, r2 = await asyncio.gather(
            self._call_reviewer([dict(m) for m in messages], "reviewer_1"),
            self._call_reviewer([dict(m) for m in messages], "reviewer_2"),
            return_exceptions=True,
        )
        if isinstance(r1, Exception):
            logger.warning("[Reviewer] reviewer_1 crashed: %s", r1)
            r1 = ReviewResult(scores={"novelty": 0.5, "evidence": 0.5, "feasibility": 0.5},
                            verdict="REVISE", reasoning="审查失败", reviewer_model="?", reviewer_role="reviewer_1")
        if isinstance(r2, Exception):
            logger.warning("[Reviewer] reviewer_2 crashed: %s", r2)
            r2 = ReviewResult(scores={"novelty": 0.5, "evidence": 0.5, "feasibility": 0.5},
                            verdict="REVISE", reasoning="审查失败", reviewer_model="?", reviewer_role="reviewer_2")

        if skip_arbitration:
            return r1, r2, None

        action, reason = self._decide_action(r1, r2)
        if action in ("arbitrate_high_value", "arbitrate_dispute"):
            arb = await self._call_arbiter(spark_content, r1, r2, reason)
            return r1, r2, arb

        return r1, r2, None

    def _decide_action(self, r1: ReviewResult, r2: ReviewResult) -> tuple[str, str]:
        o1, o2 = r1.overall, r2.overall
        both_high = self._arbitration_cfg.get("both_high_threshold", 0.8)
        divergence = self._arbitration_cfg.get("divergence_threshold", 0.25)
        both_low = self._arbitration_cfg.get("both_low_threshold", 0.4)

        if o1 >= both_high and o2 >= both_high:
            return "arbitrate_high_value", f"Both high: R1={o1:.2f}, R2={o2:.2f}"
        if abs(o1 - o2) >= divergence:
            return "arbitrate_dispute", f"Divergence: |{o1:.2f} - {o2:.2f}| = {abs(o1 - o2):.2f}"
        if o1 <= both_low and o2 <= both_low:
            return "reject", f"Both low: R1={o1:.2f}, R2={o2:.2f}"
        return "revise_pass", "Moderate consensus"

    async def _call_arbiter(
        self, spark_content: str, r1: ReviewResult, r2: ReviewResult, reason: str,
    ) -> ArbitrationResult:
        tpl = self._jinja.get_template("arbitrate_spark.jinja2")
        prompt = tpl.render(
            spark_content=spark_content,
            reviewer_1_model=r1.reviewer_model,
            r1_novelty=r1.scores.get("novelty"),
            r1_evidence=r1.scores.get("evidence"),
            r1_feasibility=r1.scores.get("feasibility"),
            r1_verdict=r1.verdict, r1_reasoning=r1.reasoning,
            reviewer_2_model=r2.reviewer_model,
            r2_novelty=r2.scores.get("novelty"),
            r2_evidence=r2.scores.get("evidence"),
            r2_feasibility=r2.scores.get("feasibility"),
            r2_verdict=r2.verdict, r2_reasoning=r2.reasoning,
            divergence=f"{abs(r1.overall - r2.overall):.2f}",
            dispute_dimension=self._dispute_dimension(r1, r2),
        )
        messages = [{"role": "user", "content": prompt}]
        for attempt in range(3):
            try:
                raw = await self._call_model("arbiter", messages, temperature=0.3)
                data = json.loads(clean_json(raw))
                return ArbitrationResult(
                    scores=data["scores"],
                    verdict=data.get("verdict", "OVERTURN"),
                    reasoning=data.get("reasoning", ""),
                    escalation_reason=reason,
                )
            except (json.JSONDecodeError, KeyError) as e:
                hint = "你的上一次响应无法解析为 JSON" if isinstance(e, json.JSONDecodeError) else f"缺少必要字段: {e}"
                messages.append({"role": "assistant", "content": "[响应格式错误]"})
                messages.append({"role": "user", "content": f"{hint}。请重新给出只包含有效 JSON 的响应。"})
        logger.warning("[Reviewer] Arbiter all retries exhausted")
        return ArbitrationResult(
            scores={"novelty": 0.5, "evidence": 0.5, "feasibility": 0.5},
            verdict="OVERTURN", reasoning="仲裁失败（LLM 响应解析错误）",
            escalation_reason=reason,
        )

    def _dispute_dimension(self, r1: ReviewResult, r2: ReviewResult) -> str:
        max_diff = 0.0
        dim = "overall"
        for key in r1.scores:
            diff = abs(r1.scores[key] - r2.scores.get(key, 0))
            if diff > max_diff:
                max_diff = diff
                dim = key
        return dim
