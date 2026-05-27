"""debate_engine.py — 8-seat multi-round debate review for pipeline S3."""
from __future__ import annotations
import asyncio, json, logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_DEBATE_ROUNDS = 5

# 所有坐席统一使用 deepseek-v4-pro (model_for() 忽略角色参数)
DEBATE_SEATS = [
    ("gen",  "generator",   "deepseek-v4-pro"),
    ("rev1", "reviewer_1",  "deepseek-v4-pro"),
    ("rev2", "reviewer_2",  "deepseek-v4-pro"),
    ("rev3", "reviewer_3",  "deepseek-v4-pro"),
    ("arb1", "arbiter_1",   "deepseek-v4-pro"),
    ("arb2", "arbiter_2",   "deepseek-v4-pro"),
]

REVIEWER_FOCUS = {
    "rev1": "新颖性与证据：评估研究假设的创新性，逐条验证 claim 是否有来源文献支持，标记过度延伸的推论",
    "rev2": "可行性与交叉验证：评估实验设计是否可行，从不同学科视角寻找遗漏的关联和应用场景",
    "rev3": "边界条件与补充：挑战假设的适用范围和局限，提出反例、边界情况或失效条件",
}


@dataclass
class DebateReviewResult:
    scores: dict
    key_concerns: list[str]
    strengths: list[str]
    verdict: str
    reasoning: str
    reviewer: str

    @property
    def overall(self) -> float:
        if not self.scores:
            return 0.0
        return sum(self.scores.values()) / len(self.scores)


@dataclass
class DebateRound:
    round_num: int
    questions: list[dict]
    gen_response: dict


@dataclass
class DebateOutcome:
    verdict: str
    final_score: float
    reasoning: str
    debate_summary: str
    initial_reviews: list[DebateReviewResult]
    debate_rounds: list[DebateRound]
    re_reviews: list[DebateReviewResult]
    briefing: dict | None
    revised_draft: str = ""


class DebateEngine:
    """8 坐席多轮辩论审查引擎。"""

    def __init__(self, *, llm, data_access):
        self._llm = llm
        self._data = data_access
        self._review_sem = asyncio.Semaphore(2)  # 限流 2 路并发，3 审查者分 2 批

    @staticmethod
    def _clean_json(raw: str) -> str:
        from paperreadagent.utils.json_utils import clean_json
        return clean_json(raw)

    async def score_sparks(self, sparks: list[dict]) -> list[dict]:
        """S2.25 闪电筛选：3 flash reviewer 对每个火花快速评分，返回带 _filter_score 的火花列表。

        每个火花调用 3 次 flash reviewer（角度：新颖性/证据/边界），Semaphore(1)。
        评分只返回 score，不做完整审查。
        """
        if len(sparks) <= 10:
            for s in sparks:
                s["_filter_score"] = 0.5
            return sparks

        reviewer_ids = [sid for sid, _, _ in DEBATE_SEATS if sid.startswith("rev")]
        perspectives = {
            "rev1": "新颖性：这个火花是否提出了新问题或新方法？0=完全重复 1=高度创新",
            "rev2": "证据基础：这个火花是否能从来源文献中找到支撑？0=无依据 1=充分支撑",
            "rev3": "边界条件：这个假设的适用范围是否合理？0=不合理 1=清晰合理",
        }

        async def _score_one(spark: dict) -> dict:
            scores = []
            for rid in reviewer_ids:
                async with self._review_sem:
                    focus = perspectives.get(rid, "综合评分")
                    prompt = (
                        f"评分以下研究火花（只返回0-1的分数）：\n\n"
                        f"火花：{spark.get('content', '')[:1000]}\n\n"
                        f"评分角度：{focus}\n\n"
                        f'返回JSON：{{"score": 0.0-1.0}}'
                    )
                    try:
                        data = await self._chat_with_retry(rid, prompt, temperature=0.2, max_tokens=2048)
                        scores.append(data.get("score", 0.5))
                    except Exception:
                        scores.append(0.5)
            spark["_filter_score"] = sum(scores) / len(scores) if scores else 0.5
            return spark

        scored = await asyncio.gather(*[_score_one(s) for s in sparks])
        return scored

    async def _chat_with_retry(self, model_role: str, prompt: str,
                                 temperature: float = 0.3, max_tokens: int = 8192,
                                 max_retries: int = 3) -> dict:
        """带对话历史重试的 LLM 调用，返回已解析的 JSON dict。

        同时重试 API 错误和 JSON 解析错误，将格式反馈注入对话历史。
        """
        messages = [{"role": "user", "content": prompt}]
        last_error = None
        for attempt in range(max_retries):
            try:
                raw = await self._llm.chat(
                    model_role=model_role, messages=messages,
                    temperature=temperature, max_tokens=max_tokens,
                )
                return json.loads(self._clean_json(raw))
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    messages.append({"role": "assistant", "content": "[响应格式错误]"})
                    messages.append({"role": "user",
                        "content": f"你的上一次响应无法解析（{str(e)[:80]}）。请重新给出只包含有效 JSON 的响应，不要 markdown 代码块。"})
        raise last_error

    async def run(self, spark_content: str, draft: str,
                  source_context: str) -> DebateOutcome:
        """执行完整辩论审查：初评 → 多轮辩论 → 重评 → 终裁 → 简报。"""

        logger.info("[DebateEngine] 开始辩论审查")

        # ── Phase 1: 背对背初评 ──────────────────────────
        reviewer_ids = [sid for sid, _, _ in DEBATE_SEATS if sid.startswith("rev")]
        initial_tasks = [
            self._call_reviewer(sid, draft, source_context, "")
            for sid in reviewer_ids
        ]
        results = await asyncio.gather(*initial_tasks, return_exceptions=True)
        initial_reviews = [r for r in results if isinstance(r, DebateReviewResult)]
        logger.info(f"[DebateEngine] 初评完成: {len(initial_reviews)}/3")

        if not initial_reviews:
            logger.warning("[DebateEngine] 全部 3 个审查者失败，辩论中止")
            return DebateOutcome(
                verdict="REVISE", final_score=0.5,
                reasoning="全部审查者调用失败，无法完成辩论审查",
                debate_summary="辩论未执行（初评阶段失败）",
                initial_reviews=[], debate_rounds=[], re_reviews=[],
                briefing=None, revised_draft=draft,
            )

        # ── Phase 2: 多轮辩论 ────────────────────────────
        rounds: list[DebateRound] = []
        current_draft = draft
        all_questions: list[dict] = []
        for r in initial_reviews:
            for c in r.key_concerns[:3]:  # 每个审查者最多 3 个质疑
                all_questions.append({"reviewer": r.reviewer, "content": c})

        for round_num in range(1, MAX_DEBATE_ROUNDS + 1):
            if not all_questions:
                logger.info("[DebateEngine] 辩论结束: 无更多审查质疑")
                break

            # Arbiter 控场判断
            decision = await self._call_arb_control(
                current_draft, initial_reviews, rounds, all_questions,
            )
            logger.info(f"[DebateEngine] 第{round_num}轮 Arbiter 判断: {decision.get('decision')}")
            if decision.get("decision") == "STOP":
                logger.info("[DebateEngine] 辩论结束: Arbiter 判定 STOP")
                break

            # Gen 辩护回应
            gen_resp = await self._call_gen_defend(
                spark_content, current_draft, source_context, all_questions,
            )
            if gen_resp is None:
                logger.info("[DebateEngine] 辩论结束: Gen 辩护失败")
                break

            dr = DebateRound(
                round_num=round_num,
                questions=[dict(q) for q in all_questions],
                gen_response=gen_resp,
            )
            rounds.append(dr)

            if gen_resp.get("revised_draft"):
                current_draft = gen_resp["revised_draft"]

            # 收集后续追问
            new_qs = await self._gather_followup_questions(
                current_draft, gen_resp, rounds, reviewer_ids,
            )
            if not new_qs:
                break
            all_questions = new_qs

        logger.info(f"[DebateEngine] 辩论完成: {len(rounds)} 轮")

        # ── Phase 3: 重新打分 ───────────────────────────
        debate_log = self._format_debate_log(rounds)
        re_tasks = [
            self._call_reviewer(sid, current_draft, source_context, debate_log)
            for sid in reviewer_ids
        ]
        re_results = await asyncio.gather(*re_tasks, return_exceptions=True)
        re_reviews = [r for r in re_results if isinstance(r, DebateReviewResult)]
        logger.info(f"[DebateEngine] 重评完成: {len(re_reviews)}/3")

        # ── Phase 4: 终裁 ──────────────────────────────
        arb_result = await self._call_arb_final(
            current_draft, initial_reviews, rounds, re_reviews,
        )

        # ── Phase 5: 书记员简报 ────────────────────────
        briefing = await self._call_rec_briefing(
            spark_content, current_draft, source_context,
            initial_reviews, rounds, re_reviews, arb_result,
        )

        return DebateOutcome(
            verdict=arb_result.get("verdict", "PASS"),
            final_score=arb_result.get("final_score", 0.5),
            reasoning=arb_result.get("reasoning", ""),
            debate_summary=arb_result.get("debate_summary", ""),
            initial_reviews=initial_reviews,
            debate_rounds=rounds,
            re_reviews=re_reviews,
            briefing=briefing,
            revised_draft=current_draft,
        )

    async def _call_reviewer(self, seat_id: str, draft: str,
                              source_context: str, debate_so_far: str) -> DebateReviewResult:
        focus = REVIEWER_FOCUS.get(seat_id, "全面审查")
        prompt = self._llm.load_prompt(
            "ideator", "debate_review",
            draft=draft, source_context=source_context,
            debate_so_far=debate_so_far or "（初评，无审查历史）",
            reviewer_focus=focus,
        )
        async with self._review_sem:
            try:
                data = await self._chat_with_retry(seat_id, prompt, temperature=0.3, max_tokens=8192)
                return DebateReviewResult(
                    scores=data.get("scores", {}),
                    key_concerns=data.get("key_concerns", []),
                    strengths=data.get("strengths", []),
                    verdict=data.get("verdict", "PASS"),
                    reasoning=data.get("reasoning", ""),
                    reviewer=seat_id,
                )
            except Exception:
                logger.warning(f"[DebateEngine] 审查者 {seat_id} 调用失败，使用 fallback", exc_info=True)
                return DebateReviewResult(
                    scores={"novelty": 0.5, "evidence": 0.5, "feasibility": 0.5},
                    key_concerns=[
                        f"审查者 {seat_id} LLM 调用异常，需人工审查",
                        "草稿可能缺少对局限性的讨论",
                    ],
                    strengths=["自动化审查不可用"],
                    verdict="REVISE",
                    reasoning=f"审查者 {seat_id} LLM 调用异常，标记为需修订",
                    reviewer=seat_id,
                )

    async def _call_gen_defend(self, spark_content: str, draft: str,
                                source_context: str,
                                questions: list[dict]) -> dict | None:
        prompt = self._llm.load_prompt(
            "ideator", "debate_gen_defend",
            spark_content=spark_content, draft=draft,
            source_context=source_context, questions=questions,
        )
        try:
            return await self._chat_with_retry("gen", prompt, temperature=0.5, max_tokens=4096)
        except Exception:
            logger.warning("[DebateEngine] Gen 辩护失败", exc_info=True)
            return None

    async def _call_arb_control(self, draft: str,
                                  initial_reviews: list[DebateReviewResult],
                                  rounds: list[DebateRound],
                                  pending_questions: list[dict]) -> dict:
        prompt = self._llm.load_prompt(
            "ideator", "debate_arb_judge",
            draft=draft,
            initial_reviews=[{"reviewer": r.reviewer, "scores": r.scores,
                              "verdict": r.verdict, "key_concerns": r.key_concerns}
                             for r in initial_reviews],
            debate_log=self._format_debate_log(rounds),
            re_reviews=[], phase="control",
        )
        try:
            return await self._chat_with_retry("arb1", prompt, temperature=0.2, max_tokens=2048)
        except Exception:
            logger.warning("[DebateEngine] arb_control 全部重试失败，回退为 CONTINUE")
            return {"decision": "CONTINUE", "reason": "arbiter 调用失败，回退继续"}

    async def _call_arb_final(self, draft: str,
                                initial_reviews: list[DebateReviewResult],
                                rounds: list[DebateRound],
                                re_reviews: list[DebateReviewResult]) -> dict:
        prompt = self._llm.load_prompt(
            "ideator", "debate_arb_judge",
            draft=draft,
            initial_reviews=[{"reviewer": r.reviewer, "scores": r.scores,
                              "verdict": r.verdict, "key_concerns": r.key_concerns}
                             for r in initial_reviews],
            debate_log=self._format_debate_log(rounds),
            re_reviews=[{"reviewer": r.reviewer, "scores": r.scores,
                         "verdict": r.verdict} for r in re_reviews],
            phase="final",
        )
        try:
            return await self._chat_with_retry("arb1", prompt, temperature=0.2, max_tokens=2048)
        except Exception:
            logger.warning("[DebateEngine] 终裁调用失败，回退为 PASS", exc_info=True)
            return {"verdict": "PASS", "final_score": 0.5, "reasoning": "仲裁失败",
                    "key_findings": [], "debate_summary": ""}

    async def _call_rec_briefing(self, spark_content: str, draft: str,
                                   source_context: str,
                                   initial_reviews: list[DebateReviewResult],
                                   rounds: list[DebateRound],
                                   re_reviews: list[DebateReviewResult],
                                   arb_result: dict) -> dict | None:
        full_log_parts = ["## 初评"]
        for r in initial_reviews:
            full_log_parts.append(
                f"[{r.reviewer}] verdict={r.verdict} scores={r.scores}\n"
                f"concerns={r.key_concerns}\nstrengths={r.strengths}"
            )
        full_log_parts.append("\n## 辩论")
        for rnd in rounds:
            full_log_parts.append(f"### 第{rnd.round_num}轮")
            for q in rnd.questions:
                full_log_parts.append(f"[{q['reviewer']}]: {q['content']}")
            full_log_parts.append(f"[gen]: {json.dumps(rnd.gen_response, ensure_ascii=False)}")
        full_log_parts.append("\n## 重评")
        for r in re_reviews:
            full_log_parts.append(f"[{r.reviewer}] verdict={r.verdict} scores={r.scores}")

        prompt = self._llm.load_prompt(
            "ideator", "debate_rec_briefing",
            spark_content=spark_content, draft=draft,
            source_context=source_context,
            debate_full_log="\n".join(full_log_parts),
            verdict=arb_result.get("verdict", ""),
            final_score=arb_result.get("final_score", 0.0),
            arb_reasoning=arb_result.get("reasoning", ""),
        )
        try:
            return await self._chat_with_retry("rec", prompt, temperature=0.3, max_tokens=2048)
        except Exception:
            logger.warning("[DebateEngine] 书记员简报失败", exc_info=True)
            return None

    async def _gather_followup_questions(self, draft: str,
                                           gen_resp: dict,
                                           rounds: list[DebateRound],
                                           reviewer_ids: list[str]) -> list[dict]:
        """收集审查者对 Gen 回应的后续追问。简化版：每个审查者看上一轮 Gen 的回应，生成最多 1 个追问。"""
        if len(rounds) >= MAX_DEBATE_ROUNDS:
            return []  # 达到最大辩论轮数，不再追问
        questions = []
        for rid in reviewer_ids:
            try:
                focus = REVIEWER_FOCUS.get(rid, "")
                prompt = (
                    f"你是审查者。Gen 刚回应了上一轮质疑。\n\n"
                    f"## 草稿\n{draft}\n\n"
                    f"## Gen 的回应\n{json.dumps(gen_resp, ensure_ascii=False)}\n\n"
                    f"## 你的审查角度\n{focus}\n\n"
                    f"如果 Gen 的回应仍然存在重大漏洞或不充分，提出 1 个追问。"
                    f"如果回应充分，返回 {{\"followup\": null}}。\n"
                    f"只返回 JSON：{{\"followup\": \"追问内容或null\"}}"
                )
                data = await self._chat_with_retry(rid, prompt, temperature=0.3, max_tokens=4096)
                fup = data.get("followup")
                if fup and fup != "null":
                    questions.append({"reviewer": rid, "content": fup})
            except Exception:
                logger.warning("[DebateEngine] followup failed for %s", rid, exc_info=True)
        return questions

    def _format_debate_log(self, rounds: list[DebateRound]) -> str:
        if not rounds:
            return "（无辩论记录）"
        lines = []
        for r in rounds:
            lines.append(f"## 第 {r.round_num} 轮")
            for q in r.questions:
                lines.append(f"[{q['reviewer']}]: {q['content']}")
            resp = r.gen_response.get("responses", [])
            for item in resp:
                lines.append(f"[gen → {item.get('reviewer','?')}]: {item.get('response','')}")
        return "\n\n".join(lines)
