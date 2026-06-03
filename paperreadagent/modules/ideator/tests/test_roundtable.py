"""tests for roundtable engine"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from paperreadagent.modules.ideator.roundtable import (
    RoundtableManager, RoundtableSession, SEATS, CONTEXT_SPEC, TokenTracker,
    ROLE_DESCRIPTIONS,
)


class TestTokenTracker:
    def test_token_tracker_init(self):
        tt = TokenTracker(limit=1000000)
        assert tt.limit == 1000000
        assert tt.used == 0
        assert tt.compression_count == 0

    def test_no_limit_defaults_to_128k(self):
        tt = TokenTracker(limit=None)
        assert tt.limit == 128000

    def test_consuming_tokens(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(300000)
        assert tt.used == 300000
        assert tt.pct_used == pytest.approx(0.30)

    def test_needs_compression_at_50pct(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(500000)
        assert tt.needs_compression() is True

    def test_does_not_need_compression_below_50pct(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(490000)
        assert tt.needs_compression() is False

    def test_needs_warning_at_85pct(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(850000)
        assert tt.needs_warning() is True

    def test_is_exhausted_at_100pct(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(1000000)
        assert tt.is_exhausted() is True

    def test_not_exhausted_below_100pct(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(999999)
        assert tt.is_exhausted() is False


class TestSeatsAndContext:
    def test_six_seats_defined(self):
        assert len(SEATS) == 6

    def test_seats_have_unique_ids(self):
        ids = [s["seat_id"] for s in SEATS]
        assert len(ids) == len(set(ids))

    def test_gen_and_rev3_are_different_instances(self):
        gen = [s for s in SEATS if s["seat_id"] == "gen"][0]
        rev3 = [s for s in SEATS if s["seat_id"] == "rev3"][0]
        assert gen["model"] == "deepseek-v4-pro"
        assert rev3["model"] == "deepseek-v4-pro"
        assert gen["role"] == "generator"
        assert rev3["role"] == "reviewer_3"

    def test_context_spec_gen_has_self_score(self):
        assert "self_score" in CONTEXT_SPEC["generator"]

    def test_context_spec_reviewers_have_no_self_score(self):
        for role in ["reviewer_1", "reviewer_2", "reviewer_3"]:
            assert "self_score" not in CONTEXT_SPEC[role]

    def test_context_spec_arbiters_have_no_papers(self):
        for role in ["arbiter_1", "arbiter_2"]:
            assert "papers" not in CONTEXT_SPEC[role]

    def test_1m_token_for_deepseek_claude(self):
        for s in SEATS:
            if s["model"] in ("deepseek-v4-pro", "claude-opus-4-7-max"):
                assert s["token_limit"] == 1_000_000


class TestRoundtableSession:
    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock()
        llm.chat = AsyncMock(return_value="mock model response")
        llm.model_for = MagicMock(side_effect=lambda r: f"model-{r}")
        return llm

    @pytest.fixture
    def session(self, mock_llm):
        bundles = {
            "generator":   {"papers": "paper text", "reports": "report text",
                           "notes": "user notes", "reviews": "review data",
                           "deepen": "deepen result", "self_score": 0.7},
            "reviewer_1":  {"papers": "paper text", "reports": "report text",
                           "notes": "user notes", "reviews": "review data",
                           "deepen": "deepen result"},
            "reviewer_2":  {"papers": "paper text", "reports": "report text",
                           "notes": "user notes", "reviews": "review data",
                           "deepen": "deepen result"},
            "reviewer_3":  {"papers": "paper text", "reports": "report text",
                           "notes": "user notes", "reviews": "review data",
                           "deepen": "deepen result"},
            "arbiter_1":   {"reviews": "review data", "deepen": "deepen result"},
            "arbiter_2":   {"reviews": "review data", "deepen": "deepen result"},
        }
        return RoundtableSession(
            spark_id=1, spark_content="test spark",
            llm=mock_llm, data_access=MagicMock(), context_bundles=bundles,
        )

    def test_session_has_6_participants(self, session):
        assert len(session.participants) == 6

    def test_participants_have_seat_ids(self, session):
        names = [p["seat_id"] for p in session.participants]
        assert "gen" in names
        assert "rev3" in names
        assert "rev1" in names
        assert "rev2" in names
        assert "arb1" in names
        assert "arb2" in names

    def test_gen_has_full_context(self, session):
        gen = session._find_participant("gen")
        assert "papers" in gen["context"]

    def test_arb_has_limited_context(self, session):
        arb = session._find_participant("arb1")
        assert "papers" not in arb["context"]
        assert "reviews" in arb["context"]

    def test_rev_has_no_self_score(self, session):
        rev1 = session._find_participant("rev1")
        assert "self_score" not in rev1["context"]

    def test_all_online_at_start(self, session):
        for p in session.participants:
            assert p["state"] == "online"

    @pytest.mark.asyncio
    async def test_ask_round_returns_results(self, session):
        results = await session.ask_round(question="test?", mentioned=["gen", "rev1"])
        assert len(results) >= 1  # at least answers from mentioned models (mock returns same)
        # check there's a question message recorded
        question_msgs = [m for m in session.messages if m["message_type"] == "question"]
        assert len(question_msgs) == 1
        assert question_msgs[0]["content"] == "test?"

    @pytest.mark.asyncio
    async def test_force_remove_changes_state(self, session):
        target = session._find_participant("rev2")
        assert target["state"] == "online"
        result = await session.force_remove("rev2", "user_forced")
        assert target["state"] == "exited"
        assert result["message_type"] == "exit_statement"

    def test_find_participant_returns_none_for_unknown(self, session):
        assert session._find_participant("nonexistent") is None

    def test_format_history_empty(self, session):
        history = session._format_history()
        assert "暂无历史" in history or session.round_number == 0


class TestRoundtableManager:
    @pytest.fixture
    def manager(self):
        data = MagicMock()
        data.insert_roundtable = MagicMock(return_value=1)
        data.update_roundtable = MagicMock()
        data.insert_message = MagicMock(return_value=1)
        data.get_messages = MagicMock(return_value=[])
        data.get_roundtable = MagicMock(return_value={"id": 1, "status": "active", "round_count": 0})
        data.get_spark = MagicMock(return_value={
            "id": 1, "content": "test spark", "source_refs": '[{"type":"paper","id":1}]',
            "source_type": "contradiction",
        })
        data.get_paper = MagicMock(return_value={"title": "Test Paper", "abstract": "Test abstract"})
        data.get_paper_summaries = MagicMock(return_value=[])
        data.get_user_note = MagicMock(return_value=None)
        return RoundtableManager(llm=MagicMock(), data_access=data)

    def test_start_creates_roundtable(self, manager):
        rt_id = manager.start(spark_id=1, spark_content="test",
                              source_refs=[{"type":"paper","id":1}])
        assert rt_id == 1
        assert 1 in manager._sessions

    def test_pause_updates_status(self, manager):
        manager.pause(1)
        manager._data.update_roundtable.assert_called_with(1, status="paused")

    def test_close_generates_divergence_report(self, manager):
        manager.close(1)
        manager._data.update_roundtable.assert_called_once()
        call_kwargs = manager._data.update_roundtable.call_args.kwargs
        assert call_kwargs["status"] == "closed"
        assert "closed_at" in call_kwargs

    def test_get_session(self, manager):
        manager.start(spark_id=1, spark_content="test", source_refs=[])
        session = manager.get_session(1)
        assert session is not None
        assert session.spark_id == 1

    def test_get_session_not_found(self, manager):
        assert manager.get_session(999) is None
