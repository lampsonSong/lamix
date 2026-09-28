"""APScheduler 封装：任务注册、生命周期管理、持久化。"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor

from src.core.task_scheduler.triggers import TaskConfig, TaskType

logger = logging.getLogger(__name__)


class TaskScheduler:
    """基于 APScheduler 的统一定时任务调度器。"""

    def __init__(self) -> None:
        executors = {
            "default": ThreadPoolExecutor(max_workers=4),
        }
        job_defaults = {
            "coalesce": True,       # 错过的合并为一次
            "max_instances": 1,     # 同一任务不并发
        }
        self._scheduler = BackgroundScheduler(
            executors=executors,
            job_defaults=job_defaults,
        )
        self._session = None  # agent session，prompt 任务需要

    def set_session(self, session) -> None:
        """设置 agent session 引用，供 prompt 模式任务使用。"""
        self._session = session

    def get_session(self):
        """返回当前 agent session 引用（未设置时为 None）。"""
        return self._session

    def start(self) -> None:
        """启动调度器。daemon 启动时调用。"""
        self._scheduler.start()
        logger.info("[task_scheduler] 调度器已启动")

    def shutdown(self, wait: bool = True) -> None:
        """停止调度器。daemon 退出时调用。"""
        self._scheduler.shutdown(wait=wait)
        logger.info("[task_scheduler] 调度器已停止")

    def schedule(self, config: TaskConfig) -> str:
        """注册任务，返回 task_id。如果已存在则替换。"""
        func = _wrap_callback(config, self._session)

        if config.task_type == TaskType.DELAYED:
            run_date = datetime.now() + timedelta(seconds=config.trigger_seconds)
            self._scheduler.add_job(
                func,
                trigger="date",
                run_date=run_date,
                id=config.task_id,
                name=config.description or config.task_id,
                replace_existing=True,
                kwargs=config.func_args,
            )
        elif config.task_type == TaskType.INTERVAL:
            self._scheduler.add_job(
                func,
                trigger="interval",
                seconds=config.interval_seconds,
                id=config.task_id,
                name=config.description or config.task_id,
                replace_existing=True,
                kwargs=config.func_args,
            )
        elif config.task_type == TaskType.CRON:
            cron_kwargs: dict[str, Any] = {}
            if config.cron_hour is not None:
                cron_kwargs["hour"] = config.cron_hour
            if config.cron_minute is not None:
                cron_kwargs["minute"] = config.cron_minute
            if config.cron_day_of_week is not None:
                cron_kwargs["day_of_week"] = config.cron_day_of_week

            self._scheduler.add_job(
                func,
                trigger="cron",
                id=config.task_id,
                name=config.description or config.task_id,
                replace_existing=True,
                kwargs=config.func_args,
                **cron_kwargs,
            )
        else:
            raise ValueError(f"不支持的任务类型: {config.task_type}")

        logger.info(
            f"[task_scheduler] 注册任务: {config.task_id} ({config.task_type.value})"
        )
        return config.task_id

    def cancel(self, task_id: str) -> bool:
        """取消任务，返回是否成功。"""
        try:
            self._scheduler.remove_job(task_id)
            logger.info(f"[task_scheduler] 取消任务: {task_id}")
            return True
        except Exception:
            return False

    def list_tasks(self) -> list[dict]:
        """列出所有任务（含下次触发时间）。"""
        jobs = self._scheduler.get_jobs()
        result = []
        for job in jobs:
            result.append({
                "task_id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
        return result

    def get_job(self, task_id: str):
        """获取单个 job 对象。"""
        return self._scheduler.get_job(task_id)


def _wrap_callback(config: TaskConfig, session) -> Callable:
    """包装任务函数，加入回调和错误处理。"""

    def wrapped(**kwargs):
        try:
            if config.prompt:
                # prompt 模式：注入 session 让 agent 执行
                if session is None:
                    logger.error(f"[task_scheduler] 任务 {config.task_id} 需要session但未设置")
                    return "[错误] session 未设置"
                logger.info(f"[task_scheduler] 触发 prompt 任务: {config.task_id}")
                result = session.handle_input(config.prompt)
            else:
                # func 模式：直接调用 Python 函数
                result = config.func(**kwargs)

            if config.on_done:
                try:
                    config.on_done(result)
                except Exception as e:
                    logger.warning(f"[task_scheduler] on_done 回调失败: {e}")
            return result
        except Exception as e:
            logger.error(
                f"[task_scheduler] 任务 {config.task_id} 执行失败: {e}\n"
                f"{traceback.format_exc()}"
            )
            if config.on_error:
                try:
                    config.on_error(e)
                except Exception as callback_err:
                    logger.warning(f"[task_scheduler] on_error 回调失败: {callback_err}")

    wrapped.__name__ = config.func.__name__ if config.func else config.task_id
    return wrapped
