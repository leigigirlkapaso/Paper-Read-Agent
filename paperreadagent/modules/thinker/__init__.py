"""
modules/thinker/__init__.py
Thinker 模块入口。唯一对外接口：def register(core) -> ModuleInfo。

思考伙伴 — 浮动侧边栏对话子系统。
"""

from __future__ import annotations

import logging
from pathlib import Path

from paperreadagent.core import Core, ModuleInfo
from .schema import LATEST_VERSION, MIGRATIONS

logger = logging.getLogger(__name__)

MODULE_NAME = "thinker"
MODULE_VERSION = "0.1.0"

_core: Core | None = None


def _is_thinker_enabled(core: Core, cfg: dict) -> bool:
    """从 core_notes 读取持久化状态，无记录时回退到 config 默认值。"""
    try:
        row = core.db.conn.execute(
            "SELECT content FROM core_notes "
            "WHERE source_module = 'thinker' AND source_ref = 'visibility' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return row["content"].strip().lower() != "disabled"
    except Exception:
        pass
    return cfg.get("enabled", True)


def register(core: Core) -> ModuleInfo:
    """
    1. 合并模块默认配置
    2. 执行 schema 迁移
    3. 注册后台任务（Phase 6 启用）
    4. 注册全局前端组件（Phase 3 启用）
    5. 订阅核心事件
    6. 挂载路由
    """
    global _core
    _core = core
    # ── 配置合并 ──────────────────────────────────────────────
    from paperreadagent.core.config import load_module_defaults, merge_configs

    default_path = Path(__file__).parent / "config.default.yaml"
    defaults = load_module_defaults(MODULE_NAME, default_path)
    if defaults:
        core.config = merge_configs(defaults, core.config)

    thinker_cfg = core.module_config(MODULE_NAME)

    # ── Schema 迁移 ───────────────────────────────────────────
    core.db.run_module_migration(MODULE_NAME, LATEST_VERSION, MIGRATIONS)
    logger.info(f"[Thinker] Schema v{LATEST_VERSION} 就绪")

    # ── 路由 ──────────────────────────────────────────────────
    from .routes import router
    core.mount_routes(router, prefix="/thinker", tags=["thinker"])

    # ── 前端组件 ─────────────────────────────────────────────
    thinker_enabled = _is_thinker_enabled(core, thinker_cfg)
    core._thinker_visible = thinker_enabled
    if thinker_enabled:
        core.frontend.register_global_component(
            name="thinker-panel",
            template="thinker/panel.html",
            mount_point="body-end",
            init_script="thinker/thinker.js",
            css_file="thinker/thinker.css",
        )
    else:
        logger.info("[Thinker] 全局组件已禁用（Web 设置页可重新开启）")

    # ── 后台任务 ─────────────────────────────────────────────
    from .questions import QuestionGenerator
    from .resolutions import ResolutionTracker

    question_gen = QuestionGenerator(core)
    resolution_tracker = ResolutionTracker(core)

    core.scheduler.add(
        module=MODULE_NAME,
        name="inactivity_check",
        func=question_gen.check_inactivity,
        trigger="interval",
        minutes=thinker_cfg.get("question_frequency_minutes", 5),
        on_error="retry",
    )

    core.scheduler.add(
        module=MODULE_NAME,
        name="resolution_check",
        func=resolution_tracker.check_daily_resolutions,
        trigger="cron",
        hour=9,
        minute=0,
        on_error="retry",
    )

    # ── 事件订阅 ────────────────────────────────────────────
    core.event_bus.subscribe(MODULE_NAME, "core:note:created", _on_note_created)
    core.event_bus.subscribe(MODULE_NAME, "thinker:summary:generated", _on_summary_generated)

    info = ModuleInfo(
        name=MODULE_NAME,
        version=MODULE_VERSION,
        schema_version=LATEST_VERSION,
        routes=router,
    )
    core.register_module(info)
    logger.info(f"[Thinker] 模块已注册 v{MODULE_VERSION}")
    return info


async def _on_note_created(event: str, **data) -> None:
    """当其他模块创建笔记时，记录事件。未来可用于知识关联。"""
    logger.debug(f"[Thinker] 收到事件: {event} source={data.get('source_module')}")


async def _on_summary_generated(event: str, **data) -> None:
    """对话摘要生成后，注册到 memory_index。"""
    global _core
    if _core is None:
        return
    note_id = data.get("note_id")
    if not note_id:
        return
    try:
        from .memory import MemoryPipeline

        mp = MemoryPipeline(_core)
        await mp.index_note(note_id, "insight")
    except Exception:
        logger.debug("[Thinker] memory_index 注册失败", exc_info=True)
