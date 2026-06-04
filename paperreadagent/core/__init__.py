"""
core/__init__.py
Core 单例 + ModuleInfo 数据类 + create_core() 工厂。

所有模块通过 register(core) 获得 Core 实例，这是模块与系统交互的唯一接口。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .decorators import stable, evolving
from .config import load_config, merge_configs, apply_env_overrides
from .schema import CORE_LATEST_VERSION, CORE_MIGRATIONS
from .database import CoreDatabase
from .llm import CoreLLM
from .voice import CoreVoice
from .knowledge import KnowledgeLayer
from .scheduler import CoreScheduler
from .event_bus import EventBus
from .frontend import CoreFrontend

logger = logging.getLogger(__name__)


@dataclass
class ModuleInfo:
    """模块注册时必须返回的数据契约。"""
    name: str
    version: str
    schema_version: int       # 0 表示模块没有自己的表
    routes: object | None     # FastAPI APIRouter 或 None


class Core:
    """
    核心层单例。所有模块通过 register(core) 获得此对象。

    暴露的公共方法均标注稳定性等级（@stable / @evolving / @internal）。
    模块只能调用 @stable 和 @evolving 的方法。
    """

    # ── 公开子系统 ────────────────────────────────────────────

    db: CoreDatabase                      # @stable
    llm: CoreLLM                          # @stable
    voice: CoreVoice                      # @evolving
    knowledge: KnowledgeLayer             # @evolving
    scheduler: CoreScheduler              # @evolving
    event_bus: EventBus                   # @evolving
    frontend: CoreFrontend                # @evolving
    config: dict                          # @stable

    def __init__(
        self,
        *,
        db: CoreDatabase,
        llm: CoreLLM,
        voice: CoreVoice,
        knowledge: KnowledgeLayer,
        scheduler: CoreScheduler,
        event_bus: EventBus,
        frontend: CoreFrontend,
        config: dict,
    ):
        self.db = db
        self.llm = llm
        self.voice = voice
        self.knowledge = knowledge
        self.scheduler = scheduler
        self.event_bus = event_bus
        self.frontend = frontend
        self.config = config
        self._modules: dict[str, ModuleInfo] = {}
        self._fastapi_app = None
        self.legacy_db: Any = None  # 由 create_app 或测试注入

    # ── 模块生命周期 ──────────────────────────────────────────

    @evolving
    def register_module(self, info: ModuleInfo) -> None:
        """注册一个模块。记录 ModuleInfo，标记已加载。"""
        self._modules[info.name] = info
        logger.info(f"[Core] 模块已注册: {info.name} v{info.version} "
                    f"(schema v{info.schema_version})")

    @evolving
    def unregister_module(self, name: str) -> None:
        """卸载模块：移除路由、DB 表、调度任务、事件订阅、前端组件。"""
        if name not in self._modules:
            return
        try:
            self.db.conn.execute(f"DROP TABLE IF EXISTS {name}_schema_version")
            self.db.conn.execute(
                "DELETE FROM core_notes WHERE source_module = ?", (name,)
            )
            self.db.conn.commit()
        except Exception:
            logger.exception(f"[Core] 卸载 {name} 时数据库清理失败")
        self.scheduler.pause_module(name)
        self.event_bus.unsubscribe_module(name)
        self.frontend.unregister_module(name)
        del self._modules[name]
        logger.info(f"[Core] 模块已卸载: {name}")

    @stable
    def get_module(self, name: str) -> ModuleInfo | None:
        return self._modules.get(name)

    @stable
    def list_modules(self) -> list[ModuleInfo]:
        return list(self._modules.values())

    @stable
    def module_config(self, name: str) -> dict:
        """Return config dict for a module, or empty dict if not found."""
        return self.config.get("modules", {}).get(name, {})

    # ── FastAPI 集成 ──────────────────────────────────────────

    @evolving
    def mount_app(self, app) -> None:
        """挂载 FastAPI 应用引用，供模块路由注册使用。"""
        self._fastapi_app = app

    @evolving
    def mount_routes(self, router, *, prefix: str = "", tags: list[str] | None = None) -> None:
        """将模块的 APIRouter 挂载到 FastAPI 应用。"""
        if self._fastapi_app:
            self._fastapi_app.include_router(router, prefix=prefix, tags=tags)

    @evolving
    def mount_static(self, path: str, directory: str) -> None:
        """挂载模块静态文件目录。"""
        if self._fastapi_app:
            from fastapi.staticfiles import StaticFiles
            self._fastapi_app.mount(path, StaticFiles(directory=directory), name=f"static_{path}")


@stable
def create_core(
    config_path: str | Path = "config.yaml",
    *,
    db_path: str | Path = "paperreadagent.db",
    timezone: str = "Asia/Shanghai",
) -> Core:
    """
    工厂函数：从配置文件创建 Core 单例。

    1. 加载并合并配置
    2. 初始化 CoreDatabase → 执行核心 schema 迁移
    3. 创建 CoreLLM（包装 OpenAI 兼容客户端）
    4. 创建 KnowledgeLayer、CoreScheduler、EventBus、CoreFrontend
    5. 组装并返回 Core 实例
    """
    # 配置
    raw_config = load_config(config_path)
    merged_config = apply_env_overrides(raw_config)

    # 从配置中提取 LLM 参数（兼容 core.llm 块和顶层 llm 块）
    llm_cfg = merged_config.get("core", {}).get("llm", {})
    if not llm_cfg:
        llm_cfg = merged_config.get("llm", {})
    knowledge_cfg = merged_config.get("core", {}).get("knowledge", {})
    embedding_model = knowledge_cfg.get("embedding_model", "BAAI/bge-m3")
    embedding_provider = knowledge_cfg.get("embedding_provider", "local")
    llm_cfg["embedding_model"] = embedding_model
    llm_cfg["embedding_provider"] = embedding_provider

    # 数据库
    db = CoreDatabase(db_path)
    db.initialize()

    # LLM
    llm = CoreLLM.from_config(llm_cfg, db=db)

    # Voice（配置路径: core.voice）
    voice_cfg = merged_config.get("core", {}).get("voice", {})
    voice = CoreVoice.from_config(voice_cfg)

    # 子系统
    knowledge = KnowledgeLayer(db)

    # 自动将 SQLite 中已有 embedding 迁移到 LanceDB（非阻塞，失败静默跳过）
    try:
        n = knowledge.populate_lance_from_sqlite()
        if n:
            import logging
            logging.getLogger(__name__).info(f"[Core] Auto-migrated {n} embeddings to LanceDB")
    except Exception:
        pass

    scheduler = CoreScheduler(
        timezone=merged_config.get("core", {}).get("scheduler", {}).get("timezone", timezone)
    )
    event_bus = EventBus()
    frontend = CoreFrontend()

    core = Core(
        db=db,
        llm=llm,
        voice=voice,
        knowledge=knowledge,
        scheduler=scheduler,
        event_bus=event_bus,
        frontend=frontend,
        config=merged_config,
    )

    logger.info("[Core] 核心层初始化完成")
    return core
