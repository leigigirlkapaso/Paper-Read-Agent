"""
db/database.py
SQLite 数据库操作层，管理项目、会话、论文、总结的全生命周期。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime

from db.schema import LATEST_VERSION, MIGRATIONS

logger = logging.getLogger(__name__)


class Database:
    """
    SQLite 数据库封装，WAL 模式，外键启用。

    使用示例:
        db = Database("paperreadagent.db")
        proj_id = db.create_project("My Research")
        session_id = db.create_session(proj_id, "full", config_dict)
        db.update_session_status(session_id, "running")
    """

    def __init__(self, db_path: str | Path = "paperreadagent.db"):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = __import__("threading").Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── 迁移 ──────────────────────────────────────────────────────

    def _migrate(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        row = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] if row and row[0] else 0

        for v in range(current + 1, LATEST_VERSION + 1):
            if v in MIGRATIONS:
                logger.info(f"[Database] 应用迁移 v{v}...")
                self.conn.executescript(MIGRATIONS[v])
                self.conn.execute(
                    "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (v,)
                )
                self.conn.commit()

    # ── 项目 CRUD ─────────────────────────────────────────────────

    def create_project(self, name: str, description: str = "") -> int:
        cursor = self.conn.execute(
            "INSERT INTO projects (name, description) VALUES (?, ?)",
            (name, description),
        )
        self.conn.commit()
        logger.info(f"[Database] 创建项目: {name} (id={cursor.lastrowid})")
        return cursor.lastrowid

    def get_or_create_project(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM projects WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        return self.create_project(name)

    def list_projects(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_project(self, project_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_project(self, project_id: int) -> None:
        # 级联删除 sessions / papers / summaries
        self.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self.conn.commit()
        logger.info(f"[Database] 删除项目 id={project_id}")

    def touch_project(self, project_id: int) -> None:
        self.conn.execute(
            "UPDATE projects SET updated_at = datetime('now') WHERE id = ?",
            (project_id,),
        )
        self.conn.commit()

    # ── 会话 CRUD ─────────────────────────────────────────────────

    def create_session(
        self,
        project_id: int,
        mode: str,
        config: dict,
        session_dir: str,
    ) -> int:
        config_json = json.dumps(config, ensure_ascii=False, sort_keys=True)
        config_hash = _sha256_hex(config_json)[:16]

        cursor = self.conn.execute(
            """
            INSERT INTO sessions (project_id, mode, config_snapshot, config_hash, session_dir)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, mode, config_json, config_hash, session_dir),
        )
        self.conn.commit()
        self.touch_project(project_id)
        logger.info(
            f"[Database] 创建会话: {session_dir} (id={cursor.lastrowid}, hash={config_hash})"
        )
        return cursor.lastrowid

    def update_session(
        self,
        session_id: int,
        status: str | None = None,
        **kwargs,
    ) -> None:
        sets: list[str] = []
        params: list = []

        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status in ("completed", "failed", "cancelled"):
                sets.append("completed_at = datetime('now')")

        for key, val in kwargs.items():
            col = _snake_to_col(key)
            sets.append(f"{col} = ?")
            params.append(val)

        if not sets:
            return

        params.append(session_id)
        self.conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params
        )
        self.conn.commit()

    def get_session(self, session_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, project_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 论文 CRUD ─────────────────────────────────────────────────

    def insert_papers(
        self, session_id: int, papers: list[dict]
    ) -> list[int]:
        """批量插入论文，返回成功插入的 paper id 列表。跳过重复的 arxiv_id。"""
        ids: list[int] = []
        try:
            for p in papers:
                platform = p.get("source_platform", "")
                if not platform:
                    platform = None
                try:
                    cursor = self.conn.execute(
                        """
                        INSERT INTO papers
                            (session_id, arxiv_id, doi, source_platform, title, authors,
                             published, abstract, relevance_score, source_url,
                             has_code, code_url, venue, citation_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            p.get("arxiv_id", ""),
                            p.get("doi", ""),
                            platform,
                            p.get("title", ""),
                            json.dumps(p.get("authors", []), ensure_ascii=False),
                            p.get("published", ""),
                            p.get("abstract", ""),
                            p.get("relevance_score", 0.0),
                            p.get("source_url", ""),
                            p.get("has_code", 0),
                            p.get("code_url", ""),
                            p.get("venue", ""),
                            p.get("citation_count", 0),
                        ),
                    )
                    if cursor.lastrowid:
                        ids.append(cursor.lastrowid)
                except sqlite3.IntegrityError as e:
                    err_msg = str(e).upper()
                    if "CHECK" in err_msg:
                        logger.error(
                            f"[Database] CHECK 约束失败，论文被丢弃: "
                            f"arxiv_id={p.get('arxiv_id', '?')}, "
                            f"source_platform={repr(platform)}, "
                            f"title={p.get('title', '?')[:80]}, "
                            f"error={e}"
                        )
                    else:
                        logger.debug(f"[Database] 跳过重复论文: {p.get('arxiv_id', '?')}")

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return ids

    def update_paper(self, paper_id: int, **kwargs) -> None:
        sets: list[str] = []
        params: list = []
        for key, val in kwargs.items():
            col = _snake_to_col(key)
            sets.append(f"{col} = ?")
            params.append(val if not isinstance(val, (list, dict)) else json.dumps(val, ensure_ascii=False))
        if not sets:
            return
        params.append(paper_id)
        self.conn.execute(
            f"UPDATE papers SET {', '.join(sets)} WHERE id = ?", params
        )
        self.conn.commit()

    def update_paper_by_arxiv_id(
        self, session_id: int, arxiv_id: str, **kwargs
    ) -> None:
        row = self.conn.execute(
            "SELECT id FROM papers WHERE session_id = ? AND arxiv_id = ?",
            (session_id, arxiv_id),
        ).fetchone()
        if row:
            self.update_paper(row["id"], **kwargs)

    def get_session_papers(self, session_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM papers WHERE session_id = ? ORDER BY relevance_score DESC",
            (session_id,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["authors"] = _safe_json_loads(d.get("authors"))
            results.append(d)
        return results

    def get_paper(self, paper_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["authors"] = _safe_json_loads(d.get("authors"))
        return d

    # ── 总结 CRUD ─────────────────────────────────────────────────

    def get_cached_summary(
        self, paper_id: int, prompt_hash: str, pdf_text_hash: str
    ) -> str | None:
        row = self.conn.execute(
            """
            SELECT content FROM summaries
            WHERE paper_id = ? AND summary_prompt_hash = ? AND pdf_text_hash = ?
            """,
            (paper_id, prompt_hash, pdf_text_hash),
        ).fetchone()
        return row["content"] if row else None

    def save_summary(
        self,
        paper_id: int,
        prompt_hash: str,
        model_name: str,
        temperature: float,
        max_chars: int,
        content: str,
        pdf_text_hash: str,
        token_count: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO summaries
                (paper_id, summary_prompt_hash, model_name, temperature,
                 max_chars, content, pdf_text_hash, token_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (paper_id, prompt_hash, model_name, temperature, max_chars, content, pdf_text_hash, token_count),
        )
        self.conn.commit()

    def get_paper_summaries(self, paper_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM summaries WHERE paper_id = ? ORDER BY created_at DESC",
            (paper_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 日志 ──────────────────────────────────────────────────────

    def log(self, session_id: int, level: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO run_logs (session_id, level, message) VALUES (?, ?, ?)",
            (session_id, level, message),
        )
        self.conn.commit()

    # ── 搜索 ──────────────────────────────────────────────────────

    def search_papers(self, query: str, project_id: int | None = None) -> list[dict]:
        """全文搜索，优先使用 FTS5，回退 LIKE。"""
        # 尝试 FTS5
        if self._fts_ready():
            return self._search_fts(query, project_id)
        # 回退 LIKE
        return self._search_like(query, project_id)

    def _fts_ready(self) -> bool:
        try:
            self.conn.execute("SELECT COUNT(*) FROM papers_fts")
            return True
        except Exception:
            return False

    def rebuild_fts(self) -> int:
        """重建 FTS5 全文索引。返回索引的论文数。"""
        try:
            self.conn.execute("DROP TABLE IF EXISTS papers_fts")
            self.conn.execute(
                "CREATE VIRTUAL TABLE papers_fts USING fts5(title, abstract, venue, content='')"
            )
            self.conn.execute(
                """
                INSERT INTO papers_fts(rowid, title, abstract, venue)
                SELECT id, title, abstract, venue FROM papers
                """
            )
            self.conn.commit()
            count = self.conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]
            logger.info(f"[Database] FTS5 索引重建完成: {count} 篇")
            return count
        except Exception:
            self.conn.rollback()
            logger.exception("[Database] FTS5 索引重建失败")
            raise

    def _search_fts(self, query: str, project_id: int | None) -> list[dict]:
        """FTS5 全文搜索。"""
        fts_query = _to_fts_query(query)
        if project_id:
            rows = self.conn.execute(
                """
                SELECT p.*, s.name as project_name, se.mode as session_mode
                FROM papers_fts f
                JOIN papers p ON p.id = f.rowid
                JOIN sessions se ON p.session_id = se.id
                JOIN projects s ON se.project_id = s.id
                WHERE s.id = ? AND papers_fts MATCH ?
                ORDER BY p.relevance_score DESC
                LIMIT 50
                """,
                (project_id, fts_query),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT p.*, s.name as project_name, se.mode as session_mode
                FROM papers_fts f
                JOIN papers p ON p.id = f.rowid
                JOIN sessions se ON p.session_id = se.id
                JOIN projects s ON se.project_id = s.id
                WHERE papers_fts MATCH ?
                ORDER BY p.relevance_score DESC
                LIMIT 50
                """,
                (fts_query,),
            ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            d["authors"] = _safe_json_loads(d.get("authors"))
            results.append(d)
        return results

    def _search_like(self, query: str, project_id: int | None) -> list[dict]:
        """LIKE 回退搜索。"""
        like = f"%{query}%"
        if project_id:
            rows = self.conn.execute(
                """
                SELECT p.*, s.name as project_name, se.mode as session_mode
                FROM papers p
                JOIN sessions se ON p.session_id = se.id
                JOIN projects s ON se.project_id = s.id
                WHERE s.id = ?
                  AND (p.title LIKE ? OR p.abstract LIKE ? OR p.venue LIKE ?)
                ORDER BY p.relevance_score DESC
                LIMIT 50
                """,
                (project_id, like, like, like),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT p.*, s.name as project_name, se.mode as session_mode
                FROM papers p
                JOIN sessions se ON p.session_id = se.id
                JOIN projects s ON se.project_id = s.id
                WHERE p.title LIKE ? OR p.abstract LIKE ? OR p.venue LIKE ?
                ORDER BY p.relevance_score DESC
                LIMIT 50
                """,
                (like, like, like),
            ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            d["authors"] = _safe_json_loads(d.get("authors"))
            results.append(d)
        return results

    # ── 笔记 CRUD ──────────────────────────────────────────────────

    def get_note(self, paper_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM notes WHERE paper_id = ?", (paper_id,)
        ).fetchone()
        return dict(row) if row else None

    def save_note(self, paper_id: int, content: str) -> None:
        self.conn.execute(
            """
            INSERT INTO notes (paper_id, content, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(paper_id) DO UPDATE SET
                content = excluded.content,
                updated_at = datetime('now')
            """,
            (paper_id, content),
        )
        self.conn.commit()

    def delete_note(self, paper_id: int) -> None:
        self.conn.execute("DELETE FROM notes WHERE paper_id = ?", (paper_id,))
        self.conn.commit()

    # ── 收藏夹 CRUD ──────────────────────────────────────────────────

    def toggle_favorite(self, paper_id: int) -> bool:
        """切换收藏状态，返回收藏后的状态（True=已收藏）。"""
        row = self.conn.execute(
            "SELECT id FROM favorites WHERE paper_id = ?", (paper_id,)
        ).fetchone()
        if row:
            self.conn.execute("DELETE FROM favorites WHERE paper_id = ?", (paper_id,))
            self.conn.commit()
            return False
        else:
            self.conn.execute(
                "INSERT INTO favorites (paper_id) VALUES (?)", (paper_id,)
            )
            self.conn.commit()
            return True

    def is_favorited(self, paper_id: int) -> bool:
        row = self.conn.execute(
            "SELECT id FROM favorites WHERE paper_id = ?", (paper_id,)
        ).fetchone()
        return row is not None

    def get_favorited_paper_ids(self) -> set[int]:
        rows = self.conn.execute("SELECT paper_id FROM favorites").fetchall()
        return {r["paper_id"] for r in rows}

    def get_favorites(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT p.*, f.created_at as favorited_at,
                   s.name as project_name, se.mode as session_mode
            FROM favorites f
            JOIN papers p ON f.paper_id = p.id
            LEFT JOIN sessions se ON p.session_id = se.id
            LEFT JOIN projects s ON se.project_id = s.id
            ORDER BY f.created_at DESC
            """
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["authors"] = _safe_json_loads(d.get("authors"))
            results.append(d)
        return results

    def get_all_notes(self, project_id: int | None = None) -> list[dict]:
        if project_id:
            rows = self.conn.execute(
                """
                SELECT n.*, p.title as paper_title, p.arxiv_id,
                       pr.name as project_name, pr.id as project_id
                FROM notes n
                LEFT JOIN papers p ON n.paper_id = p.id
                LEFT JOIN sessions s ON p.session_id = s.id
                LEFT JOIN projects pr ON s.project_id = pr.id
                WHERE pr.id = ?
                ORDER BY n.updated_at DESC
                """,
                (project_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT n.*, p.title as paper_title, p.arxiv_id,
                       pr.name as project_name, pr.id as project_id
                FROM notes n
                LEFT JOIN papers p ON n.paper_id = p.id
                LEFT JOIN sessions s ON p.session_id = s.id
                LEFT JOIN projects pr ON s.project_id = pr.id
                ORDER BY n.updated_at DESC
                """,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_cross_project_graph(self) -> dict:
        """返回项目关系图数据：节点（项目+大小）和边（共享论文数）。"""
        # 节点：每个项目的论文数和笔记数
        node_rows = self.conn.execute(
            """
            SELECT
                pr.id, pr.name,
                COUNT(DISTINCT p.id) as paper_count,
                COUNT(DISTINCT n.id) as note_count
            FROM projects pr
            LEFT JOIN sessions s ON s.project_id = pr.id
            LEFT JOIN papers p ON p.session_id = s.id
            LEFT JOIN notes n ON n.paper_id = p.id
            GROUP BY pr.id
            ORDER BY pr.name
            """
        ).fetchall()
        nodes = []
        for r in node_rows:
            weight = (r["note_count"] or 0) * 2 + (r["paper_count"] or 0) * 1
            nodes.append({
                "id": r["id"], "name": r["name"],
                "paper_count": r["paper_count"] or 0,
                "note_count": r["note_count"] or 0,
                "weight": weight,
            })

        # 边：项目之间共享的论文（按 arxiv_id 匹配）
        edge_rows = self.conn.execute(
            """
            SELECT
                pr_a.id as proj_a, pr_b.id as proj_b,
                COUNT(DISTINCT p_a.arxiv_id) as shared
            FROM papers p_a
            JOIN sessions s_a ON p_a.session_id = s_a.id
            JOIN projects pr_a ON s_a.project_id = pr_a.id
            JOIN papers p_b ON LOWER(p_b.arxiv_id) = LOWER(p_a.arxiv_id)
            JOIN sessions s_b ON p_b.session_id = s_b.id
            JOIN projects pr_b ON s_b.project_id = pr_b.id
            WHERE pr_a.id < pr_b.id
            GROUP BY pr_a.id, pr_b.id
            HAVING shared > 0
            ORDER BY shared DESC
            """
        ).fetchall()
        edges = []
        for r in edge_rows:
            edges.append({
                "source": r["proj_a"], "target": r["proj_b"],
                "weight": r["shared"],
            })

        return {"nodes": nodes, "edges": edges}

    # ── 统计 ──────────────────────────────────────────────────────

    def get_project_stats(self, project_id: int) -> dict:
        stats = self.conn.execute(
            """
            SELECT
                COUNT(DISTINCT s.id) as total_sessions,
                COUNT(DISTINCT p.id) as total_papers,
                COUNT(DISTINCT su.id) as total_summaries,
                COALESCE(SUM(su.token_count), 0) as total_tokens
            FROM projects pr
            LEFT JOIN sessions s ON s.project_id = pr.id
            LEFT JOIN papers p ON p.session_id = s.id
            LEFT JOIN summaries su ON su.paper_id = p.id
            WHERE pr.id = ?
            """,
            (project_id,),
        ).fetchone()
        return dict(stats) if stats else {}


# ── 内部工具 ──────────────────────────────────────────────────────

def _sha256_hex(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def _to_fts_query(user_query: str) -> str:
    """将用户输入转为 FTS5 查询。安全处理特殊字符。"""
    import re
    # 移除 FTS5 特殊字符，保留字母数字和中文字符
    safe = re.sub(r'[^\w一-鿿\s]', ' ', user_query)
    tokens = [t for t in safe.split() if len(t) >= 1]
    if not tokens:
        return '""'  # 空查询匹配空字符串（零结果）
    # 每个词加前缀匹配，双引号包裹含空格的词
    return " AND ".join(f'"{t}"' if " " in t else f'"{t}"' for t in tokens)


def _safe_json_loads(raw: str | None, default=None) -> list | dict:
    from utils.json_utils import safe_json_loads as _sjl
    return _sjl(raw, default=default)


def _snake_to_col(key: str) -> str:
    """将 snake_case 的字段名映射为数据库列名。"""
    mapping = {
        "pdf_url": "source_url",
        "pdf_path": "pdf_path",
        "summary_path": "summary_path",
        "download_status": "download_status",
        "parse_status": "parse_status",
        "summary_status": "summary_status",
        "arxiv_url": "source_url",
    }
    return mapping.get(key, key)
