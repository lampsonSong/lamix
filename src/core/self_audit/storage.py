"""审计报告的持久化与加载。"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.config import LAMIX_DIR
from src.core.self_audit.models import AuditFinding, AuditReport


AUDIT_REPORTS_DIR = LAMIX_DIR / "audit_reports"


def _serialize_report(report: AuditReport) -> dict:
    """将 AuditReport 序列化为 dict（用于 JSON 持久化）。"""
    return {
        "timestamp": report.timestamp,
        "duration_seconds": report.duration_seconds,
        "skills_scanned": report.skills_scanned,
        "projects_scanned": report.projects_scanned,
        "scripts_scanned": report.scripts_scanned,
        "findings": [
            {
                "severity": f.severity,
                "category": f.category,
                "target": f.target,
                "message": f.message,
                "suggestion": f.suggestion,
                "fixed": f.fixed,
                "fix_detail": f.fix_detail,
            }
            for f in report.findings
        ],
    }


def _deserialize_report(data: dict) -> AuditReport:
    """从 dict 反序列化回 AuditReport。"""
    findings = [
        AuditFinding(
            severity=f["severity"],
            category=f["category"],
            target=f["target"],
            message=f["message"],
            suggestion=f.get("suggestion", ""),
            fixed=f.get("fixed", False),
            fix_detail=f.get("fix_detail", ""),
        )
        for f in data.get("findings", [])
    ]
    return AuditReport(
        timestamp=data["timestamp"],
        duration_seconds=data["duration_seconds"],
        skills_scanned=data.get("skills_scanned", 0),
        projects_scanned=data.get("projects_scanned", 0),
        scripts_scanned=data.get("scripts_scanned", 0),
        findings=findings,
    )


def save_report(report: AuditReport) -> Path:
    """保存审计报告到磁盘，返回文件路径。

    文件命名：{timestamp}.json，例如 2026-05-12T04-00.json
    """
    AUDIT_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_ts = report.timestamp.replace(":", "-").replace(" ", "T")
    filename = f"{safe_ts}.json"
    path = AUDIT_REPORTS_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_serialize_report(report), f, ensure_ascii=False, indent=2)
    return path


def list_reports(limit: int = 10) -> list[dict]:
    """列出最近的审计报告（按时间倒序）。

    返回每个报告的摘要信息：{path, timestamp, skills, projects, modules, findings_count}
    """
    if not AUDIT_REPORTS_DIR.exists():
        return []

    reports = []
    for path in sorted(AUDIT_REPORTS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            reports.append({
                "path": str(path),
                "timestamp": data.get("timestamp", ""),
                "skills_scanned": data.get("skills_scanned", 0),
                "projects_scanned": data.get("projects_scanned", 0),
                "scripts_scanned": data.get("scripts_scanned", 0),
                "findings_count": len(data.get("findings", [])),
            })
        except Exception:
            continue
    return reports


def load_report(path: str | Path) -> AuditReport | None:
    """从磁盘加载指定路径的审计报告。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return _deserialize_report(data)
    except Exception:
        return None
