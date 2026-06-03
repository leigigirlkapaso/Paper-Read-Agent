# 管道重排 + 多轮辩论审查 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 管道重排（S2→S5→S3→S4）+ 8 坐席多轮辩论审查引擎 + 完整前端火花详情展示。

**Architecture:** S2 生成火花 → 深化为完整草稿 → 8 坐席辩论审查（Gen 辩护 + 5 Rev 质疑 + 2 Arb 控场裁量 + Rec 旁听记录）→ 入库。前端火花卡显示来源文献、书记员简报、辩论记录、研究草稿。

**Tech Stack:** Python asyncio, Jinja2 prompts, FastAPI, Vanilla JS + Tailwind CSS

---

### Task 1: 辩论审查 Prompt 模板

**Files:**
- Create: `paperreadagent/modules/ideator/prompts/debate_review.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/debate_gen_defend.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/debate_arb_judge.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/debate_rec_briefing.jinja2`

- [ ] **Step 1: Write `debate_review.jinja2`** — 审查者初评/重评 prompt

```jinja2
你是一名独立学术审查者。审阅以下研究草稿，从你的专业角度给出评估。

## 研究草稿
{{ draft }}

## 来源文献
{{ source_context }}

## 审查历史（如有）
{{ debate_so_far }}

## 你的审查角
{{ reviewer_focus }}

## 要求
返回 JSON：
{
  "scores": {"novelty": 0.0-1.0, "evidence": 0.0-1.0, "feasibility": 0.0-1.0},
  "key_concerns": ["质疑点1", "质疑点2", ...],
  "strengths": ["优点1", "优点2", ...],
  "verdict": "PASS/REVISE/REJECT",
  "reasoning": "一句话总结"
}
只返回 JSON。
```

- [ ] **Step 2: Write `debate_gen_defend.jinja2`** — Gen 辩论回应 prompt

```jinja2
你是这个研究火花的生成者。在辩论中，你需要解释推理链、展示来源证据、回应审查者的质疑。

## 你的原始火花
{{ spark_content }}

## 当前草稿
{{ draft }}

## 来源文献
{{ source_context }}

## 本轮质疑
{% for q in questions %}
[{{ q.reviewer }}]: {{ q.content }}
{% endfor %}

## 要求
逐条回应每个质疑：
1. 解释相关推理链
2. 引用来源证据（明确指出哪篇文献的哪个部分支持你的观点）
3. 承认不确定性（如果确实没有充分证据）
4. 如果有必要，提出修改方案
返回 JSON：
{
  "responses": [{"reviewer": "...", "response": "...", "draft_change": "修改内容或null"}],
  "revised_draft": "修改后的完整草稿（如有修改；如无修改留空字符串）"
}
只返回 JSON。
```

- [ ] **Step 3: Write `debate_arb_judge.jinja2`** — Arbiter 控场+终裁 prompt

```jinja2
你是仲裁者。控制辩论节奏，判断何时终止，并给出最终裁决。

## 研究草稿
{{ draft }}

## 初评结果
{% for r in initial_reviews %}
{{ r.reviewer }}: scores={{ r.scores }}, verdict={{ r.verdict }}, concerns={{ r.key_concerns }}
{% endfor %}

## 辩论记录
{{ debate_log }}

## 重评结果（如有）
{% for r in re_reviews %}
{{ r.reviewer }}: scores={{ r.scores }}, verdict={{ r.verdict }}
{% endfor %}

## 要求
{% if phase == "control" %}
判断当前辩论是否充分：
1. 核心分歧是否已充分讨论
2. Gen 是否已回应所有有效质疑
3. 是否需要更多轮次
返回 JSON：{"decision": "CONTINUE/STOP", "reason": "...", "unresolved_issues": ["..."]}
{% else %}
给出最终裁决：
返回 JSON：{
  "verdict": "PASS/REVISE/REJECT",
  "final_score": 0.0-1.0,
  "reasoning": "...",
  "key_findings": ["发现1", "发现2"],
  "debate_summary": "辩论摘要，一段话"
}
{% endif %}
只返回 JSON。
```

- [ ] **Step 4: Write `debate_rec_briefing.jinja2`** — 书记员简报 prompt

```jinja2
你是书记员。你全程旁听了审查辩论，现在需要输出结构化简报。

## 火花内容
{{ spark_content }}

## 研究草稿
{{ draft }}

## 来源文献
{{ source_context }}

## 辩论全程记录
{{ debate_full_log }}

## 终裁结果
verdict={{ verdict }}, final_score={{ final_score }}
{{ arb_reasoning }}

## 要求
输出结构化简报，返回 JSON：
{
  "background": "研究背景和核心问题域（2-3句话）",
  "breakthrough": "辩论中浮现的关键发现和突破口（2-3句话）",
  "innovation": "与现有研究的差异和创新点（2-3句话）",
  "implementation": "建议的下一步研究方向和方法（要点列表）",
  "open_issues": "辩论中未解决的分歧点（要点列表，无则空数组）"
}
只返回 JSON。
```

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/prompts/
git commit -m "feat(prompts): add 4 debate review prompts — review, gen_defend, arb_judge, rec_briefing"
```

---

### Task 2: 辩论引擎 — `debate_engine.py`

**Files:**
- Create: `paperreadagent/modules/ideator/debate_engine.py`
- Create: `paperreadagent/modules/ideator/tests/test_debate_engine.py`

- [ ] **Step 1: Write tests**

```python
"""tests for DebateEngine — 8-seat multi-round debate review"""
import pytest
from unittest.mock import MagicMock, AsyncMock
from paperreadagent.modules.ideator.debate_engine import (
    DebateEngine, ReviewResult, DebateRound, DebateOutcome,
    SEATS_8, create_debate_seats,
)


def test_create_debate_seats_returns_8():
    seats = create_debate_seats()
    assert len(seats) == 8
    ids = {s.seat_id for s in seats}
    assert ids == {"gen", "rev1", "rev2", "rev3", "rev4", "rev5", "arb1", "arb2"}
    # Rec is NOT in seats — it's managed separately by DebateEngine


def test_review_result_overall():
    r = ReviewResult(
        scores={"novelty": 0.8, "evidence": 0.7, "feasibility": 0.6},
        key_concerns=["样本量太小"], strengths=["方法创新"],
        verdict="PASS", reviewer="rev1",
    )
    assert r.overall == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_debate_engine_initial_review():
    mock_llm = MagicMock()
    mock_llm.chat = AsyncMock(return_value='{"scores":{"novelty":0.7,"evidence":0.6,"feasibility":0.8},"key_concerns":["缺少对照组"],"strengths":["方法新颖"],"verdict":"REVISE","reasoning":"需要补充对照设计"}')
    mock_llm.load_prompt = MagicMock(return_value="system prompt")
    mock_llm.model_for = MagicMock(return_value="deepseek-v4-pro")

    engine = DebateEngine(llm=mock_llm, data_access=MagicMock())
    result = await engine.run("test spark", "draft text", "source context")
    assert isinstance(result, DebateOutcome)
    assert "verdict" in result.__dict__ or hasattr(result, 'verdict')


@pytest.mark.asyncio
async def test_debate_generates_briefing():
    mock_llm = MagicMock()
    mock_llm.chat = AsyncMock()
    mock_llm.chat.side_effect = [
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev1
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev2
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev3
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev4
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev5
        '{"decision":"STOP","reason":"无分歧"}',  # arb control
        '{"verdict":"PASS","final_score":0.8,"reasoning":"无争议","key_findings":["ok"],"debate_summary":"通过"}',  # arb final
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev1 re-review
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev2 re-review
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev3 re-review
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev4 re-review
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"ok"}',  # rev5 re-review
        '{"background":"bg","breakthrough":"bt","innovation":"in","implementation":["s1"],"open_issues":[]}',  # rec briefing
    ]
    mock_llm.load_prompt = MagicMock(return_value="system prompt")
    mock_llm.model_for = MagicMock(return_value="deepseek-v4-pro")

    engine = DebateEngine(llm=mock_llm, data_access=MagicMock())
    result = await engine.run("spark", "draft", "sources")
    assert result.briefing is not None
    assert "background" in result.briefing
```

- [ ] **Step 2: Run tests — fail**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_debate_engine.py -v
```

预期：FAIL — module not found.

- [ ] **Step 3: Implement DebateEngine**

Write `paperreadagent/modules/ideator/debate_engine.py`:

```python
"""debate_engine.py — 8-seat multi-round debate review for S3."""
from __future__ import annotations
import asyncio, json, logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_DEBATE_ROUNDS = 5
DEBATE_SEATS = [
    ("gen",  "generator",   "deepseek-v4-pro"),
    ("rev1", "reviewer_1",  "deepseek-v4-pro"),
    ("rev2", "reviewer_2",  "deepseek-v4-pro"),
    ("rev3", "reviewer_3",  "deepseek-v4-pro"),
    ("rev4", "reviewer_4",  "deepseek-v4-flash"),
    ("rev5", "reviewer_5",  "deepseek-v4-flash"),
    ("arb1", "arbiter_1",   "deepseek-v4-pro"),
    ("arb2", "arbiter_2",   "deepseek-v4-pro"),
]

REVIEWER_FOCUS = {
    "rev1": "新颖性：评估该研究假设是否提出新的问题或新的方法组合",
    "rev2": "证据支撑：验证每条 claim 是否有来源文献支持，标记过度延伸的推论",
    "rev3": "可行性：评估实验设计是否可行，是否需要特殊设备或不可获取的数据",
    "rev4": "交叉验证：从不同学科视角审视，寻找遗漏的关联或应用场景",
    "rev5": "边界条件：挑战假设的适用范围和局限，提出反例或边界情况",
}


@dataclass
class ReviewResult:
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
    questions: list[dict]   # [{reviewer, content}]
    gen_response: dict       # {responses, revised_draft}


@dataclass
class DebateOutcome:
    verdict: str
    final_score: float
    reasoning: str
    debate_summary: str
    initial_reviews: list[ReviewResult]
    debate_rounds: list[DebateRound]
    re_reviews: list[ReviewResult]
    briefing: dict | None


def create_debate_seats():
    """返回 8 坐席列表（不含 Rec，Rec 由 DebateEngine 内部管理）。"""
    from paperreadagent.modules.ideator.agent_team import AgentSeat
    return [AgentSeat(sid, role, 0, []) for sid, role, _ in DEBATE_SEATS]


class DebateEngine:
    """8 坐席多轮辩论引擎。"""

    def __init__(self, *, llm, data_access):
        self._llm = llm  # IdeatorLLM adapter
        self._data = data_access

    async def run(self, spark_content: str, draft: str,
                  source_context: str) -> DebateOutcome:
        """执行完整辩论审查流程。"""

        # Phase 1: Initial reviews (parallel, blind)
        initial_tasks = [
            self._call_reviewer(seat_id, draft, source_context, "")
            for seat_id, _, _ in DEBATE_SEATS if seat_id.startswith("rev")
        ]
        initial_reviews = await asyncio.gather(*initial_tasks)
        initial_reviews = [r for r in initial_reviews if r is not None]

        # Phase 2: Debate rounds
        rounds: list[DebateRound] = []
        current_draft = draft
        all_questions: list[dict] = []
        for r in initial_reviews:
            for c in r.key_concerns:
                all_questions.append({"reviewer": r.reviewer, "content": c})

        for round_num in range(1, MAX_DEBATE_ROUNDS + 1):
            # Arbiter control
            decision = await self._call_arb_control(
                current_draft, initial_reviews, rounds, all_questions,
            )
            if decision.get("decision") == "STOP":
                break

            # Gen defends
            gen_resp = await self._call_gen_defend(
                spark_content, current_draft, source_context, all_questions,
            )
            dr = DebateRound(
                round_num=round_num,
                questions=all_questions,
                gen_response=gen_resp,
            )
            rounds.append(dr)

            if gen_resp.get("revised_draft"):
                current_draft = gen_resp["revised_draft"]

            # Gather new questions from revisers based on Gen's response
            new_qs = await self._gather_followup_questions(
                current_draft, gen_resp, rounds,
            )
            if not new_qs:
                break
            all_questions = new_qs

        # Phase 3: Re-reviews
        debate_log = self._format_debate_log(rounds)
        re_tasks = [
            self._call_reviewer(seat_id, current_draft, source_context, debate_log)
            for seat_id, _, _ in DEBATE_SEATS if seat_id.startswith("rev")
        ]
        re_reviews = await asyncio.gather(*re_tasks)
        re_reviews = [r for r in re_reviews if r is not None]

        # Phase 4: Final arbitration
        arb_result = await self._call_arb_final(
            current_draft, initial_reviews, rounds, re_reviews,
        )

        # Phase 5: Recorder briefing
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
        )

    async def _call_reviewer(self, seat_id, draft, sources, debate_so_far):
        focus = REVIEWER_FOCUS.get(seat_id, "全面审查")
        prompt = self._llm.load_prompt(
            "ideator", "debate_review",
            draft=draft, source_context=sources,
            debate_so_far=debate_so_far, reviewer_focus=focus,
        )
        try:
            raw = await self._llm.chat(
                model_role=seat_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3, response_format={"type": "json_object"},
            )
            data = json.loads(raw)
            return ReviewResult(
                scores=data.get("scores", {}),
                key_concerns=data.get("key_concerns", []),
                strengths=data.get("strengths", []),
                verdict=data.get("verdict", "PASS"),
                reasoning=data.get("reasoning", ""),
                reviewer=seat_id,
            )
        except Exception:
            logger.debug(f"Reviewer {seat_id} failed", exc_info=True)
            return None

    async def _call_arb_control(self, draft, initial_reviews, rounds, questions):
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
            raw = await self._llm.chat(
                model_role="arb1",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2, response_format={"type": "json_object"},
            )
            return json.loads(raw)
        except Exception:
            return {"decision": "STOP"}

    # (Additional helper methods: _call_gen_defend, _gather_followup_questions,
    #  _call_arb_final, _call_rec_briefing, _format_debate_log follow same pattern)

    def _format_debate_log(self, rounds: list[DebateRound]) -> str:
        if not rounds:
            return "（无辩论记录）"
        lines = []
        for r in rounds:
            lines.append(f"## 第 {r.round_num} 轮")
            for q in r.questions:
                lines.append(f"[{q['reviewer']}]: {q['content']}")
            lines.append(f"[Gen]: {json.dumps(r.gen_response, ensure_ascii=False)}")
        return "\n\n".join(lines)
```

(Full implementation includes `_call_gen_defend`, `_gather_followup_questions`, `_call_arb_final`, `_call_rec_briefing` — all following the same LLM call pattern with prompt loading and JSON parsing.)

- [ ] **Step 4: Run tests — pass**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_debate_engine.py -v
```

预期：3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/debate_engine.py paperreadagent/modules/ideator/tests/test_debate_engine.py
git commit -m "feat(ideator): add DebateEngine — 8-seat multi-round debate review"
```

---

### Task 3: 管道重排

**Files:**
- Modify: `paperreadagent/modules/ideator/pipeline.py`

S2 之后插入深阶段（生成完整草稿），然后走辩论审查（替代旧 S3/S5）。

S2 火花生成后：
1. 对每个火花调用 deepen LLM 生成草稿（类现在 S5 的 deepen 单轮逻辑）
2. 调用 DebateEngine.run() 做多轮辩论审查
3. 拿到 DebateOutcome → 提取 verdict/final_score/briefing → 存入 spark

旧 S3 `_review_sparks`、旧 S5 `_auto_deepen_sparks` 全部删除。新流程 `S2 → S5_deepen_draft → S3_debate_review → S4_save`。

保留：S0、S1、S2、S4、S6。移除：effort 相关导入（已被 beast 替代）、`_build_effort_context`。

- [ ] **Step 1: Commit**

```bash
git add paperreadagent/modules/ideator/pipeline.py
git commit -m "feat(pipeline): reorder stages — deepen before debate review"
```

---

### Task 4: 退役 effort.py + 清理

- 删除 `effort.py` 中 `auto_effort` 和 `EffortContext` 相关代码
- 保留 `EFFORT_PARAMS`（beast 配置仍被引用）
- 清理 pipeline.py 中 `auto_effort` 和 `EffortContext` 的导入

- [ ] **Step 1: Commit**

```bash
git add paperreadagent/modules/ideator/effort.py paperreadagent/modules/ideator/pipeline.py
git commit -m "refactor(ideator): retire effort auto-detection — always beast"
```

---

### Task 5: 火花详情 API + source_titles

**Files:**
- Modify: `paperreadagent/modules/ideator/routes.py`

新增 `GET /api/sparks/{spark_id}` 端点，返回单个火花完整详情（含 briefing、debate_context、depth_content、review_records、source_titles）。

修改 `GET /api/sparks` 返回的每个火花附带 `source_titles`。

- [ ] **Step 1: Commit**

```bash
git add paperreadagent/modules/ideator/routes.py
git commit -m "feat(routes): add spark detail endpoint + source_titles on list"
```

---

### Task 6: 前端火花详情

**Files:**
- Modify: `paperreadagent/modules/ideator/static/ideator.js`
- Modify: `paperreadagent/modules/ideator/static/ideator.css`

火花卡新增：来源文献链接、📋 简报折叠区。点击火花卡展开详情面板（内嵌在列表中），显示 S3 简报、研究草稿、辩论记录、圆桌简报。所有内容折叠状态，点击展开。

CSS 新增：简报卡片样式（黄色左边框）、辩论记录折叠样式。

- [ ] **Step 1: Commit**

```bash
git add paperreadagent/modules/ideator/static/ideator.js paperreadagent/modules/ideator/static/ideator.css
git commit -m "feat(frontend): spark detail panel — sources, briefing, debate records, draft"
```

---

### Task 7: 全量测试验证

- [ ] **Step 1: Run all tests**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v
uv run python -m pytest paperreadagent/tests/ -v
```

预期：全部 PASS。
