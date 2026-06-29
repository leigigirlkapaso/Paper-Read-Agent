"""
tests/test_pipeline.py
Unit tests for agent2/pipeline.py.
Tests the orchestration logic using mocked LLM — no real API calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agent1.arxiv_searcher import PaperMeta
from agent2.pipeline import run_pipelined, _do_llm_read
from utils.llm_client import LLMClient, LLMUsage


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _make_paper(arxiv_id: str = "2301.00001", **overrides) -> PaperMeta:
    defaults = {
        "arxiv_id": arxiv_id,
        "title": f"Paper {arxiv_id}",
        "authors": ["Author One"],
        "published": "2023-01-15",
        "abstract": "Sample abstract for testing.",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
    }
    defaults.update(overrides)
    return PaperMeta(**defaults)


def _make_mock_llm() -> LLMClient:
    """Create an LLMClient with a mocked async client that returns a fixed response."""
    llm = LLMClient(
        api_key="test-key",
        api_base_url="https://test.api/v1",
        model_name="test-model",
        temperature=0.3,
    )
    # Replace the async client's create method
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "**Mocked summary** for testing."
    mock_response.usage = MagicMock()
    mock_response.usage.prompt_tokens = 100
    mock_response.usage.completion_tokens = 50
    mock_response.usage.total_tokens = 150

    llm._async_client.chat.completions.create = AsyncMock(
        return_value=mock_response
    )
    return llm


# ═══════════════════════════════════════════════════════════════════
# _do_llm_read
# ═══════════════════════════════════════════════════════════════════

class TestDoLlmRead:
    """Test _do_llm_read: cache passthrough + LLM call."""

    @pytest.mark.asyncio
    async def test_cache_passthrough(self, tmp_path):
        """When pdf_text starts with '### ', return as-is (cache hit)."""
        paper = _make_paper()
        llm = _make_mock_llm()
        cached_text = "### [Cached Paper](https://arxiv.org)\n\n**作者**：Author　**发表时间**：2023\n\nCached content."

        result = await _do_llm_read(
            paper=paper,
            pdf_text=cached_text,
            summary_prompt="Summarize.",
            topic="test",
            llm=llm,
            summary_dir=tmp_path,
        )

        assert result == cached_text
        # LLM should NOT have been called
        llm._async_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_call_on_fresh_text(self, tmp_path):
        """When pdf_text is not cached, call LLM."""
        paper = _make_paper()
        llm = _make_mock_llm()
        pdf_text = "This is the full text of a research paper about haptics."

        result = await _do_llm_read(
            paper=paper,
            pdf_text=pdf_text,
            summary_prompt="Summarize.",
            topic="test",
            llm=llm,
            summary_dir=tmp_path,
        )

        assert result.startswith("### ")
        assert "Mocked summary" in result
        llm._async_client.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_saves_summary_to_file(self, tmp_path):
        """After LLM read, summary file is written."""
        paper = _make_paper()
        llm = _make_mock_llm()
        pdf_text = "Research paper text."

        await _do_llm_read(
            paper=paper,
            pdf_text=pdf_text,
            summary_prompt="Summarize.",
            topic="test",
            llm=llm,
            summary_dir=tmp_path,
        )

        # Check that at least one .md file was created
        md_files = list(tmp_path.glob("*.md"))
        assert len(md_files) >= 1
        content = md_files[0].read_text(encoding="utf-8")
        assert content.startswith("### ")


# ═══════════════════════════════════════════════════════════════════
# run_pipelined
# ═══════════════════════════════════════════════════════════════════

class TestRunPipelined:
    """Test run_pipelined orchestration."""

    @pytest.mark.asyncio
    async def test_empty_papers_returns_empty_list(self, tmp_path):
        """Empty input → empty output immediately."""
        llm = _make_mock_llm()
        result = await run_pipelined(
            papers=[],
            pdf_dir=tmp_path,
            summary_dir=tmp_path / "summaries",
            summary_prompt="Summarize.",
            topic="test",
            llm=llm,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_all_cache_hits_skip_parse_and_llm(self, tmp_path):
        """When all papers have cached summaries, no LLM calls are made."""
        summary_dir = tmp_path / "summaries"
        summary_dir.mkdir()

        papers = []
        for i in range(3):
            p = _make_paper(arxiv_id=f"2301.0000{i}")
            papers.append(p)

            # Pre-create cache files with correct prompt-aware hash
            from agent2.paper_reader import _compute_prompt_hash
            prompt_hash = _compute_prompt_hash(
                "Summarize.", "test-model", 0.3, 110000, "test"
            )
            prompt_short = prompt_hash[:8]
            cache_path = summary_dir / f"{p.arxiv_id}_{prompt_short}.md"
            cache_path.write_text(
                f"### [Paper {i}](https://arxiv.org)\n\n"
                f"**作者**：Author　**发表时间**：2023\n\n"
                f"Cached summary for paper {i}.",
                encoding="utf-8",
            )

        llm = _make_mock_llm()
        result = await run_pipelined(
            papers=papers,
            pdf_dir=tmp_path,  # no actual PDFs — cache hits don't need them
            summary_dir=summary_dir,
            summary_prompt="Summarize.",
            topic="test",
            llm=llm,
        )

        assert len(result) == 3
        for i, (paper, summary) in enumerate(result):
            assert paper.arxiv_id == f"2301.0000{i}"
            assert f"Cached summary for paper {i}" in summary

        # LLM should not have been called (all cache hits)
        llm._async_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_in_parse_produces_fallback(self, tmp_path):
        """When parse fails, fallback summary is produced. Pipeline doesn't crash."""
        papers = [_make_paper(arxiv_id="2301.00001")]
        llm = _make_mock_llm()

        # No PDF files exist → parse will fail → fallback
        with patch("agent2.pipeline.parse_pdf", side_effect=Exception("Boom")):
            result = await run_pipelined(
                papers=papers,
                pdf_dir=tmp_path,  # empty — no PDF
                summary_dir=tmp_path / "summaries",
                summary_prompt="Summarize.",
                topic="test",
                llm=llm,
                max_parse_workers=1,
            )

        assert len(result) == 1
        paper, summary = result[0]
        assert paper.arxiv_id == "2301.00001"
        # Pipeline gracefully recovers: parse fails, but LLM still runs
        # on the fallback text (title + abstract) and produces a summary.
        assert "Mocked summary" in summary

    @pytest.mark.asyncio
    async def test_result_order_matches_input(self, tmp_path):
        """Output order matches input order regardless of completion order."""
        summary_dir = tmp_path / "summaries"
        summary_dir.mkdir()

        papers = [
            _make_paper(arxiv_id="2301.00001", title="First"),
            _make_paper(arxiv_id="2301.00002", title="Second"),
            _make_paper(arxiv_id="2301.00003", title="Third"),
        ]
        llm = _make_mock_llm()

        # Mock parse_pdf to avoid real network/disk calls.
        # Note: _try_ar5iv lives in utils.pdf_parser and is called internally
        # by parse_pdf, so mocking parse_pdf at the pipeline level suffices.
        with patch("agent2.pipeline.parse_pdf", return_value="Mocked PDF text for testing pipeline order."):
            result = await run_pipelined(
                papers=papers,
                pdf_dir=tmp_path,
                summary_dir=summary_dir,
                summary_prompt="Summarize.",
                topic="test",
                llm=llm,
                max_parse_workers=2,
            )

        assert len(result) == 3
        assert result[0][0].arxiv_id == "2301.00001"
        assert result[1][0].arxiv_id == "2301.00002"
        assert result[2][0].arxiv_id == "2301.00003"
