"""
tests/test_paper_reader.py
Unit tests for pure functions in agent2/paper_reader.py.
No external API calls, no database required.
"""

from __future__ import annotations

import hashlib

import pytest
from agent1.arxiv_searcher import PaperMeta
from agent2.paper_reader import (
    _compute_prompt_hash,
    _compute_pdf_hash,
    _wrap_as_card,
    _make_fallback_summary,
    _extract_pmcid,
)


# ═══════════════════════════════════════════════════════════════════
# Helpers — PaperMeta factory
# ═══════════════════════════════════════════════════════════════════

def _make_paper(**overrides) -> PaperMeta:
    """Create a PaperMeta with sensible defaults, overridable per test."""
    defaults = {
        "arxiv_id": "2301.00001",
        "title": "A Sample Paper Title",
        "authors": ["Author One", "Author Two"],
        "published": "2023-01-15",
        "abstract": "This is a sample abstract for testing purposes.",
        "pdf_url": "https://arxiv.org/pdf/2301.00001",
        "arxiv_url": "https://arxiv.org/abs/2301.00001",
    }
    defaults.update(overrides)
    return PaperMeta(**defaults)


# ═══════════════════════════════════════════════════════════════════
# _compute_prompt_hash
# ═══════════════════════════════════════════════════════════════════

class TestComputePromptHash:
    """Test _compute_prompt_hash determinism and sensitivity."""

    def test_deterministic_same_inputs(self):
        """Same inputs produce the same hash every time."""
        h1 = _compute_prompt_hash(
            "Summarize this paper.", "gpt-4", 0.7, 110000, "haptics"
        )
        h2 = _compute_prompt_hash(
            "Summarize this paper.", "gpt-4", 0.7, 110000, "haptics"
        )
        assert h1 == h2

    def test_different_prompt_different_hash(self):
        """Different summary prompt produces different hash."""
        h1 = _compute_prompt_hash(
            "Summarize this paper.", "gpt-4", 0.7, 110000, ""
        )
        h2 = _compute_prompt_hash(
            "Analyze this paper deeply.", "gpt-4", 0.7, 110000, ""
        )
        assert h1 != h2

    def test_different_model_different_hash(self):
        """Different model name produces different hash."""
        h1 = _compute_prompt_hash(
            "prompt", "gpt-4", 0.7, 110000, ""
        )
        h2 = _compute_prompt_hash(
            "prompt", "gpt-4o", 0.7, 110000, ""
        )
        assert h1 != h2

    def test_different_temperature_different_hash(self):
        """Different temperature produces different hash."""
        h1 = _compute_prompt_hash(
            "prompt", "gpt-4", 0.0, 110000, ""
        )
        h2 = _compute_prompt_hash(
            "prompt", "gpt-4", 0.7, 110000, ""
        )
        assert h1 != h2

    def test_different_max_chars_different_hash(self):
        """Different max_chars produces different hash."""
        h1 = _compute_prompt_hash(
            "prompt", "gpt-4", 0.7, 50000, ""
        )
        h2 = _compute_prompt_hash(
            "prompt", "gpt-4", 0.7, 110000, ""
        )
        assert h1 != h2

    def test_different_topic_different_hash(self):
        """Different topic produces different hash."""
        h1 = _compute_prompt_hash(
            "prompt", "gpt-4", 0.7, 110000, "haptics"
        )
        h2 = _compute_prompt_hash(
            "prompt", "gpt-4", 0.7, 110000, "NLP"
        )
        assert h1 != h2

    def test_empty_topic_vs_nonempty(self):
        """Empty topic vs non-empty topic produce different hashes."""
        h1 = _compute_prompt_hash(
            "prompt", "gpt-4", 0.7, 110000, ""
        )
        h2 = _compute_prompt_hash(
            "prompt", "gpt-4", 0.7, 110000, "some topic"
        )
        assert h1 != h2

    def test_hash_is_hex_string(self):
        """Hash is a hexadecimal string of length 64 (SHA256)."""
        h = _compute_prompt_hash(
            "test", "model", 0.5, 100000, "topic"
        )
        assert isinstance(h, str)
        assert len(h) == 64
        # Verify it's valid hex
        int(h, 16)

    def test_same_empty_topic_produces_matching_hash(self):
        """Two calls with identical empty-topic settings match."""
        h1 = _compute_prompt_hash(
            "Prompt text", "claude-sonnet", 0.3, 80000, ""
        )
        h2 = _compute_prompt_hash(
            "Prompt text", "claude-sonnet", 0.3, 80000, ""
        )
        assert h1 == h2

    def test_chinese_topic_works(self):
        """Chinese characters in topic are handled correctly."""
        h1 = _compute_prompt_hash(
            "prompt", "model", 0.5, 100000, "基于深度学习的触觉反馈系统"
        )
        h2 = _compute_prompt_hash(
            "prompt", "model", 0.5, 100000, "基于深度学习的触觉反馈系统"
        )
        assert h1 == h2
        assert isinstance(h1, str)
        int(h1, 16)  # valid hex


# ═══════════════════════════════════════════════════════════════════
# _compute_pdf_hash
# ═══════════════════════════════════════════════════════════════════

class TestComputePdfHash:
    """Test _compute_pdf_hash determinism."""

    def test_deterministic_same_text(self):
        """Same text produces the same hash."""
        text = "This is the full text of a PDF paper."
        h1 = _compute_pdf_hash(text)
        h2 = _compute_pdf_hash(text)
        assert h1 == h2

    def test_different_text_different_hash(self):
        """Different text produces different hash."""
        h1 = _compute_pdf_hash("Hello world.")
        h2 = _compute_pdf_hash("Goodbye world.")
        assert h1 != h2

    def test_empty_string(self):
        """Empty string produces a valid hash."""
        h = _compute_pdf_hash("")
        assert isinstance(h, str)
        assert len(h) == 64
        int(h, 16)

    def test_hash_is_hex_string(self):
        """Hash is a valid SHA256 hex string."""
        h = _compute_pdf_hash("some pdf content here")
        assert len(h) == 64
        int(h, 16)

    def test_long_text(self):
        """Long text (e.g., full paper) still hashes deterministically."""
        long_text = "This is a very long paper. " * 10000
        h1 = _compute_pdf_hash(long_text)
        h2 = _compute_pdf_hash(long_text)
        assert h1 == h2

    def test_single_char_difference(self):
        """A single character difference changes the hash."""
        h1 = _compute_pdf_hash("abc")
        h2 = _compute_pdf_hash("abd")
        assert h1 != h2

    def test_unicode_text(self):
        """Unicode/Chinese text hashes correctly."""
        text = "这篇论文研究了基于深度学习的触觉渲染技术。"
        h1 = _compute_pdf_hash(text)
        h2 = _compute_pdf_hash(text)
        assert h1 == h2


# ═══════════════════════════════════════════════════════════════════
# _wrap_as_card
# ═══════════════════════════════════════════════════════════════════

class TestWrapAsCard:
    """Test _wrap_as_card Markdown card formatting."""

    def test_basic_card_structure(self):
        """Card includes title as h3 link, authors, date, score, summary."""
        paper = _make_paper(
            title="Test Paper",
            authors=["Alice", "Bob"],
            published="2024-06",
            relevance_score=0.85,
        )
        summary = "This paper proposes a novel method."
        result = _wrap_as_card(paper, summary)

        assert result.startswith("### ")
        assert "[Test Paper]" in result
        assert "Alice, Bob" in result
        assert "2024-06" in result
        assert "0.85" in result
        assert "This paper proposes a novel method." in result

    def test_title_is_clickable_link(self):
        """Title is wrapped in markdown link to arxiv_url."""
        paper = _make_paper(
            title="Linked Paper",
            arxiv_url="https://arxiv.org/abs/2301.00001",
        )
        result = _wrap_as_card(paper, "Summary.")
        assert "[Linked Paper](https://arxiv.org/abs/2301.00001)" in result

    def test_fallback_to_pdf_url_when_no_arxiv_url(self):
        """When arxiv_url is empty, uses pdf_url as link."""
        paper = _make_paper(
            title="PDF Only Paper",
            arxiv_url="",
            pdf_url="https://example.com/paper.pdf",
        )
        result = _wrap_as_card(paper, "Summary.")
        assert "[PDF Only Paper](https://example.com/paper.pdf)" in result

    def test_no_link_when_no_urls(self):
        """When both arxiv_url and pdf_url are empty, title is plain text."""
        paper = _make_paper(
            title="No URL Paper",
            arxiv_url="",
            pdf_url="",
        )
        result = _wrap_as_card(paper, "Summary.")
        assert result.startswith("### No URL Paper\n")
        assert "[" not in result.split("\n")[0]

    def test_authors_truncated_at_five(self):
        """When >5 authors, only first 5 shown with 等 suffix."""
        paper = _make_paper(
            authors=["A1", "A2", "A3", "A4", "A5", "A6", "A7"],
        )
        result = _wrap_as_card(paper, "Summary.")
        assert "A1, A2, A3, A4, A5 等" in result
        assert "A6" not in result.split("**作者**：")[1].split("　")[0]

    def test_exactly_five_authors_no_truncation(self):
        """Exactly 5 authors shown without 等."""
        paper = _make_paper(
            authors=["A1", "A2", "A3", "A4", "A5"],
        )
        result = _wrap_as_card(paper, "Summary.")
        assert "A1, A2, A3, A4, A5" in result
        assert "等" not in result.split("**作者**：")[1].split("　")[0]

    def test_score_formatting(self):
        """Relevance score formatted to 2 decimal places."""
        paper = _make_paper(relevance_score=0.9)
        result = _wrap_as_card(paper, "Summary.")
        assert "0.90" in result

        paper2 = _make_paper(relevance_score=0.753)
        result2 = _wrap_as_card(paper2, "Summary.")
        assert "0.75" in result2  # rounds to 2 decimal

    def test_summary_stripped_of_whitespace(self):
        """Summary text has leading/trailing whitespace stripped."""
        paper = _make_paper()
        result = _wrap_as_card(paper, "  \n  Clean summary.  \n")
        assert "Clean summary." in result
        # Should not have extra blank lines at the end
        assert not result.endswith("\n\n")

    def test_multiline_summary(self):
        """Multi-line summary is preserved."""
        paper = _make_paper()
        summary = "Line 1.\nLine 2.\nLine 3."
        result = _wrap_as_card(paper, summary)
        assert "Line 1." in result
        assert "Line 2." in result
        assert "Line 3." in result


# ═══════════════════════════════════════════════════════════════════
# _make_fallback_summary
# ═══════════════════════════════════════════════════════════════════

class TestMakeFallbackSummary:
    """Test _make_fallback_summary error/fallback card formatting."""

    def test_includes_error_message(self):
        """Fallback card includes the error message."""
        paper = _make_paper(title="Failed Paper")
        result = _make_fallback_summary(paper, "PDF parsing timeout")
        assert "> ⚠️ 本文处理失败" in result
        assert "PDF parsing timeout" in result

    def test_includes_original_abstract(self):
        """Fallback card includes the original abstract snippet."""
        paper = _make_paper(
            title="Failed Paper",
            abstract="This is the original abstract text from the paper metadata.",
        )
        result = _make_fallback_summary(paper, "error")
        assert "This is the original abstract text from the paper metadata." in result

    def test_title_is_link(self):
        """Title links to arxiv_url when available."""
        paper = _make_paper(
            title="Linked Fail",
            arxiv_url="https://arxiv.org/abs/2301.00001",
        )
        result = _make_fallback_summary(paper, "error")
        assert "[Linked Fail](https://arxiv.org/abs/2301.00001)" in result

    def test_no_link_when_no_urls(self):
        """When no URLs available, title is plain text."""
        paper = _make_paper(
            title="Plain Fail",
            arxiv_url="",
            pdf_url="",
        )
        result = _make_fallback_summary(paper, "error")
        assert result.startswith("### Plain Fail\n")

    def test_authors_truncated_at_three(self):
        """Fallback shows only first 3 authors (different from wrap_as_card)."""
        paper = _make_paper(
            authors=["A1", "A2", "A3", "A4", "A5"],
        )
        result = _make_fallback_summary(paper, "error")
        # _make_fallback_summary uses authors[:3], no 等 suffix
        assert "A1, A2, A3" in result

    def test_long_abstract_is_truncated(self):
        """Abstract longer than 500 chars is truncated with ..."""
        long_abstract = "X" * 1000
        paper = _make_paper(abstract=long_abstract)
        result = _make_fallback_summary(paper, "error")
        # Should contain the first 500 chars + "..."
        assert "X" * 500 + "..." in result
        assert "X" * 501 not in result  # 501st char not present

    def test_none_abstract_handled(self):
        """None abstract doesn't crash."""
        paper = _make_paper(abstract=None)
        result = _make_fallback_summary(paper, "error")
        # Should not crash; (None or '')[:500] is ''
        assert isinstance(result, str)

    def test_short_abstract_not_truncated(self):
        """Short abstract (<500 chars) shown in full."""
        short_abs = "A brief abstract."
        paper = _make_paper(abstract=short_abs)
        result = _make_fallback_summary(paper, "error")
        assert short_abs in result

    def test_markdown_card_format(self):
        """Fallback is valid markdown starting with h3."""
        paper = _make_paper()
        result = _make_fallback_summary(paper, "test error")
        assert result.startswith("### ")


# ═══════════════════════════════════════════════════════════════════
# _extract_pmcid
# ═══════════════════════════════════════════════════════════════════

class TestExtractPmcid:
    """Test _extract_pmcid PMCID extraction from PaperMeta."""

    def test_pmcid_prefix_in_arxiv_id(self):
        """PMCID with pmcid_ prefix in arxiv_id is extracted correctly."""
        paper = _make_paper(arxiv_id="pmcid_PMC123456")
        assert _extract_pmcid(paper) == "PMC123456"

    def test_pmcid_lowercase_prefix(self):
        """Lowercase prefix also works."""
        paper = _make_paper(arxiv_id="pmcid_PMC987654")
        assert _extract_pmcid(paper) == "PMC987654"

    def test_pmcid_from_doi(self):
        """DOI that looks like PMCID is returned."""
        paper = _make_paper(arxiv_id="2301.00001", doi="PMC555123")
        assert _extract_pmcid(paper) == "PMC555123"

    def test_doi_not_pmcid_returns_none(self):
        """Non-PMC DOI returns None."""
        paper = _make_paper(arxiv_id="2301.00001", doi="10.1234/foo.bar")
        assert _extract_pmcid(paper) is None

    def test_no_pmcid_no_doi_returns_none(self):
        """Paper without PMCID or PMC-like DOI returns None."""
        paper = _make_paper(arxiv_id="2301.00001", doi="")
        assert _extract_pmcid(paper) is None

    def test_arxiv_id_has_priority_over_doi(self):
        """When arxiv_id has pmcid_ prefix, it takes priority over doi."""
        paper = _make_paper(arxiv_id="pmcid_PMC111", doi="PMC222")
        assert _extract_pmcid(paper) == "PMC111"

    def test_empty_arxiv_id(self):
        """Empty arxiv_id falls through to doi check."""
        paper = _make_paper(arxiv_id="", doi="PMC333")
        assert _extract_pmcid(paper) == "PMC333"

    def test_none_arxiv_id_handled(self):
        """None arxiv_id doesn't crash and falls through to doi check."""
        paper = _make_paper(arxiv_id=None, doi="PMC444")
        assert _extract_pmcid(paper) == "PMC444"

    def test_pmcid_without_doi_and_non_pmcid_arxiv(self):
        """Non-pmcid arxiv_id and no doi returns None."""
        paper = _make_paper(arxiv_id="2301.00001", doi="")
        assert _extract_pmcid(paper) is None


# ── AGENT2 fact-card + self-check enhancement ──────────────────

from agent2.paper_reader import _build_user_prompt, _FACTCARD_SELFCHECK_BLOCK


def _sample_paper():
    from agent1.arxiv_searcher import PaperMeta
    return PaperMeta(
        arxiv_id="2401.12345", title="A Tactile Paper",
        authors=["Alice", "Bob"], published="2024-01-01",
        abstract="abs", pdf_url="", arxiv_url="https://arxiv.org/abs/2401.12345",
        doi="", relevance_score=0.0, source_platform="arxiv",
        venue="", code_url="", citation_count=0,
    )


def test_build_user_prompt_includes_factcard_block():
    prompt = _build_user_prompt(_sample_paper(), "PDF TEXT", "分析这篇论文", "具身智能触觉")
    assert "关键数据卡" in prompt
    assert "未提取自检" in prompt


def test_factcard_hard_constraint_present():
    assert "严禁模糊词" in _FACTCARD_SELFCHECK_BLOCK
    assert "有所提升" in _FACTCARD_SELFCHECK_BLOCK


def test_constraint_scoped_to_factcard():
    assert "仅适用于本节" in _FACTCARD_SELFCHECK_BLOCK


def test_factcard_block_appended_after_user_summary_prompt():
    prompt = _build_user_prompt(_sample_paper(), "PDF", "USER_ANALYSIS_REQ", "topic")
    assert prompt.index("USER_ANALYSIS_REQ") < prompt.index("关键数据卡")


def test_prompt_hash_changes_with_factcard_version():
    from agent2.paper_reader import _compute_prompt_hash, _PROMPT_VERSION
    h_current = _compute_prompt_hash("p", "m", 0.3, 1000, "t")
    import hashlib
    old_components = "p|m|0.3|1000|t"
    h_old = hashlib.sha256(old_components.encode()).hexdigest()
    assert h_current != h_old
    assert _PROMPT_VERSION
