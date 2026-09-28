"""任务调度器：基于 APScheduler 的统一定时任务管理。

支持三种触发模式：
- DELAYED: 一次性延迟任务
- INTERVAL: 固定间隔周期任务
- CRON: Cron 表达式定时任务

支持两种执行方式：
- prompt: 自然语言提示，触发时注入 agent session
- func: Python 函数引用（用于内置功能如自我审计）
"""

from __future__ import annotations

from src.core.task_scheduler.triggers import TaskType, TaskConfig
from src.core.task_scheduler.scheduler import TaskScheduler

# 全局单例
_scheduler: TaskScheduler | None = None


def get_scheduler() -> TaskScheduler:
    """获取全局调度器实例。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler


def set_session(session) -> None:
    """设置 agent session 引用。"""
    get_scheduler().set_session(session)


def get_session():
    """获取当前 agent session 引用（未设置时返回 None）。"""
    return get_scheduler().get_session()


def schedule(config: TaskConfig) -> str:
    """注册任务，返回 task_id。"""
    return get_scheduler().schedule(config)


def cancel(task_id: str) -> bool:
    """取消任务。"""
    return get_scheduler().cancel(task_id)


def list_tasks() -> list[dict]:
    """列出所有任务。"""
    return get_scheduler().list_tasks()


def start() -> None:
    """启动调度器。"""
    get_scheduler().start()


def shutdown() -> None:
    """停止调度器。"""
    get_scheduler().shutdown()
