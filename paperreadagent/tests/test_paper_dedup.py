"""Unit tests for paperreadagent.utils.paper_dedup."""
import pytest

from utils.paper_dedup import (
    _is_real_arxiv_id,
    _pdf_url_priority,
    _longer_or_first,
    _real_or_first,
    _merge_platforms,
    _normalize_title,
    extract_identifiers,
    merge_papers,
)


def _make_paper(**overrides):
    """Build a PaperMeta with sensible defaults for terser tests."""
    from agent1.arxiv_searcher import PaperMeta
    defaults = dict(
        arxiv_id="", title="", authors=[], published="unknown",
        abstract="", pdf_url="", arxiv_url="", doi="",
        relevance_score=0.0, source_platform="", venue="",
        code_url="", citation_count=0,
    )
    defaults.update(overrides)
    return PaperMeta(**defaults)


class TestIsRealArxivId:
    def test_modern_id(self):
        assert _is_real_arxiv_id("2401.12345")
        assert _is_real_arxiv_id("2401.12345v2")
        assert _is_real_arxiv_id("2401.123456")  # 5-digit suffix is also valid

    def test_legacy_id(self):
        assert _is_real_arxiv_id("cs/0703123")
        assert _is_real_arxiv_id("math-ph/0506123")

    def test_not_real_arxiv(self):
        assert not _is_real_arxiv_id("oa_W12345")
        assert not _is_real_arxiv_id("pmid_12345")
        assert not _is_real_arxiv_id("dblp_conf_chi_X")
        assert not _is_real_arxiv_id("10.1145/3491102.3502067")
        assert not _is_real_arxiv_id("")
        assert not _is_real_arxiv_id(None)


class TestPdfUrlPriority:
    def test_arxiv_url_wins(self):
        assert _pdf_url_priority("https://arxiv.org/pdf/2401.12345") == 0

    def test_other_https_url(self):
        assert _pdf_url_priority("https://example.com/paper.pdf") == 1

    def test_non_https(self):
        assert _pdf_url_priority("http://example.com/paper.pdf") == 2

    def test_empty_url(self):
        assert _pdf_url_priority("") == 99
        assert _pdf_url_priority(None) == 99


class TestLongerOrFirst:
    def test_longer_wins(self):
        assert _longer_or_first("foo", "foobar") == "foobar"
        assert _longer_or_first("foobar", "foo") == "foobar"

    def test_same_length_returns_lex_smaller(self):
        # commutative tie-breaker: lexicographically smaller
        assert _longer_or_first("bbb", "aaa") == "aaa"
        assert _longer_or_first("aaa", "bbb") == "aaa"

    def test_one_empty(self):
        assert _longer_or_first("", "foo") == "foo"
        assert _longer_or_first("foo", "") == "foo"

    def test_both_empty(self):
        assert _longer_or_first("", "") == ""
        assert _longer_or_first(None, None) == ""


class TestRealOrFirst:
    def test_real_beats_unknown(self):
        assert _real_or_first("2024-01-01", "unknown") == "2024-01-01"
        assert _real_or_first("unknown", "2024-01-01") == "2024-01-01"

    def test_both_real_lex_smaller(self):
        assert _real_or_first("2024-01-01", "2025-01-01") == "2024-01-01"
        assert _real_or_first("2025-01-01", "2024-01-01") == "2024-01-01"

    def test_both_unknown(self):
        assert _real_or_first("unknown", "unknown") == "unknown"

    def test_empty_handled(self):
        assert _real_or_first("", "2024-01-01") == "2024-01-01"
        assert _real_or_first("", "") == ""


class TestMergePlatforms:
    def test_merge_distinct(self):
        assert _merge_platforms("arxiv", "dblp") == "arxiv,dblp"

    def test_merge_dedup(self):
        assert _merge_platforms("arxiv,dblp", "dblp") == "arxiv,dblp"

    def test_sorted_output(self):
        # commutative: same result regardless of order
        assert _merge_platforms("openalex", "arxiv") == "arxiv,openalex"
        assert _merge_platforms("arxiv", "openalex") == "arxiv,openalex"

    def test_empty(self):
        assert _merge_platforms("", "") == ""
        assert _merge_platforms("arxiv", "") == "arxiv"


class TestNormalizeTitle:
    def test_basic_lowercase_strip(self):
        assert _normalize_title("Foo: A Bar Baz.") == "foo a bar baz"

    def test_collapses_whitespace(self):
        assert _normalize_title("FOO  A   bar BAZ") == "foo a bar baz"

    def test_strips_latex_with_braces(self):
        assert _normalize_title(r"\textit{Foo}: A Bar") == "foo a bar"

    def test_strips_bare_latex(self):
        assert _normalize_title(r"Foo \LaTeX bar") == "foo bar"

    def test_strips_html(self):
        assert _normalize_title("<i>Foo</i>: A Bar") == "foo a bar"

    def test_handles_empty(self):
        assert _normalize_title("") == ""
        assert _normalize_title("   ") == ""


class TestExtractIdentifiers:
    def test_arxiv_native_id(self):
        p = _make_paper(arxiv_id="2401.12345", title="Foo")
        ids = extract_identifiers(p)
        assert ids["arxiv_id"] == "2401.12345"
        assert ids["title_norm"] == "foo"

    def test_arxiv_strips_version(self):
        p = _make_paper(arxiv_id="2401.12345v2")
        ids = extract_identifiers(p)
        assert ids["arxiv_id"] == "2401.12345"

    def test_arxiv_legacy_id(self):
        p = _make_paper(arxiv_id="cs/0703123")
        ids = extract_identifiers(p)
        assert ids["arxiv_id"] == "cs/0703123"

    def test_openalex_with_arxiv_field(self):
        p = _make_paper(arxiv_id="2401.12345", doi="10.1145/foo")
        ids = extract_identifiers(p)
        assert ids["arxiv_id"] == "2401.12345"
        assert ids["doi"] == "10.1145/foo"

    def test_openalex_pure(self):
        p = _make_paper(arxiv_id="oa_W12345", doi="10.1145/foo")
        ids = extract_identifiers(p)
        assert "arxiv_id" not in ids   # oa_W is NOT a real arxiv ID
        assert ids["oa_id"] == "w12345"
        assert ids["doi"] == "10.1145/foo"

    def test_dblp_raw_doi_in_arxiv_id_field(self):
        # DBLP sets arxiv_id = doi (no prefix) — clean_id from dblp_searcher
        p = _make_paper(arxiv_id="10.1145/3491102.3502067",
                        doi="10.1145/3491102.3502067")
        ids = extract_identifiers(p)
        # raw DOI in arxiv_id field is NOT recognised as arxiv_id
        assert "arxiv_id" not in ids
        assert ids["doi"] == "10.1145/3491102.3502067"

    def test_dblp_no_doi_uses_dblp_key(self):
        p = _make_paper(arxiv_id="dblp_conf_chi_Smith2024")
        ids = extract_identifiers(p)
        assert ids["dblp_key"] == "conf_chi_smith2024"

    def test_pmc_pmid(self):
        p = _make_paper(arxiv_id="pmid_12345678", doi="10.xxx/yyy")
        ids = extract_identifiers(p)
        assert ids["pmid"] == "12345678"
        assert ids["doi"] == "10.xxx/yyy"

    def test_pmc_pmcid(self):
        p = _make_paper(arxiv_id="pmcid_PMC1234")
        ids = extract_identifiers(p)
        assert ids["pmcid"] == "pmc1234"

    def test_arxiv_doi_reverse_lookup(self):
        """KEY: Bug 2 fix — 10.48550/arxiv.X DOI maps back to arxiv_id."""
        p = _make_paper(arxiv_id="10.48550/arxiv.2401.12345",
                        doi="10.48550/arxiv.2401.12345")
        ids = extract_identifiers(p)
        assert ids["doi"] == "10.48550/arxiv.2401.12345"
        assert ids["arxiv_id"] == "2401.12345"

    def test_doi_normalize_url_prefix(self):
        p = _make_paper(arxiv_id="oa_W1", doi="https://doi.org/10.1145/foo/")
        ids = extract_identifiers(p)
        assert ids["doi"] == "10.1145/foo"   # prefix + trailing slash stripped

    def test_empty_paper_returns_empty_dict(self):
        p = _make_paper()
        ids = extract_identifiers(p)
        assert ids == {}   # no identifiers, no title

    def test_title_only_paper(self):
        p = _make_paper(title="Some Paper")
        ids = extract_identifiers(p)
        assert ids == {"title_norm": "some paper"}

    def test_openreview_id(self):
        p = _make_paper(arxiv_id="or_2qE4mO5a4Q")
        ids = extract_identifiers(p)
        assert ids["or_id"] == "2qe4mo5a4q"  # lowercased
        assert "arxiv_id" not in ids   # or_X is NOT a real arxiv ID


class TestMergePapers:
    def test_real_arxiv_id_wins_over_oa(self):
        p1 = _make_paper(arxiv_id="2401.12345")
        p2 = _make_paper(arxiv_id="oa_W12345")
        merged = merge_papers(p1, p2)
        assert merged.arxiv_id == "2401.12345"

    def test_pdf_url_arxiv_priority(self):
        p1 = _make_paper(pdf_url="https://example.com/pdf")
        p2 = _make_paper(pdf_url="https://arxiv.org/pdf/2401.12345")
        assert merge_papers(p1, p2).pdf_url == "https://arxiv.org/pdf/2401.12345"
        assert merge_papers(p2, p1).pdf_url == "https://arxiv.org/pdf/2401.12345"

    def test_longer_title_wins(self):
        p1 = _make_paper(title="Foo")
        p2 = _make_paper(title="Foo: A Subtitle")
        assert merge_papers(p1, p2).title == "Foo: A Subtitle"
        assert merge_papers(p2, p1).title == "Foo: A Subtitle"

    def test_longer_abstract_wins(self):
        p1 = _make_paper(abstract="[DBLP 元数据] Foo")  # short DBLP placeholder
        p2 = _make_paper(abstract="A long real abstract paragraph here describing the work in detail.")
        assert "long real abstract" in merge_papers(p1, p2).abstract
        assert "long real abstract" in merge_papers(p2, p1).abstract

    def test_real_date_beats_unknown(self):
        p1 = _make_paper(published="unknown")
        p2 = _make_paper(published="2024-03-15")
        assert merge_papers(p1, p2).published == "2024-03-15"
        assert merge_papers(p2, p1).published == "2024-03-15"

    def test_doi_filled_from_dup(self):
        p1 = _make_paper(arxiv_id="2401.12345", doi="")
        p2 = _make_paper(arxiv_id="2401.12345", doi="10.48550/arxiv.2401.12345")
        merged = merge_papers(p1, p2)
        assert merged.doi == "10.48550/arxiv.2401.12345"

    def test_source_platform_concat_sorted_dedup(self):
        p1 = _make_paper(source_platform="dblp")
        p2 = _make_paper(source_platform="arxiv")
        assert merge_papers(p1, p2).source_platform == "arxiv,dblp"
        assert merge_papers(p2, p1).source_platform == "arxiv,dblp"

    def test_source_platform_dedups(self):
        p1 = _make_paper(source_platform="arxiv,dblp")
        p2 = _make_paper(source_platform="dblp")
        assert merge_papers(p1, p2).source_platform == "arxiv,dblp"

    def test_citation_count_max(self):
        p1 = _make_paper(citation_count=3)
        p2 = _make_paper(citation_count=10)
        assert merge_papers(p1, p2).citation_count == 10
        assert merge_papers(p2, p1).citation_count == 10

    def test_authors_longer_list_wins(self):
        p1 = _make_paper(authors=["A", "B"])
        p2 = _make_paper(authors=["A", "B", "C", "D", "E"])
        assert merge_papers(p1, p2).authors == ["A", "B", "C", "D", "E"]
        assert merge_papers(p2, p1).authors == ["A", "B", "C", "D", "E"]

    def test_authors_same_length_lex_smaller(self):
        p1 = _make_paper(authors=["B"])
        p2 = _make_paper(authors=["A"])
        # commutative: lex-smaller list wins
        assert merge_papers(p1, p2).authors == ["A"]
        assert merge_papers(p2, p1).authors == ["A"]

    def test_relevance_score_reset(self):
        p1 = _make_paper(relevance_score=0.8)
        p2 = _make_paper(relevance_score=0.3)
        assert merge_papers(p1, p2).relevance_score == 0.0

    def test_commutative_full(self):
        """Spec §3.1: merge(A, B) == merge(B, A) for all fields."""
        p1 = _make_paper(arxiv_id="2401.12345", title="Foo",
                         abstract="full abstract", source_platform="arxiv",
                         doi="", published="2024-01-01", citation_count=5,
                         authors=["A", "B"], code_url="https://github.com/a")
        p2 = _make_paper(arxiv_id="oa_W12345", title="Foo: Subtitle",
                         abstract="", source_platform="openalex",
                         doi="10.1145/foo", published="unknown", citation_count=8,
                         authors=["A", "B", "C"], code_url="")
        a = merge_papers(p1, p2)
        b = merge_papers(p2, p1)
        assert a == b


from utils.paper_dedup import dedup_papers


class TestDedupPapers:
    def test_empty_input(self):
        assert dedup_papers([]) == []

    def test_no_duplicates_all_kept(self):
        p1 = _make_paper(arxiv_id="2401.12345", title="Foo")
        p2 = _make_paper(arxiv_id="2401.99999", title="Bar")
        p3 = _make_paper(arxiv_id="2402.00000", title="Baz")
        result = dedup_papers([p1, p2, p3])
        assert len(result) == 3

    def test_dedup_arxiv_doi_link(self):
        """Bug 2 regression: arxiv preprint + DBLP record with arxiv-DOI."""
        p_arxiv = _make_paper(arxiv_id="2401.12345", title="Foo",
                              source_platform="arxiv")
        p_dblp = _make_paper(arxiv_id="10.48550/arxiv.2401.12345",
                             doi="10.48550/arxiv.2401.12345",
                             title="Foo", source_platform="dblp")
        result = dedup_papers([p_arxiv, p_dblp])
        assert len(result) == 1
        assert "arxiv" in result[0].source_platform
        assert "dblp" in result[0].source_platform

    def test_dedup_dblp_openalex_same_doi(self):
        """Bug 1 regression: DBLP raw-doi + OpenAlex oa_id with matching DOI."""
        p_dblp = _make_paper(arxiv_id="10.1145/3491102.3502067",
                             doi="10.1145/3491102.3502067",
                             title="A CHI Paper", source_platform="dblp")
        p_oa = _make_paper(arxiv_id="oa_W12345",
                           doi="10.1145/3491102.3502067",
                           title="A CHI Paper", source_platform="openalex")
        result = dedup_papers([p_dblp, p_oa])
        assert len(result) == 1
        assert result[0].source_platform == "dblp,openalex"

    def test_dedup_pmc_openalex_same_doi(self):
        """Bug 3 regression: PMC pmid + OpenAlex oa_id with matching DOI."""
        p_pmc = _make_paper(arxiv_id="pmid_12345", doi="10.xxx/yyy",
                            title="A Paper", source_platform="pmc")
        p_oa = _make_paper(arxiv_id="oa_W67890", doi="10.xxx/yyy",
                           title="A Paper", source_platform="openalex")
        result = dedup_papers([p_pmc, p_oa])
        assert len(result) == 1
        assert result[0].source_platform == "openalex,pmc"

    def test_dedup_title_only_match(self):
        """No IDs, but identical normalised titles → merged."""
        p1 = _make_paper(title="Foo: Bar.")
        p2 = _make_paper(title="Foo Bar")
        # both normalise to "foo bar"
        result = dedup_papers([p1, p2])
        assert len(result) == 1

    def test_dedup_title_normalized_diff_punctuation(self):
        p1 = _make_paper(title="Foo: A Bar")
        p2 = _make_paper(title="Foo - A Bar")
        result = dedup_papers([p1, p2])
        assert len(result) == 1

    def test_bridge_three_clusters(self):
        """Union-Find: A bridges B and C into one cluster."""
        p1 = _make_paper(arxiv_id="2401.12345", title="Foo",
                         source_platform="arxiv")
        p2 = _make_paper(arxiv_id="oa_W12", doi="10.48550/arxiv.2401.12345",
                         title="Foo Different", source_platform="openalex")
        p3 = _make_paper(arxiv_id="dblp_X", doi="10.48550/arxiv.2401.12345",
                         title="Foo Yet Another", source_platform="dblp")
        # p1 ~ p2 via arxiv_id (reverse-lookup); p2 ~ p3 via DOI; bridge → 1 cluster
        for order in [[p1, p2, p3], [p3, p1, p2], [p2, p3, p1], [p3, p2, p1]]:
            result = dedup_papers(order)
            assert len(result) == 1, f"order {[p.source_platform for p in order]}"
            assert set(result[0].source_platform.split(",")) == {"arxiv", "dblp", "openalex"}

    def test_dedup_no_identifier_dropped(self):
        """A paper with no IDs and empty title is dropped."""
        p1 = _make_paper(arxiv_id="", title="", doi="")
        p2 = _make_paper(arxiv_id="2401.12345", title="Foo")
        result = dedup_papers([p1, p2])
        assert len(result) == 1
        assert result[0].arxiv_id == "2401.12345"

    def test_order_independent_output_size(self):
        """Same set of papers, different orders, same output size."""
        ps = [
            _make_paper(arxiv_id="2401.10001"),
            _make_paper(arxiv_id="2401.10002"),
            _make_paper(arxiv_id="2401.10001", title="Same as first"),  # dup of [0]
            _make_paper(arxiv_id="2401.10003"),
        ]
        sizes = {len(dedup_papers(list(reversed(ps)))),
                 len(dedup_papers(ps)),
                 len(dedup_papers([ps[2], ps[0], ps[3], ps[1]]))}
        assert sizes == {3}   # exactly one size: 3

    def test_log_output(self, caplog):
        """Verify the diagnostic log is emitted."""
        import logging
        with caplog.at_level(logging.INFO, logger="utils.paper_dedup"):
            dedup_papers([
                _make_paper(arxiv_id="2401.10001", source_platform="arxiv"),
                _make_paper(arxiv_id="2401.10002", source_platform="arxiv"),
            ])
        msgs = [r.message for r in caplog.records]
        assert any("输入 2 篇" in m for m in msgs)
        assert any("输出 2 篇" in m for m in msgs)
