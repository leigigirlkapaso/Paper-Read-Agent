"""Tests for utils.ai_style_critique."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from utils.ai_style_critique import critique_ai_style, rewrite_deai


def _db_with_summary():
    db = MagicMock()
    db.get_paper = MagicMock(return_value={"id": 1, "session_id": 1, "summary_path": "", "arxiv_id": "x"})
    db.get_paper_summaries = MagicMock(return_value=[{"content": "一段精读分析"}])
    db.get_session = MagicMock(return_value=None)
    return db


def _fake_llm(reply):
    llm = MagicMock()
    llm.achat = AsyncMock(return_value=(reply, {}))
    return llm


def _valid_critique_json():
    return json.dumps({
        "overall_score": 72, "level": "高",
        "dimension_scores": {"套路化结构": 80, "空泛措辞": 75,
                             "缺具体数据": 60, "缺个人视角": 85, "过度对仗": 50},
        "flagged": [{"excerpt": "该方法显著提升性能", "dimension": "空泛措辞",
                     "reason": "无数值", "fix": "改为 52%→78%"}],
        "suggestions": ["加具体数字", "加反例", "加第一人称推断"],
    }, ensure_ascii=False)


class TestCritique:
    @pytest.mark.asyncio
    async def test_happy(self):
        llm = _fake_llm(_valid_critique_json())
        out = await critique_ai_style("一段分析文本", llm)
        assert out["overall_score"] == 72
        assert out["level"] == "高"
        assert len(out["dimension_scores"]) == 5
        assert out["flagged"][0]["dimension"] == "空泛措辞"
        assert len(out["suggestions"]) >= 3

    @pytest.mark.asyncio
    async def test_markdown_fence(self):
        llm = _fake_llm("```json\n" + _valid_critique_json() + "\n```")
        out = await critique_ai_style("text", llm)
        assert out["overall_score"] == 72

    @pytest.mark.asyncio
    async def test_empty_text_no_llm_call(self):
        llm = _fake_llm(_valid_critique_json())
        out = await critique_ai_style("   ", llm)
        assert "error" in out
        llm.achat.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_error(self):
        llm = MagicMock()
        llm.achat = AsyncMock(side_effect=RuntimeError("LLM down"))
        out = await critique_ai_style("text", llm)
        assert "error" in out
        assert out.get("overall_score") is None

    @pytest.mark.asyncio
    async def test_unparseable_returns_error(self):
        llm = _fake_llm("not json at all")
        out = await critique_ai_style("text", llm)
        assert "error" in out


class TestRewrite:
    @pytest.mark.asyncio
    async def test_happy(self):
        llm = _fake_llm("改写后的正文，含 52%→78% 等具体数据。")
        out = await rewrite_deai("原文", {"suggestions": ["加数字"]}, llm)
        assert "改写后的正文" in out

    @pytest.mark.asyncio
    async def test_empty_text(self):
        llm = _fake_llm("x")
        out = await rewrite_deai("", {}, llm)
        assert out == ""
        llm.achat.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self):
        llm = MagicMock()
        llm.achat = AsyncMock(side_effect=RuntimeError("down"))
        out = await rewrite_deai("原文", {}, llm)
        assert out == ""


class TestRoutes:
    def _req(self, db, llm):
        req = MagicMock()
        req.app.state.db = db
        req.app.state.core.llm = llm
        return req

    @pytest.mark.asyncio
    async def test_critique_route_404(self):
        from web.routes import papers as P
        from fastapi import HTTPException
        db = MagicMock(); db.get_paper = MagicMock(return_value=None)
        with pytest.raises(HTTPException) as ei:
            await P.ai_critique(self._req(db, MagicMock()), 999)
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    async def test_critique_route_happy(self, monkeypatch):
        from web.routes import papers as P
        db = _db_with_summary()
        async def fake_critique(text, llm):
            return {"overall_score": 50, "level": "中", "dimension_scores": {},
                    "flagged": [], "suggestions": []}
        monkeypatch.setattr(P, "critique_ai_style", fake_critique)
        resp = await P.ai_critique(self._req(db, MagicMock()), 1)
        assert resp["overall_score"] == 50

    @pytest.mark.asyncio
    async def test_rewrite_route_happy(self, monkeypatch):
        from web.routes import papers as P
        db = _db_with_summary()
        async def fake_rewrite(text, critique, llm):
            return "去 AI 改写版"
        monkeypatch.setattr(P, "rewrite_deai", fake_rewrite)
        req = self._req(db, MagicMock())
        req.json = AsyncMock(return_value={"critique": {"suggestions": ["加数字"]}})
        resp = await P.ai_rewrite(req, 1)
        assert resp["rewritten"] == "去 AI 改写版"
