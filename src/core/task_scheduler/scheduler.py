"""APScheduler 封装：任务注册、生命周期管理、持久化。"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor

from src.core.task_scheduler.triggers import (
    TaskConfig,
    TaskType,
    prompt_has_taskid_literal,
)

logger = logging.getLogger(__name__)


class TaskScheduler:
    """基于 APScheduler 的统一定时任务调度器。

    2026-10-11 增强：监控任务 max_fires 自停 + 注册期 prompt 字面量检查
    （cron-monitor-lifecycle skill 落地，60519 教训：弱模型跳过 task_cancel）
    """

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
        self._fire_counts: dict[str, int] = {}  # task_id → 已触发次数（max_fires 自停用）

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
        """注册任务，返回 task_id。如果已存在则替换。

        2026-10-11 新增：
        - max_fires > 0 时包装 callback 计数自停
        - prompt 模式任务检查 task_id 字面量（缺失则 warning，不阻塞）
        """
        # 注册期 prompt 检查（cron-monitor-lifecycle skill）
        if config.prompt and not prompt_has_taskid_literal(config.prompt, config.task_id):
            logger.warning(
                f"[task_scheduler] prompt 任务 {config.task_id} 缺少 task_id 字面量，"
                f"弱模型可能跳 task_cancel。建议把 task_id 字面量写进 prompt。"
                f"（cron-monitor-lifecycle skill 纪律）"
            )

        # max_fires 计数初始化（replace_existing 时清零旧计数）
        if config.max_fires > 0:
            self._fire_counts[config.task_id] = 0

        func = _wrap_callback(config, self._session, self)

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
            + (f" max_fires={config.max_fires}" if config.max_fires else "")
        )
        return config.task_id

    def _after_fire(self, task_id: str, max_fires: int) -> bool:
        """任务触发后回调（由 _wrap_callback 调用）：递增计数，到达上限返回 True 让 wrapper 自取消。

        返回 True 表示已达上限，调用方应取消任务。
        """
        cur = self._fire_counts.get(task_id, 0) + 1
        self._fire_counts[task_id] = cur
        if max_fires > 0 and cur >= max_fires:
            logger.info(
                f"[task_scheduler] 任务 {task_id} 已触发 {cur}/{max_fires} 次，自动取消"
            )
            return True
        return False

    def cancel(self, task_id: str) -> bool:
        """取消任务，返回是否成功。同时清理 max_fires 计数。"""
        try:
            self._scheduler.remove_job(task_id)
            self._fire_counts.pop(task_id, None)
            logger.info(f"[task_scheduler] 取消任务: {task_id}")
            return True
        except Exception:
            return False

    def list_tasks(self) -> list[dict]:
        """列出所有任务（含下次触发时间）。"""
        jobs = self._scheduler.get_jobs()
        result = []
        for job in jobs:
            entry: dict[str, Any] = {
                "task_id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            # max_fires 任务附带计数
            fired = self._fire_counts.get(job.id)
            if fired is not None:
                entry["fired"] = fired
            result.append(entry)
        return result

    def get_job(self, task_id: str):
        """获取单个 job 对象。"""
        return self._scheduler.get_job(task_id)


def _wrap_callback(config: TaskConfig, session, scheduler: TaskScheduler | None = None) -> Callable:
    """包装任务函数，加入回调和错误处理。

    2026-10-11 增强：当 config.max_fires > 0 时，每次执行后递增计数，
    到达上限后自动调 scheduler.cancel（cron-monitor-lifecycle skill）。
    """

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
            elif config.prompt and getattr(result, "reply", ""):
                # on_done 未设置时：自动把 prompt 任务结果推送给用户
                # 解决监控任务跑完无人告知的 bug（结果被 return 给 APScheduler 后丢弃）
                _default_notify(config, result)
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
        finally:
            # max_fires 自停检查（finally 里跑确保异常也算一次触发）
            if config.max_fires > 0 and scheduler is not None:
                if scheduler._after_fire(config.task_id, config.max_fires):
                    # 异步取消避免 APScheduler 内部状态冲突
                    import threading
                    threading.Timer(0.1, lambda: scheduler.cancel(config.task_id)).start()

    wrapped.__name__ = config.func.__name__ if config.func else config.task_id
    return wrapped


def _default_notify(config: TaskConfig, result: Any) -> None:
    """on_done 未设置时，自动把 prompt 任务的 LLM 回复推送给用户。

    复用 daemon_boot._notify_user 的 fallback 链：
    1. session.partial_sender（活动飞书 session 优先）
    2. 飞书直发（owner_chat_id / user_open_id）
    3. print 兜底

    推迟 import 避免启动顺序耦合。
    """
    reply = getattr(result, "reply", "")
    if not reply:
        return
    text = f"[定时任务 {config.task_id}]\n\n{reply}"
    try:
        from src.daemon_boot import _notify_user
        from src.core.config import load_config
        _notify_user(text, config=load_config())
        logger.info(f"[task_scheduler] 默认外发成功: {config.task_id}")
    except Exception as e:
        logger.warning(f"[task_scheduler] 默认外发失败 ({config.task_id}): {e}")