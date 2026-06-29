"""Tests for paperreadagent.agent1.crossref_searcher."""
import json
import pytest

from agent1.crossref_searcher import (
    search_crossref,
    _strip_jats,
    _parse_item_to_paper,
    _ALLOWED_TYPES,
)


class TestStripJats:
    def test_basic_jats_p(self):
        assert _strip_jats("<jats:p>Hello world</jats:p>") == "Hello world"

    def test_nested_tags(self):
        s = "<jats:p>This is <jats:italic>contact-rich</jats:italic> manipulation.</jats:p>"
        result = _strip_jats(s)
        assert "<jats:" not in result
        assert "contact-rich manipulation" in result

    def test_collapse_whitespace(self):
        s = "<jats:p>Foo  bar\n\nbaz</jats:p>"
        assert _strip_jats(s) == "Foo bar baz"

    def test_empty(self):
        assert _strip_jats("") == ""
        assert _strip_jats(None) == ""


class TestParseItemToPaper:
    def test_journal_article(self):
        item = {
            "DOI": "10.1145/foo",
            "title": ["A Tactile Paper"],
            "author": [{"given": "Alice", "family": "Smith"},
                        {"given": "Bob", "family": "Jones"}],
            "issued": {"date-parts": [[2024]]},
            "abstract": "<jats:p>This is the abstract.</jats:p>",
            "container-title": ["IJRR"],
            "URL": "https://doi.org/10.1145/foo",
            "type": "journal-article",
            "is-referenced-by-count": 5,
        }
        p = _parse_item_to_paper(item)
        assert p.doi == "10.1145/foo"
        assert p.title == "A Tactile Paper"
        assert p.authors == ["Alice Smith", "Bob Jones"]
        assert p.published.startswith("2024")
        assert p.abstract == "This is the abstract."
        assert p.venue == "IJRR"
        assert p.citation_count == 5
        assert p.source_platform == "crossref"
        assert p.arxiv_id == ""

    def test_proceedings_article(self):
        item = {
            "DOI": "10.1109/iros.2024.foo",
            "title": ["IROS Paper"],
            "author": [{"given": "C", "family": "D"}],
            "issued": {"date-parts": [[2024]]},
            "container-title": ["2024 IEEE/RSJ IROS"],
            "URL": "...",
            "type": "proceedings-article",
        }
        p = _parse_item_to_paper(item)
        assert p.doi == "10.1109/iros.2024.foo"
        assert "IROS" in p.venue

    def test_missing_abstract_empty(self):
        item = {
            "DOI": "10.x/y", "title": ["T"], "author": [],
            "issued": {"date-parts": [[2024]]},
            "container-title": [], "URL": "", "type": "journal-article",
        }
        p = _parse_item_to_paper(item)
        assert p.abstract == ""

    def test_missing_title_returns_none(self):
        """Items without title are unparseable; return None."""
        item = {"DOI": "10.x/y", "author": [], "type": "journal-article"}
        result = _parse_item_to_paper(item)
        assert result is None


class TestSearchCrossref:
    def test_empty_queries(self):
        assert search_crossref([]) == []

    def test_search_filters_disallowed_types(self, monkeypatch):
        """type=dataset should be filtered out."""
        from agent1 import crossref_searcher as crs

        def fake_fetch(*args, **kwargs):
            body = json.dumps({
                "message": {
                    "items": [
                        {"DOI": "10.x/1", "title": ["P1"], "author": [],
                         "issued": {"date-parts": [[2024]]},
                         "container-title": ["IJRR"], "URL": "",
                         "type": "journal-article"},
                        {"DOI": "10.x/2", "title": ["Dataset"], "author": [],
                         "issued": {"date-parts": [[2024]]},
                         "container-title": ["X"], "URL": "",
                         "type": "dataset"},
                    ]
                }
            }).encode()
            return body, 200

        monkeypatch.setattr(crs, "limited_fetch_sync", fake_fetch)
        result = search_crossref(["query"], max_queries=1)
        assert len(result) == 1
        assert result[0].doi == "10.x/1"

    def test_min_year_filter(self, monkeypatch):
        from agent1 import crossref_searcher as crs

        def fake_fetch(*args, **kwargs):
            body = json.dumps({
                "message": {
                    "items": [
                        {"DOI": "10.x/old", "title": ["Old"], "author": [],
                         "issued": {"date-parts": [[2020]]},
                         "container-title": ["X"], "URL": "",
                         "type": "journal-article"},
                        {"DOI": "10.x/new", "title": ["New"], "author": [],
                         "issued": {"date-parts": [[2024]]},
                         "container-title": ["X"], "URL": "",
                         "type": "journal-article"},
                    ]
                }
            }).encode()
            return body, 200

        monkeypatch.setattr(crs, "limited_fetch_sync", fake_fetch)
        result = search_crossref(["q"], min_year=2024, max_queries=1)
        assert len(result) == 1
        assert result[0].doi == "10.x/new"
