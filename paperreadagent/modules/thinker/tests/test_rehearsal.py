"""Thinker RehearsalEngine tests."""
import pytest
from paperreadagent.core import create_core


@pytest.fixture
def core():
    c = create_core(config_path="config.yaml", db_path=":memory:")
    from paperreadagent.modules.thinker import register
    register(c)
    return c


@pytest.fixture
def engine(core):
    from paperreadagent.modules.thinker.rehearsal import RehearsalEngine
    return RehearsalEngine(core)


@pytest.mark.asyncio
async def test_create_rehearsal(engine):
    rid = await engine.create(
        title="Test Rehearsal",
        question_list_source="projects/test/qa.md",
        question_list_content="# Test\n1. What is this?\n2. How does it work?",
    )
    assert isinstance(rid, int)
    assert rid > 0

    row = engine._core.db.conn.execute(
        "SELECT * FROM thinker_rehearsals WHERE id = ?", (rid,)
    ).fetchone()
    assert row["title"] == "Test Rehearsal"
    assert row["status"] == "preparing"
    assert row["question_list_source"] == "projects/test/qa.md"
    assert "What is this" in row["question_list_content"]


@pytest.mark.asyncio
async def test_update_status_transitions(engine):
    rid = await engine.create(title="T", question_list_source="", question_list_content="")

    await engine.update_status(rid, "presenting")
    row = engine._core.db.conn.execute(
        "SELECT status FROM thinker_rehearsals WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "presenting"

    await engine.update_status(rid, "qa")
    await engine.update_status(rid, "summarizing")
    await engine.update_status(rid, "completed")
    row = engine._core.db.conn.execute(
        "SELECT status FROM thinker_rehearsals WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "completed"

    # Invalid status should raise
    with pytest.raises(ValueError):
        await engine.update_status(rid, "invalid_status")


@pytest.mark.asyncio
async def test_append_transcript(engine):
    rid = await engine.create(title="T", question_list_source="", question_list_content="")

    await engine.append_presentation_transcript(rid, "Hello world.")
    await engine.append_presentation_transcript(rid, " This is a test.")

    row = engine._core.db.conn.execute(
        "SELECT presentation_transcript FROM thinker_rehearsals WHERE id = ?", (rid,)
    ).fetchone()
    assert "Hello world. This is a test." in row["presentation_transcript"]


@pytest.mark.asyncio
async def test_append_qa_turn(engine):
    rid = await engine.create(title="T", question_list_source="", question_list_content="")

    await engine.append_qa_turn(rid, "What is the method?", "We used Latin square.")
    await engine.append_qa_turn(rid, "Why?", "To counterbalance.")

    row = engine._core.db.conn.execute(
        "SELECT qa_transcript FROM thinker_rehearsals WHERE id = ?", (rid,)
    ).fetchone()
    transcript = row["qa_transcript"]
    assert "What is the method?" in transcript
    assert "We used Latin square." in transcript
    assert "Why?" in transcript


@pytest.mark.asyncio
async def test_save_summary(engine):
    rid = await engine.create(title="T", question_list_source="", question_list_content="")

    corrections = [
        {"id": 1, "original": "the data shows", "corrected": "the data show", "note": "data is plural"},
    ]
    suggestions = [
        {"id": 1, "category": "content", "issue": "too vague", "suggestion": "be specific", "example": "..."},
    ]
    await engine.save_summary(
        rid,
        briefing="Overall good presentation.",
        grammar_corrections=corrections,
        suggestions=suggestions,
    )

    row = engine._core.db.conn.execute(
        "SELECT summary_briefing, summary_grammar_corrections, summary_suggestions, status "
        "FROM thinker_rehearsals WHERE id = ?", (rid,)
    ).fetchone()
    assert row["summary_briefing"] == "Overall good presentation."
    assert "the data shows" in row["summary_grammar_corrections"]
    assert "too vague" in row["summary_suggestions"]
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_list_rehearsals(engine):
    await engine.create(title="Rehearsal A", question_list_source="", question_list_content="")
    await engine.create(title="Rehearsal B", question_list_source="", question_list_content="")

    all_items = await engine.list_rehearsals()
    assert len(all_items) >= 2

    results = await engine.list_rehearsals(q="Rehearsal A")
    assert len(results) == 1
    assert results[0]["title"] == "Rehearsal A"


@pytest.mark.asyncio
async def test_get_rehearsal(engine):
    rid = await engine.create(title="Full Test", question_list_source="x.md", question_list_content="# Q")

    result = await engine.get_rehearsal(rid)
    assert result is not None
    assert result["title"] == "Full Test"
    assert result["status"] == "preparing"
    assert result["question_list_source"] == "x.md"


@pytest.mark.asyncio
async def test_delete_rehearsal(engine):
    rid = await engine.create(title="ToDelete", question_list_source="", question_list_content="")
    await engine.delete_rehearsal(rid)

    result = await engine.get_rehearsal(rid)
    assert result is None


@pytest.mark.asyncio
async def test_set_audio_path(engine):
    rid = await engine.create(title="T", question_list_source="", question_list_content="")
    test_path = "projects/test/sessions/001/full_audio.opus"
    await engine.set_audio_path(rid, test_path)

    row = engine._core.db.conn.execute(
        "SELECT full_audio_path FROM thinker_rehearsals WHERE id = ?", (rid,)
    ).fetchone()
    assert row["full_audio_path"] == test_path


@pytest.mark.asyncio
async def test_parse_summary_json():
    """Test JSON parsing helper with various LLM response formats."""
    from paperreadagent.modules.thinker.rehearsal import _parse_summary_json

    # Clean JSON
    raw = '{"briefing": "Good", "grammar_corrections": [], "suggestions": []}'
    result = _parse_summary_json(raw)
    assert result["briefing"] == "Good"
    assert result["grammar_corrections"] == []
    assert result["suggestions"] == []

    # With markdown code block
    raw2 = '```json\n{"briefing": "Fine", "grammar_corrections": [], "suggestions": []}\n```'
    result2 = _parse_summary_json(raw2)
    assert result2["briefing"] == "Fine"

    # With extra text around JSON
    raw3 = 'Here is the summary:\n{"briefing": "OK", "grammar_corrections": [], "suggestions": []}\nHope this helps!'
    result3 = _parse_summary_json(raw3)
    assert result3["briefing"] == "OK"
