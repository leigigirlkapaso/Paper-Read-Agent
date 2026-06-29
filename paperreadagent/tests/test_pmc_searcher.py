"""
tests/test_pmc_searcher.py
Unit tests for pure functions in agent1/pmc_searcher.py.
No external API calls, no database required.
"""

from __future__ import annotations

import pytest
from unittest import mock

from agent1.pmc_searcher import _strip_arxiv_syntax, _run_query


# ═══════════════════════════════════════════════════════════════════
# _strip_arxiv_syntax
# ═══════════════════════════════════════════════════════════════════

class TestStripArxivSyntax:
    """Test _strip_arxiv_syntax which removes arxiv-specific search syntax."""

    def test_strips_all_prefix(self):
        """Strips all: prefix."""
        result = _strip_arxiv_syntax('all:"haptic rendering"')
        assert result == "haptic rendering"

    def test_strips_ti_prefix(self):
        """Strips ti: prefix."""
        result = _strip_arxiv_syntax("ti:brain computer interface")
        assert result == "brain computer interface"

    def test_strips_abs_prefix(self):
        """Strips abs: prefix."""
        result = _strip_arxiv_syntax("abs:deep learning for EEG")
        assert result == "deep learning for EEG"

    def test_strips_au_prefix(self):
        """Strips au: prefix."""
        result = _strip_arxiv_syntax("au:Smith AND fMRI")
        assert result == "Smith fMRI"

    def test_strips_cat_prefix(self):
        """Strips cat: prefix but preserves the category value."""
        result = _strip_arxiv_syntax("cat:cs.AI neural networks")
        assert result == "cs.AI neural networks"

    def test_strips_AND_operator(self):
        """Strips AND boolean operator."""
        result = _strip_arxiv_syntax("fNIRS AND Brain-Computer Interface")
        assert result == "fNIRS Brain-Computer Interface"

    def test_strips_OR_operator(self):
        """Strips OR boolean operator."""
        result = _strip_arxiv_syntax("EEG OR fNIRS OR MEG")
        assert result == "EEG fNIRS MEG"

    def test_strips_quotes(self):
        """Strips double quote characters."""
        result = _strip_arxiv_syntax('"motor imagery" BCI')
        assert result == "motor imagery BCI"

    def test_collapses_whitespace(self):
        """Collapses multiple spaces into single spaces."""
        result = _strip_arxiv_syntax("  all:foo   AND   bar  ")
        assert result == "foo bar"

    def test_empty_input(self):
        """Empty string returns empty string."""
        result = _strip_arxiv_syntax("")
        assert result == ""

    def test_only_operators(self):
        """String with only operators and prefix keywords returns empty."""
        result = _strip_arxiv_syntax("AND OR ti: AND")
        assert result == ""


# ═══════════════════════════════════════════════════════════════════
# _run_query
# ═══════════════════════════════════════════════════════════════════

PMC_MOCK_RESPONSE = {
    "version": "6.8",
    "hitCount": 3,
    "request": {
        "query": "fNIRS brain computer interface",
        "resultType": "core",
        "pageSize": 100,
    },
    "resultList": {
        "result": [
            {
                "id": "PMC12345",
                "source": "MED",
                "pmid": "12345678",
                "pmcid": "PMC12345",
                "title": "Advances in fNIRS-based Brain-Computer Interfaces",
                "authorString": "Smith J, Doe A, Lee K",
                "journalTitle": "Nature Neuroscience",
                "pubYear": 2024,
                "abstractText": "Functional near-infrared spectroscopy (fNIRS) has emerged as a promising non-invasive brain imaging modality for brain-computer interfaces.",
                "pubType": "research-article",
            },
            {
                "id": "PMC67890",
                "source": "PMC",
                "pmid": "",
                "pmcid": "PMC67890",
                "title": "A Review of Haptic Feedback for Neurorehabilitation",
                "authorString": "Wang L, Chen Y",
                "journalTitle": "Journal of Neural Engineering",
                "pubYear": 2023,
                "abstractText": "Haptic feedback plays a crucial role in neurorehabilitation systems by providing sensory information to patients.",
                "pubType": "review",
            },
            {
                "id": "PMC11111",
                "source": "MED",
                "pmid": "",
                "pmcid": "",
                "title": "No ID Paper That Should Be Skipped",
                "authorString": "Nobody",
                "journalTitle": "Unknown Journal",
                "pubYear": 2022,
                "abstractText": "This paper has no PMID and no PMCID and should be skipped by the searcher.",
                "pubType": "research-article",
            },
        ]
    },
}


class TestRunQuery:
    """Test _run_query with mocked API responses."""

    @pytest.fixture
    def mock_successful_get(self):
        """Mock requests.get returning the standard PMC response."""
        with mock.patch("agent1.pmc_searcher.requests.get") as mock_get:
            mock_resp = mock.Mock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = PMC_MOCK_RESPONSE
            mock_get.return_value = mock_resp
            yield mock_get

    def test_paper_with_pmid_only(self, mock_successful_get):
        """Paper with only PMID constructs arxiv_id = pmid_{pmid}."""
        papers: list = []
        seen_ids: set[str] = set()

        n = _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

        # We expect 2 papers (the 3rd has no PMID/PMCID and should be skipped)
        assert n == 2

        # First paper has PMID
        pmid_paper = papers[0]
        assert pmid_paper.arxiv_id == "pmid_12345678"
        assert pmid_paper.title == "Advances in fNIRS-based Brain-Computer Interfaces"
        assert pmid_paper.authors == ["Smith J", "Doe A", "Lee K"]
        assert pmid_paper.published == "2024-01-01"
        assert pmid_paper.venue == "Nature Neuroscience"
        assert pmid_paper.source_platform == "pmc"
        assert pmid_paper.pdf_url == ""

    def test_paper_with_pmcid_only(self, mock_successful_get):
        """Paper with only PMCID constructs arxiv_id = pmcid_{pmcid}."""
        papers: list = []
        seen_ids: set[str] = set()

        _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

        # Second paper has PMCID only (pmid empty string)
        pmcid_paper = papers[1]
        assert pmcid_paper.arxiv_id == "pmcid_PMC67890"
        assert pmcid_paper.title == "A Review of Haptic Feedback for Neurorehabilitation"
        assert pmcid_paper.authors == ["Wang L", "Chen Y"]
        assert pmcid_paper.published == "2023-01-01"
        assert pmcid_paper.venue == "Journal of Neural Engineering"
        assert pmcid_paper.source_platform == "pmc"
        assert pmcid_paper.pdf_url == ""

    def test_source_platform_is_pmc(self, mock_successful_get):
        """All papers from PMC searcher have source_platform == 'pmc'."""
        papers: list = []
        seen_ids: set[str] = set()

        _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

        for paper in papers:
            assert paper.source_platform == "pmc", (
                f"Expected source_platform='pmc', got '{paper.source_platform}' "
                f"for paper '{paper.title}'"
            )

    def test_skipped_when_no_pmid_and_no_pmcid(self, mock_successful_get):
        """Papers with no PMID and no PMCID are skipped."""
        papers: list = []
        seen_ids: set[str] = set()

        _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

        # Only 2 papers should be added (the 3rd has no identifiers)
        assert len(papers) == 2
        arxiv_ids = {p.arxiv_id for p in papers}
        assert "pmid_12345678" in arxiv_ids
        assert "pmcid_PMC67890" in arxiv_ids
        # No paper without identifiers should be present
        for p in papers:
            assert p.arxiv_id in ("pmid_12345678", "pmcid_PMC67890")

    def test_arxiv_url_with_pmid(self, mock_successful_get):
        """arxiv_url uses MEDLINE URL when PMID is present."""
        papers: list = []
        seen_ids: set[str] = set()

        _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

        pmid_paper = papers[0]
        assert pmid_paper.arxiv_url == "https://europepmc.org/article/MED/12345678"

    def test_arxiv_url_with_pmcid_only(self, mock_successful_get):
        """arxiv_url uses PMC URL when only PMCID is present."""
        papers: list = []
        seen_ids: set[str] = set()

        _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

        pmcid_paper = papers[1]
        assert pmcid_paper.arxiv_url == "https://europepmc.org/article/PMC/PMC67890"

    def test_duplicate_detection(self, mock_successful_get):
        """Papers already in seen_ids are not added again."""
        papers: list = []
        seen_ids: set[str] = {"pmid_12345678"}  # Pre-mark first paper as seen

        n = _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

        # Only the PMCID-only paper should be added
        assert n == 1
        assert len(papers) == 1
        assert papers[0].arxiv_id == "pmcid_PMC67890"

    def test_abstract_required(self, mock_successful_get):
        """Override mock to return a paper with empty abstract — it should be skipped."""
        with mock.patch("agent1.pmc_searcher.requests.get") as mock_get:
            mock_resp = mock.Mock()
            mock_resp.status_code = 200
            response_no_abstract = {
                "resultList": {
                    "result": [
                        {
                            "pmid": "99999999",
                            "pmcid": "",
                            "title": "Paper Without Abstract",
                            "authorString": "Author X",
                            "journalTitle": "Some Journal",
                            "pubYear": 2021,
                            "abstractText": "",
                        }
                    ]
                }
            }
            mock_resp.json.return_value = response_no_abstract
            mock_get.return_value = mock_resp

            papers: list = []
            seen_ids: set[str] = set()

            n = _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

            assert n == 0
            assert len(papers) == 0

    def test_title_required(self, mock_successful_get):
        """Override mock to return a paper with no title — it should be skipped."""
        with mock.patch("agent1.pmc_searcher.requests.get") as mock_get:
            mock_resp = mock.Mock()
            mock_resp.status_code = 200
            response_no_title = {
                "resultList": {
                    "result": [
                        {
                            "pmid": "99999999",
                            "pmcid": "",
                            "title": "",
                            "authorString": "Author X",
                            "journalTitle": "Some Journal",
                            "pubYear": 2021,
                            "abstractText": "Valid abstract text.",
                        }
                    ]
                }
            }
            mock_resp.json.return_value = response_no_title
            mock_get.return_value = mock_resp

            papers: list = []
            seen_ids: set[str] = set()

            n = _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

            assert n == 0
            assert len(papers) == 0

    def test_empty_result_list(self):
        """Empty resultList returns 0 papers."""
        with mock.patch("agent1.pmc_searcher.requests.get") as mock_get:
            mock_resp = mock.Mock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"resultList": {"result": []}}
            mock_get.return_value = mock_resp

            papers: list = []
            seen_ids: set[str] = set()

            n = _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

            assert n == 0
            assert len(papers) == 0

    def test_doi_stores_pmid_or_pmcid(self, mock_successful_get):
        """doi field stores PMID when available, otherwise PMCID."""
        papers: list = []
        seen_ids: set[str] = set()

        _run_query("test", limit=100, min_year=0, seen_ids=seen_ids, papers=papers)

        # Paper with PMID stores PMID in doi
        assert papers[0].doi == "12345678"
        # Paper with PMCID only stores PMCID in doi
        assert papers[1].doi == "PMC67890"
