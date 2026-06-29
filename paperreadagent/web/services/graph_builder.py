"""graph_builder.py — Build node/edge data for the project relationship graph.

Pure read-only assembly: queries papers/notes/projects/ideator_sparks/
ideator_cross_links and emits a Cytoscape-compatible {nodes, edges, truncated}
structure. No side effects.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class GraphOptions:
    layers: set[str] = field(default_factory=lambda: {"project", "paper"})
    limit: int = 200
    center: str | None = None       # e.g. "paper_42"
    hops: int = 1
    project_id: int | None = None


@dataclass
class GraphResult:
    nodes: list[dict]
    edges: list[dict]
    truncated: bool = False


_VALID_LAYERS = {"project", "paper", "note", "spark"}


class GraphBuilderService:
    """Construct nodes+edges for the project relationship graph."""

    def __init__(self, db):
        """db: paperreadagent.db.database.Database (or any object with .conn)."""
        self._db = db

    def build(self, options: GraphOptions) -> GraphResult:
        layers = options.layers & _VALID_LAYERS
        if not layers:
            layers = {"project", "paper"}
        if options.center:
            return self._build_neighborhood(options, layers)
        return self._build_overview(options, layers)

    # ── Panorama mode ───────────────────────────────────────────

    def _build_overview(self, options: GraphOptions, layers: set[str]) -> GraphResult:
        nodes: list[dict] = []
        edges: list[dict] = []
        truncated = False

        project_rows = self._fetch_projects(options.project_id)
        if "project" in layers:
            nodes.extend(self._project_to_node(r) for r in project_rows)

        paper_rows: list[dict] = []
        paper_ids: set[int] = set()
        if "paper" in layers:
            paper_rows = self._fetch_papers(options.limit, options.project_id)
            # _fetch_papers fetches limit+1 to detect truncation; trim back to limit
            if len(paper_rows) > options.limit:
                truncated = True
                paper_rows = paper_rows[:options.limit]
            nodes.extend(self._paper_to_node(r) for r in paper_rows)
            paper_ids = {r["paper_id"] for r in paper_rows}
            edges.extend(self._build_contains_edges(paper_rows))
            edges.extend(self._build_shared_edges(paper_rows))

        if "note" in layers and paper_ids:
            note_rows = self._fetch_notes(paper_ids)
            nodes.extend(self._note_to_node(r) for r in note_rows)
            edges.extend(self._build_has_note_edges(note_rows))

        if "spark" in layers:
            spark_rows = self._fetch_sparks(options.limit // 2)
            nodes.extend(self._spark_to_node(r) for r in spark_rows)
            if paper_ids:
                edges.extend(self._build_cites_edges(spark_rows, paper_ids))
            # cross_link edges connect papers, but they come from ideator's
            # spark analysis pipeline — show them as part of the spark layer
            # rather than auto-showing on paper-only views (avoid information
            # surprise on the default panorama).
            if paper_ids:
                edges.extend(self._build_cross_link_edges(paper_ids))

        return GraphResult(nodes=nodes, edges=edges, truncated=truncated)

    # ── Neighborhood mode — Task 3 ───────────────────────────────

    def _build_neighborhood(self, options: GraphOptions, layers: set[str]) -> GraphResult:
        """1-hop neighborhood of a single center node.

        Supports center='project_<id>' / 'paper_<id>' / 'spark_<id>'.
        Layer filters control which neighbor types to include.
        """
        center = options.center or ""
        if not center or "_" not in center:
            return GraphResult(nodes=[], edges=[], truncated=False)
        kind, _, raw_id = center.partition("_")
        try:
            db_id = int(raw_id)
        except ValueError:
            return GraphResult(nodes=[], edges=[], truncated=False)

        nodes: list[dict] = []
        edges: list[dict] = []

        if kind == "paper":
            # The paper itself
            paper_rows = self._fetch_papers_by_ids({db_id})
            if not paper_rows:
                return GraphResult(nodes=[], edges=[], truncated=False)
            nodes.extend(self._paper_to_node(r) for r in paper_rows)
            project_ids = {r["project_id"] for r in paper_rows if r.get("project_id")}
            arxiv = paper_rows[0].get("arxiv_id") or ""

            # Sibling papers sharing same arxiv_id (in OTHER projects too)
            if arxiv:
                sibling_rows = self._fetch_sibling_papers_by_arxiv(arxiv, exclude_id=db_id)
                nodes.extend(self._paper_to_node(r) for r in sibling_rows)
                for r in sibling_rows:
                    project_ids.add(r["project_id"])
                # shared edges
                for sib in sibling_rows:
                    lo, hi = sorted([db_id, sib["paper_id"]])
                    edges.append({
                        "id": f"edge_shared_{lo}_{hi}",
                        "source": f"paper_{lo}",
                        "target": f"paper_{hi}",
                        "etype": "shared",
                        "label": arxiv,
                    })
                # contains edges for sibling papers
                edges.extend(self._build_contains_edges(sibling_rows))

            # The center paper's own contains edges
            edges.extend(self._build_contains_edges(paper_rows))

            # Project nodes for those projects
            if "project" in layers and project_ids:
                project_rows = self._fetch_projects_by_ids(project_ids)
                nodes.extend(self._project_to_node(r) for r in project_rows)

            # Notes
            if "note" in layers:
                note_rows = self._fetch_notes({db_id})
                nodes.extend(self._note_to_node(r) for r in note_rows)
                edges.extend(self._build_has_note_edges(note_rows))

            # Sparks citing this paper
            if "spark" in layers:
                spark_rows = self._fetch_sparks_citing_paper(db_id)
                nodes.extend(self._spark_to_node(r) for r in spark_rows)
                edges.extend(self._build_cites_edges(spark_rows, {db_id}))

        elif kind == "project":
            project_rows = self._fetch_projects_by_ids({db_id})
            nodes.extend(self._project_to_node(r) for r in project_rows)
            # Papers in this project
            if "paper" in layers:
                paper_rows = self._fetch_papers(limit=200, project_id=db_id)
                nodes.extend(self._paper_to_node(r) for r in paper_rows)
                edges.extend(self._build_contains_edges(paper_rows))

        elif kind == "spark":
            spark_rows = self._fetch_sparks_by_ids({db_id})
            if not spark_rows:
                return GraphResult(nodes=[], edges=[], truncated=False)
            nodes.extend(self._spark_to_node(r) for r in spark_rows)
            # Papers cited by this spark
            if "paper" in layers:
                cited_paper_ids = self._cited_paper_ids(spark_rows[0])
                if cited_paper_ids:
                    paper_rows = self._fetch_papers_by_ids(cited_paper_ids)
                    nodes.extend(self._paper_to_node(r) for r in paper_rows)
                    edges.extend(self._build_cites_edges(spark_rows, cited_paper_ids))

        return GraphResult(nodes=nodes, edges=edges, truncated=False)

    # ── Neighborhood helpers ────────────────────────────────────

    def _fetch_papers_by_ids(self, paper_ids: set[int]) -> list[dict]:
        if not paper_ids:
            return []
        placeholders = ",".join("?" * len(paper_ids))
        sql = f"""
            SELECT
                p.id AS paper_id, p.arxiv_id, p.title,
                s.project_id AS project_id,
                COUNT(DISTINCT n.id) AS note_count,
                (
                    SELECT COUNT(DISTINCT s2.project_id)
                    FROM papers p2
                    JOIN sessions s2 ON p2.session_id = s2.id
                    WHERE LOWER(p2.arxiv_id) = LOWER(p.arxiv_id)
                      AND p.arxiv_id IS NOT NULL AND p.arxiv_id != ''
                ) AS project_count
            FROM papers p
            JOIN sessions s ON p.session_id = s.id
            LEFT JOIN notes n ON n.paper_id = p.id
            WHERE p.id IN ({placeholders})
            GROUP BY p.id
        """
        rows = self._db.conn.execute(sql, tuple(paper_ids)).fetchall()
        return [dict(r) for r in rows]

    def _fetch_sibling_papers_by_arxiv(self, arxiv: str, *, exclude_id: int) -> list[dict]:
        rows = self._db.conn.execute(
            """SELECT
                   p.id AS paper_id, p.arxiv_id, p.title,
                   s.project_id AS project_id,
                   0 AS note_count,
                   (
                       SELECT COUNT(DISTINCT s2.project_id)
                       FROM papers p2
                       JOIN sessions s2 ON p2.session_id = s2.id
                       WHERE LOWER(p2.arxiv_id) = LOWER(?)
                   ) AS project_count
               FROM papers p
               JOIN sessions s ON p.session_id = s.id
               WHERE LOWER(p.arxiv_id) = LOWER(?) AND p.id != ?""",
            (arxiv, arxiv, exclude_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_projects_by_ids(self, project_ids: set[int]) -> list[dict]:
        if not project_ids:
            return []
        placeholders = ",".join("?" * len(project_ids))
        rows = self._db.conn.execute(
            f"""SELECT pr.id, pr.name,
                       COUNT(DISTINCT p.id) AS paper_count,
                       COUNT(DISTINCT n.id) AS note_count
                FROM projects pr
                LEFT JOIN sessions s ON s.project_id = pr.id
                LEFT JOIN papers p ON p.session_id = s.id
                LEFT JOIN notes n ON n.paper_id = p.id
                WHERE pr.id IN ({placeholders})
                GROUP BY pr.id""",
            tuple(project_ids),
        ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_sparks_by_ids(self, spark_ids: set[int]) -> list[dict]:
        if not spark_ids:
            return []
        placeholders = ",".join("?" * len(spark_ids))
        rows = self._db.conn.execute(
            f"""SELECT id, content, source_refs, quality_score
                FROM ideator_sparks
                WHERE id IN ({placeholders})""",
            tuple(spark_ids),
        ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_sparks_citing_paper(self, paper_id: int) -> list[dict]:
        # JSON search isn't ideal — scan all mature sparks and parse.
        # Acceptable since spark count is small in this codebase.
        rows = self._db.conn.execute(
            """SELECT id, content, source_refs, quality_score
               FROM ideator_sparks
               WHERE status IN ('deepening','deep_done')"""
        ).fetchall()
        matched: list[dict] = []
        for r in rows:
            row = dict(r)
            raw = row.get("source_refs") or "[]"
            try:
                refs = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if (isinstance(ref, dict) and ref.get("type") == "paper"
                        and ref.get("id") == paper_id):
                    matched.append(row)
                    break
        return matched

    def _cited_paper_ids(self, spark_row: dict) -> set[int]:
        raw = spark_row.get("source_refs") or "[]"
        try:
            refs = json.loads(raw)
        except (TypeError, ValueError):
            return set()
        if not isinstance(refs, list):
            return set()
        return {
            ref["id"]
            for ref in refs
            if isinstance(ref, dict) and ref.get("type") == "paper"
            and isinstance(ref.get("id"), int)
        }

    # ── Fetchers ────────────────────────────────────────────────

    def _fetch_projects(self, project_id: int | None) -> list[dict]:
        if project_id is not None:
            rows = self._db.conn.execute(
                """
                SELECT pr.id, pr.name,
                       COUNT(DISTINCT p.id) AS paper_count,
                       COUNT(DISTINCT n.id) AS note_count
                FROM projects pr
                LEFT JOIN sessions s ON s.project_id = pr.id
                LEFT JOIN papers p ON p.session_id = s.id
                LEFT JOIN notes n ON n.paper_id = p.id
                WHERE pr.id = ?
                GROUP BY pr.id
                """,
                (project_id,),
            ).fetchall()
        else:
            rows = self._db.conn.execute(
                """
                SELECT pr.id, pr.name,
                       COUNT(DISTINCT p.id) AS paper_count,
                       COUNT(DISTINCT n.id) AS note_count
                FROM projects pr
                LEFT JOIN sessions s ON s.project_id = pr.id
                LEFT JOIN papers p ON p.session_id = s.id
                LEFT JOIN notes n ON n.paper_id = p.id
                GROUP BY pr.id
                ORDER BY pr.name
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_papers(self, limit: int, project_id: int | None) -> list[dict]:
        # Each paper row carries: which project it belongs to (single, via session),
        # plus a project_count = how many distinct projects ANY paper with the
        # same arxiv_id appears in (cross-project shared metric).
        where = "WHERE 1=1"
        params: list = []
        if project_id is not None:
            where += " AND s.project_id = ?"
            params.append(project_id)
        sql = f"""
            SELECT
                p.id AS paper_id,
                p.arxiv_id,
                p.title,
                s.project_id AS project_id,
                COUNT(DISTINCT n.id) AS note_count,
                (
                    SELECT COUNT(DISTINCT s2.project_id)
                    FROM papers p2
                    JOIN sessions s2 ON p2.session_id = s2.id
                    WHERE LOWER(p2.arxiv_id) = LOWER(p.arxiv_id)
                      AND p.arxiv_id IS NOT NULL AND p.arxiv_id != ''
                ) AS project_count
            FROM papers p
            JOIN sessions s ON p.session_id = s.id
            LEFT JOIN notes n ON n.paper_id = p.id
            {where}
            GROUP BY p.id
            ORDER BY project_count DESC, note_count DESC, p.id DESC
            LIMIT ?
        """
        # Fetch one extra so we can distinguish "exactly limit results" from
        # "truncated at limit" without false-positives.
        params.append(limit + 1)
        rows = self._db.conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # ── Node converters ─────────────────────────────────────────

    def _project_to_node(self, r: dict) -> dict:
        paper_count = r.get("paper_count") or 0
        size = min(paper_count * 5, 100)
        return {
            "id": f"project_{r['id']}",
            "type": "project",
            "db_id": r["id"],
            "label": r["name"],
            "name": r["name"],
            "paper_count": paper_count,
            "note_count": r.get("note_count") or 0,
            "size": size,
        }

    def _paper_to_node(self, r: dict) -> dict:
        project_count = r.get("project_count") or 1
        size = min(project_count * 30, 100)
        title = r.get("title") or "(无标题)"
        label = title[:24] + ("…" if len(title) > 24 else "")
        return {
            "id": f"paper_{r['paper_id']}",
            "type": "paper",
            "db_id": r["paper_id"],
            "label": label,
            "title": title,
            "arxiv_id": r.get("arxiv_id") or "",
            "project_count": project_count,
            "note_count": r.get("note_count") or 0,
            "shared": "true" if project_count > 1 else "false",
            "size": size,
        }

    # ── Edge builders ──────────────────────────────────────────

    def _build_contains_edges(self, paper_rows: list[dict]) -> list[dict]:
        return [
            {
                "id": f"edge_proj{r['project_id']}_paper{r['paper_id']}",
                "source": f"project_{r['project_id']}",
                "target": f"paper_{r['paper_id']}",
                "etype": "contains",
            }
            for r in paper_rows
            if r.get("project_id") is not None
        ]

    def _build_shared_edges(self, paper_rows: list[dict]) -> list[dict]:
        # Group rows in-memory by arxiv_id; emit one edge per unordered pair.
        by_arxiv: dict[str, list[dict]] = {}
        for r in paper_rows:
            ax = (r.get("arxiv_id") or "").lower().strip()
            if not ax:
                continue
            by_arxiv.setdefault(ax, []).append(r)
        edges: list[dict] = []
        for ax, group in by_arxiv.items():
            if len(group) < 2:
                continue
            sorted_group = sorted(group, key=lambda r: r["paper_id"])
            for i, a in enumerate(sorted_group):
                for b in sorted_group[i + 1:]:
                    edges.append({
                        "id": f"edge_shared_{a['paper_id']}_{b['paper_id']}",
                        "source": f"paper_{a['paper_id']}",
                        "target": f"paper_{b['paper_id']}",
                        "etype": "shared",
                        "label": ax,
                    })
        return edges

    # ── Notes ──────────────────────────────────────────────────

    def _fetch_notes(self, paper_ids: set[int]) -> list[dict]:
        if not paper_ids:
            return []
        placeholders = ",".join("?" * len(paper_ids))
        rows = self._db.conn.execute(
            f"SELECT id, paper_id, content FROM notes WHERE paper_id IN ({placeholders})",
            tuple(paper_ids),
        ).fetchall()
        return [dict(r) for r in rows]

    def _note_to_node(self, r: dict) -> dict:
        content = r.get("content") or ""
        preview = content[:60] + ("…" if len(content) > 60 else "")
        return {
            "id": f"note_{r['id']}",
            "type": "note",
            "db_id": r["id"],
            "label": "笔记",
            "preview": preview,
            "paper_id": r["paper_id"],
            "size": 50,
        }

    def _build_has_note_edges(self, note_rows: list[dict]) -> list[dict]:
        return [
            {
                "id": f"edge_paper{r['paper_id']}_note{r['id']}",
                "source": f"paper_{r['paper_id']}",
                "target": f"note_{r['id']}",
                "etype": "has_note",
            }
            for r in note_rows
        ]

    # ── Sparks ─────────────────────────────────────────────────

    def _fetch_sparks(self, limit: int) -> list[dict]:
        """Fetch top-quality mature sparks (skip raw seeds)."""
        rows = self._db.conn.execute(
            """SELECT id, content, source_refs, quality_score
               FROM ideator_sparks
               WHERE status IN ('deepening', 'deep_done')
               ORDER BY quality_score DESC
               LIMIT ?""",
            (max(1, limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def _spark_to_node(self, r: dict) -> dict:
        content = r.get("content") or ""
        preview = content[:80] + ("…" if len(content) > 80 else "")
        quality = float(r.get("quality_score") or 0.0)
        size = round(quality * 100)
        return {
            "id": f"spark_{r['id']}",
            "type": "spark",
            "db_id": r["id"],
            "label": "火花",
            "content_preview": preview,
            "quality_score": quality,
            "size": size,
        }

    def _build_cites_edges(self, spark_rows: list[dict], paper_ids: set[int]) -> list[dict]:
        """Parse spark.source_refs JSON; emit cites edges to papers in the graph."""
        edges: list[dict] = []
        for r in spark_rows:
            raw = r.get("source_refs") or "[]"
            try:
                refs = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                if ref.get("type") != "paper":
                    continue
                pid = ref.get("id")
                if not isinstance(pid, int) or pid not in paper_ids:
                    continue
                edges.append({
                    "id": f"edge_spark{r['id']}_paper{pid}",
                    "source": f"spark_{r['id']}",
                    "target": f"paper_{pid}",
                    "etype": "cites",
                })
        return edges

    def _build_cross_link_edges(self, paper_ids: set[int]) -> list[dict]:
        """Cross_links between papers (skip core_note/resolution side per Pre-Flight #5)."""
        if not paper_ids:
            return []
        rows = self._db.conn.execute(
            """SELECT source_a_type, source_a_id, source_b_type, source_b_id,
                      link_type, relevance_score
               FROM ideator_cross_links
               WHERE source_a_type = 'paper' AND source_b_type = 'paper'"""
        ).fetchall()
        edges: list[dict] = []
        for r in rows:
            a = r["source_a_id"]
            b = r["source_b_id"]
            if a not in paper_ids or b not in paper_ids:
                continue
            lo, hi = (a, b) if a < b else (b, a)
            edges.append({
                "id": f"edge_xlink_{lo}_{hi}",
                "source": f"paper_{lo}",
                "target": f"paper_{hi}",
                "etype": "cross_link",
                "label": r.get("link_type") or "",
            })
        return edges
