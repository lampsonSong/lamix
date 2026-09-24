"""审计主流程 + 报告格式化 + 基于使用频率的归档清理。"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from src.core.config import LAMIX_DIR, SKILLS_DIR, PROJECTS_DIR
from src.core.self_audit.models import AuditFinding, AuditReport, logger
from src.core.self_audit.scanners import (
    scan_skills,
    scan_skill_overlap,
    scan_projects,
    scan_skill_scripts,
    scan_user_patterns,
)


_LAST_ACTIVE_FILE = LAMIX_DIR / ".last_active_date"


def touch_last_active_date() -> None:
    """记录用户最后活跃日期（每次 handle_input 调用）。"""
    try:
        _LAST_ACTIVE_FILE.write_text(date.today().isoformat(), encoding="utf-8")
    except OSError:
        pass


def _get_last_active_date() -> date | None:
    """读取用户最后活跃日期。

    优先级：
    1. ~/.lamix/.last_active_date 文件（最可靠）
    2. heartbeat 中的 get_last_activity_time()（fallback）
    3. None（无记录）
    """
    try:
        text = _LAST_ACTIVE_FILE.read_text(encoding="utf-8").strip()
        if text:
            return date.fromisoformat(text)
    except (OSError, ValueError):
        pass

    try:
        from src.core.heartbeat import get_last_activity_time
        dt = get_last_activity_time()
        if dt is not None:
            return dt.date()
    except Exception:
        pass

    return None


def run_audit(auto_fix: bool = True) -> AuditReport:
    """执行完整审计，返回报告。

    auto_fix=True 时，对可安全修复的发现执行自动修复，修复结果写入 finding 的 fixed 字段。
    """
    import time
    start = time.time()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    skills_count = sum(
        1 for f in SKILLS_DIR.glob("**/*.md")
        if ".archived" not in f.parts and not f.name.startswith(".")
    ) if SKILLS_DIR.exists() else 0
    projects_count = len(list(PROJECTS_DIR.glob("*.md"))) if PROJECTS_DIR.exists() else 0
    scripts_count = len(list((SKILLS_DIR / "scripts").glob("*.py"))) if (SKILLS_DIR / "scripts").is_dir() else 0

    findings: list[AuditFinding] = []
    findings.extend(scan_skills(auto_fix=auto_fix))
    findings.extend(scan_skill_overlap())
    findings.extend(scan_projects(auto_fix=auto_fix))
    findings.extend(scan_skill_scripts(auto_fix=auto_fix))
    findings.extend(scan_user_patterns())
    findings.extend(cleanup_stale_knowledge(auto_fix=auto_fix))

    duration = time.time() - start

    report = AuditReport(
        timestamp=timestamp,
        duration_seconds=duration,
        skills_scanned=skills_count,
        projects_scanned=projects_count,
        scripts_scanned=scripts_count,
        findings=findings,
    )
    return report


def format_report_detail(report: AuditReport) -> str:
    """格式化报告详情，用于飞书消息。"""
    lines = [report.summary_text(), ""]

    by_severity = report.findings_by_severity

    for severity in ("error", "warning", "info"):
        items = by_severity[severity]
        if not items:
            continue

        icon = {"error": "[ERR]", "warning": "[WRN]", "info": "[INF]"}[severity]
        header = f"{icon} {severity.upper()} ({len(items)} 条)"

        by_category: dict[str, list[AuditFinding]] = {}
        for f in items:
            by_category.setdefault(f.category, []).append(f)

        lines.append(header)
        for cat, cat_findings in by_category.items():
            cat_icon = {"skill": "[S]", "project": "[P]", "module": "[M]"}.get(cat, "•")
            lines.append(f"  {cat_icon} {cat}: {len(cat_findings)} 条")
            for f in cat_findings:
                lines.append(f"    • [{f.target}] {f.message}")
                if f.suggestion:
                    lines.append(f"      → {f.suggestion}")
        lines.append("")

    fixed_items = [f for f in report.findings if f.fixed]
    if fixed_items:
        lines.append("  [AUTO-FIX] 已自动修复 ({} 条)".format(len(fixed_items)))
        for f in fixed_items:
            lines.append(f"  • [{f.target}] {f.fix_detail}")
        lines.append("")

    return "\n".join(lines)


def cleanup_stale_knowledge(auto_fix: bool = True) -> list[AuditFinding]:
    """根据使用频率归档长期未用的 skill/info/project。

    基准日期：基于用户最后活跃日期（而非 date.today()），
    避免用户一段时间没用后回来发现知识被归档。

    规则（三类统一）：
    - 7天内有调用的，留着
    - 7天内没调用，且总调用次数0次的，归档
    - 7天内没调用，但总调用次数>0的，暂时留着
    - 30天内没调用的，归档
    """
    from datetime import timedelta
    import shutil

    findings: list[AuditFinding] = []
    today = date.today()

    anchor_date = _get_last_active_date()
    if anchor_date is None:
        anchor_date = today
        logger.debug("[归档] 无活跃记录，使用 today=%s 作为基准", today)

    stale_7 = anchor_date - timedelta(days=7)
    stale_30 = anchor_date - timedelta(days=30)

    def _parse_date(s: str) -> date | None:
        try:
            return date.fromisoformat(str(s)[:10])
        except (ValueError, TypeError):
            return None

    # ── Skills 清理（平铺 .md 格式 + 子目录 SKILL.md 格式）─
    if SKILLS_DIR.exists():
        for skill_file in SKILLS_DIR.glob("**/*.md"):
            if ".archived" in skill_file.parts or skill_file.name.startswith("."):
                continue
            if skill_file.parent == SKILLS_DIR:
                skill_name_dir = skill_file.stem
            else:
                skill_name_dir = skill_file.parent.name
            try:
                raw = skill_file.read_text(encoding="utf-8")
            except OSError:
                continue
            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if not fm_match:
                continue
            try:
                import yaml
                meta = yaml.safe_load(fm_match.group(1)) or {}
            except Exception:
                continue

            name = meta.get("name", skill_file.parent.name if skill_file.parent != SKILLS_DIR else skill_file.stem)
            last_used = _parse_date(meta.get("last_used_at", ""))
            created = _parse_date(meta.get("created_at", ""))
            invocation_count = int(meta.get("invocation_count", 0))

            should_archive = False
            reason = ""
            anchor = last_used or created

            if anchor and anchor <= stale_30:
                should_archive = True
                reason = f"30天未使用（最后使用: {anchor}）"
            elif anchor and anchor <= stale_7 and invocation_count == 0:
                should_archive = True
                reason = f"7天未使用且从未被调用（创建于: {created}）"

            if should_archive and auto_fix:
                archive_dir = SKILLS_DIR / ".archived"
                archive_dir.mkdir(parents=True, exist_ok=True)
                dest = archive_dir / skill_file.name
                if dest.exists():
                    import uuid
                    dest = archive_dir / f"{skill_name_dir}_{uuid.uuid4().hex[:6]}.md"
                shutil.move(str(skill_file), str(dest))
                findings.append(AuditFinding(
                    severity="info",
                    category="skill",
                    target=name,
                    message=f"已归档: {reason}",
                    suggestion="如需恢复，从 .archived/ 目录移回",
                    fixed=True,
                    fix_detail=f"移至 {dest}",
                ))
            elif should_archive:
                findings.append(AuditFinding(
                    severity="warning",
                    category="skill",
                    target=name,
                    message=f"建议归档: {reason}",
                    suggestion="auto_fix=True 时自动归档",
                ))

    # ── Info 清理 ──
    if PROJECTS_DIR.exists():
        _info_dir = PROJECTS_DIR.parent / "info"
    else:
        _info_dir = LAMIX_DIR / "memory" / "info"

    if _info_dir.exists():
        for info_file in _info_dir.glob("*.md"):
            try:
                raw = info_file.read_text(encoding="utf-8")
            except OSError:
                continue
            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if not fm_match:
                continue
            try:
                import yaml
                meta = yaml.safe_load(fm_match.group(1)) or {}
            except Exception:
                continue

            name = meta.get("name", info_file.stem)
            last_used = _parse_date(meta.get("last_used_at", ""))
            created = _parse_date(meta.get("created_at", ""))

            should_archive = False
            reason = ""
            anchor = last_used or created
            invocation_count = int(meta.get("invocation_count", 0))

            if anchor and anchor <= stale_30:
                should_archive = True
                reason = f"30天未使用（最后使用: {anchor}）"
            elif anchor and anchor <= stale_7 and invocation_count == 0:
                should_archive = True
                reason = f"7天未使用且从未被调用（创建于: {created}）"

            if should_archive and auto_fix:
                archive_dir = _info_dir / ".archived"
                archive_dir.mkdir(parents=True, exist_ok=True)
                dest = archive_dir / info_file.name
                if dest.exists():
                    import uuid
                    dest = archive_dir / f"{info_file.stem}_{uuid.uuid4().hex[:6]}.md"
                shutil.move(str(info_file), str(dest))
                findings.append(AuditFinding(
                    severity="info",
                    category="info",
                    target=name,
                    message=f"已归档: {reason}",
                    fixed=True,
                    fix_detail=f"移至 {dest}",
                ))
            elif should_archive:
                findings.append(AuditFinding(
                    severity="warning",
                    category="info",
                    target=name,
                    message=f"建议归档: {reason}",
                ))

    # ── Projects 清理 ──
    if PROJECTS_DIR.exists():
        for proj_file in PROJECTS_DIR.glob("*.md"):
            try:
                raw = proj_file.read_text(encoding="utf-8")
            except OSError:
                continue
            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if not fm_match:
                continue
            try:
                import yaml
                meta = yaml.safe_load(fm_match.group(1)) or {}
            except Exception:
                continue

            name = meta.get("name", proj_file.stem)
            last_used = _parse_date(meta.get("last_used_at", ""))
            created = _parse_date(meta.get("created_at", ""))

            should_archive = False
            reason = ""
            anchor = last_used or created
            invocation_count = int(meta.get("invocation_count", 0))

            if anchor and anchor <= stale_30:
                should_archive = True
                reason = f"30天未使用（最后使用: {anchor}）"
            elif anchor and anchor <= stale_7 and invocation_count == 0:
                should_archive = True
                reason = f"7天未使用且从未被调用（创建于: {created}）"

            if should_archive and auto_fix:
                archive_dir = PROJECTS_DIR / ".archived"
                archive_dir.mkdir(parents=True, exist_ok=True)
                dest = archive_dir / proj_file.name
                if dest.exists():
                    import uuid
                    dest = archive_dir / f"{proj_file.stem}_{uuid.uuid4().hex[:6]}.md"
                shutil.move(str(proj_file), str(dest))
                findings.append(AuditFinding(
                    severity="info",
                    category="project",
                    target=name,
                    message=f"已归档: {reason}",
                    fixed=True,
                    fix_detail=f"移至 {dest}",
                ))
            elif should_archive:
                findings.append(AuditFinding(
                    severity="warning",
                    category="project",
                    target=name,
                    message=f"建议归档: {reason}",
                ))

    return findings
