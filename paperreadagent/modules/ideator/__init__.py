"""
modules/ideator/__init__.py
Ideator 模块入口。唯一对外接口：def register(core) -> ModuleInfo。

跨知识联想挖掘器 — 从论文/笔记/洞察中发现潜在研究方向。
"""

from __future__ import annotations

import logging
from pathlib import Path

from paperreadagent.core import Core, ModuleInfo
from .schema import LATEST_VERSION, MIGRATIONS

logger = logging.getLogger(__name__)

MODULE_NAME = "ideator"
MODULE_VERSION = "0.1.0"

_pipeline = None
_roundtable_manager = None


def register(core: Core) -> ModuleInfo:
    """
    1. 合并模块默认配置
    2. 执行 schema 迁移
    3. 注册后台任务
    4. 注册全局前端组件
    5. 订阅核心事件
    6. 挂载路由
    """
    global _pipeline, _roundtable_manager

    # ── 配置合并 ──────────────────────────────────────────────
    from paperreadagent.core.config import load_module_defaults, merge_configs

    default_path = Path(__file__).parent / "config.default.yaml"
    defaults = load_module_defaults(MODULE_NAME, default_path)
    if defaults:
        core.config = merge_configs(defaults, core.config)

    ideator_cfg = core.module_config(MODULE_NAME)

    # ── Schema 迁移 ───────────────────────────────────────────
    core.db.run_module_migration(MODULE_NAME, LATEST_VERSION, MIGRATIONS)
    logger.info(f"[Ideator] Schema v{LATEST_VERSION} 就绪")

    # ── DataAccess + Pipeline ─────────────────────────────────
    from .data_access import DataAccess
    from .pipeline import IdeatorPipeline

    data = DataAccess(core)
    _pipeline = IdeatorPipeline(core, data)

    # AgentTeam 基础设施（替代旧 RoundtableManager）
    from .ideator_llm import IdeatorLLM
    from .agent_team import AgentTeamManager
    from .team_memory import TeamMemory
    from .graduation import GraduationManager
    from .tool_registry import create_default_registry
    from .arbiter import Arbiter

    ideator_llm = IdeatorLLM(core_llm=core.llm)
    team_memory = TeamMemory(core.db.conn)
    graduation_mgr = GraduationManager(core.db.conn, team_memory)
    tool_registry = create_default_registry()
    arbiter = Arbiter(llm=ideator_llm, graduation=graduation_mgr,
                      tool_registry=tool_registry, team_memory=team_memory)
    _roundtable_manager = AgentTeamManager(
        llm=ideator_llm, data_access=data,
        tool_registry=tool_registry, team_memory=team_memory,
        graduation=graduation_mgr, arbiter=arbiter,
    )

    # ── 路由 ──────────────────────────────────────────────────
    from .routes import router
    core.mount_routes(router, prefix="/ideator", tags=["ideator"])

    # ── 前端组件 ─────────────────────────────────────────────
    core.frontend.register_global_component(
        name="ideator-styles",
        template="ideator/empty.html",
        mount_point="body-end",
        init_script="",
        css_file="ideator/ideator.css",
    )

    # ── 事件订阅 ────────────────────────────────────────────
    core.event_bus.subscribe(MODULE_NAME, "core:note:created", _on_new_note)
    core.event_bus.subscribe(MODULE_NAME, "thinker:summary:generated", _on_new_summary)

    # ── 后台任务 ─────────────────────────────────────────────
    core.scheduler.add(
        module=MODULE_NAME,
        name="daily_deep_mine",
        func=_daily_mine,
        trigger="cron",
        hour=ideator_cfg.get("full_mine_hour", 3),
        minute=0,
        on_error="retry",
    )

    core.scheduler.add(
        module=MODULE_NAME,
        name="spark_gc",
        func=_spark_gc,
        trigger="interval",
        minutes=ideator_cfg.get("gc_interval_minutes", 30),
        on_error="skip",
    )

    info = ModuleInfo(
        name=MODULE_NAME,
        version=MODULE_VERSION,
        schema_version=LATEST_VERSION,
        routes=router,
    )
    core.register_module(info)
    logger.info(f"[Ideator] 模块已注册 v{MODULE_VERSION}")
    return info


def get_roundtable_manager():
    """Return the module-level RoundtableManager singleton."""
    global _roundtable_manager
    return _roundtable_manager


async def _daily_mine() -> None:
    """每日全量挖掘任务。"""
    global _pipeline
    if _pipeline is None:
        return
    try:
        ids = await _pipeline.run_full()
        logger.info(f"[Ideator] 每日挖掘完成：{len(ids)} 个火花")
    except Exception:
        logger.exception("[Ideator] 每日挖掘失败")


async def _spark_gc() -> None:
    """定期清理低质量火花。"""
    global _pipeline
    if _pipeline is None:
        return
    try:
        n = _pipeline.store.gc_low_quality()
        if n > 0:
            logger.info(f"[Ideator] GC 清理 {n} 条低分火花")
    except Exception:
        logger.exception("[Ideator] GC 失败")


async def _on_new_note(event: str, **data) -> None:
    """当其他模块创建笔记时，触发增量挖掘。"""
    global _pipeline
    if _pipeline is None:
        return
    try:
        from datetime import datetime, timezone
        since = data.get("timestamp", datetime.now(timezone.utc).isoformat())
        await _pipeline.run_incremental(since)
    except Exception:
        logger.warning("[Ideator] 增量挖掘失败", exc_info=True)


async def _on_new_summary(event: str, **data) -> None:
    """当对话摘要生成后，触发增量挖掘。"""
    global _pipeline
    if _pipeline is None:
        return
    try:
        from datetime import datetime, timezone
        since = data.get("timestamp", datetime.now(timezone.utc).isoformat())
        await _pipeline.run_incremental(since)
    except Exception:
        logger.warning("[Ideator] 增量挖掘失败", exc_info=True)
