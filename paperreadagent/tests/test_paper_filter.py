"""
tests/test_paper_filter.py
Unit tests for pure functions in agent1/paper_filter.py.
No external API calls, no database required.
"""

from __future__ import annotations

import pytest
from agent1.arxiv_searcher import PaperMeta
from agent1.paper_filter import (
    _parse_scores,
    _compute_quality_score,
    _format_paper_for_scoring,
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
# _parse_scores
# ═══════════════════════════════════════════════════════════════════

class TestParseScores:
    """Test _parse_scores with valid JSON, markdown wrapping, and invalid input."""

    def test_valid_json_array(self):
        """Parse a clean JSON array of score objects."""
        raw = '[{"id": 1, "score": 0.9}, {"id": 2, "score": 0.3}, {"id": 3, "score": 0.7}]'
        result = _parse_scores(raw)
        assert len(result) == 3
        assert result[0] == {"id": 1, "score": 0.9}
        assert result[1] == {"id": 2, "score": 0.3}
        assert result[2] == {"id": 3, "score": 0.7}

    def test_markdown_code_block_json(self):
        """Parse JSON wrapped in ```json ... ```."""
        raw = '```json\n[{"id": 1, "score": 0.95}]\n```'
        result = _parse_scores(raw)
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["score"] == 0.95

    def test_markdown_code_block_no_lang(self):
        """Parse JSON wrapped in ``` ... ``` without language tag."""
        raw = '```\n[{"id": 1, "score": 0.5}]\n```'
        result = _parse_scores(raw)
        assert len(result) == 1
        assert result[0]["score"] == 0.5

    def test_extra_text_around_array(self):
        """Parse JSON array with extra explanatory text before/after."""
        raw = 'Here are the scores:\n[{"id": 1, "score": 0.8}]\nLet me know if you need more.'
        result = _parse_scores(raw)
        assert len(result) == 1
        assert result[0]["score"] == 0.8

    def test_invalid_json_returns_empty(self):
        """Invalid JSON returns an empty list instead of raising."""
        raw = "This is not valid JSON at all."
        result = _parse_scores(raw)
        assert result == []

    def test_json_object_not_array(self):
        """A JSON object (not array) should return empty list."""
        raw = '{"id": 1, "score": 0.9}'
        result = _parse_scores(raw)
        assert result == []

    def test_empty_string(self):
        """Empty string returns empty list."""
        result = _parse_scores("")
        assert result == []

    def test_single_score_array(self):
        """Single-element array parses correctly."""
        raw = '[{"id": 5, "score": 1.0}]'
        result = _parse_scores(raw)
        assert len(result) == 1
        assert result[0]["id"] == 5
        assert result[0]["score"] == 1.0

    def test_zero_scores(self):
        """Scores with zero values."""
        raw = '[{"id": 1, "score": 0.0}, {"id": 2, "score": 0.0}]'
        result = _parse_scores(raw)
        assert len(result) == 2
        assert all(item["score"] == 0.0 for item in result)


# ═══════════════════════════════════════════════════════════════════
# _compute_quality_score
# ═══════════════════════════════════════════════════════════════════

class TestComputeQualityScore:
    """Test _compute_quality_score with various metadata combinations."""

    def test_no_metadata(self):
        """Paper with no venue, code, or citations scores 0."""
        paper = _make_paper()
        score = _compute_quality_score(paper)
        assert score == 0.0

    def test_top_venue_neurips(self):
        """Paper at NeurIPS gets 0.15 venue bonus."""
        paper = _make_paper(venue="NeurIPS 2023")
        score = _compute_quality_score(paper)
        assert score == 0.15

    def test_top_venue_iclr(self):
        """Paper at ICLR gets 0.15 venue bonus."""
        paper = _make_paper(venue="ICLR 2024")
        score = _compute_quality_score(paper)
        assert score == 0.15

    def test_top_venue_case_insensitive(self):
        """Venue matching is case-insensitive."""
        paper = _make_paper(venue="icml 2022")
        score = _compute_quality_score(paper)
        assert score == 0.15

    def test_top_venue_substring_match(self):
        """Venue matching as substring (e.g., 'nature communications' contains 'nature')."""
        paper = _make_paper(venue="Nature Communications")
        score = _compute_quality_score(paper)
        assert score == 0.15

    def test_non_top_venue(self):
        """Paper at a non-top venue gets no bonus."""
        paper = _make_paper(venue="Journal of Unknown Things")
        score = _compute_quality_score(paper)
        assert score == 0.0

    def test_code_url_bonus(self):
        """Paper with code URL gets 0.10 bonus."""
        paper = _make_paper(code_url="https://github.com/example/repo")
        score = _compute_quality_score(paper)
        assert score == 0.10

    def test_high_citation_bonus(self):
        """Paper with >=50 citations gets 0.10 bonus."""
        paper = _make_paper(citation_count=100)
        score = _compute_quality_score(paper)
        assert score == 0.10

    def test_medium_citation_bonus(self):
        """Paper with 10-49 citations gets 0.05 bonus."""
        paper = _make_paper(citation_count=25)
        score = _compute_quality_score(paper)
        assert score == 0.05

    def test_low_citation_no_bonus(self):
        """Paper with <10 citations gets no bonus."""
        paper = _make_paper(citation_count=5)
        score = _compute_quality_score(paper)
        assert score == 0.0

    def test_combined_bonuses(self):
        """Multiple bonuses stack additively."""
        paper = _make_paper(
            venue="CVPR 2023",
            code_url="https://github.com/example/cvpr",
            citation_count=200,
        )
        score = _compute_quality_score(paper)
        assert score == 0.35  # 0.15 + 0.10 + 0.10

    def test_combined_bonuses_medium_cite(self):
        """Venue + code + medium citations."""
        paper = _make_paper(
            venue="ACL 2024",
            code_url="https://github.com/example/acl",
            citation_count=15,
        )
        score = _compute_quality_score(paper)
        assert score == 0.30  # 0.15 + 0.10 + 0.05

    def test_score_capped_at_one(self):
        """Score is capped at 1.0 even with excessive bonuses."""
        # In reality the bonuses sum to 0.40 max (0.15+0.10+0.10),
        # so this tests the min(score, 1.0) clamping
        paper = _make_paper(
            venue="Nature",
            code_url="https://github.com/example/nature",
            citation_count=1000,
        )
        score = _compute_quality_score(paper)
        assert 0.0 <= score <= 1.0

    def test_empty_venue_not_scored(self):
        """Empty string venue gets no bonus."""
        paper = _make_paper(venue="")
        score = _compute_quality_score(paper)
        assert score == 0.0


# ═══════════════════════════════════════════════════════════════════
# _format_paper_for_scoring
# ═══════════════════════════════════════════════════════════════════

class TestFormatPaperForScoring:
    """Test _format_paper_for_scoring output format and truncation."""

    def test_basic_format(self):
        """Basic paper with title and abstract."""
        paper = _make_paper(title="Test Paper", abstract="Test abstract text.")
        result = _format_paper_for_scoring(1, paper)
        assert "[1]" in result
        assert "Test Paper" in result
        assert "Test abstract text." in result

    def test_with_venue(self):
        """Paper with venue information."""
        paper = _make_paper(venue="NeurIPS 2023")
        result = _format_paper_for_scoring(3, paper)
        assert "发表场合：NeurIPS 2023" in result

    def test_with_citations(self):
        """Paper with citation count."""
        paper = _make_paper(citation_count=42)
        result = _format_paper_for_scoring(5, paper)
        assert "引用数：42" in result

    def test_with_code_url(self):
        """Paper with code URL."""
        paper = _make_paper(code_url="https://github.com/user/repo")
        result = _format_paper_for_scoring(7, paper)
        assert "代码：https://github.com/user/repo" in result

    def test_long_abstract_truncation(self):
        """Abstracts over 3000 characters are truncated."""
        long_abstract = "A" * 5000
        paper = _make_paper(abstract=long_abstract)
        result = _format_paper_for_scoring(1, paper)
        # The abstract in output should be at most 3000 chars
        # Find the abstract part after "摘要："
        abstract_part = result.split("摘要：", 1)[1]
        # The first line of abstract_part (before any newline) is the abstract
        abstract_line = abstract_part.split("\n")[0]
        assert len(abstract_line) <= 3000
        assert len(abstract_line) == 3000  # Exactly truncated at 3000

    def test_none_abstract(self):
        """Paper with None abstract handled gracefully."""
        paper = _make_paper(abstract=None)
        result = _format_paper_for_scoring(1, paper)
        # Should not crash
        assert "摘要：" in result
        assert "[1]" in result

    def test_zero_citations_omitted(self):
        """Citation count of 0 is omitted from output."""
        paper = _make_paper(citation_count=0)
        result = _format_paper_for_scoring(1, paper)
        assert "引用数" not in result

    def test_all_fields_present(self):
        """Paper with all optional fields includes everything."""
        paper = _make_paper(
            title="Complete Paper",
            abstract="An abstract.",
            venue="ICML 2024",
            citation_count=88,
            code_url="https://github.com/example/icml",
        )
        result = _format_paper_for_scoring(10, paper)
        assert "[10]" in result
        assert "Complete Paper" in result
        assert "发表场合：ICML 2024" in result
        assert "引用数：88" in result
        assert "代码：https://github.com/example/icml" in result
