"""测试 boot_tasks 执行前的通知功能。"""

from unittest.mock import patch
import sys

# setproctitle 可能在某些环境未安装，mock 掉再导入 daemon
# 必须做成完整的 mock，否则 pytest.importorskip 后测试内部访问属性会失败
_mock_setproctitle = __import__('types', fromlist=['']).ModuleType('setproctitle')
_mock_setproctitle.setproctitle = lambda *a, **k: None
_mock_setproctitle.getproctitle = lambda: ''
sys.modules['setproctitle'] = _mock_setproctitle
from src.daemon import _notify_boot_tasks_running


def test_single_task():
    """单条 boot_task 时生成正确的通知文本。"""
    tasks = [{"task": "验证 xxx 功能"}]

    with patch("src.daemon_boot._notify_user") as mock_notify:
        _notify_boot_tasks_running(config=None, tasks=tasks)
        mock_notify.assert_called_once_with(
            "⚡ 正在执行 1 条启动待办任务：\n1. 验证 xxx 功能",
        )


def test_multiple_tasks():
    """多条 boot_task 时逐条列出。"""
    tasks = [
        {"task": "验证 A"},
        {"task": "验证 B"},
        {"task": "验证 C"},
    ]

    with patch("src.daemon_boot._notify_user") as mock_notify:
        _notify_boot_tasks_running(config=None, tasks=tasks)
        expected = "⚡ 正在执行 3 条启动待办任务：\n1. 验证 A\n2. 验证 B\n3. 验证 C"
        mock_notify.assert_called_once_with(expected)


def test_long_task_description_truncated():
    """超长描述截断到 80 字符。"""
    long_desc = "A" * 100
    tasks = [{"task": long_desc}]

    with patch("src.daemon_boot._notify_user") as mock_notify:
        _notify_boot_tasks_running(config=None, tasks=tasks)
        sent_text = mock_notify.call_args[0][0]
        task_line = sent_text.split("\n")[1]
        assert len(task_line) <= 83  # "1. " + 77 + "..."
        assert task_line.endswith("...")


def test_missing_task_key_falls_back_to_str():
    """task 字典没有 task key 时用 str(t) 兜底。"""
    tasks = [{"desc": "something"}]

    with patch("src.daemon_boot._notify_user") as mock_notify:
        _notify_boot_tasks_running(config=None, tasks=tasks)
        sent_text = mock_notify.call_args[0][0]
        assert "1. {'desc': 'something'}" in sent_text
