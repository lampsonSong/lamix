"""触发器类型定义：任务配置数据结构。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class TaskType(Enum):
    """任务触发类型。"""
    DELAYED = "delayed"      # 一次性延迟
    INTERVAL = "interval"    # 固定间隔
    CRON = "cron"            # Cron 表达式


@dataclass
class TaskConfig:
    """任务配置。

    支持两种执行模式（二选一）：
    - prompt: 自然语言提示，触发时注入 agent session，由 LLM 用工具执行
    - func: Python 函数引用（如内置的自我审计）

    监控任务自检字段（cron-monitor-lifecycle skill，2026-10-11 新增）：
    - max_fires: 触发次数上限，到达后自动取消（0 = 无限制）。用于防 prompt 监控任务空转刷屏。
    """
    task_id: str                              # 全局唯一 ID
    task_type: TaskType                       # 触发类型

    # 执行方式（二选一）
    func: Callable[..., Any] | None = None    # Python 函数
    prompt: str = ""                          # 自然语言 prompt（触发时注入 session）

    func_args: dict[str, Any] = field(default_factory=dict)
    description: str = ""                     # 任务描述（日志/通知用）

    # DELAYED 参数
    trigger_seconds: int = 0

    # INTERVAL 参数
    interval_seconds: int = 0

    # CRON 参数
    cron_hour: int | None = None
    cron_minute: int | None = None
    cron_day_of_week: str | None = None       # e.g. "mon-fri"

    # 回调
    on_done: Callable[[Any], None] | None = None
    on_error: Callable[[Exception], None] | None = None

    # ── 监控任务自检（cron-monitor-lifecycle skill，2026-10-11 新增）──
    max_fires: int = 0                        # 0=无限制；>0 时触发 N 次后自动取消（防空转）

    def __post_init__(self):
        if not self.func and not self.prompt:
            raise ValueError("TaskConfig 必须指定 func 或 prompt（二选一）")
        if self.max_fires < 0:
            raise ValueError(f"max_fires 必须 >= 0，got {self.max_fires}")


#: 宽松检查：prompt 里同时出现 "task_id" 关键词 和 task_id 字符串本身即视为含字面量
#: 比严格正则（要求紧邻引号）更耐中文 / 模板插值，避免弱模型场景漏报
_TASK_ID_KEYWORD_RE = re.compile(r"task[_\-]?id", re.IGNORECASE)


def prompt_has_taskid_literal(prompt: str, task_id: str) -> bool:
    """检查 prompt 里是否同时含 task_id 关键词 + task_id 字面量字符串。

    用于 scheduler 注册期自检：cron-monitor-lifecycle skill 推荐
    监控任务 prompt 显式包含自己 task_id（任意位置、任意形式），
    便于弱模型也能正确调 task_cancel 自己。
    """
    if not prompt or not task_id:
        return False
    return bool(_TASK_ID_KEYWORD_RE.search(prompt)) and task_id in prompt