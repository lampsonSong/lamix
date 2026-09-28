"""审计 LLM 修复闭环测试：记忆类发现分类、修复 prompt 构造、scan_infos、_do_self_audit 闭环。"""

import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import src.core.self_audit as sa
from src.core.self_audit import (
    AuditFinding,
    AuditReport,
    build_repair_prompt,
    is_memory_finding,
    scan_infos,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_info_dir(tmp_path):
    """用临时目录替代 INFO_DIR 用于测试。"""
    info_dir = tmp_path / "info"
    info_dir.mkdir()
    old_info = sa.INFO_DIR
    sa.INFO_DIR = info_dir
    yield info_dir
    sa.INFO_DIR = old_info


def _finding(category: str, target: str = "x", fixed: bool = False,
             message: str = "问题描述", suggestion: str = "建议") -> AuditFinding:
    return AuditFinding(
        severity="warning",
        category=category,
        target=target,
        message=message,
        suggestion=suggestion,
        fixed=fixed,
    )


# ── is_memory_finding ─────────────────────────────────────────────────────────

class TestIsMemoryFinding:
    def test_memory_categories(self):
        for cat in ("skill", "project", "info"):
            assert is_memory_finding(_finding(cat)) is True

    def test_code_categories(self):
        for cat in ("module", "script", "scripts"):
            assert is_memory_finding(_finding(cat)) is False


# ── build_repair_prompt ───────────────────────────────────────────────────────

class TestBuildRepairPrompt:
    def test_empty_findings_returns_empty(self):
        assert build_repair_prompt([]) == ""

    def test_all_fixed_returns_empty(self):
        findings = [_finding("skill", fixed=True)]
        assert build_repair_prompt(findings) == ""

    def test_code_findings_excluded(self):
        findings = [_finding("module", target="bad_script")]
        assert build_repair_prompt(findings) == ""

    def test_prompt_contains_rules_and_targets(self):
        findings = [
            _finding("skill", target="my-skill", message="正文过短"),
            _finding("project", target="proj-a", message="路径不存在"),
        ]
        prompt = build_repair_prompt(findings)
        # 铁律
        assert "禁止修改任何代码文件" in prompt
        assert "禁止执行任何 git 操作" in prompt
        assert "禁止用 rm 删除文件" in prompt
        # finding 内容
        assert "my-skill" in prompt
        assert "proj-a" in prompt
        assert "正文过短" in prompt
        # 输出要求
        assert "finding → 动作 → 结果" in prompt

    def test_mixed_findings_only_memory_pending(self):
        findings = [
            _finding("skill", target="good"),
            _finding("module", target="code"),
            _finding("info", target="note", fixed=True),
        ]
        prompt = build_repair_prompt(findings)
        assert "good" in prompt
        assert "code" not in prompt
        assert "note" not in prompt


# ── scan_infos ────────────────────────────────────────────────────────────────

class TestScanInfos:
    def test_no_dir_returns_empty(self, tmp_path):
        with patch.object(sa, "INFO_DIR", tmp_path / "nonexistent"):
            assert scan_infos() == []

    def test_normal_file_no_findings(self, temp_info_dir):
        (temp_info_dir / "good.md").write_text(
            "# good\n\n正文内容足够长，这里是正常的 info 文件。\n", encoding="utf-8")
        assert scan_infos() == []

    def test_missing_title_autofixed(self, temp_info_dir):
        (temp_info_dir / "no-title.md").write_text("直接正文，没有标题行。\n", encoding="utf-8")
        findings = scan_infos(auto_fix=True)
        title_fixed = [f for f in findings if "标题" in f.message]
        assert len(title_fixed) == 1 and title_fixed[0].fixed
        fixed_content = (temp_info_dir / "no-title.md").read_text(encoding="utf-8")
        assert fixed_content.startswith("# no-title")

    def test_missing_title_report_only(self, temp_info_dir):
        (temp_info_dir / "no-title.md").write_text("直接正文。\n", encoding="utf-8")
        findings = scan_infos(auto_fix=False)
        assert any("标题" in f.message and not f.fixed for f in findings)
        # 文件未被修改
        assert (temp_info_dir / "no-title.md").read_text(encoding="utf-8") == "直接正文。\n"

    def test_unclosed_code_fence_autofixed(self, temp_info_dir):
        (temp_info_dir / "fence.md").write_text(
            "# fence\n\n```python\nprint(1)\n", encoding="utf-8")
        findings = scan_infos(auto_fix=True)
        fence_fixed = [f for f in findings if "代码块" in f.message]
        assert len(fence_fixed) == 1 and fence_fixed[0].fixed
        assert (temp_info_dir / "fence.md").read_text(encoding="utf-8").endswith("```")

    def test_empty_file_reported(self, temp_info_dir):
        (temp_info_dir / "empty.md").write_text("", encoding="utf-8")
        findings = scan_infos()
        assert any("文件为空" in f.message for f in findings)

    def test_stale_local_path_reported(self, temp_info_dir):
        (temp_info_dir / "paths.md").write_text(
            "# paths\n\n路径：/Users/nobody/definitely/not/exists\n", encoding="utf-8")
        findings = scan_infos()
        assert any("/Users/nobody/definitely/not/exists" in f.message for f in findings)

    def test_remote_path_not_checked(self, temp_info_dir):
        (temp_info_dir / "remote.md").write_text(
            "# remote\n\n路径：/nas/syh/somewhere\n", encoding="utf-8")
        assert scan_infos() == []


# ── AuditReport infos_scanned 字段 ────────────────────────────────────────────

class TestReportInfosField:
    def test_serialize_roundtrip_keeps_infos_scanned(self):
        report = AuditReport(
            timestamp="2026-09-24 04:00",
            duration_seconds=1.0,
            skills_scanned=10,
            projects_scanned=5,
            scripts_scanned=2,
            infos_scanned=7,
            findings=[],
        )
        data = sa._serialize_report(report)
        assert data["infos_scanned"] == 7
        restored = sa._deserialize_report(data)
        assert restored.infos_scanned == 7

    def test_deserialize_legacy_report_without_infos(self):
        data = {
            "timestamp": "t", "duration_seconds": 0.0,
            "skills_scanned": 1, "projects_scanned": 1, "scripts_scanned": 1,
            "findings": [],
        }
        restored = sa._deserialize_report(data)
        assert restored.infos_scanned == 0

    def test_summary_text_contains_infos(self):
        report = AuditReport(
            timestamp="t", duration_seconds=0.0, infos_scanned=3, findings=[])
        assert "3 infos" in report.summary_text()


# ── _do_self_audit LLM 闭环（mock） ────────────────────────────────────────────

def _make_report(findings):
    return AuditReport(
        timestamp="2026-09-24 04:00",
        duration_seconds=0.1,
        findings=findings,
    )


class TestDoSelfAuditLlmLoop:
    def _run(self, session_mock, report_findings):
        import src.daemon as daemon
        notify_calls = []

        def fake_notify(text, config=None):
            notify_calls.append(text)

        with patch.object(daemon, "_notify_user", side_effect=fake_notify), \
             patch.object(daemon, "save_report", return_value=Path("/tmp/x.json")), \
             patch.object(daemon, "run_audit", return_value=_make_report(report_findings)), \
             patch.object(daemon, "LAMIX_DIR", Path("/tmp/_audit_test_lamix")), \
             patch("src.core.task_scheduler.get_session", return_value=session_mock), \
             patch("src.core.config.load_config", return_value={}):
            daemon._do_self_audit()
        return notify_calls

    def test_llm_repair_success_appended_to_report(self):
        session = MagicMock()
        session.handle_input.return_value = SimpleNamespace(
            reply="1. [skill] old → 归档到 .archived → 完成")
        calls = self._run(session, [_finding("skill", target="old")])
        assert len(calls) == 1
        assert "LLM 自动修复" in calls[0]
        assert "归档到 .archived" in calls[0]
        # prompt 注入了 session
        injected_prompt = session.handle_input.call_args[0][0]
        assert "old" in injected_prompt

    def test_llm_repair_failure_degrades_but_report_sent(self):
        session = MagicMock()
        session.handle_input.side_effect = RuntimeError("LLM down")
        calls = self._run(session, [_finding("project", target="p1")])
        assert len(calls) == 1
        assert "LLM 修复注入失败" in calls[0]

    def test_no_session_degrades(self):
        calls = self._run(None, [_finding("info", target="n1")])
        assert len(calls) == 1
        assert "session 未设置" in calls[0]

    def test_no_pending_findings_no_repair_section(self):
        session = MagicMock()
        calls = self._run(session, [_finding("module", target="code1")])
        assert len(calls) == 1
        assert "LLM 自动修复" not in calls[0]
        session.handle_input.assert_not_called()
