"""Integration test: pipeline parses <JSON> block and persists to papers.extraction_json."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import AsyncMock, MagicMock
import pytest

from agent1.arxiv_searcher import PaperMeta
from db.database import Database


def _paper() -> PaperMeta:
    return PaperMeta(
        arxiv_id="2401.00001", title="Test", authors=["A"], published="2024-01-01",
        abstract="abs", pdf_url="", arxiv_url="", doi="",
    )


def _seed_db(tmp_path: Path):
    db = Database(str(tmp_path / "t.db"))
    project_id = db.create_project("p", "")
    sid = db.create_session(project_id, "full", {}, str(tmp_path / "s1"))
    # Same dict shape used in test_db_extraction_column.py (Task 2's discovery):
    db.insert_papers(sid, [{
        "arxiv_id": "2401.00001", "title": "Test", "authors": [],
        "published": "2024", "abstract": "abs", "source_url": "", "doi": "",
        "relevance_score": 0.9, "source_platform": "",
    }])
    return db, sid


def _raw_llm_output() -> str:
    return """## 关键数据卡
- Acc: 92.3% on ImageNet val

## 未提取自检
- table 3 deeper details not extracted

## 结构化抽取
<JSON>
{
  "problem": "Recognize images",
  "methods": ["CNN", "Transformer"],
  "datasets": ["ImageNet"],
  "metrics": [{"name": "Acc", "value": "92.3%", "condition": "ImageNet val"}],
  "baselines": ["ResNet"],
  "limitations": ["English only"],
  "contributions": ["Faster"]
}
</JSON>
"""


@pytest.mark.asyncio
async def test_do_llm_read_persists_extraction(tmp_path):
    db, sid = _seed_db(tmp_path)
    llm = MagicMock()
    llm.model_name = "test-model"
    llm.temperature = 0.3
    usage = MagicMock(); usage.total_tokens = 100
    llm.achat = AsyncMock(return_value=(_raw_llm_output(), usage))

    from agent2.pipeline import _do_llm_read

    md_card = await _do_llm_read(
        paper=_paper(),
        pdf_text="some pdf text long enough to NOT match cache-passthrough so the LLM branch runs",
        summary_prompt="say something",
        topic="test topic",
        llm=llm,
        summary_dir=tmp_path,
        max_chars=110000,
        db=db,
        session_id=sid,
    )

    # md_card still contains the markdown (existing path unchanged)
    assert "Acc: 92.3%" in md_card

    # DB now has extraction_json on the paper row
    extractions = db.get_session_extractions(sid)
    assert len(extractions) == 1
    e = extractions[0]["extraction"]
    assert e["problem"] == "Recognize images"
    assert e["methods"] == ["CNN", "Transformer"]
    assert e["metrics"][0]["name"] == "Acc"


@pytest.mark.asyncio
async def test_do_llm_read_extraction_failure_does_not_break_summary(tmp_path):
    """LLM forgets the <JSON> tag → extraction stays NULL, but md_card still saved."""
    db, sid = _seed_db(tmp_path)
    llm = MagicMock()
    llm.model_name = "test-model"
    llm.temperature = 0.3
    usage = MagicMock(); usage.total_tokens = 50
    llm.achat = AsyncMock(return_value=("## just markdown\nno json block here", usage))

    from agent2.pipeline import _do_llm_read

    md_card = await _do_llm_read(
        paper=_paper(),
        pdf_text="some pdf text long enough to NOT match cache-passthrough so the LLM branch runs",
        summary_prompt="say something",
        topic="test topic",
        llm=llm,
        summary_dir=tmp_path,
        max_chars=110000,
        db=db,
        session_id=sid,
    )

    # md_card still saved
    assert "just markdown" in md_card
    # No extractions persisted (parse failure → NULL)
    assert db.get_session_extractions(sid) == []


@pytest.mark.asyncio
async def test_do_llm_read_cache_passthrough_backfills_extraction(tmp_path):
    """File-cache passthrough (pdf_text starts with '### ') must still backfill
    papers.extraction_json from the cached markdown's <JSON> block, so the row
    isn't left with NULL extraction forever."""
    db, sid = _seed_db(tmp_path)
    llm = MagicMock()
    llm.model_name = "test-model"
    llm.temperature = 0.3
    llm.achat = AsyncMock(return_value=("should not be called", MagicMock(total_tokens=0)))

    from agent2.pipeline import _do_llm_read

    # Simulate a file-cached markdown card already containing <JSON> block.
    # Starting with "### " triggers the passthrough branch.
    cached_card = "### " + _raw_llm_output()

    md = await _do_llm_read(
        paper=_paper(),
        pdf_text=cached_card,
        summary_prompt="prompt",
        topic="topic",
        llm=llm,
        summary_dir=tmp_path,
        max_chars=110000,
        db=db,
        session_id=sid,
    )

    # passthrough returns the cached card unchanged
    assert md is cached_card or md == cached_card
    # but extraction was backfilled
    extractions = db.get_session_extractions(sid)
    assert len(extractions) == 1
    assert extractions[0]["extraction"]["problem"] == "Recognize images"
    # LLM was NOT called (this is cache passthrough)
    llm.achat.assert_not_called()
