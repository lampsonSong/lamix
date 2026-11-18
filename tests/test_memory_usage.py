"""记忆层「真实使用信号」sidecar 与归档判据测试（2026-11-18）。

背景：projects/info 的归档判据原用 frontmatter 的 last_used_at，而该字段会被
写入事件污染（审计补 frontmatter 填当天、治理战役整篇重写带当天日期），每轮
战役把计时器清零 → 30 天线从未触发（实测 326 个 project 无一超过 23 天）。

覆盖：
1. sidecar 读写（mark_read / get_read_at / seed_baseline / forget）
2. 读取路径记录信号（load_project_context / load_info）
3. 归档判据只看 sidecar 信号——治理重写把 frontmatter 改成今天也不豁免
4. 存量文件按历史基线 seed 一次，之后不被写入事件重置
5. 归档后条目仍可按名读取（.archived/ 回退）
"""

import json
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.core import memory_usage


# ══ 辅助 ═════════════════════════════════════════════════════════════════════


def _fm(meta: dict, body: str = "正文内容\n") -> str:
    """生成带 frontmatter 的 markdown。"""
    fm = yaml.dump(meta, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body}"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _set_mtime(path: Path, days: int) -> None:
    """把文件 mtime 设为 days 天前（模拟存量文件，而不是刚写出来的）。"""
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


def _meta_of(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    return yaml.safe_load(m.group(1)) if m else {}


# ══ 1. sidecar 基础读写 ══════════════════════════════════════════════════════


class TestUsageStore:
    def test_mark_and_get(self, tmp_path):
        memory_usage.mark_read(tmp_path, "projects", "foo")
        got = memory_usage.get_read_at(tmp_path, "projects", "foo")
        assert got == date.today()

    def test_get_missing_returns_none(self, tmp_path):
        assert memory_usage.get_read_at(tmp_path, "projects", "nope") is None
        assert memory_usage.get_read_at(tmp_path, "info", "nope") is None

    def test_corrupted_file_treated_as_empty(self, tmp_path):
        path = memory_usage.store_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 这不是 json", encoding="utf-8")
        assert memory_usage.get_read_at(tmp_path, "projects", "foo") is None
        # 坏文件不应阻断写入
        memory_usage.mark_read(tmp_path, "projects", "foo")
        assert memory_usage.get_read_at(tmp_path, "projects", "foo") == date.today()

    def test_seed_baseline_only_when_absent(self, tmp_path):
        assert memory_usage.seed_baseline(tmp_path, "projects", "foo", "2026-10-27") is True
        assert memory_usage.seed_baseline(tmp_path, "projects", "foo", "2026-11-18") is False
        assert memory_usage.get_read_at(tmp_path, "projects", "foo") == date(2026, 10, 27)

    def test_forget_removes_entry(self, tmp_path):
        memory_usage.mark_read(tmp_path, "info", "bar")
        memory_usage.forget(tmp_path, "info", "bar")
        assert memory_usage.get_read_at(tmp_path, "info", "bar") is None

    def test_store_layout(self, tmp_path):
        memory_usage.mark_read(tmp_path, "projects", "foo", "2026-11-18")
        path = memory_usage.store_path(tmp_path)
        assert path.name == "usage.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["projects"]["foo"] == "2026-11-18"


# ══ 2. 读取路径记录信号 ══════════════════════════════════════════════════════


class TestReadPathMarksUsage:
    def test_load_project_context_marks_read(self, tmp_path):
        from src.core import prompt_builder

        projects = tmp_path / "projects"
        _write(projects / "demo.md", _fm({"name": "demo", "description": "d"}, "# demo\n\n正文\n"))

        with patch.object(prompt_builder, "PROJECTS_DIR", projects):
            out = prompt_builder.load_project_context("demo")

        assert "正文" in out
        assert memory_usage.get_read_at(tmp_path, "projects", "demo") == date.today()

    def test_load_info_marks_read(self, tmp_path):
        from src.core import prompt_builder

        info = tmp_path / "info"
        _write(info / "demo.md", _fm({"name": "demo", "description": "d"}, "# demo\n\n正文\n"))

        with patch.object(prompt_builder, "INFO_DIR", info):
            out = prompt_builder.load_info("demo")

        assert "正文" in out
        assert memory_usage.get_read_at(tmp_path, "info", "demo") == date.today()

    def test_write_path_does_not_mark_read(self, tmp_path):
        """写入（治理重写）不得产生使用信号——这正是原 bug 的根因。"""
        projects = tmp_path / "projects"
        _write(projects / "demo.md", _fm({"name": "demo", "description": "d"}, "正文\n"))
        # 模拟整篇重写（file_write 不经过任何 mark_read）
        _write(projects / "demo.md", _fm(
            {"name": "demo", "description": "d", "created_at": "2026-10-01",
             "last_used_at": date.today().isoformat()},
            "重写后的正文\n",
        ))
        assert memory_usage.get_read_at(tmp_path, "projects", "demo") is None


# ══ 3. 归档判据只看 sidecar 信号 ════════════════════════════════════════════


class TestArchiveUsesReadSignal:
    def _run(self, tmp_path, anchor):
        from src.core.self_audit import cleanup_stale_knowledge
        # 必须建出 projects/：否则 lifecycle 会回退到真实 ~/.lamix/memory/info
        (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
        (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
        with patch("src.core.self_audit.lifecycle.SKILLS_DIR", tmp_path / "skills"), \
             patch("src.core.self_audit.lifecycle.PROJECTS_DIR", tmp_path / "projects"), \
             patch("src.core.self_audit.lifecycle._get_last_active_date", return_value=anchor):
            return cleanup_stale_knowledge(auto_fix=True)

    def test_read_signal_older_than_30d_archived(self, tmp_path):
        projects = tmp_path / "projects"
        _write(projects / "cold.md", _fm({
            "name": "cold", "created_at": "2026-01-01",
            "last_used_at": (date.today() - timedelta(days=60)).isoformat(),
        }))
        _set_mtime(projects / "cold.md", days=60)
        memory_usage.mark_read(tmp_path, "projects", "cold",
                               (date.today() - timedelta(days=40)).isoformat())

        self._run(tmp_path, date.today())
        assert not (projects / "cold.md").exists()
        assert (projects / ".archived" / "cold.md").exists()

    def test_recent_read_signal_kept(self, tmp_path):
        projects = tmp_path / "projects"
        _write(projects / "warm.md", _fm({
            "name": "warm", "created_at": "2026-01-01",
            "last_used_at": (date.today() - timedelta(days=60)).isoformat(),
        }))
        memory_usage.mark_read(tmp_path, "projects", "warm",
                               (date.today() - timedelta(days=5)).isoformat())

        self._run(tmp_path, date.today())
        assert (projects / "warm.md").exists()

    def test_rewrite_to_today_does_not_rescue(self, tmp_path):
        """核心回归：治理战役把 last_used_at 改成今天，也不能豁免归档。

        旧判据读 frontmatter → 每次战役重置计时器 → 永不归档（实测 326 个
        project 零归档）。新判据读 sidecar，写入事件碰不到它。
        """
        projects = tmp_path / "projects"
        _write(projects / "rewritten.md", _fm({
            "name": "rewritten", "created_at": "2026-01-01",
            # 治理战役重写时盖的「今天」——正是污染源
            "last_used_at": date.today().isoformat(),
        }))
        _set_mtime(projects / "rewritten.md", days=40)
        # sidecar 记录：40 天前读过，之后没再被读
        memory_usage.mark_read(tmp_path, "projects", "rewritten",
                               (date.today() - timedelta(days=40)).isoformat())

        self._run(tmp_path, date.today())
        assert not (projects / "rewritten.md").exists(), "写入事件重置了归档计时器"

    def test_info_rewrite_to_today_does_not_rescue(self, tmp_path):
        info = tmp_path / "info"
        _write(info / "rewritten.md", _fm({
            "name": "rewritten", "created_at": "2026-01-01",
            "last_used_at": date.today().isoformat(),
        }))
        _set_mtime(info / "rewritten.md", days=40)
        memory_usage.mark_read(tmp_path, "info", "rewritten",
                               (date.today() - timedelta(days=40)).isoformat())

        self._run(tmp_path, date.today())
        assert not (info / "rewritten.md").exists()


# ══ 4. 存量基线 seed ═════════════════════════════════════════════════════════


class TestLegacyBaselineSeeding:
    def _run(self, tmp_path, anchor):
        from src.core.self_audit import cleanup_stale_knowledge
        (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
        (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
        with patch("src.core.self_audit.lifecycle.SKILLS_DIR", tmp_path / "skills"), \
             patch("src.core.self_audit.lifecycle.PROJECTS_DIR", tmp_path / "projects"), \
             patch("src.core.self_audit.lifecycle._get_last_active_date", return_value=anchor):
            return cleanup_stale_knowledge(auto_fix=True)

    def test_legacy_file_seeded_from_history(self, tmp_path):
        """存量文件（无 sidecar 记录）首次审计时按历史基线入库，时间线不变。"""
        projects = tmp_path / "projects"
        _write(projects / "legacy.md", _fm({
            "name": "legacy", "created_at": "2026-01-01",
            "last_used_at": (date.today() - timedelta(days=20)).isoformat(),
        }))
        _set_mtime(projects / "legacy.md", days=20)

        self._run(tmp_path, date.today())

        seeded = memory_usage.get_read_at(tmp_path, "projects", "legacy")
        assert seeded == date.today() - timedelta(days=20)
        assert (projects / "legacy.md").exists(), "20 天未读不该被归档"

    def test_seed_not_reset_by_later_rewrite(self, tmp_path):
        """seed 之后治理重写（last_used_at 改成今天）不再影响基线。"""
        projects = tmp_path / "projects"
        _write(projects / "legacy.md", _fm({
            "name": "legacy", "created_at": "2026-01-01",
            "last_used_at": (date.today() - timedelta(days=40)).isoformat(),
        }))
        _set_mtime(projects / "legacy.md", days=40)
        self._run(tmp_path, date.today())
        assert not (projects / "legacy.md").exists(), "40 天未读应归档"

    def test_no_immediate_archive_for_fresh_files(self, tmp_path):
        """刚写完的文件不会立刻被归档（基线 = 当天）。"""
        projects = tmp_path / "projects"
        _write(projects / "fresh.md", _fm({
            "name": "fresh", "created_at": date.today().isoformat(),
            "last_used_at": date.today().isoformat(),
        }))
        self._run(tmp_path, date.today())
        assert (projects / "fresh.md").exists()


# ══ 5. 归档后仍可按名读取 ════════════════════════════════════════════════════


class TestArchivedStillReadable:
    def test_archived_project_loadable(self, tmp_path):
        from src.core import prompt_builder

        projects = tmp_path / "projects"
        _write(projects / ".archived" / "gone.md", "# gone\n\n归档时的快照\n")

        with patch.object(prompt_builder, "PROJECTS_DIR", projects):
            out = prompt_builder.load_project_context("gone")

        assert "归档时的快照" in out
        assert "已归档" in out

    def test_archived_info_loadable(self, tmp_path):
        from src.core import prompt_builder

        info = tmp_path / "info"
        _write(info / ".archived" / "gone.md", "# gone\n\n归档时的快照\n")

        with patch.object(prompt_builder, "INFO_DIR", info):
            out = prompt_builder.load_info("gone")

        assert "归档时的快照" in out
        assert "已归档" in out


# ══ 6. 审计补 frontmatter 不再盖「今天」 ═════════════════════════════════════


class TestAuditFillDoesNotStampToday:
    def test_filled_last_used_at_is_mtime_date(self, tmp_path):
        from src.core.self_audit.scanners import scan_memory_frontmatter

        projects = tmp_path / "projects"
        info = tmp_path / "info"
        target = projects / "nofm.md"
        _write(target, "# nofm\n\n正文\n")

        _set_mtime(target, days=10)

        with patch("src.core.self_audit.scanners.PROJECTS_DIR", projects), \
             patch("src.core.self_audit.INFO_DIR", info):
            scan_memory_frontmatter(auto_fix=True)

        meta = _meta_of(target)
        assert meta["last_used_at"] != date.today().isoformat(), \
            "补齐时盖今天会重置归档计时器"
        assert date.fromisoformat(meta["created_at"]) == date.today() - timedelta(days=10)