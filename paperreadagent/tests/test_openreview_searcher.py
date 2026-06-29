"""Tests for paperreadagent.agent1.openreview_searcher."""
import json
from unittest.mock import patch, MagicMock
import pytest

from agent1.openreview_searcher import (
    search_openreview,
    _format_venue,
    _parse_note_to_paper,
    ROBOTICS_INVITATIONS,
)


class TestFormatVenue:
    def test_corl_accept(self):
        v = _format_venue("robot-learning.org/CoRL/2024/Conference", "Accept")
        assert "CoRL" in v and "2024" in v and "[reject]" not in v

    def test_iclr_reject(self):
        v = _format_venue("ICLR.cc/2025/Conference", "Reject")
        assert "ICLR" in v and "2025" in v and "[reject]" in v

    def test_withdrawn_marked(self):
        v = _format_venue("ICLR.cc/2024/Conference", "Withdrawn")
        assert "[reject]" in v

    def test_neurips_workshop(self):
        v = _format_venue("NeurIPS.cc/2024/Workshop/RoboLearning", "Accept")
        assert "NeurIPS" in v


class TestParseNoteToPaper:
    def test_full_note(self):
        note = {
            "id": "2qE4mO5a4Q",
            "cdate": 1700000000000,  # epoch millis
            "content": {
                "title": {"value": "A Robotic Tactile Paper"},
                "authors": {"value": ["Alice", "Bob"]},
                "abstract": {"value": "We propose a new tactile method."},
                "venue": {"value": "ICLR 2025 Conference"},
            },
        }
        p = _parse_note_to_paper(note, "ICLR.cc/2025/Conference", "Accept")
        assert p.arxiv_id == "or_2qE4mO5a4Q"
        assert p.title == "A Robotic Tactile Paper"
        assert p.authors == ["Alice", "Bob"]
        assert "tactile method" in p.abstract
        assert "ICLR" in p.venue
        assert "2025" in p.venue
        assert p.pdf_url == "https://openreview.net/pdf?id=2qE4mO5a4Q"
        assert p.source_platform == "openreview"
        assert p.doi == ""

    def test_reject_venue_tagged(self):
        note = {
            "id": "abc123",
            "cdate": 1700000000000,
            "content": {
                "title": {"value": "Some Paper"},
                "authors": {"value": ["X"]},
                "abstract": {"value": "abs"},
                "venue": {"value": "ICLR 2024 Conference"},
            },
        }
        p = _parse_note_to_paper(note, "ICLR.cc/2024/Conference", "Reject")
        assert "[reject]" in p.venue

    def test_missing_abstract_empty_string(self):
        note = {
            "id": "x",
            "cdate": 1700000000000,
            "content": {
                "title": {"value": "T"},
                "authors": {"value": []},
            },  # no abstract / venue
        }
        p = _parse_note_to_paper(note, "robot-learning.org/CoRL/2024/Conference", "Accept")
        assert p.abstract == ""

    def test_invitation_list_nonempty(self):
        assert len(ROBOTICS_INVITATIONS) >= 8
        assert any("CoRL" in inv for inv in ROBOTICS_INVITATIONS)
        assert any("ICLR" in inv for inv in ROBOTICS_INVITATIONS)


class TestSearchOpenreview:
    def test_empty_queries_returns_empty(self):
        result = search_openreview([])
        assert result == []

    def test_search_calls_for_each_invitation(self, monkeypatch):
        """Each query x each invitation = one API call."""
        from agent1 import openreview_searcher as ors

        calls = []

        def fake_run(query, invitation):
            calls.append((query, invitation))
            return []

        monkeypatch.setattr(ors, "_run_query_for_invitation", fake_run)
        monkeypatch.setattr(ors, "ROBOTICS_INVITATIONS", [
            "ICLR.cc/2024/Conference",
            "robot-learning.org/CoRL/2024/Conference",
        ])

        search_openreview(queries=["tactile robot", "manipulation"], max_queries=2)
        # 2 queries x 2 invitations = 4 calls
        assert len(calls) == 4

    def test_dedup_within_searcher(self, monkeypatch):
        """If the same OR id appears in two queries, only one PaperMeta returned."""
        from agent1 import openreview_searcher as ors
        from agent1.arxiv_searcher import PaperMeta

        def fake_run(query, invitation):
            return [PaperMeta(
                arxiv_id="or_dup", title="Paper", authors=[], published="2024",
                abstract="a", pdf_url="", arxiv_url="", doi="",
                relevance_score=0, source_platform="openreview", venue="ICLR 2024",
                code_url="", citation_count=0,
            )]

        monkeypatch.setattr(ors, "_run_query_for_invitation", fake_run)
        monkeypatch.setattr(ors, "ROBOTICS_INVITATIONS", ["ICLR.cc/2024/Conference"])

        result = search_openreview(queries=["q1", "q2"])
        assert len(result) == 1   # deduped within searcher
