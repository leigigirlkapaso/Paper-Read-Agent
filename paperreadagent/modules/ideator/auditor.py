"""
modules/ideator/auditor.py
SparkAuditor -- independent model verifies sparks against source texts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from paperreadagent.utils.json_utils import clean_json

logger = logging.getLogger(__name__)


@dataclass
class AuditResult:
    verdict: str  # SUPPORTED / STRETCHED / UNSUPPORTED
    claims_check: list[dict]
    reasoning: str


class SparkAuditor:
    def __init__(self, *, llm):
        self._llm = llm
        from jinja2 import Environment, FileSystemLoader
        self._jinja = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "prompts"),
            autoescape=False,
        )

    @staticmethod
    def score_delta(verdict: str) -> float:
        if verdict == "SUPPORTED":
            return 0.1
        if verdict == "UNSUPPORTED":
            return -0.3
        return 0.0

    async def audit(self, *, spark_content: str, source_refs: list[dict]) -> AuditResult:
        tpl = self._jinja.get_template("audit_spark.jinja2")
        prompt = tpl.render(spark_content=spark_content, source_refs=source_refs)
        messages = [{"role": "user", "content": prompt}]
        for attempt in range(3):
            try:
                raw = await self._llm.chat(
                    model_role="auditor", messages=messages,
                    temperature=0.2, max_tokens=8192,
                )
                data = json.loads(clean_json(raw))
                return AuditResult(
                    verdict=data["verdict"],
                    claims_check=data.get("claims_check", []),
                    reasoning=data.get("reasoning", ""),
                )
            except (json.JSONDecodeError, KeyError) as e:
                hint = "你的上一次响应无法解析为 JSON" if isinstance(e, json.JSONDecodeError) else f"缺少必要字段: {e}"
                messages.append({"role": "assistant", "content": "[响应格式错误]"})
                messages.append({"role": "user", "content": f"{hint}。请重新给出只包含有效 JSON 的响应。"})
            except Exception:
                logger.warning("[Auditor] LLM call attempt %d failed", attempt + 1, exc_info=True)
        logger.warning("[Auditor] All retries exhausted, returning UNSUPPORTED fallback")
        return AuditResult(
            verdict="UNSUPPORTED", claims_check=[],
            reasoning="审计失败（LLM 响应解析错误）",
        )
