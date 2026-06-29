"""Tests for Ideator project-brief: DataAccess CRUD + context + service."""
import json
import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock

from paperreadagent.modules.ideator.schema import MIGRATIONS, LATEST_VERSION
from paperreadagent.modules.ideator.data_access import DataAccess


def _make_core_with_db():
    """Fake Core whose .db.conn is in-memory SQLite with all ideator migrations
    applied, plus dict_row/dict_rows helpers."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for v in range(1, LATEST_VERSION + 1):
        if v in MIGRATIONS:
            conn.executescript(MIGRATIONS[v])
    conn.commit()

    class FakeDB:
        def __init__(self, c):
            self.conn = c
        @staticmethod
        def dict_row(row):
            return dict(row) if row is not None else None
        @staticmethod
        def dict_rows(rows):
            return [dict(r) for r in rows]

    core = MagicMock()
    core.db = FakeDB(conn)
    return core, conn


def _insert_spark(conn, content="idea X", depth="deep draft", roundtable_id=None):
    cur = conn.execute(
        "INSERT INTO ideator_sparks (content, depth_content, status, roundtable_id) "
        "VALUES (?, ?, 'deep_done', ?)",
        (content, depth, roundtable_id),
    )
    conn.commit()
    return cur.lastrowid


class TestBriefCRUD:
    def test_insert_and_get_brief(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        bid = data.insert_project_brief(sid)
        assert bid > 0
        brief = data.get_project_brief(bid)
        assert brief["spark_id"] == sid
        assert brief["status"] == "generating"

    def test_update_brief_status_and_json(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        bid = data.insert_project_brief(sid)
        data.update_project_brief(bid, status="done",
                                  brief_json=json.dumps({"feasibility": {}}),
                                  model_name="deepseek-v4-pro")
        brief = data.get_project_brief(bid)
        assert brief["status"] == "done"
        assert json.loads(brief["brief_json"]) == {"feasibility": {}}
        assert brief["model_name"] == "deepseek-v4-pro"

    def test_update_brief_rejects_unknown_columns(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        bid = data.insert_project_brief(sid)
        data.update_project_brief(bid, spark_id=999, bogus="x", status="done")
        brief = data.get_project_brief(bid)
        assert brief["spark_id"] == sid
        assert brief["status"] == "done"

    def test_list_briefs_newest_first(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        b1 = data.insert_project_brief(sid)
        b2 = data.insert_project_brief(sid)
        briefs = data.list_project_briefs(sid)
        assert [b["id"] for b in briefs] == [b2, b1]

    def test_get_missing_brief_returns_none(self):
        core, _ = _make_core_with_db()
        data = DataAccess(core)
        assert data.get_project_brief(99999) is None


class TestGatherContext:
    def test_context_without_roundtable(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn, content="my idea", depth="my deepening")
        ctx = data.gather_brief_context(sid)
        assert ctx["spark_content"] == "my idea"
        assert ctx["depth_content"] == "my deepening"
        assert ctx["cross_links"] == []
        assert ctx["team_memory"] == []

    def test_context_with_cross_links(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        conn.execute(
            "INSERT INTO ideator_cross_links "
            "(source_a_type, source_a_id, source_b_type, source_b_id, link_type, reasoning, spark_id) "
            "VALUES ('paper', 1, 'paper', 2, 'similarity', 'they both do X', ?)", (sid,))
        conn.commit()
        ctx = data.gather_brief_context(sid)
        assert len(ctx["cross_links"]) == 1
        assert "they both do X" in ctx["cross_links"][0]["reasoning"]

    def test_context_with_roundtable_team_memory(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        rt = conn.execute("INSERT INTO ideator_roundtables (spark_id) VALUES (0)").lastrowid
        conn.commit()
        sid = _insert_spark(conn, roundtable_id=rt)
        conn.execute(
            "INSERT INTO ideator_team_memory (roundtable_id, spark_id, memory_type, content) "
            "VALUES (?, ?, 'open_question', 'is X scalable?')", (rt, sid))
        conn.execute(
            "INSERT INTO ideator_team_memory (roundtable_id, spark_id, memory_type, content) "
            "VALUES (?, ?, 'assumption', 'assume Y holds')", (rt, sid))
        conn.commit()
        ctx = data.gather_brief_context(sid)
        types = {m["memory_type"] for m in ctx["team_memory"]}
        assert "open_question" in types and "assumption" in types

    def test_context_missing_spark_raises(self):
        core, _ = _make_core_with_db()
        data = DataAccess(core)
        with pytest.raises(ValueError):
            data.gather_brief_context(99999)


from paperreadagent.modules.ideator.project_brief import ProjectBriefService


def _valid_brief_json():
    return json.dumps({
        "feasibility": {"score": 3, "required_resources": ["GPU"],
                        "knowledge_prereqs": ["RL"], "estimated_duration": "6 月",
                        "team_size": "1-2", "main_challenges": ["数据稀缺"],
                        "mitigation_strategies": ["合成数据"]},
        "theory": {"theoretical_basis": "...", "key_hypotheses": ["H1"],
                   "related_work_grounding": "..."},
        "experiment_plan": {"phases": [{"phase": "P1", "goal": "g",
                            "methods": ["m"], "deliverables": ["d"],
                            "milestone": "ms", "est_duration": "2 月"}]},
        "expected_results": {"success_scenario": "s", "partial_scenario": "p",
                             "failure_scenario": "f", "rescue_plan": "r"},
        "risk_assessment": {"risks": [{"type": "technical", "description": "d",
                            "severity": "med", "mitigation": "m"}]},
        "differentiation": {"vs_existing_work": "v", "novelty_claim": "n",
                            "why_feasible_now": "w", "signals_to_watch": "x->ok, y->rethink"},
        "evidence_confidence": "证据充分",
    })


class TestProjectBriefService:
    @pytest.mark.asyncio
    async def test_generate_happy_path(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn, content="idea", depth="draft")
        fake_llm = MagicMock()
        fake_llm.load_prompt = MagicMock(return_value="prompt")
        fake_llm.chat = AsyncMock(return_value=_valid_brief_json())
        svc = ProjectBriefService(core, data, llm=fake_llm)
        brief_id = await svc.generate(sid)
        brief = data.get_project_brief(brief_id)
        assert brief["status"] == "done"
        parsed = json.loads(brief["brief_json"])
        assert set(parsed.keys()) >= {"feasibility", "theory", "experiment_plan",
                                      "expected_results", "risk_assessment", "differentiation"}
        cs = json.loads(brief["context_sources"])
        assert cs["depth_content"] is True

    @pytest.mark.asyncio
    async def test_generate_parses_json_with_markdown_fence(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        fenced = "```json\n" + _valid_brief_json() + "\n```"
        fake_llm = MagicMock()
        fake_llm.load_prompt = MagicMock(return_value="prompt")
        fake_llm.chat = AsyncMock(return_value=fenced)
        svc = ProjectBriefService(core, data, llm=fake_llm)
        bid = await svc.generate(sid)
        assert data.get_project_brief(bid)["status"] == "done"

    @pytest.mark.asyncio
    async def test_generate_llm_failure_marks_failed(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        fake_llm = MagicMock()
        fake_llm.load_prompt = MagicMock(return_value="prompt")
        fake_llm.chat = AsyncMock(side_effect=RuntimeError("LLM down"))
        svc = ProjectBriefService(core, data, llm=fake_llm)
        bid = await svc.generate(sid)
        brief = data.get_project_brief(bid)
        assert brief["status"] == "failed"
        assert "LLM down" in brief["error"]

    @pytest.mark.asyncio
    async def test_generate_unparseable_json_marks_failed(self):
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        fake_llm = MagicMock()
        fake_llm.load_prompt = MagicMock(return_value="prompt")
        fake_llm.chat = AsyncMock(return_value="this is not json at all")
        svc = ProjectBriefService(core, data, llm=fake_llm)
        bid = await svc.generate(sid)
        assert data.get_project_brief(bid)["status"] == "failed"

    @pytest.mark.asyncio
    async def test_generate_missing_spark_raises(self):
        core, _ = _make_core_with_db()
        data = DataAccess(core)
        fake_llm = MagicMock()
        svc = ProjectBriefService(core, data, llm=fake_llm)
        with pytest.raises(ValueError):
            await svc.generate(99999)


class TestProjectBriefRoutes:
    def _fake_request(self, core):
        req = MagicMock()
        req.app.state.core = core
        return req

    @pytest.mark.asyncio
    async def test_generate_route_creates_done_brief(self, monkeypatch):
        from paperreadagent.modules.ideator import routes as R
        core, conn = _make_core_with_db()
        sid = _insert_spark(conn, content="idea", depth="draft")

        async def fake_generate(self, spark_id):
            bid = self._data.insert_project_brief(spark_id)
            self._data.update_project_brief(bid, status="done",
                                            brief_json=json.dumps({"feasibility": {}}))
            return bid
        monkeypatch.setattr(R.ProjectBriefService, "generate", fake_generate)

        resp = await R.generate_project_brief(self._fake_request(core), sid)
        assert resp["status"] == "done"
        assert "brief_id" in resp

    @pytest.mark.asyncio
    async def test_list_route_returns_briefs(self):
        from paperreadagent.modules.ideator import routes as R
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        data.insert_project_brief(sid)
        resp = await R.list_project_briefs(self._fake_request(core), sid)
        assert resp["spark_id"] == sid
        assert len(resp["briefs"]) == 1

    @pytest.mark.asyncio
    async def test_get_route_returns_brief(self):
        from paperreadagent.modules.ideator import routes as R
        core, conn = _make_core_with_db()
        data = DataAccess(core)
        sid = _insert_spark(conn)
        bid = data.insert_project_brief(sid)
        resp = await R.get_project_brief(self._fake_request(core), bid)
        assert resp["id"] == bid

    @pytest.mark.asyncio
    async def test_get_route_missing_returns_404(self):
        from paperreadagent.modules.ideator import routes as R
        from fastapi import HTTPException
        core, _ = _make_core_with_db()
        with pytest.raises(HTTPException) as ei:
            await R.get_project_brief(self._fake_request(core), 99999)
        assert ei.value.status_code == 404


class TestProjectBriefIntegration:
    @pytest.mark.asyncio
    async def test_end_to_end_generate_then_fetch(self, monkeypatch):
        """Full flow: spark with rich context -> generate via service ->
        list + get via routes -> brief_json has all 6 dimensions, and rich
        context (depth + cross_links + team_memory) was injected."""
        from paperreadagent.modules.ideator import routes as R
        core, conn = _make_core_with_db()
        data = DataAccess(core)

        rt = conn.execute("INSERT INTO ideator_roundtables (spark_id) VALUES (0)").lastrowid
        conn.commit()
        sid = _insert_spark(conn, content="tactile world model",
                            depth="detailed draft", roundtable_id=rt)
        conn.execute("INSERT INTO ideator_cross_links "
                     "(source_a_type, source_a_id, source_b_type, source_b_id, link_type, reasoning, spark_id) "
                     "VALUES ('paper',1,'paper',2,'contradiction','A says X, B says not-X', ?)", (sid,))
        conn.execute("INSERT INTO ideator_team_memory (roundtable_id, spark_id, memory_type, content) "
                     "VALUES (?, ?, 'open_question', 'does it scale?')", (rt, sid))
        conn.commit()

        captured = {}
        def fake_load_prompt(mod, name, **kw):
            if name == "project_brief_user":
                captured.update(kw)
            return name
        fake_llm = MagicMock()
        fake_llm.load_prompt = MagicMock(side_effect=fake_load_prompt)
        fake_llm.chat = AsyncMock(return_value=_valid_brief_json())
        fake_llm.model_for = MagicMock(return_value="deepseek-v4-pro")

        monkeypatch.setattr(
            "paperreadagent.modules.ideator.project_brief.IdeatorLLM",
            lambda **kw: fake_llm,
        )

        svc = ProjectBriefService(core, data)   # builds patched IdeatorLLM
        bid = await svc.generate(sid)

        brief = data.get_project_brief(bid)
        assert brief["status"] == "done"
        parsed = json.loads(brief["brief_json"])
        for dim in ("feasibility", "theory", "experiment_plan",
                    "expected_results", "risk_assessment", "differentiation"):
            assert dim in parsed

        assert captured["depth_content"] == "detailed draft"
        assert len(captured["cross_links"]) == 1
        assert len(captured["team_memory"]) == 1

        req = MagicMock(); req.app.state.core = core
        listed = await R.list_project_briefs(req, sid)
        assert any(x["id"] == bid for x in listed["briefs"])
        fetched = await R.get_project_brief(req, bid)
        assert fetched["id"] == bid
