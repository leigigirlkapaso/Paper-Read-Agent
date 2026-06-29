"""
modules/ideator/cross_recall.py
CrossRecall — 6 路交叉召回。并行拉取不同来源的候选关联对。
"""

from __future__ import annotations

import json
import logging
import random

from .data_access import DataAccess
from .constants import (
    SOURCE_CROSS_PROJECT, SOURCE_CROSS_LAYER, SOURCE_CONTRADICTION,
    SOURCE_RANDOM, SOURCE_TIMELINE,
    LINK_SIMILARITY, LINK_CONTRADICTION, LINK_TEMPORAL,
    LINK_RANDOM, LINK_CROSS_LAYER, LINK_RANDOM_WALK, LINK_TIMELINE,
)

logger = logging.getLogger(__name__)

_CANDIDATE_SNIPPET_LEN = 1000


class CrossRecall:
    """6 路交叉召回层。所有召回路径失败不影响其他路径。"""

    def __init__(self, data: DataAccess, idea_extractor=None):
        self.data = data
        self._idea_extractor = idea_extractor  # IdeaExtractor | None

    async def recall(self, core_llm, scope: str = "all", effort_params=None) -> list[dict]:
        """
        并行执行召回路径，合并去重后返回候选关联对列表。
        根据 effort_params 过滤启用的路径，并通过权重表排除低权重路径。
        每条关联对格式: {source_a: {type, id, content}, source_b: {type, id, content}, recall_path: str}
        """
        import asyncio

        if effort_params is None:
            effort_params = {
                "recall_paths": [LINK_SIMILARITY, LINK_CONTRADICTION, SOURCE_CROSS_PROJECT,
                                 LINK_CROSS_LAYER, LINK_RANDOM_WALK, LINK_TIMELINE],
                "sample_size": 3,
            }

        sample_size = effort_params.get("sample_size", 3)

        path_methods = {
            LINK_SIMILARITY: self._recall_similarity,
            LINK_CONTRADICTION: self._recall_contradiction,
            SOURCE_CROSS_PROJECT: self._recall_cross_project,
            LINK_CROSS_LAYER: self._recall_cross_layer,
            LINK_RANDOM_WALK: self._recall_random_walk,
            LINK_TIMELINE: self._recall_timeline,
        }

        active_paths = effort_params["recall_paths"].copy()

        # Exclude paths with weight < 0.2 from ideator_recall_weights
        try:
            weights = self.data.get_recall_weights()
            disabled = {row["source_type"] for row in weights if row["weight"] < 0.2}
            active_paths = [p for p in active_paths if p not in disabled]
        except Exception:
            logger.warning("[CrossRecall] weight fetch failed", exc_info=True)

        tasks = {}
        for name in active_paths:
            if name not in path_methods:
                continue
            method = path_methods[name]
            # Pass sample_size to methods that accept it
            if name in ("similarity", "contradiction", "cross_project",
                        "cross_layer", "random_walk", "timeline"):
                tasks[name] = method(core_llm, sample_size=sample_size)
            else:
                tasks[name] = method(core_llm)

        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        results = {}
        for key, result in zip(tasks.keys(), gathered):
            if isinstance(result, Exception):
                logger.debug(f"[CrossRecall] 路径 {key} 失败", exc_info=result)
                results[key] = []
            else:
                results[key] = result or []

        merged = []
        seen_pairs = set()
        for path, pairs in results.items():
            for pair in pairs:
                a_id = str(pair.get("source_a", {}).get("id", ""))
                b_id = str(pair.get("source_b", {}).get("id", ""))
                sig = tuple(sorted([a_id, b_id]))
                if sig in seen_pairs:
                    continue
                seen_pairs.add(sig)
                pair["recall_path"] = path
                merged.append(pair)

        return merged

    async def recall_single_path(self, core_llm, path: str, *, sample_size: int = 3,
                                  direction: str = "", keywords: list[str] | None = None) -> list[dict]:
        """Execute a single recall path on demand (for agent tool invocation).

        Args:
            core_llm: CoreLLM instance (for embedding calls)
            path: One of 'similarity','contradiction','cross_project','cross_layer','random_walk','timeline'
            sample_size: Number of items to sample
            direction: Optional search direction description
            keywords: Optional keywords to narrow search
        """
        path_methods = {
            LINK_SIMILARITY: self._recall_similarity,
            LINK_CONTRADICTION: self._recall_contradiction,
            SOURCE_CROSS_PROJECT: self._recall_cross_project,
            LINK_CROSS_LAYER: self._recall_cross_layer,
            LINK_RANDOM_WALK: self._recall_random_walk,
            LINK_TIMELINE: self._recall_timeline,
        }
        method = path_methods.get(path)
        if not method:
            raise ValueError(f"Unknown recall path: {path}. Valid: {sorted(path_methods.keys())}")
        result = await method(core_llm, sample_size=sample_size)
        return result or []

    async def _recall_similarity(self, core_llm, sample_size: int = 5) -> list[dict]:
        """语义相似召回。有 idea_extractor 时走 idea 级搜索 + MaxSim 聚合。"""
        insights = self.data.get_recent_insights(limit=max(sample_size, 5))
        if not insights:
            return []
        if self._idea_extractor:
            return await self._search_idea_level(
                core_llm, insights, sample_size, contradictions=False,
            )

        # Fallback: note-level embedding
        pairs = []
        for insight in insights:
            emb_text = insight.get("content", "")[:5000]
            if not emb_text:
                continue
            emb = await core_llm.embed(emb_text, module="ideator")
            if not emb:
                continue
            similar = self.data.search_core_notes(emb, top_k=3)
            for s in similar:
                pairs.append({
                    "source_a": {"type": "core_note", "id": insight["id"],
                                  "content": insight["content"][:_CANDIDATE_SNIPPET_LEN]},
                    "source_b": {"type": "core_note", "id": s["id"],
                                  "content": s["content"][:_CANDIDATE_SNIPPET_LEN]},
                })
        return pairs

    async def _recall_contradiction(self, core_llm, sample_size: int = 3) -> list[dict]:
        """矛盾检测召回。有 idea_extractor 时走 idea 级搜索 + MaxSim 聚合。"""
        insights = self.data.get_recent_insights(limit=max(sample_size, 3))
        if not insights:
            return []
        if self._idea_extractor:
            return await self._search_idea_level(
                core_llm, insights, sample_size, contradictions=True,
            )

        # Fallback: note-level embedding
        pairs = []
        for insight in insights:
            emb_text = insight.get("content", "")[:5000]
            if not emb_text:
                continue
            emb = await core_llm.embed(emb_text, module="ideator")
            if not emb:
                continue
            contradictions = self.data.find_contradictions(emb, top_k=3)
            for c in contradictions:
                pairs.append({
                    "source_a": {"type": "core_note", "id": insight["id"],
                                  "content": insight["content"][:_CANDIDATE_SNIPPET_LEN]},
                    "source_b": {"type": "core_note", "id": c["id"],
                                  "content": c["content"][:_CANDIDATE_SNIPPET_LEN]},
                })
        return pairs

    async def _search_idea_level(
        self, core_llm, insights: list[dict], sample_size: int,
        *, contradictions: bool,
    ) -> list[dict]:
        """Idea 级语义搜索 + MaxSim 聚合回笔记对。

        对每条笔记提取 idea → 每个 idea 独立搜索 →
        按 parent note 分组取 MaxSim → 返回 top 笔记对。
        """
        # 1. 为每条笔记提取/获取 idea
        notes_with_ideas: list[tuple[dict, list[dict]]] = []
        for insight in insights:
            note_id = insight.get("id", 0)
            note_text = insight.get("content", "")
            if not note_text:
                continue
            # 判断来源：core_notes 还是 legacy
            note_source = insight.get("source_module", "literature")
            if isinstance(note_id, str):
                note_source = "legacy"

            ideas = await self._idea_extractor.get_or_extract_ideas(
                note_source, note_id, note_text,
            )
            if ideas:
                notes_with_ideas.append((insight, ideas))

        if not notes_with_ideas:
            return []

        # 2. 对每条笔记的每个 idea 搜索，聚合到 parent note
        # note_pair_scores: {(note_a_id, note_b_id): {"max_sim": float, "matches": [...]}}
        note_pair_scores: dict[tuple, dict] = {}

        # Build lookup: (source_module, note_id) -> paper_id for legacy notes
        _legacy_paper_id: dict[tuple, int] = {}
        for insight in insights:
            if insight.get("source_module") == "legacy":
                pid = insight.get("paper_id", 0)
                if pid:
                    _legacy_paper_id[("legacy", insight["id"])] = pid

        for insight_a, ideas_a in notes_with_ideas:
            a_id = insight_a.get("id", 0)
            a_source = insight_a.get("source_module", "literature")
            a_ref_id = _legacy_paper_id.get((a_source, a_id), a_id)

            for idea_a in ideas_a:
                emb_a = idea_a.get("embedding", [])
                if not emb_a:
                    continue

                if contradictions:
                    similar_ideas = self.data.search_contradictory_ideas(
                        emb_a, top_k=5,
                        exclude_note_source=a_source, exclude_note_id=a_id,
                    )
                else:
                    similar_ideas = self.data.search_similar_ideas(
                        emb_a, top_k=5, min_similarity=0.3,
                        exclude_note_source=a_source, exclude_note_id=a_id,
                    )

                for idea_b in similar_ideas:
                    b_id = idea_b["note_id"]
                    b_source = idea_b["note_source"]
                    sim = idea_b["_similarity"]

                    # 跳过自匹配
                    if b_source == a_source and b_id == a_id:
                        continue

                    b_ref_id = _legacy_paper_id.get((b_source, b_id), b_id)

                    pair_key = (str(a_id), str(b_id))
                    if pair_key not in note_pair_scores:
                        note_pair_scores[pair_key] = {
                            "source_a": {
                                "type": "core_note" if a_source != "legacy" else "paper",
                                "id": a_ref_id,
                                "content": insight_a.get("content", "")[:_CANDIDATE_SNIPPET_LEN],
                            },
                            "source_b": {
                                "type": "core_note" if b_source != "legacy" else "paper",
                                "id": b_ref_id,
                                "content": "",  # 后续在 pipeline 中解析
                            },
                            "max_similarity": sim,
                            "idea_match_count": 1,
                        }
                    else:
                        entry = note_pair_scores[pair_key]
                        if sim > entry["max_similarity"]:
                            entry["max_similarity"] = sim
                        entry["idea_match_count"] += 1

        # 3. 按 max_similarity 排序，返回 top pairs
        sorted_pairs = sorted(
            note_pair_scores.values(),
            key=lambda x: (x["max_similarity"], x["idea_match_count"]),
            reverse=True,
        )
        return sorted_pairs[:sample_size * 3]

    async def _recall_cross_project(self, core_llm=None, sample_size: int = 2) -> list[dict]:
        graph = self.data.get_cross_project_graph()
        nodes = graph.get("nodes", [])
        if len(nodes) < 2:
            return []

        papers_per_project = max(1, sample_size)
        papers_by_project = {}
        for node in nodes:
            project_id = node.get("project_id")
            if project_id and project_id not in papers_by_project:
                papers_by_project[project_id] = self.data.get_all_papers_with_notes(project_id)[:papers_per_project]

        pairs = []
        projects = list(papers_by_project.keys())
        for i in range(len(projects)):
            for j in range(i + 1, len(projects)):
                for pa in papers_by_project[projects[i]]:
                    for pb in papers_by_project[projects[j]]:
                        pairs.append({
                            "source_a": {"type": "paper", "id": pa.get("id", 0),
                                          "content": pa.get("abstract", pa.get("title", ""))[:_CANDIDATE_SNIPPET_LEN]},
                            "source_b": {"type": "paper", "id": pb.get("id", 0),
                                          "content": pb.get("abstract", pb.get("title", ""))[:_CANDIDATE_SNIPPET_LEN]},
                        })
        return pairs

    async def _recall_cross_layer(self, core_llm, sample_size: int = 5) -> list[dict]:
        size = max(sample_size, 5)
        recent_insights = self.data.get_recent_insights(limit=size)
        notes = self.data.get_all_notes()[:size]
        if not recent_insights or not notes:
            return []
        pairs = []
        for note in notes:
            for insight in recent_insights:
                src_type = "paper" if insight.get("source_module") == "legacy" else "core_note"
                src_id = insight.get("paper_id") if insight.get("source_module") == "legacy" else insight["id"]
                pairs.append({
                    "source_a": {"type": "paper", "id": note.get("paper_id", 0),
                                  "content": note.get("content", "")[:_CANDIDATE_SNIPPET_LEN]},
                    "source_b": {"type": src_type, "id": src_id,
                                  "content": insight["content"][:_CANDIDATE_SNIPPET_LEN]},
                })
        return pairs

    async def _recall_random_walk(self, core_llm=None, sample_size: int = 3) -> list[dict]:
        notes = self.data.get_all_notes()
        insights = self.data.get_recent_insights(limit=max(sample_size, 5))
        if len(notes) < 2:
            return []
        papers_sample = random.sample(notes, min(sample_size, len(notes)))
        insight_sample = random.sample(insights, min(max(sample_size - 1, 1), len(insights))) if insights else []
        pairs = []
        for i, pa in enumerate(papers_sample):
            for j, pb in enumerate(papers_sample):
                if i >= j:
                    continue
                pairs.append({
                    "source_a": {"type": "paper", "id": pa.get("paper_id", 0),
                                  "content": pa.get("content", "")[:_CANDIDATE_SNIPPET_LEN]},
                    "source_b": {"type": "paper", "id": pb.get("paper_id", 0),
                                  "content": pb.get("content", "")[:_CANDIDATE_SNIPPET_LEN]},
                })
        for pa in papers_sample:
            for ins in insight_sample:
                src_type = "paper" if ins.get("source_module") == "legacy" else "core_note"
                src_id = ins.get("paper_id") if ins.get("source_module") == "legacy" else ins.get("id", 0)
                pairs.append({
                    "source_a": {"type": "paper", "id": pa.get("paper_id", 0),
                                  "content": pa.get("content", "")[:_CANDIDATE_SNIPPET_LEN]},
                    "source_b": {"type": src_type, "id": src_id,
                                  "content": ins.get("content", "")[:_CANDIDATE_SNIPPET_LEN]},
                })
        return pairs

    async def _recall_timeline(self, core_llm=None, sample_size: int = 5) -> list[dict]:
        limit = max(sample_size * 2, 10)
        insights = self.data.get_recent_insights(limit=limit)
        if len(insights) < 2:
            return []
        insights_sorted = sorted(insights, key=lambda x: x.get("created_at", ""))
        pairs = []
        for i in range(len(insights_sorted) - 1):
            a, b = insights_sorted[i], insights_sorted[i + 1]
            src_type_a = "paper" if a.get("source_module") == "legacy" else "core_note"
            src_type_b = "paper" if b.get("source_module") == "legacy" else "core_note"
            src_id_a = a.get("paper_id") if a.get("source_module") == "legacy" else a["id"]
            src_id_b = b.get("paper_id") if b.get("source_module") == "legacy" else b["id"]
            pairs.append({
                "source_a": {"type": src_type_a, "id": src_id_a,
                              "content": a["content"][:_CANDIDATE_SNIPPET_LEN]},
                "source_b": {"type": src_type_b, "id": src_id_b,
                              "content": b["content"][:_CANDIDATE_SNIPPET_LEN]},
            })
        return pairs[:sample_size]
