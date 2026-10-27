"""重复检测与阈值修正的回归测试（2026-10-27 审计治理实战沉淀）。

覆盖：
- scan_duplicate_knowledge：文件名归一化重复 / 内容近似重复 / skill 同名 .md+目录并存
- scan_projects：任意数量『## 更新』小节即报（阈值 10 → 0）
- scan_infos：更新小节检测 + 13KB 体积预警
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.self_audit.dup_scan import scan_duplicate_knowledge
from src.core.self_audit.scanners import scan_infos, scan_projects


@pytest.fixture
def mem_dirs(tmp_path):
    projects = tmp_path / "projects"
    skills = tmp_path / "skills"
    info = tmp_path / "info"
    for d in (projects, skills, info):
        d.mkdir()
    return projects, skills, info


# ── scan_duplicate_knowledge ────────────────────────────────────────────────

class TestScanDuplicateKnowledge:
    def test_name_normalized_duplicate_detected(self, mem_dirs):
        """2026-10-27 新语义：跨层同名降为 info（治理规则：跨层同名≠重复）；同层仍 warning。"""
        projects, skills, info = mem_dirs
        # 跨层（project + info）：降为 info
        (projects / "git-pager.md").write_text("# git-pager\n\n非交互 shell pager 挂起\n", encoding="utf-8")
        (info / "git-pagers.md").write_text("# git-pagers\n\n非交互 shell pager 挂起\n", encoding="utf-8")
        # 同层（projects 内）：仍 warning
        (projects / "evalr.md").write_text("# evalr\n\n评测\n", encoding="utf-8")
        (projects / "evalrs.md").write_text("# evalrs\n\n评测\n", encoding="utf-8")
        findings = scan_duplicate_knowledge(projects, skills, info)
        assert any("跨层同名" in f.message and f.severity == "info" for f in findings)
        assert any("归一化后重复" in f.message and f.severity == "warning" for f in findings)

    def test_content_near_duplicate_detected(self, mem_dirs):
        projects, skills, info = mem_dirs
        body = ("飞书 API 调用踩坑记录：消息发送接口在权限不足时返回 400 错误码，"
                "需要在重试逻辑中区分业务错误与网络错误，token 过期时先刷新凭据再重试，"
                "否则会造成重复发送消息的问题，排查时优先检查 app 配置中的 scope 列表。") * 4
        (info / "feishu-api-pitfall.md").write_text(f"# a\n\n{body}", encoding="utf-8")
        (info / "feishu-api-pitfalls.md").write_text(f"# b\n\n{body}", encoding="utf-8")
        findings = scan_duplicate_knowledge(projects, skills, info)
        assert any("内容疑似重复" in f.message for f in findings)

    def test_skill_same_name_dir_and_file_detected(self, mem_dirs):
        projects, skills, info = mem_dirs
        (skills / "demo.md").write_text("---\nname: demo\ndescription: x\n---\n\n内容\n", encoding="utf-8")
        (skills / "demo").mkdir()
        (skills / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\n\n内容\n", encoding="utf-8")
        findings = scan_duplicate_knowledge(projects, skills, info)
        assert any("同名并存" in f.message for f in findings)

    def test_no_duplicate_no_finding(self, mem_dirs):
        projects, skills, info = mem_dirs
        (projects / "alpha.md").write_text("# alpha\n\n完全不同的话题内容甲乙丙丁戊己庚辛。\n", encoding="utf-8")
        (info / "beta.md").write_text("# beta\n\n毫不相干的东西：数据库连接池调优与索引设计。\n", encoding="utf-8")
        findings = scan_duplicate_knowledge(projects, skills, info)
        assert findings == []

    def test_archived_files_ignored(self, mem_dirs):
        projects, skills, info = mem_dirs
        arc = info / ".archived"
        arc.mkdir()
        (arc / "dup.md").write_text("# dup\n\n内容\n", encoding="utf-8")
        (info / "dup.md").write_text("# dup\n\n内容\n", encoding="utf-8")
        findings = scan_duplicate_knowledge(projects, skills, info)
        assert not any(".archived" in f.message for f in findings)


# ── scan_projects 阈值修正 ─────────────────────────────────────────────────

class TestProjectUpdateSectionThreshold:
    def test_single_update_section_reported(self, mem_dirs, monkeypatch):
        projects, _, _ = mem_dirs
        monkeypatch.setattr("src.core.self_audit.scanners.PROJECTS_DIR", projects)
        (projects / "p1.md").write_text(
            "# p1\n\n## 现状\n\n- 事实\n\n## 更新 2026-10-27\n\n- 新增一条\n", encoding="utf-8")
        findings = scan_projects(auto_fix=False)
        assert any("『## 更新』" in f.message for f in findings)

    def test_no_update_section_not_reported(self, mem_dirs, monkeypatch):
        projects, _, _ = mem_dirs
        monkeypatch.setattr("src.core.self_audit.scanners.PROJECTS_DIR", projects)
        (projects / "p2.md").write_text("# p2\n\n## 现状\n\n- 事实\n", encoding="utf-8")
        findings = scan_projects(auto_fix=False)
        assert not any(f.target == "p2" and "『## 更新』" in f.message for f in findings)


# ── scan_infos 新增检查 ────────────────────────────────────────────────────

class TestInfoUpdateSectionAndSize:
    def test_info_update_section_reported(self, tmp_path, monkeypatch):
        import src.core.self_audit as sa
        info = tmp_path / "info"
        info.mkdir()
        (info / "i1.md").write_text("# i1\n\n## 更新 2026-10-01\n\n- 内容\n", encoding="utf-8")
        old = sa.INFO_DIR
        sa.INFO_DIR = info
        try:
            findings = scan_infos(auto_fix=False)
        finally:
            sa.INFO_DIR = old
        assert any("『## 更新』" in f.message for f in findings)

    def test_info_oversize_reported(self, tmp_path, monkeypatch):
        import src.core.self_audit as sa
        info = tmp_path / "info"
        info.mkdir()
        (info / "big.md").write_text("# big\n\n" + ("内容填充。" * 3000), encoding="utf-8")
        old = sa.INFO_DIR
        sa.INFO_DIR = info
        try:
            findings = scan_infos(auto_fix=False)
        finally:
            sa.INFO_DIR = old
        assert any("13KB 预警线" in f.message for f in findings)
