"""
modules/ideator/pipeline.py
IdeatorPipeline — 多阶段管道：召回→评分→火花生成→交叉审查→去重入库→深化→审计。

S0: CrossRecall     — 6 路交叉召回
S1: Link Scoring    — 批量评分过滤
S2: Spark Generate  — LLM 生成火花候选
S3: Review          — 双模型交叉审查 + Tier 3 仲裁
S4: Dedup & Save    — 去重入库 + core_notes 同步
S5: Deepen          — 迭代深化 + 审查反馈循环
S6: Audit           — 独立模型溯源审计
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from paperreadagent.core import Core
from .data_access import DataAccess
from .cross_recall import CrossRecall
from .idea_extractor import IdeaExtractor
from .spark_store import SparkStore
from .reviewer import SparkReviewer, ReviewResult, ArbitrationResult
from .auditor import SparkAuditor, AuditResult
from .effort import EFFORT_PARAMS
from .state import PipelineState, save_state, load_state

logger = logging.getLogger(__name__)


class IdeatorPipeline:
    """ideator 挖掘管道。三种触发模式：全量、增量、定点。

    v2 升级：集成跨模型交叉审查、Tier 3 仲裁、迭代深化、溯源审计。
    """

    def __init__(self, core: Core, data: DataAccess):
        self.core = core
        self.data = data

        # ── 跨模型审查组件（统一走 core.llm → deepseek-v4-pro）──
        from .ideator_llm import IdeatorLLM
        ideator_cfg = core.module_config("ideator")
        self.ideator_llm = IdeatorLLM(core_llm=core.llm)

        # ── Idea 提取器（flash 模型） ─────────────────────
        self.idea_extractor = IdeaExtractor(
            llm=self.ideator_llm, core_llm=core.llm, data=data,
        )

        self.recall = CrossRecall(data, idea_extractor=self.idea_extractor)
        self.store = SparkStore(data)
        self.reviewer = SparkReviewer(
            llm=self.ideator_llm,
            arbitration_cfg=ideator_cfg.get("arbitration", {}),
        )
        self.auditor = SparkAuditor(llm=self.ideator_llm)

        from .debate_engine import DebateEngine
        self.debate_engine = DebateEngine(llm=self.ideator_llm, data_access=self.data)

        # ── 状态持久化目录 ─────────────────────────────────
        self._state_dir = Path(".aris/ideator")

    # ═══════════════════════════════════════════════════════════════
    # 公开方法（保持现有签名）
    # ═══════════════════════════════════════════════════════════════

    async def run_full(self, scope: str = "all") -> list[int]:
        """每日全量挖掘。"""
        return await self._run(trigger="daily_cron", scope=scope)

    async def run_full_with_diag(self, scope: str = "all") -> dict:
        """手动触发全量挖掘（带诊断信息）。返回 {spark_ids, count, diag: {...}}"""
        diag = {}
        try:
            # Pre-check: data availability
            insights = self.data.get_recent_insights(limit=20)
            notes = self.data.get_all_notes()
            papers = self.data.get_all_papers_with_notes()
            diag["pre_check"] = {
                "core_insights": len(insights),
                "legacy_notes": len(notes or []),
                "papers_with_notes": len(papers or []),
            }

            # Check embedding
            try:
                test_emb = await self.core.llm.embed("test", module="ideator")
                diag["pre_check"]["embedding_ok"] = bool(test_emb)
            except Exception:
                logger.warning("[IdeatorPipeline] 预检 embedding 不可用", exc_info=True)
                diag["pre_check"]["embedding_ok"] = False

            spark_ids = await self._run_with_diag(trigger="user_manual", scope=scope, diag=diag)
            diag["result"] = "ok"
            return {"spark_ids": spark_ids, "count": len(spark_ids), "diag": diag}
        except Exception as e:
            logger.exception("[IdeatorPipeline] 诊断运行失败")
            diag["result"] = "error"
            diag["error"] = str(e)
            return {"spark_ids": [], "count": 0, "diag": diag}

    async def _run_with_diag(self, *, trigger, scope, diag) -> list[int]:
        """诊断包装：委托 _run 执行，从返回统计填充 diag。"""
        diag["effort"] = "beast"
        diag["stages"] = {}
        try:
            return await self._run(trigger=trigger, scope=scope)
        except Exception:
            logger.exception("[IdeatorPipeline] 诊断管道异常")
            return []

    async def run_incremental(self, since: str) -> list[int]:
        """事件驱动增量挖掘。"""
        logger.info(f"[IdeatorPipeline] 增量挖掘 since={since}")
        return await self._run(trigger="event")

    async def run_targeted(self, source_refs: list[dict]) -> list[int]:
        """用户手动定点挖掘。"""
        return await self._run(trigger="user_manual", source_refs=source_refs)

    async def deepen(self, spark_id: int, *, run_id: str | None = None) -> str | None:
        """迭代深化火花：生成草稿 → 交叉审查 → 修订重审（最多 3 轮）。

        返回最终 depth_content，失败返回 None。
        """
        spark = self.data.get_spark(spark_id)
        if not spark:
            return None

        deepen_cfg = self.core.module_config("ideator").get("deepen", {})
        pass_threshold = deepen_cfg.get("pass_threshold", 0.7)
        max_rounds = deepen_cfg.get("max_rounds", 3)

        # 解析来源文本
        source_a_type, source_a_text, source_b_type, source_b_text = \
            self._resolve_sources(spark)
        sources_combined = f"{source_a_text}\n\n{source_b_text}"

        current_draft = ""
        final_overall = 0.0

        for round_num in range(1, max_rounds + 1):
            # ── 生成/修订草稿（使用 core.llm） ──────────────
            if round_num == 1:
                prompt = self.core.llm.load_prompt(
                    "ideator", "spark_deepen",
                    spark_content=spark["content"],
                    sources=sources_combined,
                )
            else:
                # 修订轮次：将上一轮审查反馈注入 prompt
                revision_instruction = (
                    f"Previous draft scored {final_overall:.2f} (threshold {pass_threshold}). "
                    "Please revise to improve novelty, evidence grounding, and feasibility. "
                    "Focus on: tighter source citation, clearer causal chain, more specific next steps."
                )
                prompt = self.core.llm.load_prompt(
                    "ideator", "spark_deepen",
                    spark_content=spark["content"],
                    sources=sources_combined
                    + f"\n\n[REVISION INSTRUCTION - Round {round_num}]\n{revision_instruction}",
                )

            try:
                raw, _ = await self.core.llm.achat(
                    user_prompt=prompt, module="ideator", purpose="spark_deepen",
                )
                current_draft = raw
            except Exception:
                logger.warning("[IdeatorPipeline] 深化 LLM 调用失败", exc_info=True)
                if round_num == 1:
                    return None
                break  # 使用上一轮草稿

            # ── 交叉审查草稿 ─────────────────────────────
            try:
                r1, r2, _arb = await self.reviewer.review_spark(
                    spark_content=current_draft,
                    source_a_type=source_a_type,
                    source_a_text=source_a_text,
                    source_b_type=source_b_type,
                    source_b_text=source_b_text,
                    skip_arbitration=True,  # 深化阶段不触发 Tier 3 仲裁
                )
                if run_id:
                    self._save_review_record(spark_id, r1, "review", run_id)
                    self._save_review_record(spark_id, r2, "review", run_id)

                final_overall = (r1.overall + r2.overall) / 2
                if final_overall >= pass_threshold:
                    break  # 通过，保留当前草稿
            except Exception:
                logger.warning("[IdeatorPipeline] 深化审查失败", exc_info=True)
                break  # 无法审查，保留当前草稿

        # ── 保存最终结果 ─────────────────────────────────
        if current_draft:
            self.store.deepen_spark(spark_id, current_draft)
        return current_draft if current_draft else None

    # ═══════════════════════════════════════════════════════════════
    # 核心管道
    # ═══════════════════════════════════════════════════════════════

    async def _run(
        self,
        *,
        trigger: str = "event",
        scope: str = "all",
        source_refs: list[dict] | None = None,
    ) -> list[int]:
        """S0→S1→S2→S3→S4→S5→S6 完整管道。

        每个阶段完成后保存 PipelineState，支持断点恢复。
        """
        run_id = uuid.uuid4().hex

        # ── Effort 固定最大 ──
        effort = "beast"
        params = EFFORT_PARAMS["beast"]
        logger.info(
            f"[IdeatorPipeline] run_id={run_id} trigger={trigger} "
            f"effort={effort}"
        )

        # ── 记录 pipeline 运行开始 ────────────────────────
        self._write_pipeline_run(run_id, trigger, effort, start=True)

        # ── 初始化管道状态 ──────────────────────────────
        state = PipelineState(run_id=run_id, effort=effort, current_stage="recall")
        save_state(state, self._state_dir)

        saved_ids: list[int] = []
        sparks: list[dict] = []

        try:
            # ── S0: 交叉召回 ───────────────────────────────
            candidates = await self.recall.recall(
                self.core.llm, scope=scope, effort_params=params,
            )
            state.candidates_count = len(candidates)
            state.current_stage = "score"
            state.stages_completed.append("recall")
            save_state(state, self._state_dir)

            if not candidates:
                logger.info("[IdeatorPipeline] S0 无候选关联对")
                return []

            # ── S1: 链接评分 ───────────────────────────────
            scored_links = await self._score_links(candidates)
            state.current_stage = "generate"
            state.stages_completed.append("score")
            save_state(state, self._state_dir)

            if not scored_links:
                logger.info("[IdeatorPipeline] S1 无通过评分的链接")
                return []

            # ── S2: 火花生成 ───────────────────────────────
            sparks = await self._generate_sparks(scored_links, params)
            state.sparks_generated = len(sparks)
            state.current_stage = "deepen"
            state.stages_completed.append("generate")
            save_state(state, self._state_dir)

            if not sparks:
                logger.info("[IdeatorPipeline] S2 无生成火花")
                return []

            # ── S2.25: 闪电筛选 → 排名制 top 10 ────────────
            if len(sparks) > 10:
                sparks = await self.debate_engine.score_sparks(sparks)
                sparks.sort(key=lambda s: s.get("_filter_score", 0), reverse=True)
                top_sparks = sparks[:10]
            else:
                top_sparks = sparks

            # ── S2.5: 深化草稿 ──────────────────────────────
            top_sparks = await self._deepen_sparks_for_review(top_sparks, params)
            state.sparks_generated = len(top_sparks)

            # ── S3: 辩论审查（DebateEngine） ───────────────
            top_sparks = await self._debate_review_sparks(top_sparks, params, run_id)
            state.sparks_reviewed = sum(
                1 for s in top_sparks
                if s.get("_debate_outcome") is not None
            )

            state.current_stage = "dedup"
            state.stages_completed.append("review")
            save_state(state, self._state_dir)

            # ── S4: 去重入库 ───────
            saved_ids = await self._save_sparks(top_sparks, run_id, params)
            state.current_stage = "audit"
            state.stages_completed.append("dedup")
            save_state(state, self._state_dir)

            # ── S6: 溯源审计 ───────────────────────────────
            if not params.get("skip_audit", False) and saved_ids:
                audit_top_n = params.get("audit_top_n", 0)
                await self._audit_sparks(saved_ids, audit_top_n, run_id)

            state.current_stage = "done"
            state.stages_completed.append("audit")
            save_state(state, self._state_dir)

        except Exception as e:
            logger.exception("[IdeatorPipeline] 管道执行异常")
            state.error = str(e)
        finally:
            # ── 记录 pipeline 运行结束 ────────────────────
            stats = {
                "stages_completed": state.stages_completed,
                "candidates_count": state.candidates_count,
                "sparks_generated": state.sparks_generated,
                "sparks_saved": len(saved_ids),
                "sparks_reviewed": state.sparks_reviewed,
                "effort": effort,
                "trigger": trigger,
                "error": state.error if hasattr(state, "error") else None,
            }
            self._write_pipeline_run(run_id, trigger, effort, start=False, stats=stats)

        logger.info(
            f"[IdeatorPipeline] 完成 run_id={run_id}: "
            f"{len(saved_ids)} 个火花，effort={effort}"
            + (f", error={state.error}" if hasattr(state, "error") and state.error else "")
        )
        return saved_ids

    # ═══════════════════════════════════════════════════════════════
    # 来源解析（供 S1/S2/S3/S5/S6 共用）
    # ═══════════════════════════════════════════════════════════════

    def _resolve_source_content(self, source: dict) -> str:
        """从 DB 解析来源完整内容（不截断），回退到 snippet。"""
        ref_type = source.get("type", "")
        ref_id = source.get("id", 0)
        try:
            if ref_type == "paper":
                paper = self.data.get_paper(ref_id)
                if paper:
                    title = paper.get("title", "")
                    abstract = paper.get("abstract", "")
                    notes = paper.get("_note", "")
                    parts = [f"标题: {title}", f"摘要: {abstract}"]
                    if notes:
                        parts.append(f"笔记: {notes}")
                    return "\n".join(parts)
            elif ref_type == "core_note":
                note = self.data._core.knowledge.get_note(ref_id)
                if note:
                    return note.get("content", "")
        except Exception:
            logger.warning("[IdeatorPipeline] 来源内容解析失败", exc_info=True)
        return source.get("content", "")

    # ═══════════════════════════════════════════════════════════════
    # S2.5: 深化草稿（供辩论审查使用）
    # ═══════════════════════════════════════════════════════════════

    async def _deepen_sparks_for_review(
        self, sparks: list[dict], params: dict,
    ) -> list[dict]:
        """S2.5: 为每个火花生成完整研究草稿，供辩论审查使用。"""
        sem = asyncio.Semaphore(3)

        async def _deepen_one(spark: dict) -> dict:
            async with sem:
                sources = self._resolve_source_content_for_spark(spark)
                prompt = self.core.llm.load_prompt(
                    "ideator", "spark_deepen",
                    spark_content=spark.get("content", ""),
                    sources=sources,
                )
                try:
                    raw, _ = await self.core.llm.achat(
                        user_prompt=prompt, module="ideator", purpose="spark_deepen",
                    )
                    spark["_draft"] = raw
                except Exception:
                    logger.warning("[IdeatorPipeline] 深化草稿失败", exc_info=True)
                    spark["_draft"] = spark.get("content", "")  # fallback to spark content
                return spark

        return await asyncio.gather(*[_deepen_one(s) for s in sparks])

    def _resolve_source_content_for_spark(self, spark: dict) -> str:
        """为火花构建来源上下文字符串。"""
        parts = []
        source_refs = spark.get("source_refs", [])
        if isinstance(source_refs, str):
            try:
                source_refs = json.loads(source_refs)
            except Exception:
                source_refs = []
        for ref in (source_refs or []):
            source = {"type": ref.get("type", ""), "id": ref.get("id", 0),
                      "content": ""}
            text = self._resolve_source_content(source)
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    # ═══════════════════════════════════════════════════════════════
    # S1: 链接评分
    # ═══════════════════════════════════════════════════════════════

    async def _score_links(self, candidates: list[dict]) -> list[dict]:
        """逐对独立评分：每个关联对一个 LLM 调用，专注评判。

        候选数 <= 10 时全部保留（默认 0.5 分），避免 LLM 调用。
        """
        if len(candidates) <= 10:
            for c in candidates:
                c["relevance_score"] = 0.5
            return candidates

        sem = asyncio.Semaphore(5)

        async def _score_one(c: dict) -> dict | None:
            async with sem:
                text_a = self._resolve_source_content(c["source_a"])
                text_b = self._resolve_source_content(c["source_b"])
                prompt = self.core.llm.load_prompt(
                    "ideator", "cross_link_single",
                    a=text_a, b=text_b,
                    link_type=c.get("recall_path", ""),
                )
                from paperreadagent.utils.json_utils import clean_json as _clean
                messages = [{"role": "user", "content": prompt}]
                for attempt in range(3):
                    try:
                        raw = await self.ideator_llm.chat(
                            model_role="reviewer_1",
                            messages=list(messages),
                            temperature=0.3,
                            max_tokens=2048,
                        )
                        raw = _clean(raw)
                        result = json.loads(raw)
                        c["relevance_score"] = result.get("score", 0.5)
                        c["reasoning"] = result.get("reason", "")
                        if c["relevance_score"] >= 0.4:
                            return c
                        return None
                    except json.JSONDecodeError as e:
                        if attempt < 2:
                            messages.append({"role": "assistant", "content": "[响应格式错误]"})
                            messages.append({"role": "user",
                                "content": f"JSON 解析失败（{str(e)[:200]}）。请只返回 {{\"score\": 0.0-1.0, \"reason\": \"...\"}}"})
                        else:
                            logger.warning("[IdeatorPipeline] 单对评分 JSON 重试耗尽", exc_info=True)
                            c["relevance_score"] = 0.5
                            return c
                    except Exception:
                        logger.warning("[IdeatorPipeline] 单对评分 LLM 调用失败", exc_info=True)
                        c["relevance_score"] = 0.5
                        return c  # fail-safe: keep the candidate

        results = await asyncio.gather(*[_score_one(c) for c in candidates])
        return [r for r in results if r is not None]

    # ═══════════════════════════════════════════════════════════════
    # S2: 火花生成
    # ═══════════════════════════════════════════════════════════════

    def _group_by_shared_source(self, links: list[dict], max_per_group: int = 5) -> list[list[dict]]:
        """Greedy C1 grouping: cluster links by shared source, max N per group.

        Links sharing the same source (by type+id) are grouped together.
        Sources are prioritized by occurrence frequency (most frequent first).
        Orphan links (no shared source) each form their own group.
        """
        if not links:
            return []

        # 1. Count source occurrences
        source_count: dict[str, int] = {}
        for l in links:
            for src in (l["source_a"], l["source_b"]):
                key = f"{src['type']}:{src['id']}"
                source_count[key] = source_count.get(key, 0) + 1

        # 2. Sort sources by frequency descending
        sorted_sources = sorted(source_count.keys(), key=lambda k: source_count[k], reverse=True)

        # 3. Greedy grouping
        assigned: set[int] = set()
        groups: list[list[dict]] = []

        for src_key in sorted_sources:
            group: list[dict] = []
            for i, l in enumerate(links):
                if i in assigned:
                    continue
                if len(group) >= max_per_group:
                    break
                a_key = f"{l['source_a']['type']}:{l['source_a']['id']}"
                b_key = f"{l['source_b']['type']}:{l['source_b']['id']}"
                if a_key == src_key or b_key == src_key:
                    group.append(l)
                    assigned.add(i)
            if group:
                groups.append(group)

        # 4. Unassigned links each become their own group
        for i, l in enumerate(links):
            if i not in assigned:
                groups.append([l])

        return groups

    async def _generate_sparks(
        self, scored_links: list[dict], params: dict | None = None,
    ) -> list[dict]:
        """S2: 工具增强火花生成。每分组最多 5 轮工具调用，每分组产出 1 个火花。

        使用贪心共享源分组 (C1)，每组独立工具调用循环，
        并行限流 Semaphore(5)。
        """
        if not scored_links:
            return []

        max_pairs = params.get("spark_pair_limit", 10) if params else 10
        top_links = sorted(
            scored_links, key=lambda x: x.get("relevance_score", 0), reverse=True,
        )[:max_pairs]

        groups = self._group_by_shared_source(top_links)

        from .tool_executor import ToolExecutor
        from .tool_registry import create_default_registry
        tool_registry = create_default_registry()
        tool_executor = ToolExecutor(
            data_access=self.data, core_llm=self.core.llm,
            tool_registry=tool_registry,
        )
        tools = tool_executor.to_openai_tools()

        max_tool_rounds = 5
        sem = asyncio.Semaphore(5)

        def _build_source_refs(group: list[dict], spark: dict) -> dict:
            """注入每组的精确来源引用。"""
            refs = []
            seen = set()
            for l in group:
                for src in (l["source_a"], l["source_b"]):
                    key = f"{src['type']}:{src['id']}"
                    if key not in seen:
                        seen.add(key)
                        refs.append({"type": src["type"], "id": src["id"]})
            spark["source_refs"] = refs
            spark["source_type"] = group[0].get("recall_path", "cross_layer")
            return spark

        async def _generate_group(group: list[dict]) -> dict | None:
            from paperreadagent.utils.json_utils import clean_json as _clean
            async with sem:
                links_data = [
                    {"a": self._resolve_source_content(l["source_a"]),
                     "b": self._resolve_source_content(l["source_b"]),
                     "reason": l.get("reasoning", ""),
                     "link_type": l.get("recall_path", ""),
                     "quality_score": l.get("relevance_score", 0)}
                    for l in group
                ]

                system_prompt = self.core.llm.load_prompt(
                    "ideator", "spark_generate_system",
                )
                user_prompt = self.core.llm.load_prompt(
                    "ideator", "spark_generate_user",
                    links=links_data,
                )

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]

                for _round in range(max_tool_rounds):
                    try:
                        resp = await self.core.llm.achat_with_tools(
                            messages=messages,
                            tools=tools,
                            tool_choice="auto",
                            module="ideator",
                            purpose=f"spark_gen_tool_round_{_round}",
                            max_tokens=16384,
                        )
                    except Exception:
                        logger.warning(
                            "[IdeatorPipeline] S2 工具调用 round %d LLM 失败",
                            _round, exc_info=True,
                        )
                        break

                    if resp["tool_calls"]:
                        # Batch all tool calls in one assistant message (OpenAI spec)
                        openai_tool_calls = []
                        for tc in resp["tool_calls"]:
                            openai_tool_calls.append({
                                "id": tc["id"], "type": "function",
                                "function": {"name": tc["name"],
                                             "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
                            })
                        messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": openai_tool_calls,
                        })
                        # Execute tools and append individual results
                        for tc in resp["tool_calls"]:
                            result = await tool_executor.execute(
                                tc["name"], tc["arguments"],
                            )
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result,
                            })
                        continue

                    if resp["content"]:
                        try:
                            raw = _clean(resp["content"])
                            spark = json.loads(raw)
                            if spark is None:
                                return None
                            if isinstance(spark, dict) and spark.get("content"):
                                return _build_source_refs(group, spark)
                        except json.JSONDecodeError:
                            logger.warning(
                                "[IdeatorPipeline] S2 火花 JSON 解析失败，重试",
                            )
                            messages.append({
                                "role": "user",
                                "content": "JSON 解析失败，请返回纯 JSON：{\"content\": \"...\", \"quality_score\": 0.0-1.0} 或 null",
                            })
                            continue
                        logger.warning(
                            "[IdeatorPipeline] S2 火花 JSON 格式异常（缺少 content），丢弃: %s",
                            str(spark)[:200],
                        )
                        return None

                # Fallback: 5 rounds with no output → pure prompt call
                try:
                    raw, _ = await self.core.llm.achat(
                        user_prompt=f"{system_prompt}\n\n{user_prompt}\n\n请直接返回 JSON（不调用工具）：",
                        module="ideator", purpose="spark_gen_fallback",
                    )
                    raw = _clean(raw)
                    spark = json.loads(raw)
                    if isinstance(spark, dict) and spark.get("content"):
                        return _build_source_refs(group, spark)
                except Exception:
                    logger.warning("[IdeatorPipeline] S2 降级生成失败", exc_info=True)

                return None

        results = await asyncio.gather(*[_generate_group(g) for g in groups])
        return [r for r in results if r is not None]

    # ═══════════════════════════════════════════════════════════════
    # S3 (new): 辩论审查（DebateEngine 8 坐席）
    # ═══════════════════════════════════════════════════════════════

    async def _debate_review_sparks(
        self, sparks: list[dict], params: dict, run_id: str,
    ) -> list[dict]:
        """S3: 8 坐席多轮辩论审查。"""
        skip_debate = params.get("skip_debate", False)
        if skip_debate:
            return sparks

        async def _debate_one(spark: dict) -> dict:
            try:
                draft = spark.get("_draft", spark.get("content", ""))
                source_context = self._resolve_source_content_for_spark(spark)
                outcome = await self.debate_engine.run(
                    spark_content=spark.get("content", ""),
                    draft=draft,
                    source_context=source_context,
                )
                spark["_debate_outcome"] = outcome
                spark["_draft"] = outcome.revised_draft or draft
            except Exception:
                logger.warning("[IdeatorPipeline] 辩论审查失败", exc_info=True)
            return spark

        return await asyncio.gather(*[_debate_one(s) for s in sparks])

    # ═══════════════════════════════════════════════════════════════
    # S4: 去重入库
    # ═══════════════════════════════════════════════════════════════

    async def _save_sparks(
        self, sparks: list[dict], run_id: str, params: dict,
    ) -> list[int]:
        """去重后入库，根据审查结果决定最终评分，同步 core_notes。"""
        saved_ids = []

        for i, spark in enumerate(sparks):
            # ── 审查结果判定 ───────────────────────────
            verdict = "PASS"
            final_score = spark.get("quality_score", 0.5)
            r1 = spark.get("_review_r1")
            r2 = spark.get("_review_r2")
            arb = spark.get("_review_arb")

            # ── DebateOutcome 优先于旧审查结果 ───────────
            debate = spark.get("_debate_outcome")
            if debate is not None:
                verdict = debate.verdict
                final_score = debate.final_score
                # Store debate context in metadata
                try:
                    meta = json.loads(spark.get("metadata", "{}") or "{}")
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["debate_summary"] = debate.debate_summary
                if debate.briefing:
                    meta["s3_briefing"] = debate.briefing
                spark["metadata"] = json.dumps(meta, ensure_ascii=False)

            elif r1 is not None and r2 is not None:
                if arb is not None:
                    final_score = (
                        sum(arb.scores.values()) / len(arb.scores)
                        if arb.scores else 0.5
                    )
                    verdict = arb.verdict
                else:
                    final_score = (r1.overall + r2.overall) / 2
                    if r1.verdict == "REJECT" and r2.verdict == "REJECT":
                        verdict = "REJECT"
                    elif r1.verdict == "PASS" or r2.verdict == "PASS":
                        verdict = "PASS"
                    else:
                        verdict = "REVISE"

            # 审查阶段未跳过时，REJECT 不入库
            skip_review = params.get("skip_review", True)
            if not skip_review and verdict == "REJECT":
                logger.debug(f"[IdeatorPipeline] 火花 {i} REJECT，跳过入库")
                continue

            # ── Embedding ──────────────────────────────
            try:
                emb = await self.core.llm.embed(
                    spark["content"][:5000], module="ideator",
                )
            except Exception:
                logger.warning("[IdeatorPipeline] embedding 失败", exc_info=True)
                emb = None

            if not emb:
                logger.info("[IdeatorPipeline] embedding 不可用，火花仍保存但不做向量去重")
                emb = []  # 空向量 = 跳过去重，火花仍入库

            # ── SparkStore 去重入库 ─────────────────────
            spark["_temp_id"] = i  # 用于后续审查记录关联

            # Build metadata from debate outcome
            save_meta = {}
            filter_score = spark.get("_filter_score")
            if filter_score is not None:
                save_meta["filter_score"] = round(filter_score, 2)

            if debate is not None:
                save_meta["debate_summary"] = debate.debate_summary
                if debate.briefing:
                    save_meta["s3_briefing"] = debate.briefing
                # 辩论回合详情（每轮质疑+Gen回应）
                save_meta["debate_rounds"] = [
                    {
                        "round": r.round_num,
                        "questions": r.questions,
                        "gen_responses": r.gen_response.get("responses", []),
                    }
                    for r in debate.debate_rounds
                ]

            sid = self.store.save_spark(
                content=spark["content"],
                source_type=spark.get("source_type", "cross_layer"),
                source_refs=spark.get("source_refs", []),
                embedding=emb,
                quality_score=final_score,
                core_llm=self.core.llm,
                run_id=run_id,
                metadata=save_meta,
                depth_content=spark.get("_draft", ""),
            )

            if sid:
                saved_ids.append(sid)

                # Write review records with real spark_id
                if debate is not None:
                    for rev in debate.initial_reviews:
                        self._save_review_record(
                            sid, rev, "debate_initial", run_id,
                        )
                    for rev in debate.re_reviews:
                        self._save_review_record(
                            sid, rev, "debate_re_review", run_id,
                        )

                # 更新审查状态到 DB
                if r1 is not None or debate is not None:
                    review_status_map = {
                        "PASS": "passed", "REVISE": "revised",
                        "REJECT": "rejected",
                    }
                    if debate is not None:
                        status = review_status_map.get(debate.verdict, "pending")
                    else:
                        status = review_status_map.get(verdict, "pending")
                        if arb is not None:
                            status = "escalated"
                    try:
                        self.store.update_review_result(
                            sid, final_score=final_score,
                            review_status=status, verdict=verdict,
                        )
                    except Exception:
                        logger.warning("[IdeatorPipeline] 更新审查结果失败", exc_info=True)

                # ── 事件发射 ────────────────────────────
                try:
                    await self.core.event_bus.emit(
                        "ideator:spark:created",
                        spark_id=sid,
                        preview=spark["content"][:200],
                    )
                except Exception:
                    logger.warning("[IdeatorPipeline] 事件发射失败", exc_info=True)

        return saved_ids

    # ═══════════════════════════════════════════════════════════════
    # S6: 溯源审计
    # ═══════════════════════════════════════════════════════════════

    async def _audit_sparks(
        self, saved_ids: list[int], audit_top_n: int, run_id: str,
    ) -> None:
        """独立模型验证火花是否被原始文献支持。"""
        to_audit = saved_ids
        if audit_top_n > 0:
            to_audit = saved_ids[:audit_top_n]

        for sid in to_audit:
            try:
                spark = self.data.get_spark(sid)
                if not spark:
                    continue

                source_refs = self._resolve_all_sources(spark)
                result = await self.auditor.audit(
                    spark_content=spark["content"],
                    source_refs=source_refs,
                )
                self._save_audit_record(sid, result, run_id)

                # 应用审计分数增量
                delta = SparkAuditor.score_delta(result.verdict)
                current_qs = spark.get("quality_score", 0.5)
                new_score = max(0.0, min(1.0, current_qs + delta))
                self.data.update_spark(sid, quality_score=new_score)
            except Exception:
                logger.warning(
                    f"[IdeatorPipeline] 审计火花 {sid} 失败", exc_info=True,
                )

    # ═══════════════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════════════

    def _resolve_sources(self, spark: dict) -> tuple[str, str, str, str]:
        """解析 spark 的 source_refs，返回 (type_a, text_a, type_b, text_b)。

        用于审查时提供原始文献上下文。
        """
        source_refs = spark.get("source_refs", [])
        if isinstance(source_refs, str):
            try:
                source_refs = json.loads(source_refs)
            except (json.JSONDecodeError, TypeError):
                source_refs = []

        source_a_type = ""
        source_a_text = ""
        source_b_type = ""
        source_b_text = ""

        for i, ref in enumerate(source_refs[:2]):
            ref_type = ref.get("type", "") if isinstance(ref, dict) else ""
            ref_id = ref.get("id", 0) if isinstance(ref, dict) else 0
            text = ""
            try:
                if ref_type == "paper":
                    paper = self.data.get_paper(ref_id)
                    if paper:
                        text = (
                            f"{paper.get('title', '')}\n"
                            f"{paper.get('abstract', '')}"
                        )
                elif ref_type == "core_note":
                    note = self.data._core.knowledge.get_note(ref_id)
                    if note:
                        text = note.get("content", "")
            except Exception:
                logger.warning("[IdeatorPipeline] 来源解析失败", exc_info=True)

            if i == 0:
                source_a_type = ref_type
                source_a_text = text
            else:
                source_b_type = ref_type
                source_b_text = text

        return source_a_type, source_a_text, source_b_type, source_b_text

    def _resolve_all_sources(self, spark: dict) -> list[dict]:
        """解析 spark 的全部 source_refs，返回结构化列表用于审计。"""
        source_refs = spark.get("source_refs", [])
        if isinstance(source_refs, str):
            try:
                source_refs = json.loads(source_refs)
            except (json.JSONDecodeError, TypeError):
                source_refs = []

        resolved = []
        for ref in source_refs:
            if not isinstance(ref, dict):
                continue
            ref_type = ref.get("type", "")
            ref_id = ref.get("id", 0)
            try:
                if ref_type == "paper":
                    paper = self.data.get_paper(ref_id)
                    if paper:
                        resolved.append({
                            "type": "paper",
                            "title": paper.get("title", ""),
                            "content": paper.get("abstract", ""),
                        })
                elif ref_type == "core_note":
                    note = self.data._core.knowledge.get_note(ref_id)
                    if note:
                        resolved.append({
                            "type": "core_note",
                            "content": note.get("content", ""),
                        })
            except Exception:
                logger.warning("[IdeatorPipeline] 审计来源解析失败", exc_info=True)
        return resolved

    def _save_review_record(
        self, spark_id: int, result, stage: str, run_id: str,
    ) -> None:
        """将审查/仲裁结果写入 ideator_review_records 表。使用 duck-typing 兼容多种 ReviewResult。"""
        try:
            reviewer_model = getattr(result, "reviewer_model", "deepseek-v4-pro")
            reviewer_role = getattr(result, "reviewer_role", None) or getattr(result, "reviewer", "rev1")
            scores = getattr(result, "scores", {})
            verdict = getattr(result, "verdict", "REVISE")
            reasoning = getattr(result, "reasoning", "")
            escalation_reason = getattr(result, "escalation_reason", "")

            # Guard: LLM may return verdicts outside CHECK whitelist
            _VALID_VERDICTS = frozenset({
                "PASS", "REVISE", "REJECT", "ARBITRATE", "OVERTURN",
                "CONFIRM_R1", "CONFIRM_R2", "SUPPORTED", "STRETCHED",
                "UNSUPPORTED",
            })
            if verdict not in _VALID_VERDICTS:
                logger.warning(
                    "[IdeatorPipeline] LLM 返回非法 verdict %r，回退为 REVISE", verdict,
                )
                verdict = "REVISE"

            if escalation_reason:
                self.data._core.db.conn.execute(
                    """INSERT INTO ideator_review_records
                       (spark_id, stage, reviewer_model, reviewer_role,
                        scores, verdict, reasoning, escalation_reason, run_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (spark_id, stage, reviewer_model, reviewer_role,
                     json.dumps(scores, ensure_ascii=False),
                     verdict, reasoning, escalation_reason, run_id),
                )
            else:
                self.data._core.db.conn.execute(
                    """INSERT INTO ideator_review_records
                       (spark_id, stage, reviewer_model, reviewer_role,
                        scores, verdict, reasoning, run_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (spark_id, stage, reviewer_model, reviewer_role,
                     json.dumps(scores, ensure_ascii=False),
                     verdict, reasoning, run_id),
                )
            self.data._core.db.conn.commit()
        except Exception:
            logger.warning("[IdeatorPipeline] review_record 写入失败", exc_info=True)

    def _save_audit_record(
        self, spark_id: int, result: AuditResult, run_id: str,
    ) -> None:
        """将审计结果写入 ideator_review_records 表。"""
        try:
            self.data._core.db.conn.execute(
                """INSERT INTO ideator_review_records
                   (spark_id, stage, reviewer_model, reviewer_role,
                    scores, verdict, reasoning, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    spark_id, "audit", self.ideator_llm.model_for("auditor"), "auditor",
                    json.dumps({"verdict": result.verdict}, ensure_ascii=False),
                    result.verdict, result.reasoning, run_id,
                ),
            )
            self.data._core.db.conn.commit()
        except Exception:
            logger.warning("[IdeatorPipeline] audit_record 写入失败", exc_info=True)

    def _write_pipeline_run(
        self,
        run_id: str,
        trigger: str,
        effort: str,
        *,
        start: bool = True,
        stats: dict | None = None,
    ) -> None:
        """写入或更新 ideator_pipeline_runs 记录。"""
        try:
            if start:
                self.data._core.db.conn.execute(
                    """INSERT OR REPLACE INTO ideator_pipeline_runs
                       (run_id, trigger, effort, stages_completed, stats,
                        started_at)
                       VALUES (?, ?, ?, '[]', '{}', datetime('now'))""",
                    (run_id, trigger, effort),
                )
            else:
                self.data._core.db.conn.execute(
                    """UPDATE ideator_pipeline_runs
                       SET finished_at = datetime('now'),
                           stages_completed = ?,
                           stats = ?
                       WHERE run_id = ?""",
                    (
                        json.dumps(
                            (stats or {}).get("stages_completed", []),
                            ensure_ascii=False,
                        ),
                        json.dumps(stats or {}, ensure_ascii=False),
                        run_id,
                    ),
                )
            self.data._core.db.conn.commit()
        except Exception:
            logger.warning("[IdeatorPipeline] pipeline_runs 写入失败", exc_info=True)

    # ── Agent Team 桥接方法 ─────────────────────────────────

    async def push_sparks_to_team(self, spark_ids: list[int]) -> list[dict]:
        """将管道产出的火花推入 Agent Team 讨论。"""
        sparks = []
        for sid in spark_ids:
            spark = self.data.get_spark(sid)
            if spark:
                sparks.append(spark)
        return sparks

    async def run_targeted_recall(self, direction: str, keywords: list[str]) -> list[dict]:
        """定向增量召回（仅系统内论文，可跨项目）。"""
        from .cross_recall import CrossRecall
        cr = CrossRecall(self.data, idea_extractor=self.idea_extractor)
        results = []
        paths = ["similarity"]
        if "contradiction" in direction.lower():
            paths.append("contradiction")
        if "cross_project" in direction.lower():
            paths.append("cross_project")
        for path in paths:
            try:
                pairs = await cr.recall_single_path(
                    self.core.llm, path, sample_size=3,
                    direction=direction, keywords=keywords,
                )
                results.extend(pairs)
            except Exception:
                logger.warning(f"[Pipeline] targeted recall path {path} failed", exc_info=True)
        return results
