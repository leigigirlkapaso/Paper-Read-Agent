"""
modules/ideator/roundtable.py
RoundtableManager + RoundtableSession — 6 模型圆桌讨论引擎。
管理多轮对话、token 追踪、自压缩、强制退场、分歧分析。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader as JFSL
from paperreadagent.utils.json_utils import clean_json

logger = logging.getLogger(__name__)

_JINJA_ENV = Environment(
    loader=JFSL(Path(__file__).parent / "prompts"),
    autoescape=False,
)

# ── 坐席定义 ──────────────────────────────────────────────

SEATS = [
    {"seat_id": "gen",  "model": "deepseek-v4-pro",        "role": "generator",    "token_limit": 1_000_000},
    {"seat_id": "rev3", "model": "deepseek-v4-pro",        "role": "reviewer_3",   "token_limit": 1_000_000},
    {"seat_id": "rev1", "model": "gemini-3-flash-preview", "role": "reviewer_1",   "token_limit": None},
    {"seat_id": "rev2", "model": "qwen3.6-plus",           "role": "reviewer_2",   "token_limit": None},
    {"seat_id": "arb1", "model": "claude-opus-4-7-max",    "role": "arbiter_1",    "token_limit": 1_000_000},
    {"seat_id": "arb2", "model": "gpt-5.5-2026-04-24",     "role": "arbiter_2",    "token_limit": None},
]

ROLE_DESCRIPTIONS = {
    "generator":  "你是这个研究火花的创造者。你了解火花的来龙去脉，可以辩护你的推理，也可以承认错误。",
    "reviewer_1": "你是独立审查者。从新颖性、证据支撑度、可行性三个维度评估火花。",
    "reviewer_2": "你是独立审查者+审计者。侧重验证火花的 claim 是否被源文本支撑。",
    "reviewer_3": "你是独立审查者。你与生成者使用同一模型但独立实例，不带创作偏见。",
    "arbiter_1":  "你是资深仲裁者。在审查者产生分歧时给出终裁决。你是圆桌中除用户外最高权力的角色。",
    "arbiter_2":  "你是资深仲裁者+深化者。侧重深度扩展和高价值方向探索。",
}

CONTEXT_SPEC = {
    "generator":   ["papers", "reports", "notes", "reviews", "deepen", "self_score"],
    "reviewer_1":  ["papers", "reports", "notes", "reviews", "deepen"],
    "reviewer_2":  ["papers", "reports", "notes", "reviews", "deepen"],
    "reviewer_3":  ["papers", "reports", "notes", "reviews", "deepen"],
    "arbiter_1":   ["reviews", "deepen"],
    "arbiter_2":   ["reviews", "deepen"],
}

_INTERJECTION_MAX_CHARS = 150


# ── Token Tracker ─────────────────────────────────────────

class TokenTracker:
    def __init__(self, limit: int | None):
        self.limit = limit or 128000
        self.used = 0
        self.compression_count = 0

    @property
    def pct_used(self) -> float:
        return self.used / self.limit if self.limit > 0 else 0.0

    def consume(self, tokens: int) -> None:
        self.used += tokens

    def needs_compression(self) -> bool:
        return self.pct_used >= 0.50

    def needs_warning(self) -> bool:
        return self.pct_used >= 0.85

    def is_exhausted(self) -> bool:
        return self.pct_used >= 1.0


# ── Roundtable Session ────────────────────────────────────

class RoundtableSession:
    def __init__(self, *, spark_id, spark_content, llm, data_access, context_bundles):
        self.spark_id = spark_id
        self.spark_content = spark_content
        self._llm = llm
        self._data = data_access
        self.round_number = 0
        self.messages: list[dict] = []
        self.participants = self._init_participants(context_bundles)

    def _init_participants(self, bundles):
        participants = []
        for seat in SEATS:
            role = seat["role"]
            ctx_keys = CONTEXT_SPEC.get(role, [])
            ctx = {k: bundles.get(role, {}).get(k) for k in ctx_keys if bundles.get(role, {}).get(k)}
            participants.append({
                **seat,
                "context": ctx,
                "token_tracker": TokenTracker(seat["token_limit"]),
                "state": "online",
                "can_interject": True,
            })
        return participants

    def _find_participant(self, seat_id: str):
        for p in self.participants:
            if p["seat_id"] == seat_id:
                return p
        return None

    # ── 核心：执行一轮对话 ──────────────────────────────

    async def ask_round(self, *, question: str, mentioned: list[str]) -> list[dict]:
        self.round_number += 1
        results = []

        # 1. 记录用户提问
        self._record_message(
            sender_type="user", sender_name="user", sender_role=None,
            message_type="question", content=question,
            mentioned_by=mentioned,
        )

        # 2. 被指名模型并行回答
        tasks = []
        for p in self.participants:
            if p["state"] != "online":
                continue
            if p["seat_id"] in mentioned or "all" in mentioned:
                tasks.append(self._model_answer(p, question, mentioned))
        answers = await asyncio.gather(*tasks, return_exceptions=True)
        for ans in answers:
            if isinstance(ans, list):
                results.extend(ans)
            elif isinstance(ans, dict) and ans:
                results.append(ans)

        # 3. 未指名模型插话
        interjections = await self._collect_interjections(mentioned, question)
        results.extend(interjections)

        # 4. 分歧分析
        div = await self._divergence_scan()
        if div:
            results.append(div)

        return results

    async def _model_answer(self, participant, question, mentioned):
        results = []
        if participant["token_tracker"].needs_compression():
            comp_msg = await self._compress(participant)
            if comp_msg:
                results.append(comp_msg)
        if participant["token_tracker"].is_exhausted():
            exit_msg = await self._force_exit(participant, "token_exhausted")
            if exit_msg:
                results.append(exit_msg)
            return results

        messages = self._build_messages(participant, question, mentioned)
        raw = await self._llm.chat(
            model_role=participant["role"],
            messages=messages,
            temperature=0.7,
        )
        tokens = self._estimate_tokens(messages, raw)
        participant["token_tracker"].consume(tokens)

        results.append(self._record_message(
            sender_type="model", sender_name=participant["model"],
            sender_role=participant["seat_id"], message_type="answer",
            content=raw, metadata={"tokens": tokens},
        ))
        return results

    async def _collect_interjections(self, mentioned: list[str], question: str) -> list[dict]:
        async def _interject(p):
            try:
                content = f"本轮讨论问题：{question}\n\n火花内容：{self.spark_content}\n\n如果你有重要补充请发言（限{_INTERJECTION_MAX_CHARS}字以内，直接说）："
                raw = await self._llm.chat(
                    model_role=p["role"],
                    messages=[{"role": "user", "content": content}],
                    temperature=0.5, max_tokens=2048,
                )
                content = raw[:_INTERJECTION_MAX_CHARS]
                return self._record_message(
                    sender_type="model", sender_name=p["model"],
                    sender_role=p["seat_id"], message_type="interjection",
                    content=content,
                )
            except Exception:
                logger.warning(f"[Roundtable] interjection failed for {p['seat_id']}", exc_info=True)
                return None

        tasks = []
        for p in self.participants:
            if p["state"] != "online" or not p.get("can_interject"):
                continue
            if p["seat_id"] in mentioned or "all" in mentioned:
                continue
            tasks.append(_interject(p))
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in gathered if isinstance(r, dict) and r]

    # ── 强制退场 ──────────────────────────────────────────

    async def force_remove(self, seat_id: str, reason: str = "user_forced") -> dict | None:
        p = self._find_participant(seat_id)
        if not p:
            return None
        p["state"] = "exited"
        prompt = self._render("exit_statement",
            exit_reason=reason, model_name=p["model"],
            model_role=p["role"], history=self._format_history(),
        )
        raw = await self._llm.chat(
            model_role=p["role"],
            messages=[{"role":"user","content":prompt}],
            temperature=0.5,
        )
        return self._record_message(
            sender_type="model", sender_name=p["model"],
            sender_role=p["seat_id"], message_type="exit_statement",
            content=raw,
        )

    async def _force_exit(self, participant, reason: str) -> dict:
        return await self.force_remove(participant["seat_id"], reason)

    # ── 自压缩 ────────────────────────────────────────────

    async def _compress(self, participant):
        prompt = self._render("compress_history", history=self._format_history())
        summary = await self._llm.chat(
            model_role=participant["role"],
            messages=[{"role":"user","content":prompt}],
            temperature=0.3,
        )
        participant["token_tracker"].compression_count += 1
        participant["token_tracker"].used = max(
            self._estimate_tokens_text(summary),
            int(participant["token_tracker"].limit * 0.15),  # at least 15%
        )
        return self._record_message(
            sender_type="system", sender_name="system", sender_role=None,
            message_type="compression",
            content=f"{participant['model']}（{participant['seat_id']}）第 {participant['token_tracker'].compression_count} 次自动压缩上下文",
        )

    # ── 分歧分析 ──────────────────────────────────────────

    async def _divergence_scan(self) -> dict | None:
        round_msgs = [m for m in self.messages if m.get("round_number") == self.round_number]
        model_msgs = [m for m in round_msgs if m["sender_type"] == "model" and m["message_type"] in ("answer","interjection")]
        if len(model_msgs) < 2:
            return None
        prompt = self._render("divergence_scan", round_messages=round_msgs)
        raw = await self._llm.chat(
            model_role="reviewer_1",
            messages=[{"role":"user","content":prompt}],
            temperature=0.2,
            max_tokens=4096,
        )
        # Parse JSON and format as human-readable text
        try:
            data = json.loads(clean_json(raw))
            verdict = data.get("verdict", "")
            reasoning = data.get("reasoning", "")
            disagreements = data.get("key_disagreements", [])
            formatted = f"分歧分析：{verdict}\n\n{reasoning}\n\n主要分歧点：{', '.join(str(d) for d in disagreements)}"
        except (json.JSONDecodeError, AttributeError):
            formatted = raw
        return self._record_message(
            sender_type="system", sender_name="system", sender_role=None,
            message_type="divergence_report", content=formatted,
        )

    # ── Helpers ────────────────────────────────────────────

    def _record_message(self, **kwargs) -> dict:
        # Serialize dict/list values for DB storage
        for field in ("metadata", "mentioned_by"):
            if field in kwargs and isinstance(kwargs[field], (dict, list)):
                kwargs[field] = json.dumps(kwargs[field], ensure_ascii=False)
        msg = {**kwargs, "round_number": self.round_number,
               "created_at": datetime.now().isoformat()}
        self.messages.append(msg)
        return msg

    def _build_messages(self, participant, question, mentioned) -> list[dict]:
        ctx = participant["context"]
        system = self._render("roundtable_model_answer",
            model_name=participant["model"],
            model_role=participant["seat_id"],
            role_description=ROLE_DESCRIPTIONS.get(participant["role"], ""),
            spark_content=self.spark_content,
            context_papers=ctx.get("papers"),
            context_reports=ctx.get("reports"),
            context_notes=ctx.get("notes"),
            context_reviews=ctx.get("reviews"),
            context_deepen=ctx.get("deepen"),
            discussion_history=self._format_history(),
            question=question,
            mentioned_by_str=", ".join(mentioned),
        )
        return [{"role": "system", "content": system}]

    def _render(self, template_name: str, **vars) -> str:
        tpl = _JINJA_ENV.get_template(f"{template_name}.jinja2")
        return tpl.render(**vars)

    def _format_history(self) -> str:
        if not self.messages:
            return "暂无历史（首轮讨论）"
        lines = []
        for m in self.messages[-20:]:  # last 20 messages
            role = f"{m['sender_name']}({m.get('sender_role','')})" if m["sender_type"] != "user" else "用户"
            lines.append(f"[{role}] {m['content']}")
        return "\n\n".join(lines)

    def _estimate_tokens(self, messages: list[dict], response: str) -> int:
        chars = sum(len(m.get("content", "")) for m in messages) + len(response)
        return max(chars // 3, 1)

    def _estimate_tokens_text(self, text: str) -> int:
        return max(len(text) // 3, 1)


# ── Roundtable Manager ────────────────────────────────────

class RoundtableManager:
    def __init__(self, *, llm, data_access):
        self._llm = llm
        self._data = data_access
        self._sessions: dict[int, RoundtableSession] = {}

    def start(self, *, spark_id: int, spark_content: str, source_refs: list[dict]) -> int:
        bundles = self._assemble_contexts(spark_id, source_refs)
        rt_id = self._data.insert_roundtable(spark_id=spark_id)
        session = RoundtableSession(
            spark_id=spark_id, spark_content=spark_content,
            llm=self._llm, data_access=self._data, context_bundles=bundles,
        )
        self._sessions[rt_id] = session
        return rt_id

    def get_session(self, rt_id: int) -> RoundtableSession | None:
        return self._sessions.get(rt_id)

    def pause(self, rt_id: int) -> None:
        self._data.update_roundtable(rt_id, status="paused")

    def resume(self, rt_id: int) -> None:
        self._data.update_roundtable(rt_id, status="active")

    def close(self, rt_id: int) -> None:
        self._data.update_roundtable(rt_id, status="closed", closed_at=datetime.now().isoformat())

    def _assemble_contexts(self, spark_id: int, source_refs: list[dict]) -> dict:
        papers_text = self._load_papers(source_refs)
        reports_text = self._load_reports(source_refs)
        notes_text = self._load_notes(source_refs)
        reviews_text = self._load_reviews(spark_id)
        deepen_text = self._load_deepen(spark_id)
        self_score = self._load_self_score(spark_id)

        gen = {"papers": papers_text, "reports": reports_text, "notes": notes_text,
               "reviews": reviews_text, "deepen": deepen_text, "self_score": self_score}
        rev = {"papers": papers_text, "reports": reports_text, "notes": notes_text,
               "reviews": reviews_text, "deepen": deepen_text}
        arb = {"reviews": reviews_text, "deepen": deepen_text}

        return {"generator": gen, "reviewer_1": rev, "reviewer_2": rev,
                "reviewer_3": rev, "arbiter_1": arb, "arbiter_2": arb}

    def _load_papers(self, source_refs: list[dict]) -> str:
        texts = []
        for ref in (source_refs or []):
            if ref.get("type") == "paper":
                p = self._data.get_paper(ref["id"])
                if p:
                    texts.append(f"# {p.get('title','')}\n{p.get('abstract','')}")
        return "\n\n".join(texts) if texts else ""

    def _load_reports(self, source_refs: list[dict]) -> str:
        texts = []
        for ref in (source_refs or []):
            if ref.get("type") == "paper":
                summaries = self._data.get_paper_summaries(ref["id"])
                for s in (summaries or []):
                    texts.append(s.get("content", ""))
        return "\n\n".join(texts[:3]) if texts else ""

    def _load_notes(self, source_refs: list[dict]) -> str:
        texts = []
        for ref in (source_refs or []):
            if ref.get("type") == "paper":
                note = self._data.get_user_note(ref["id"])
                if note:
                    texts.append(note.get("content", ""))
        return "\n\n".join(texts) if texts else ""

    def _load_reviews(self, spark_id: int) -> str:
        try:
            spark = self._data.get_spark(spark_id)
            if spark and spark.get("review_status"):
                return f"审查状态: {spark.get('review_status')}\n最终分数: {spark.get('final_score', 'N/A')}"
        except Exception:
            logger.warning("[Roundtable] _load_reviews failed", exc_info=True)
        return ""

    def _load_deepen(self, spark_id: int) -> str:
        try:
            spark = self._data.get_spark(spark_id)
            if spark and spark.get("depth_content"):
                return spark["depth_content"]
        except Exception:
            logger.warning("[Roundtable] _load_deepen failed", exc_info=True)
        return ""

    def _load_self_score(self, spark_id: int) -> float:
        try:
            spark = self._data.get_spark(spark_id)
            return spark.get("generator_score", 0.0) if spark else 0.0
        except Exception:
            return 0.0
