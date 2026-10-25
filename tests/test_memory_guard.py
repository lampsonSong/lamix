"""memory_guard 单元测试：projects 写入守卫（2026-10-25 用户定稿）。

覆盖：管辖范围判定、硬指标拒绝（体积红线 / 单次增长超限）、形态指标告警、
以及 file_write 的端到端接入（拒绝时不得落盘、force 可显式豁免）。
"""

from pathlib import Path

import pytest

from src.tools import fileops as fileops_tool
from src.tools import memory_guard


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    """把 HOME 指向 tmp_path，构造受管辖的 ~/.lamix/memory/projects 目录。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".lamix" / "memory" / "projects"
    d.mkdir(parents=True)
    return d


def _write_old(path: Path, size_bytes: int) -> None:
    """造一个指定体积的旧文件。"""
    path.write_text("x" * size_bytes, encoding="utf-8")


class TestIsProjectTarget:
    """管辖范围判定。"""

    def test_direct_md_child_is_guarded(self, projects_dir):
        assert memory_guard.is_project_target(projects_dir / "demo.md") is True

    def test_archived_subdir_is_exempt(self, projects_dir):
        archived = projects_dir / ".archived" / "demo-updates.md"
        archived.parent.mkdir(parents=True)
        assert memory_guard.is_project_target(archived) is False

    def test_non_md_file_is_exempt(self, projects_dir):
        assert memory_guard.is_project_target(projects_dir / "demo.txt") is False

    def test_other_memory_dir_is_exempt(self, projects_dir):
        info = projects_dir.parent / "info"
        info.mkdir(parents=True)
        assert memory_guard.is_project_target(info / "demo.md") is False

    def test_unrelated_path_is_exempt(self, tmp_path):
        assert memory_guard.is_project_target(tmp_path / "elsewhere" / "x.md") is False


class TestCheckProjectWrite:
    """守卫判定逻辑。"""

    def test_small_update_passes(self, projects_dir):
        target = projects_dir / "demo.md"
        _write_old(target, 500)
        result = memory_guard.check_project_write(target, "y" * 700)
        assert result is None

    def test_growth_over_limit_is_rejected(self, projects_dir):
        target = projects_dir / "demo.md"
        _write_old(target, 1000)
        result = memory_guard.check_project_write(target, "y" * (1000 + 4 * 1024))
        assert result is not None
        assert "[拒绝]" in result
        assert "单次增长" in result
        assert "正确做法" in result

    def test_size_over_redline_is_rejected(self, projects_dir):
        target = projects_dir / "demo.md"
        result = memory_guard.check_project_write(target, "y" * (21 * 1024))
        assert result is not None
        assert "[拒绝]" in result
        assert "20KB" in result

    def test_new_section_triggers_warning_only(self, projects_dir):
        """新增『## 更新』式小节只告警不拦截。"""
        target = projects_dir / "demo.md"
        _write_old(target, 200)
        content = "头部\n\n## 更新 2026-10-25\n\n今天做了 ABC。\n"
        result = memory_guard.check_project_write(target, content)
        assert result is not None
        assert "[已写入·带告警]" in result
        assert "追加式小节" in result

    def test_date_title_triggers_warning(self, projects_dir):
        target = projects_dir / "demo.md"
        _write_old(target, 200)
        content = "## 下班断点（2026-10-25 更新）\n\n- 做了 ABC\n"
        result = memory_guard.check_project_write(target, content)
        assert result is not None
        assert "[已写入·带告警]" in result

    def test_date_mark_density_triggers_warning(self, projects_dir):
        target = projects_dir / "demo.md"
        _write_old(target, 200)
        body = "\n".join(f"- 10-25 第 {i} 条流水" for i in range(20))
        result = memory_guard.check_project_write(target, body)
        assert result is not None
        assert "内联日期标记" in result

    def test_non_project_path_always_passes(self, tmp_path):
        target = tmp_path / "big.md"
        result = memory_guard.check_project_write(target, "y" * (30 * 1024))
        assert result is None


class TestFileWriteIntegration:
    """file_write 端到端接入。"""

    def test_rejected_write_does_not_touch_disk(self, projects_dir):
        target = projects_dir / "demo.md"
        _write_old(target, 1000)
        before = target.read_text(encoding="utf-8")
        result = fileops_tool.file_write(str(target), "z" * (1000 + 5 * 1024))
        assert "[拒绝]" in result
        assert target.read_text(encoding="utf-8") == before

    def test_force_bypasses_guard(self, projects_dir):
        target = projects_dir / "demo.md"
        _write_old(target, 1000)
        big = "z" * (1000 + 5 * 1024)
        result = fileops_tool.file_write(str(target), big, force=True)
        assert "[成功]" in result
        assert target.stat().st_size == len(big.encode("utf-8"))

    def test_normal_project_update_succeeds(self, projects_dir):
        target = projects_dir / "demo.md"
        _write_old(target, 1000)
        result = fileops_tool.file_write(str(target), "更新后的状态快照正文")
        assert "[成功]" in result
        assert target.read_text(encoding="utf-8") == "更新后的状态快照正文"

    def test_run_file_write_passes_force_param(self, projects_dir):
        target = projects_dir / "demo.md"
        _write_old(target, 1000)
        big = "z" * (1000 + 5 * 1024)
        result = fileops_tool.run_file_write(
            {"path": str(target), "content": big, "force": True}
        )
        assert "[成功]" in result

    def test_write_to_non_project_file_unaffected(self, tmp_path):
        target = tmp_path / "note.md"
        result = fileops_tool.file_write(str(target), "z" * (30 * 1024))
        assert "[成功]" in result
