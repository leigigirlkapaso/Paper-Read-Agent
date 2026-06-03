"""
core/scheduler.py
CoreScheduler — APScheduler AsyncIOScheduler 封装，管理所有模块的后台任务。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

from .decorators import stable, evolving

logger = logging.getLogger(__name__)


class CoreScheduler:
    """
    后台任务调度器。封装 APScheduler AsyncIOScheduler。

    每个模块通过 add() 注册任务，调度器负责：
    - 按 trigger 准时触发
    - 任务异常隔离（一个任务崩溃不影响其他任务）
    - on_error 策略：retry（重试一次）| skip（跳过）| pause（暂停该模块任务）
    """

    def __init__(self, timezone: str = "Asia/Shanghai"):
        self._timezone = timezone
        self._scheduler = None
        self._jobs: dict[str, dict] = {}  # job_id → {module, name, on_error, error_count}
        self._paused_modules: set[str] = set()

    @evolving
    def add(
        self,
        *,
        module: str,
        name: str,
        func: Callable[..., Awaitable[None]],
        trigger: str = "interval",
        on_error: str = "retry",
        **trigger_kwargs,
    ) -> str:
        """
        注册一个后台任务。

        参数：
        - module: 模块名
        - name: 任务名（与 module 组成唯一 job_id）
        - func: async callable
        - trigger: "interval" | "cron" | "date"
        - on_error: "retry" | "skip" | "pause"
        - **trigger_kwargs: 传递给 APScheduler trigger 的参数
          interval: minutes=5, seconds=30, hours=1
          cron: hour=9, minute=0
        """
        job_id = f"{module}:{name}"

        async def _wrapper():
            if module in self._paused_modules:
                return
            try:
                await func()
            except Exception:
                logger.exception(f"[Scheduler] 任务 {job_id} 执行失败")
                info = self._jobs.get(job_id, {})
                info["error_count"] = info.get("error_count", 0) + 1

                if on_error == "pause":
                    self._paused_modules.add(module)
                    logger.warning(f"[Scheduler] 模块 {module} 因错误被暂停")
                # retry: 下次触发时自然重试
                # skip: 什么都不做

        self._jobs[job_id] = {
            "module": module, "name": name, "on_error": on_error, "error_count": 0,
        }
        self._add_aps_job(job_id, _wrapper, trigger, trigger_kwargs)
        logger.info(f"[Scheduler] 注册任务: {job_id} trigger={trigger}")
        return job_id

    @evolving
    def remove(self, module: str, name: str) -> None:
        """移除任务。"""
        job_id = f"{module}:{name}"
        self._jobs.pop(job_id, None)
        if self._scheduler:
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass

    @evolving
    def pause_module(self, module: str) -> None:
        """暂停某模块的所有任务。"""
        self._paused_modules.add(module)
        logger.info(f"[Scheduler] 模块 {module} 任务已暂停")

    @evolving
    def resume_module(self, module: str) -> None:
        """恢复某模块的任务。"""
        self._paused_modules.discard(module)
        # 重置错误计数
        for job_id, info in self._jobs.items():
            if info["module"] == module:
                info["error_count"] = 0
        logger.info(f"[Scheduler] 模块 {module} 任务已恢复")

    @stable
    async def shutdown(self) -> None:
        """关闭调度器。在应用关闭时调用。"""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("[Scheduler] APScheduler 已关闭")

    def _add_aps_job(self, job_id: str, func, trigger: str, kwargs: dict) -> None:
        """将任务实际添加到 APScheduler。调度器未启动时暂存。"""
        if self._scheduler is None:
            # 延迟注册：待 start() 后在 _flush_pending 中批量添加
            if not hasattr(self, "_pending_jobs"):
                self._pending_jobs: list = []
            self._pending_jobs.append((job_id, func, trigger, kwargs))
            return

        if trigger == "cron":
            self._scheduler.add_job(
                func, trigger="cron", id=job_id, replace_existing=True, **kwargs,
            )
        elif trigger == "interval":
            self._scheduler.add_job(
                func, trigger="interval", id=job_id, replace_existing=True, **kwargs,
            )
        elif trigger == "date":
            self._scheduler.add_job(
                func, trigger="date", id=job_id, replace_existing=True, **kwargs,
            )

    def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self._scheduler = AsyncIOScheduler(timezone=self._timezone)
        self._scheduler.start()

        # flush 延迟注册的任务
        for job_id, func, trigger, kwargs in getattr(self, "_pending_jobs", []):
            self._add_aps_job(job_id, func, trigger, kwargs)
        if hasattr(self, "_pending_jobs"):
            del self._pending_jobs

        logger.info("[Scheduler] APScheduler 已启动")
