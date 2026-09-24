"""审计数据模型与日志工具。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from src.core.config import LAMIX_DIR

logger = logging.getLogger(__name__)


AUDIT_LOG_DIR = LAMIX_DIR / "logs"
AUDIT_LOG_PATH = AUDIT_LOG_DIR / "self_audit.log"


def _audit_log(msg: str) -> None:
    """写入审计专用日志文件（同时输出到 stdout）。"""
    logger.info(msg)
    try:
        AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


@dataclass
class AuditFinding:
    """一条审计发现。"""
    severity: str          # info / warning / error
    category: str          # skill / project / module
    target: str            # 文件/目录名
    message: str           # 发现描述
    suggestion: str = ""   # 修复建议
    fixed: bool = False    # 是否已自动修复
    fix_detail: str = ""   # 修复了什么


@dataclass
class AuditReport:
    """一份完整的审计报告。"""
    timestamp: str
    duration_seconds: float
    skills_scanned: int = 0
    projects_scanned: int = 0
    scripts_scanned: int = 0
    findings: list[AuditFinding] = field(default_factory=list)

    @property
    def findings_by_severity(self) -> dict[str, list[AuditFinding]]:
        result: dict[str, list[AuditFinding]] = {"error": [], "warning": [], "info": []}
        for f in self.findings:
            result[f.severity].append(f)
        return result

    def summary_text(self) -> str:
        total = len(self.findings)
        errors = len(self.findings_by_severity["error"])
        warnings = len(self.findings_by_severity["warning"])
        lines = [
            f"审计时间：{self.timestamp}",
            f"扫描范围：{self.skills_scanned} skills / {self.projects_scanned} projects / {self.scripts_scanned} scripts",
            f"发现问题：{total} 条（error={errors}, warning={warnings}, info={total - errors - warnings}）",
        ]
        if total == 0:
            lines.append("OK - 没有发现问题，知识库状态良好。")
        return "\n".join(lines)
