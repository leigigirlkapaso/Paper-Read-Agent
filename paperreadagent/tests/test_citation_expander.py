"""Tests for citation snowballing: OpenAlex graph fetch + orchestration."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # import agent1.* like the app

from unittest.mock import MagicMock, patch

from agent1.arxiv_searcher import PaperMeta


def _meta(arxiv_id="2301.00001", doi="", title="Seed paper", abstract="abs"):
    return PaperMeta(
        arxiv_id=arxiv_id, title=title, authors=["A"], published="2023-01-01",
        abstract=abstract, pdf_url="", arxiv_url="", doi=doi,
    )


def _oa_work(wid="W100", title="Neighbor", with_refs=None):
    """Minimal OpenAlex work JSON shaped like the real /works payload."""
    return {
        "id": f"https://openalex.org/{wid}",
        "ids": {},
        "title": title,
        "abstract_inverted_index": {"Hello": [0], "world": [1]},
        "authorships": [{"author": {"display_name": "Jane Doe"}}],
        "publication_date": "2022-05-01",
        "open_access": {"oa_url": "http://x/p.pdf"},
        "best_oa_location": {},
        "referenced_works": with_refs or [],
        "cited_by_count": 7,
    }


class TestWorkToPaper:
    def test_parses_core_fields(self):
        from agent1.openalex_searcher import work_to_paper
        p = work_to_paper(_oa_work(wid="W2963403868", title="Attention"))
        assert p is not None
        assert p.arxiv_id == "oa_W2963403868"
        assert p.title == "Attention"
        assert p.abstract == "Hello world"
        assert p.authors == ["Jane Doe"]

    def test_drops_when_no_abstract(self):
        from agent1.openalex_searcher import work_to_paper
        w = _oa_work()
        w["abstract_inverted_index"] = None
        assert work_to_paper(w) is None


class TestResolveWork:
    def test_resolve_via_existing_oa_id(self):
        from agent1 import openalex_searcher as OA
        seed = _meta(arxiv_id="oa_W500")
        captured = {}
        def fake_get(url, **kw):
            captured["url"] = url
            r = MagicMock(); r.status_code = 200
            r.json.return_value = _oa_work(wid="W500", with_refs=["https://openalex.org/W1"])
            return r
        with patch.object(OA.requests, "get", fake_get):
            work = OA.resolve_work(seed)
        assert work is not None and "W500" in captured["url"]

    def test_resolve_returns_none_on_failure(self):
        from agent1 import openalex_searcher as OA
        seed = _meta(arxiv_id="dblp_xyz", doi="")  # no oa/doi/arxiv resolvable
        assert OA.resolve_work(seed) is None

    def test_resolve_cascade_doi_and_arxiv(self):
        from agent1 import openalex_searcher as OA
        urls = []
        def fake_get(url, **kw):
            urls.append(url)
            r = MagicMock(); r.status_code = 200
            r.json.return_value = _oa_work(wid="W1")
            return r
        with patch.object(OA.requests, "get", fake_get):
            OA.resolve_work(_meta(arxiv_id="", doi="10.1/abc"))
            OA.resolve_work(_meta(arxiv_id="2301.07041", doi=""))
        assert any("doi:10.1/abc" in u for u in urls)
        assert any("doi:10.48550/arXiv.2301.07041" in u for u in urls)


class TestFetchNeighbors:
    def test_fetch_referenced_works_capped(self):
        from agent1 import openalex_searcher as OA
        work = _oa_work(wid="W1", with_refs=[f"https://openalex.org/W{i}" for i in range(50)])
        captured = {}
        def fake_get(url, params=None, **kw):
            captured["params"] = params
            r = MagicMock(); r.status_code = 200
            r.json.return_value = {"results": [_oa_work(wid=f"W{i}", title=f"ref{i}") for i in range(25)]}
            return r
        with patch.object(OA.requests, "get", fake_get):
            refs = OA.fetch_referenced_works(work, limit=25)
        assert len(refs) == 25
        assert all(isinstance(p, PaperMeta) for p in refs)
        assert captured["params"]["filter"].count("|") == 24  # 25 ids => 24 separators

    def test_fetch_citing_works_sorted_desc(self):
        from agent1 import openalex_searcher as OA
        captured = {}
        def fake_get(url, params=None, **kw):
            captured["params"] = params
            r = MagicMock(); r.status_code = 200
            r.json.return_value = {"results": [_oa_work(wid="W9", title="citing")]}
            return r
        with patch.object(OA.requests, "get", fake_get):
            cits = OA.fetch_citing_works("W1", limit=25)
        assert len(cits) == 1
        assert captured["params"]["filter"] == "cites:W1"
        assert captured["params"]["sort"] == "cited_by_count:desc"


class TestS2Fallback:
    def test_fetch_neighbors_s2_both_dirs(self):
        from agent1 import semantic_scholar_searcher as S2
        seed = _meta(arxiv_id="2301.00001")
        def fake_get(url, **kw):
            r = MagicMock(); r.status_code = 200
            if "references" in url:
                r.json.return_value = {"data": [{"citedPaper": {
                    "paperId": "p1", "title": "Ref A", "abstract": "a",
                    "externalIds": {"ArXiv": "2105.00001"}, "year": 2021,
                    "authors": [{"name": "X"}]}}]}
            else:  # citations
                r.json.return_value = {"data": [{"citingPaper": {
                    "paperId": "p2", "title": "Cite B", "abstract": "b",
                    "externalIds": {"ArXiv": "2310.00002"}, "year": 2023,
                    "authors": [{"name": "Y"}]}}]}
            return r
        with patch.object(S2.requests, "get", fake_get):
            neigh = S2.fetch_neighbors_s2(seed, limit=25)
        titles = {p.title for p in neigh}
        assert "Ref A" in titles and "Cite B" in titles
        assert all(isinstance(p, PaperMeta) for p in neigh)

    def test_fetch_neighbors_s2_graceful(self):
        from agent1 import semantic_scholar_searcher as S2
        seed = _meta(arxiv_id="2301.00001")
        def boom(*a, **k):
            raise RuntimeError("S2 down")
        with patch.object(S2.requests, "get", boom):
            assert S2.fetch_neighbors_s2(seed, limit=25) == []

    def test_fetch_neighbors_s2_no_external_id(self):
        from agent1 import semantic_scholar_searcher as S2
        seed = _meta(arxiv_id="dblp_xyz", doi="")  # neither arxiv nor doi resolvable
        called = {"n": 0}
        def fake_get(*a, **k):
            called["n"] += 1
            return MagicMock()
        with patch.object(S2.requests, "get", fake_get):
            assert S2.fetch_neighbors_s2(seed, limit=25) == []
        assert called["n"] == 0  # no network call when no resolvable id

    def test_fetch_neighbors_s2_drops_abstractless(self):
        from agent1 import semantic_scholar_searcher as S2
        seed = _meta(arxiv_id="2301.00001")
        def fake_get(url, **kw):
            r = MagicMock(); r.status_code = 200
            if "references" in url:
                r.json.return_value = {"data": [{"citedPaper": {
                    "paperId": "p1", "title": "No abstract here", "abstract": None,
                    "externalIds": {"ArXiv": "2105.00001"}, "year": 2021, "authors": []}}]}
            else:
                r.json.return_value = {"data": []}
            return r
        with patch.object(S2.requests, "get", fake_get):
            neigh = S2.fetch_neighbors_s2(seed, limit=25)
        assert neigh == []  # abstract-less object dropped


class TestExpandByCitations:
    def _cfg(self, **over):
        c = {"enable_citation_snowball": True, "snowball_seed_count": 8,
             "snowball_per_seed_per_direction": 25, "snowball_max_neighbors": 300,
             "snowball_extra_slots": 10, "snowball_quality_weight": 0.15,
             "relevance_threshold": 0.8, "search_batch_size": 10}
        c.update(over); return c

    def test_disabled_returns_empty(self):
        from agent1 import citation_expander as CE
        out = CE.expand_by_citations([_meta()], [_meta()], "topic", MagicMock(),
                                     self._cfg(enable_citation_snowball=False))
        assert out == []

    def test_happy_path_admits_scored_neighbors(self, monkeypatch):
        from agent1 import citation_expander as CE
        seed = _meta(arxiv_id="2301.00001")
        n1 = _meta(arxiv_id="2105.11111", title="Backward ref")
        n2 = _meta(arxiv_id="2310.22222", title="Forward cite")
        monkeypatch.setattr(CE, "resolve_work", lambda p: {"id": "https://openalex.org/W1", "referenced_works": ["x"]})
        monkeypatch.setattr(CE, "fetch_referenced_works", lambda work, limit: [n1])
        monkeypatch.setattr(CE, "fetch_citing_works", lambda wid, limit: [n2])
        def fake_filter(papers, topic, llm, **kw):
            for p in papers: p.relevance_score = 0.9
            return papers[:kw.get("max_download_papers", 20)]
        monkeypatch.setattr(CE, "filter_papers", fake_filter)
        out = CE.expand_by_citations([seed], [seed], "topic", MagicMock(), self._cfg())
        titles = {p.title for p in out}
        assert "Backward ref" in titles and "Forward cite" in titles
        assert all(p.discovered_via.startswith("snowball:2301.00001:") for p in out)
        assert all(p.source_platform in {"oa", "s2"} for p in out)

    def test_dedup_drops_papers_already_in_pool(self, monkeypatch):
        from agent1 import citation_expander as CE
        seed = _meta(arxiv_id="2301.00001")
        dup = _meta(arxiv_id="2105.11111", title="Already have it")
        monkeypatch.setattr(CE, "resolve_work", lambda p: {"id": "https://openalex.org/W1", "referenced_works": ["x"]})
        monkeypatch.setattr(CE, "fetch_referenced_works", lambda work, limit: [dup])
        monkeypatch.setattr(CE, "fetch_citing_works", lambda wid, limit: [])
        monkeypatch.setattr(CE, "filter_papers", lambda papers, *a, **kw: papers)
        out = CE.expand_by_citations([seed], [seed, dup], "topic", MagicMock(), self._cfg())
        assert out == []

    def test_global_neighbor_cap(self, monkeypatch):
        from agent1 import citation_expander as CE
        seed = _meta(arxiv_id="2301.00001")
        many = [_meta(arxiv_id=f"2100.{i:05d}", title=f"n{i}") for i in range(400)]
        monkeypatch.setattr(CE, "resolve_work", lambda p: {"id": "https://openalex.org/W1", "referenced_works": ["x"]})
        monkeypatch.setattr(CE, "fetch_referenced_works", lambda work, limit: many)
        monkeypatch.setattr(CE, "fetch_citing_works", lambda wid, limit: [])
        captured = {}
        def fake_filter(papers, *a, **kw):
            captured["n"] = len(papers); return []
        monkeypatch.setattr(CE, "filter_papers", fake_filter)
        CE.expand_by_citations([seed], [seed], "topic", MagicMock(), self._cfg(snowball_max_neighbors=300))
        assert captured["n"] <= 300

    def test_graceful_when_resolve_fails(self, monkeypatch):
        from agent1 import citation_expander as CE
        seed = _meta()
        monkeypatch.setattr(CE, "resolve_work", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        out = CE.expand_by_citations([seed], [seed], "topic", MagicMock(), self._cfg())
        assert out == []

    def test_one_seed_fails_others_still_processed(self, monkeypatch):
        from agent1 import citation_expander as CE
        bad = _meta(arxiv_id="2301.00001", title="Bad seed")
        good = _meta(arxiv_id="2302.00002", title="Good seed")
        good_neighbor = _meta(arxiv_id="2105.33333", title="Good neighbor")
        def resolve(p):
            if p.arxiv_id == "2301.00001":
                raise RuntimeError("seed A resolve boom")
            return {"id": "https://openalex.org/W2", "referenced_works": ["x"]}
        monkeypatch.setattr(CE, "resolve_work", resolve)
        monkeypatch.setattr(CE, "fetch_referenced_works", lambda work, limit: [good_neighbor])
        monkeypatch.setattr(CE, "fetch_citing_works", lambda wid, limit: [])
        def fake_filter(papers, topic, llm, **kw):
            for p in papers: p.relevance_score = 0.9
            return papers[:kw.get("max_download_papers", 20)]
        monkeypatch.setattr(CE, "filter_papers", fake_filter)
        out = CE.expand_by_citations([bad, good], [bad, good], "topic", MagicMock(), self._cfg())
        # seed A raised but seed B's neighbor still made it through
        assert {p.title for p in out} == {"Good neighbor"}

    def test_s2_fallback_when_openalex_unresolved(self, monkeypatch):
        from agent1 import citation_expander as CE
        seed = _meta(arxiv_id="2301.00001")
        s2_neighbor = _meta(arxiv_id="2104.44444", title="S2 neighbor")
        monkeypatch.setattr(CE, "resolve_work", lambda p: None)          # OpenAlex can't resolve
        monkeypatch.setattr(CE, "fetch_neighbors_s2", lambda p, limit: [s2_neighbor])
        def fake_filter(papers, topic, llm, **kw):
            for p in papers: p.relevance_score = 0.9
            return papers[:kw.get("max_download_papers", 20)]
        monkeypatch.setattr(CE, "filter_papers", fake_filter)
        out = CE.expand_by_citations([seed], [seed], "topic", MagicMock(), self._cfg())
        assert {p.title for p in out} == {"S2 neighbor"}
        assert out[0].discovered_via == "snowball:2301.00001:backward"
        assert out[0].source_platform == "s2"
