"""agent_team.py — Agent Team: 6 identity-secret seats, mesh communication, open floor."""

from __future__ import annotations
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

_MAX_INTERJECTION_CHARS = 150
ROLES = ["gen", "rev1", "rev2", "rev3", "arb1", "arb2"]
_ROLE_LABELS = {"gen":"生成者","rev1":"审查α","rev2":"审查β","rev3":"审查γ","arb1":"仲裁α","arb2":"仲裁β"}


@dataclass
class AgentSeat:
    seat_id: str
    role: str
    quota: int
    tools: list[str]
    remaining_quota: int = 0
    state: str = "online"

    def __post_init__(self):
        self.remaining_quota = self.quota

    def consume_quota(self, chars: int) -> None:
        self.remaining_quota = max(0, self.remaining_quota - chars)

    def quota_exhausted(self) -> bool:
        return self.remaining_quota <= 0

    def reset_quota(self) -> None:
        self.remaining_quota = self.quota


def create_default_seats() -> list[AgentSeat]:
    """工厂方法：6 坐席默认配置，Arb1/Arb2 工具对等。"""
    return [
        AgentSeat("gen",  "generator",   2000, [
            "search_papers", "read_paper", "read_note",
            "create_spark", "update_spark", "check_duplicate",
            "trigger_recall", "fetch_snapshot", "read_memory",
        ]),
        AgentSeat("rev1", "reviewer_1",   800, [
            "search_papers", "read_paper", "fetch_snapshot", "read_memory",
        ]),
        AgentSeat("rev2", "reviewer_2",   800, [
            "search_papers", "read_paper", "audit_claim",
            "fetch_snapshot", "read_memory",
        ]),
        AgentSeat("rev3", "reviewer_3",   800, [
            "search_papers", "read_paper", "read_note",
            "fetch_snapshot", "read_memory",
        ]),
        AgentSeat("arb1", "arbiter_1",    500, [
            "write_memory", "read_memory", "fetch_snapshot",
            "report_watermark", "adjust_quota", "grant_tool",
        ]),
        AgentSeat("arb2", "arbiter_2",    500, [
            "write_memory", "read_memory", "fetch_snapshot",
            "report_watermark", "adjust_quota", "grant_tool",
        ]),
    ]


class AgentTeam:
    """Agent Team roundtable — replaces RoundtableSession orchestration."""

    def __init__(self, *, spark_id, spark_content, seats, llm,
                 team_memory, graduation, arbiter, tool_registry,
                 source_context: str = ""):
        self.spark_id = spark_id
        self.spark_content = spark_content
        self.seats: dict[str, AgentSeat] = {s.seat_id: s for s in seats}
        self._llm = llm  # IdeatorLLM adapter
        self._memory = team_memory
        self._graduation = graduation
        self._arbiter = arbiter
        self._tool_registry = tool_registry
        self._source_context = source_context
        self.round_number = 0
        self.messages: list[dict] = []
        self._warm_context: str = ""

    _MAX_MESSAGES = 500

    def _record_message(self, **kwargs) -> dict:
        for field in ("metadata", "mentioned_by"):
            if field in kwargs and isinstance(kwargs[field], (dict, list)):
                kwargs[field] = json.dumps(kwargs[field], ensure_ascii=False)
        msg = {**kwargs, "round_number": self.round_number,
               "created_at": datetime.now().isoformat()}
        self.messages.append(msg)
        if len(self.messages) > self._MAX_MESSAGES:
            self.messages = self.messages[-self._MAX_MESSAGES:]
        return msg

    async def start_round(self, *, question: str, mentioned: list[str]) -> list[dict]:
        self.round_number += 1
        results = []

        hot_pct = self._graduation.layers["hot"].pct if self._graduation else 40.0
        warm_pct = self._graduation.layers["warm"].pct if self._graduation else 30.0
        if self._arbiter:
            quotas = self._arbiter.calculate_round_quotas(hot_pct, warm_pct)
        else:
            quotas = {"gen": 2000, "rev1": 800, "rev2": 800, "rev3": 800, "arb1": 500, "arb2": 500}

        for seat_id, seat in self.seats.items():
            seat.quota = quotas.get(seat_id, 800)
            seat.reset_quota()

        user_msg = self._record_message(
            sender_type="user", sender_name="user", sender_role=None,
            message_type="question", content=question,
            mentioned_by=mentioned,
        )
        results.append(user_msg)

        wants_all = "all" in mentioned

        # ── ① gen 单独回答 ────────────────────────────────
        gen_seat = self.seats.get("gen")
        gen_answer = None
        if gen_seat and gen_seat.state == "online" and (wants_all or "gen" in mentioned):
            gen_answer = await self._agent_speak(gen_seat, question, mentioned)
            if gen_answer:
                results.append(gen_answer)

        # ── ② rev 并行回答（可看到 gen 本轮回答）─────────
        rev_tasks = []
        for sid in ("rev1", "rev2", "rev3"):
            seat = self.seats.get(sid)
            if seat and seat.state == "online" and (wants_all or sid in mentioned):
                rev_tasks.append(self._agent_speak(
                    seat, question, mentioned,
                    round_context=self._build_round_context(gen_answer, None),
                ))
        if rev_tasks:
            rev_answers = await asyncio.gather(*rev_tasks, return_exceptions=True)
            for ans in rev_answers:
                if isinstance(ans, dict) and ans:
                    results.append(ans)

        # ── ③ arb 并行回答（可看到 gen + rev 本轮回答）───
        arb_tasks = []
        for sid in ("arb1", "arb2"):
            seat = self.seats.get(sid)
            if seat and seat.state == "online" and (wants_all or sid in mentioned):
                arb_tasks.append(self._agent_speak(
                    seat, question, mentioned,
                    round_context=self._build_round_context(gen_answer, results),
                ))
        if arb_tasks:
            arb_answers = await asyncio.gather(*arb_tasks, return_exceptions=True)
            for ans in arb_answers:
                if isinstance(ans, dict) and ans:
                    results.append(ans)

        # ── 插话 + 分歧分析 ────────────────────────────────
        interjections = await self._collect_interjections(mentioned, question)
        results.extend(interjections)

        div = await self._divergence_scan()
        if div:
            results.append(div)

        return results

    def _build_round_context(self, gen_answer: dict | None, all_answers: list[dict] | None) -> str:
        """构建本轮上文供 rev/arb 看到 gen 或 gen+rev 的回复。"""
        parts = []
        if gen_answer and gen_answer.get("content"):
            parts.append(f"[本轮 生成者 的回答]\n{gen_answer['content']}")
        if all_answers:
            rev_msgs = [m for m in all_answers
                        if m.get("sender_name") in ("rev1", "rev2", "rev3")]
            for m in rev_msgs:
                name = _ROLE_LABELS.get(m.get("sender_name", ""), m.get("sender_name", "?"))
                if m.get("content"):
                    parts.append(f"[本轮 {name} 的回答]\n{m['content']}")
        return "\n\n".join(parts)

    async def _agent_speak(self, seat: AgentSeat, question: str, mentioned: list[str],
                           round_context: str = "") -> dict | None:
        if seat.quota_exhausted():
            return None

        system_prompt = self._build_agent_system_prompt(seat)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_agent_user_prompt(seat, question, mentioned, round_context)},
        ]

        try:
            raw = await self._llm.chat(
                model_role=seat.role,
                messages=messages,
                temperature=0.7,
                max_tokens=32768,
            )
        except Exception:
            logger.warning("[AgentTeam] _agent_speak failed for %s", seat.seat_id, exc_info=True)
            return self._record_message(
                sender_type="system", sender_name="system",
                sender_role=None, message_type="answer",
                content=f"[{_ROLE_LABELS.get(seat.seat_id, seat.seat_id)} 暂时无法回应，请稍后重试]",
            )

        seat.consume_quota(len(raw))

        return self._record_message(
            sender_type="model", sender_name=seat.seat_id,
            sender_role=seat.role, message_type="answer",
            content=raw, metadata={"tokens": len(raw) // 2},
        )

    def _build_agent_system_prompt(self, seat: AgentSeat) -> str:
        identity_prompt = self._llm.load_prompt(
            "ideator", f"agent_identity_{seat.seat_id}",
            tools_list=self._format_tools_for_seat(seat),
        )
        memory_text = self._memory.format_for_context(self.spark_id) if self._memory else ""
        source_block = f"\n\n---\n来源上下文:\n{self._source_context}" if self._source_context else ""
        return f"{identity_prompt}\n\n---\n火花内容:\n{self.spark_content}{source_block}\n\n---\n团队记忆:\n{memory_text}"

    def _build_agent_user_prompt(self, seat: AgentSeat, question: str, mentioned: list[str],
                                  round_context: str = "") -> str:
        parts = [f"当前讨论问题: {question}"]
        if round_context:
            parts.append(f"本轮上文:\n{round_context}")
        if self._warm_context:
            parts.append(f"历史讨论摘要: {self._warm_context}")
        parts.append(self._format_recent_history())
        return "\n\n".join(parts)

    def _format_tools_for_seat(self, seat: AgentSeat) -> str:
        if not self._tool_registry:
            return "无可用工具"
        tools = self._tool_registry.list_for_role(seat.seat_id)
        if not tools:
            return "无可用工具"
        return "\n".join(f"- {t['name']}: {t['description']}" for t in tools)

    def _format_recent_history(self) -> str:
        if not self.messages:
            return "暂无讨论历史"
        lines = []
        for m in self.messages:
            sender = m.get("sender_name", "unknown")
            content = m.get("content", "")
            lines.append(f"[{sender}] {content}")
        return "\n\n".join(lines)

    async def _collect_interjections(self, mentioned: list[str], question: str) -> list[dict]:
        async def _interject(seat):
            try:
                prompt = (
                    f"本轮讨论问题: {question}\n\n"
                    f"火花内容: {self.spark_content}\n\n"
                    f"如果你有重要补充请发言（限{_MAX_INTERJECTION_CHARS}字以内，直接说）："
                )
                raw = await self._llm.chat(
                    model_role=seat.role,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.5, max_tokens=2048,
                )
                content = raw[:_MAX_INTERJECTION_CHARS]
                return self._record_message(
                    sender_type="model", sender_name=seat.seat_id,
                    sender_role=seat.role, message_type="interjection",
                    content=content,
                )
            except Exception:
                logger.warning(f"Interjection failed for {seat.seat_id}", exc_info=True)
                return None

        tasks = []
        for seat in self.seats.values():
            if seat.state != "online":
                continue
            if seat.seat_id in mentioned or "all" in mentioned:
                continue
            tasks.append(_interject(seat))

        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in gathered if isinstance(r, dict) and r]

    async def _divergence_scan(self) -> dict | None:
        round_msgs = [m for m in self.messages if m.get("round_number") == self.round_number]
        model_msgs = [m for m in round_msgs if m["sender_type"] == "model"]
        if len(model_msgs) < 2:
            return None
        prompt = self._llm.load_prompt("ideator", "divergence_scan",
            round_messages=json.dumps(round_msgs, ensure_ascii=False))
        raw = await self._llm.chat(
            model_role="reviewer_1",
            messages=[{"role":"user","content":prompt}],
            temperature=0.2,
            max_tokens=4096,
        )
        from paperreadagent.utils.json_utils import clean_json
        raw = clean_json(raw)
        try:
            data = json.loads(raw)
            verdict = data.get("verdict", "")
            reasoning = data.get("reasoning", "")
            disagreements = data.get("key_disagreements", [])
            if not verdict and not reasoning and not disagreements:
                return None  # 无实质分歧，不产生报告
            parts = [f"分歧判定：{verdict}"] if verdict else []
            if reasoning:
                parts.append(reasoning)
            if disagreements:
                parts.append("主要分歧点：" + "；".join(disagreements))
            formatted = "\n\n".join(parts)
        except (json.JSONDecodeError, AttributeError):
            formatted = raw[:500] if raw else None
            if not formatted:
                return None

        return self._record_message(
            sender_type="system", sender_name="system", sender_role=None,
            message_type="divergence_report", content=formatted,
        )

    async def execute_graduation_cycle(self, *, roundtable_id: int) -> dict:
        if not self._arbiter:
            return {"verdict": "no_arbiter"}
        round_content = self._format_recent_history()
        existing = self._memory.format_for_context(self.spark_id) if self._memory else ""

        decision = await self._arbiter.execute_graduation(
            roundtable_id=roundtable_id,
            spark_id=self.spark_id,
            round_number=self.round_number,
            round_content=round_content,
            existing_memories=existing,
        )

        self._warm_context = decision.get("warm_summary", self._warm_context or "")
        return decision


class AgentTeamManager:
    """管理多个 AgentTeam 实例，替代 RoundtableManager。"""

    def __init__(self, *, llm, data_access,
                 tool_registry, team_memory, graduation, arbiter):
        self._llm = llm
        self._data = data_access
        self._tool_registry = tool_registry
        self._team_memory = team_memory
        self._graduation = graduation
        self._arbiter = arbiter
        self._teams: dict[int, AgentTeam] = {}

    def create_team(self, *, spark_id: int, spark_content: str,
                    source_refs: list[dict] | None = None,
                    spark_content_override: str | None = None) -> int:
        """创建 AgentTeam 并分配 roundtable_id。

        直接圆桌模式：spark_content_override 传入用户研究内容，
        spark_id 传 0（无火花关联），source_refs 为空。
        """
        refs = source_refs or []
        rt_id = self._data.insert_roundtable(spark_id=spark_id)
        source_context = self._resolve_source_context(spark_id, refs) if spark_id else ""
        if self._arbiter:
            self._arbiter.reset_for_new_team()

        # 每个圆桌独立 GraduationManager，避免跨圆桌水位污染
        from .graduation import GraduationManager
        team_graduation = GraduationManager(self._data._core.db.conn, self._team_memory)

        seats = create_default_seats()
        team = AgentTeam(
            spark_id=spark_id,
            spark_content=spark_content_override or spark_content,
            seats=seats,
            llm=self._llm,
            team_memory=self._team_memory,
            graduation=team_graduation,
            arbiter=self._arbiter,
            tool_registry=self._tool_registry,
            source_context=source_context,
        )
        self._teams[rt_id] = team
        return rt_id

    def get_team(self, rt_id: int) -> AgentTeam | None:
        return self._teams.get(rt_id)

    def pause_team(self, rt_id: int) -> None:
        self._data.update_roundtable(rt_id, status="paused")

    def close_team(self, rt_id: int) -> None:
        team = self._teams.pop(rt_id, None)
        if team:
            from datetime import datetime
            self._data.update_roundtable(rt_id, status="closed",
                                          closed_at=datetime.now().isoformat())

    def _resolve_source_context(self, spark_id: int,
                                 source_refs: list[dict]) -> str:
        """加载 spark 的来源原文、审查记录、深化内容。"""
        parts = []
        for ref in (source_refs or []):
            ref_type = ref.get("type", "")
            ref_id = ref.get("id", 0)
            try:
                if ref_type == "paper":
                    paper = self._data.get_paper(ref_id)
                    if paper:
                        parts.append(
                            f"## 论文: {paper.get('title', '')}\n"
                            f"{paper.get('abstract', '')}"
                        )
                        note = self._data.get_user_note(ref_id)
                        if note and note.get("content"):
                            parts.append(f"笔记: {note['content']}")
                elif ref_type == "core_note":
                    note = self._data._core.knowledge.get_note(ref_id)
                    if note:
                        parts.append(f"## 笔记: {note.get('content', '')}")
            except Exception:
                logger.warning("[AgentTeam] source resolution failed", exc_info=True)

        try:
            spark = self._data.get_spark(spark_id)
            if spark:
                if spark.get("review_status"):
                    parts.append(
                        f"审查状态: {spark.get('review_status')} "
                        f"分数: {spark.get('final_score', 'N/A')}"
                    )
                if spark.get("depth_content"):
                    parts.append(f"## 深化内容\n{spark['depth_content']}")
        except Exception:
            logger.warning("[AgentTeam] spark detail lookup failed for spark_id=%s", spark_id, exc_info=True)

        return "\n\n".join(parts)
