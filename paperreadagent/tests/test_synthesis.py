"""Tests for agent2.synthesis (cross-paper review synthesis)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent2.synthesis import generate_synthesis, _compute_synthesis_budget


def _paper(title):
    from agent1.arxiv_searcher import PaperMeta
    return PaperMeta(
        arxiv_id="x", title=title, authors=["A"], published="2024",
        abstract="", pdf_url="", arxiv_url="", doi="",
        relevance_score=0.0, source_platform="arxiv", venue="",
        code_url="", citation_count=0,
    )


def _fake_llm(reply="综述正文", usage=None):
    llm = MagicMock()
    llm.model_name = "deepseek-v4-pro"
    llm.temperature = 0.3
    llm.achat = AsyncMock(return_value=(reply, usage or MagicMock()))
    return llm


class TestBudget:
    def test_tiers(self):
        assert _compute_synthesis_budget(2) == (400, 1200)
        assert _compute_synthesis_budget(5) == (800, 2000)
        assert _compute_synthesis_budget(15) == (1500, 3500)
        assert _compute_synthesis_budget(40) == (2500, 6000)
        assert _compute_synthesis_budget(120) == (4000, 8000)

    def test_boundaries(self):
        assert _compute_synthesis_budget(3) == (400, 1200)
        assert _compute_synthesis_budget(4) == (800, 2000)
        assert _compute_synthesis_budget(8) == (800, 2000)
        assert _compute_synthesis_budget(9) == (1500, 3500)


class TestGenerateSynthesis:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        cfg = {"research": {"topic": "具身智能触觉"}}
        papers = [(_paper("Paper A"), "summary A"), (_paper("Paper B"), "summary B")]
        llm = _fake_llm("这批论文研究 X……未解决问题：1)… 2)…")
        out = await generate_synthesis(cfg, papers, llm)
        assert "未解决问题" in out
        sent = llm.achat.call_args.kwargs.get("user_prompt") or llm.achat.call_args.args[0]
        assert "Paper A" in sent and "Paper B" in sent

    @pytest.mark.asyncio
    async def test_truncates_long_summary(self):
        cfg = {"research": {"topic": "t"}}
        long_summary = "x" * 5000
        papers = [(_paper("P"), long_summary)]
        llm = _fake_llm()
        await generate_synthesis(cfg, papers, llm)
        sent = llm.achat.call_args.kwargs.get("user_prompt") or llm.achat.call_args.args[0]
        assert sent.count("x") <= 1600

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self):
        cfg = {"research": {"topic": "t"}}
        papers = [(_paper("P"), "s")]
        llm = _fake_llm()
        llm.achat = AsyncMock(side_effect=RuntimeError("LLM down"))
        out = await generate_synthesis(cfg, papers, llm)
        assert out == ""

    @pytest.mark.asyncio
    async def test_empty_papers_no_llm_call(self):
        cfg = {"research": {"topic": "t"}}
        llm = _fake_llm()
        out = await generate_synthesis(cfg, [], llm)
        assert out == ""
        llm.achat.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_max_tokens_passed(self):
        cfg = {"research": {"topic": "t"}}
        papers = [(_paper(f"P{i}"), "s") for i in range(15)]
        llm = _fake_llm()
        await generate_synthesis(cfg, papers, llm)
        assert llm.achat.call_args.kwargs.get("max_tokens") == 3500


class TestReportInjection:
    def _cfg(self):
        return {"research": {"topic": "具身智能触觉"}}

    def test_injects_overview(self):
        from main import build_final_report
        papers = [(_paper("Paper A"), "### card\n\nbody A")]
        md = build_final_report(self._cfg(), ["kw"], ["q"], papers,
                                overview="这是真实综述，未解决问题：1)…")
        assert "这是真实综述" in md
        assert "请综合以上信息进行整体归纳" not in md

    def test_empty_overview_keeps_placeholder(self):
        from main import build_final_report
        papers = [(_paper("Paper A"), "### card\n\nbody A")]
        md = build_final_report(self._cfg(), ["kw"], ["q"], papers, overview="")
        assert "请综合以上信息进行整体归纳" in md

    def test_overview_default_is_placeholder(self):
        from main import build_final_report
        papers = [(_paper("Paper A"), "### card\n\nbody A")]
        md = build_final_report(self._cfg(), ["kw"], ["q"], papers)
        assert "请综合以上信息进行整体归纳" in md


class TestLargeBatchCap:
    @pytest.mark.asyncio
    async def test_caps_papers_and_picks_top_relevance(self):
        """>60 papers → only top-60 by relevance fed to the synthesis call."""
        from agent1.arxiv_searcher import PaperMeta
        cfg = {"research": {"topic": "t"}}
        papers = []
        for i in range(100):
            p = PaperMeta(
                arxiv_id=f"x{i}", title=f"P{i}", authors=["A"], published="2024",
                abstract="", pdf_url="", arxiv_url="", doi="",
                relevance_score=float(i),  # P99 highest, P0 lowest
                source_platform="arxiv", venue="", code_url="", citation_count=0,
            )
            papers.append((p, f"summary {i}"))
        llm = _fake_llm()
        await generate_synthesis(cfg, papers, llm)
        sent = llm.achat.call_args.kwargs.get("user_prompt") or llm.achat.call_args.args[0]
        # highest-relevance P99 must be included; lowest P0 excluded
        assert "### P99\n" in sent
        assert "### P0\n" not in sent
        # n_papers in prompt reflects the cap (60), not 100
        assert "共 60 篇" in sent
