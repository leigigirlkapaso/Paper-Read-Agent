"""Tests for /sessions/{id}/compare-table route."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import MagicMock
import pytest


def _req(db):
    req = MagicMock()
    req.app.state.db = db
    return req


class TestCompareTable:
    @pytest.mark.asyncio
    async def test_compare_table_happy(self):
        from web.routes import sessions as S
        db = MagicMock()
        db.get_session.return_value = {"id": 5, "project_id": 1}
        db.get_session_extractions.return_value = [
            {"id": 1, "arxiv_id": "a1", "title": "T1", "relevance_score": 0.9,
             "extraction": {"problem": "p1", "methods": ["m1"], "datasets": [],
                            "metrics": [], "baselines": [], "limitations": [],
                            "contributions": []}},
            {"id": 2, "arxiv_id": "a2", "title": "T2", "relevance_score": 0.8,
             "extraction": {"problem": "p2", "methods": [], "datasets": [],
                            "metrics": [], "baselines": [], "limitations": [],
                            "contributions": []}},
        ]
        db.get_session_papers.return_value = [
            {"id": 1, "summary_status": "success"},
            {"id": 2, "summary_status": "success"},
            {"id": 3, "summary_status": "success"},  # has summary but no extraction
        ]
        resp = await S.compare_table(_req(db), 5)
        assert len(resp["papers"]) == 2
        assert resp["papers"][0]["arxiv_id"] == "a1"
        assert resp["missing_count"] == 1

    @pytest.mark.asyncio
    async def test_compare_table_empty(self):
        from web.routes import sessions as S
        db = MagicMock()
        db.get_session.return_value = {"id": 5, "project_id": 1}
        db.get_session_extractions.return_value = []
        db.get_session_papers.return_value = []
        resp = await S.compare_table(_req(db), 5)
        assert resp["papers"] == []
        assert resp["missing_count"] == 0

    @pytest.mark.asyncio
    async def test_compare_table_session_not_found(self):
        from web.routes import sessions as S
        from fastapi import HTTPException
        db = MagicMock()
        db.get_session.return_value = None
        with pytest.raises(HTTPException) as ei:
            await S.compare_table(_req(db), 999)
        assert ei.value.status_code == 404


    @pytest.mark.asyncio
    async def test_compare_table_excludes_pending_from_missing(self):
        """missing_count must only count papers with summary_status='success'/'cached'
        but no extraction. Pending papers don't count as 'missing extraction'."""
        from web.routes import sessions as S
        db = MagicMock()
        db.get_session.return_value = {"id": 5, "project_id": 1}
        db.get_session_extractions.return_value = []  # no extractions yet
        db.get_session_papers.return_value = [
            {"id": 1, "summary_status": "pending"},  # don't count
            {"id": 2, "summary_status": "failed"},   # don't count
            {"id": 3, "summary_status": "success"},  # count: 1
            {"id": 4, "summary_status": "cached"},   # count: 2
        ]
        resp = await S.compare_table(_req(db), 5)
        assert resp["missing_count"] == 2


class TestReanalyzeFilter:
    """The reanalyze flow must not skip papers that have summary content
    but no structured extraction (extraction_json is NULL).

    Strategy: point session_dir at a non-existent path so papers_dir.exists()
    is False and pdf_files is empty. Then papers that pass the summary filter
    fall into skipped_no_pdf; papers that hit the summary filter are counted
    in skipped_has_content. We inspect the [Reanalyze] log line to verify
    which bucket each paper landed in.
    """

    @pytest.mark.asyncio
    async def test_paper_missing_extraction_is_not_skipped_by_summary_filter(
        self, capsys
    ):
        """Paper has summary content but extraction_json=NULL → must be passed
        through the summary filter (and only then fall into skipped_no_pdf
        because we have no PDFs on disk in this test)."""
        from web.routes import sessions as S
        db = MagicMock()
        db.get_session.return_value = {
            "id": 5,
            "project_id": 1,
            "session_dir": "definitely/does/not/exist/abcxyz",
        }
        db.get_session_papers.return_value = [
            {
                "id": 1,
                "arxiv_id": "a1",
                "title": "T1",
                "authors": [],
                "published": "",
                "abstract": "",
                "source_url": "",
                "relevance_score": 0.9,
                "download_status": "success",
                "summary_status": "success",
                "extraction_json": None,  # ← the key bit
                "pdf_path": None,
            },
        ]
        db.get_paper_summaries.return_value = [{"content": "old summary text"}]

        req = _req(db)
        await S.session_reanalyze(req, 5)

        out = capsys.readouterr().out
        # Filter must NOT have skipped this paper as "已有内容";
        # since no PDF exists on disk, it should land in skipped(无PDF).
        assert "跳过(有内容)=0" in out
        assert "跳过(无PDF)=1" in out

    @pytest.mark.asyncio
    async def test_paper_with_both_summary_and_extraction_is_skipped(
        self, capsys
    ):
        """Paper has summary content AND extraction_json set → still skipped
        as 'has content' (original behavior preserved)."""
        from web.routes import sessions as S
        db = MagicMock()
        db.get_session.return_value = {
            "id": 5,
            "project_id": 1,
            "session_dir": "definitely/does/not/exist/abcxyz",
        }
        db.get_session_papers.return_value = [
            {
                "id": 1,
                "arxiv_id": "a1",
                "title": "T1",
                "authors": [],
                "published": "",
                "abstract": "",
                "source_url": "",
                "relevance_score": 0.9,
                "download_status": "success",
                "summary_status": "success",
                "extraction_json": '{"problem": "p1"}',  # ← already extracted
                "pdf_path": None,
            },
        ]
        db.get_paper_summaries.return_value = [{"content": "old summary text"}]

        req = _req(db)
        await S.session_reanalyze(req, 5)

        out = capsys.readouterr().out
        # Filter MUST skip this one — it has both summary and extraction.
        assert "跳过(有内容)=1" in out
        assert "跳过(无PDF)=0" in out
