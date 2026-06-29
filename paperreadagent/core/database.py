"""
core/database.py
CoreDatabase — 核心表初始化 + 模块迁移协调。
与 paperreadagent/db/database.py 共享同一 SQLite 文件，但管理独立的版号表。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from .decorators import stable, evolving
from .schema import CORE_LATEST_VERSION, CORE_MIGRATIONS

logger = logging.getLogger(__name__)


class CoreDatabase:
    """
    核心数据库层。管理 core_* 表，协调模块 schema 迁移。

    与现有的 Database 类共享同一个 SQLite 文件但互不冲突：
    - Database 管理 projects/sessions/papers/summaries（schema_version 表）
    - CoreDatabase 管理 core_notes/core_llm_usage（core_schema_version 表）
    """

    def __init__(self, db_path: str | Path, *, existing_conn: sqlite3.Connection | None = None):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._existing_conn = existing_conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._existing_conn is not None:
            return self._existing_conn
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    @stable
    def initialize(self) -> None:
        """执行核心层 schema 迁移。应在 Core 工厂中调用。"""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS core_schema_version (version INTEGER PRIMARY KEY)"
        )
        row = self.conn.execute(
            "SELECT MAX(version) FROM core_schema_version"
        ).fetchone()
        current = row[0] if row and row[0] else 0

        for v in range(current + 1, CORE_LATEST_VERSION + 1):
            if v in CORE_MIGRATIONS:
                logger.info(f"[CoreDB] 应用核心迁移 v{v}...")
                try:
                    self.conn.executescript(CORE_MIGRATIONS[v])
                    self.conn.execute(
                        "INSERT OR REPLACE INTO core_schema_version (version) VALUES (?)",
                        (v,),
                    )
                    self.conn.commit()
                except sqlite3.OperationalError:
                    logger.warning(
                        f"[CoreDB] 迁移 v{v} 执行出错（可能已应用），跳过"
                    )

    @evolving
    def run_module_migration(self, module_name: str, latest_version: int, migrations: dict[int, str]) -> None:
        """
        为指定模块执行 schema 迁移。
        模块用独立版本表 {module}_schema_version。
        """
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {module_name}_schema_version "
            "(version INTEGER PRIMARY KEY)"
        )
        row = self.conn.execute(
            f"SELECT MAX(version) FROM {module_name}_schema_version"
        ).fetchone()
        current = row[0] if row and row[0] else 0

        for v in range(current + 1, latest_version + 1):
            if v in migrations:
                logger.info(f"[CoreDB] 应用 {module_name} 迁移 v{v}...")
                self.conn.executescript(migrations[v])
                self.conn.execute(
                    f"INSERT OR REPLACE INTO {module_name}_schema_version (version) VALUES (?)",
                    (v,),
                )
                self.conn.commit()

    @stable
    def execute(self, sql: str, params: tuple | None = None) -> sqlite3.Cursor:
        return self.conn.execute(sql, params or ())

    @evolving
    async def execute_async(self, sql: str, params: tuple | None = None) -> sqlite3.Cursor:
        return await self._run_in_executor(self.conn.execute, sql, params or ())

    @evolving
    async def commit_async(self) -> None:
        await self._run_in_executor(self.conn.commit)

    @evolving
    async def fetchone_async(self, sql: str, params: tuple | None = None) -> sqlite3.Row | None:
        cursor = await self.execute_async(sql, params)
        return cursor.fetchone()

    @evolving
    async def fetchall_async(self, sql: str, params: tuple | None = None) -> list[sqlite3.Row]:
        cursor = await self.execute_async(sql, params)
        return cursor.fetchall()

    async def _run_in_executor(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)

    @evolving
    def dict_row(self, row: sqlite3.Row | None) -> dict | None:
        """Convert a sqlite3.Row to dict. None-safe."""
        return dict(row) if row else None

    @evolving
    def dict_rows(self, rows: list[sqlite3.Row]) -> list[dict]:
        return [dict(r) for r in rows]

    @evolving
    def record_llm_usage(
        self,
        source_module: str,
        purpose: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        self.conn.execute(
            """INSERT INTO core_llm_usage
               (source_module, purpose, model_name, prompt_tokens, completion_tokens, total_tokens)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source_module, purpose, model_name, prompt_tokens, completion_tokens, total_tokens),
        )
        self.conn.commit()

    def close(self) -> None:
        if self._conn and self._existing_conn is None:
            self._conn.close()
            self._conn = None
