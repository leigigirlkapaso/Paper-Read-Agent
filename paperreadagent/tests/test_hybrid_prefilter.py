"""Tests for agent1.hybrid_prefilter — dense + BM25 pre-filter via RRF."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from agent1.arxiv_searcher import PaperMeta


def _meta(arxiv_id="2301.00001", title="t", abstract="a"):
    return PaperMeta(
        arxiv_id=arxiv_id, title=title, authors=["A"], published="2023-01-01",
        abstract=abstract, pdf_url="", arxiv_url="", doi="",
    )


def _cfg(**over):
    c = {
        "enable_hybrid_prefilter": True,
        "prefilter_top_k": 150,
        "prefilter_min_input": 60,
        "prefilter_dense_weight": 0.5,
        "prefilter_embed_concurrency": 16,
        "prefilter_passage_chars": 1500,
        # llm config the lazy CoreLLM needs:
        "_llm_cfg": {"api_key": "k", "api_base_url": "https://x/v1",
                     "model_name": "m", "embedding_model": "BAAI/bge-m3",
                     "embedding_provider": "local"},
    }
    c.update(over)
    return c


def _patch_corellm(monkeypatch, embed_batch_return):
    """Make hybrid_prefilter's lazy CoreLLM use a stub with embed_batch -> given list."""
    from agent1 import hybrid_prefilter as HP
    fake = MagicMock()
    fake.embed_batch = AsyncMock(return_value=embed_batch_return)
    monkeypatch.setattr(HP, "_get_core_llm", lambda llm_cfg: fake)
    return fake


class TestHybridPrefilter:
    def test_disabled_returns_input_unchanged(self):
        from agent1.hybrid_prefilter import hybrid_prefilter
        papers = [_meta(f"id{i}") for i in range(200)]
        out = hybrid_prefilter(papers, "topic", _cfg(enable_hybrid_prefilter=False))
        assert out is papers or out == papers

    def test_small_pool_bypasses(self, monkeypatch):
        from agent1.hybrid_prefilter import hybrid_prefilter
        called = {"n": 0}
        def fake(_cfg): called["n"] += 1; return MagicMock()
        from agent1 import hybrid_prefilter as HP
        monkeypatch.setattr(HP, "_get_core_llm", fake)
        papers = [_meta(f"id{i}") for i in range(40)]
        out = hybrid_prefilter(papers, "topic", _cfg(prefilter_min_input=60))
        assert out == papers
        assert called["n"] == 0  # never even constructed CoreLLM

    def test_happy_path_filters_to_top_k(self, monkeypatch):
        from agent1.hybrid_prefilter import hybrid_prefilter
        N = 300
        papers = [_meta(f"id{i}", title=f"Title about machine learning {i}",
                        abstract=f"Abstract on topic {i}") for i in range(N)]
        # Return a distinct unit vector per paper so dense order is well-defined
        vecs = [[1.0 if j == (i % 5) else 0.0 for j in range(5)] for i in range(N)]
        query_vec = [1.0, 0.0, 0.0, 0.0, 0.0]  # aligns with first bucket
        _patch_corellm(monkeypatch, vecs + [query_vec])
        out = hybrid_prefilter(papers, "machine learning topic",
                               _cfg(prefilter_top_k=50))
        assert len(out) == 50
        assert all(isinstance(p, PaperMeta) for p in out)
        # original papers not mutated (relevance_score still default)
        assert all(p.relevance_score == 0.0 for p in papers)

    def test_rrf_fusion_combines_signals(self, monkeypatch):
        """A paper that BM25 ranks 1st (exact token match) should still rank
        high even when its dense rank is poor — proves RRF combines, not
        just picks one channel."""
        from agent1.hybrid_prefilter import hybrid_prefilter
        # 80 papers: paper "bm25_winner" has the exact query token "transformer"
        papers = []
        for i in range(79):
            papers.append(_meta(f"id{i}", title=f"misc {i}", abstract="generic"))
        papers.append(_meta("bm25_winner",
                            title="The Transformer architecture",
                            abstract="Self-attention transformer transformer"))
        # Make dense vectors put bm25_winner LAST in dense rank (orthogonal to query)
        vecs = [[0.0, 1.0]] * 79 + [[1.0, 0.0]]  # last paper orthogonal
        # Patch query embedding to align with [0,1] so first 79 dominate dense
        fake = _patch_corellm(monkeypatch, vecs + [[0.0, 1.0]])  # last is query

        out = hybrid_prefilter(papers, "transformer", _cfg(prefilter_top_k=20))
        winner_ids = {p.arxiv_id for p in out}
        assert "bm25_winner" in winner_ids  # BM25 channel rescued it

    def test_embed_failure_graceful(self, monkeypatch):
        from agent1.hybrid_prefilter import hybrid_prefilter
        from agent1 import hybrid_prefilter as HP
        fake = MagicMock()
        fake.embed_batch = AsyncMock(side_effect=RuntimeError("embed boom"))
        monkeypatch.setattr(HP, "_get_core_llm", lambda c: fake)
        papers = [_meta(f"id{i}") for i in range(100)]
        out = hybrid_prefilter(papers, "topic", _cfg())
        assert out == papers  # graceful: original list back

    def test_partial_embed_failure(self, monkeypatch):
        """Some passages get [] from embed_batch -> they get BM25-only score
        but the call must still succeed and return top_k results."""
        from agent1.hybrid_prefilter import hybrid_prefilter
        N = 100
        papers = [_meta(f"id{i}", title=f"t{i}", abstract="x") for i in range(N)]
        vecs = []
        for i in range(N):
            vecs.append([] if i % 3 == 0 else [1.0 if j == (i % 5) else 0.0 for j in range(5)])
        _patch_corellm(monkeypatch, vecs + [[1.0, 0.0, 0.0, 0.0, 0.0]])  # query last
        out = hybrid_prefilter(papers, "topic", _cfg(prefilter_top_k=30))
        assert len(out) == 30

    def test_dense_weight_extreme_zero_is_bm25_only(self, monkeypatch):
        from agent1.hybrid_prefilter import hybrid_prefilter
        papers = [
            _meta("p_match", title="transformer attention", abstract="self attention"),
            _meta("p_other1", title="a", abstract="b"),
            _meta("p_other2", title="c", abstract="d"),
        ] + [_meta(f"f{i}", title=f"f{i}", abstract="x") for i in range(70)]
        vecs = [[0.0, 1.0]] * 73 + [[1.0, 0.0]]  # query last, orthogonal to all
        _patch_corellm(monkeypatch, vecs)
        out = hybrid_prefilter(papers, "transformer attention",
                               _cfg(prefilter_top_k=5, prefilter_dense_weight=0.0))
        assert out[0].arxiv_id == "p_match"  # BM25 wins clearly when dense weight=0


class TestIntegrationWithPaperFilter:
    """Spec §6 integration: 300 papers -> hybrid_prefilter -> filter_papers
    (mock LLM) — verify LLM is called ~2× fewer times than without pre-filter."""

    def _run(self, papers, topic, cfg, monkeypatch):
        from agent1.hybrid_prefilter import hybrid_prefilter
        from agent1 import paper_filter as PF
        # Stub embed_batch with passages + 1 query slot at the end
        vecs = [[1.0 if j == (i % 7) else 0.0 for j in range(7)] for i in range(len(papers))]
        _patch_corellm(monkeypatch, vecs + [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]])

        # Stub LLM via the LLMClient interface filter_papers uses: llm.chat
        chat_calls = {"n": 0}
        def fake_chat(user_prompt, system_prompt=None, **kw):
            chat_calls["n"] += 1
            import re as _re, json as _json
            ids = _re.findall(r"\[(\d+)\]", user_prompt)
            return (_json.dumps([{"id": int(i), "score": 0.85} for i in ids]), {})
        llm = MagicMock()
        llm.chat = fake_chat

        prefiltered = hybrid_prefilter(papers, topic, cfg)
        filtered = PF.filter_papers(
            papers=prefiltered, topic=topic, llm=llm,
            relevance_threshold=0.8, max_download_papers=20, batch_size=10,
            max_concurrent=200,
        )
        return prefiltered, filtered, chat_calls["n"]

    def test_llm_call_count_drops(self, monkeypatch):
        papers = [_meta(f"id{i}", title=f"t{i}", abstract=f"a{i}") for i in range(300)]
        # WITH pre-filter
        pre, _, n_with = self._run(papers, "topic", _cfg(prefilter_top_k=150), monkeypatch)
        assert len(pre) == 150  # prefilter took effect
        # WITHOUT pre-filter
        _, _, n_without = self._run(papers, "topic",
                                    _cfg(enable_hybrid_prefilter=False), monkeypatch)
        # 300/10 = 30 vs 150/10 = 15. Roughly 2× drop. Allow slack for borderline retries.
        assert n_with * 2 <= n_without + 5, f"with={n_with}, without={n_without}"
