"""自我审计模块：定时扫描 skills、projects、skill scripts，检查过时、错误、冗余。

审计触发：
    1. 定时：daemon 每 4 小时检查一次是否需要执行（每天按日历日期至少一次）
    2. 手动：用户输入 /self-audit 命令

审计维度：
    - Skills：frontmatter 缺失、内容过短、孤立文件
    - Projects：路径失效、信息过时、格式异常
    - Skill Scripts：语法错误、危险 import、TOOL_SCHEMA/TOOL_RUNNER 配置不完整
"""

from src.core.config import LAMIX_DIR, SKILLS_DIR, PROJECTS_DIR
from src.core.self_audit.lifecycle import (
    _LAST_ACTIVE_FILE,
    _get_last_active_date,
    cleanup_stale_knowledge,
    format_report_detail,
    run_audit,
    touch_last_active_date,
)
from src.core.self_audit.models import (
    AUDIT_LOG_DIR,
    AUDIT_LOG_PATH,
    AuditFinding,
    AuditReport,
    _audit_log,
)
from src.core.self_audit.scanners import (
    scan_projects,
    scan_skill_overlap,
    scan_skill_scripts,
    scan_skills,
    scan_user_patterns,
)
from src.core.self_audit.storage import (
    AUDIT_REPORTS_DIR,
    _deserialize_report,
    _serialize_report,
    list_reports,
    load_report,
    save_report,
)

__all__ = [
    "AUDIT_LOG_DIR",
    "AUDIT_LOG_PATH",
    "AUDIT_REPORTS_DIR",
    "AuditFinding",
    "AuditReport",
    "LAMIX_DIR",
    "PROJECTS_DIR",
    "SKILLS_DIR",
    "_LAST_ACTIVE_FILE",
    "_audit_log",
    "_deserialize_report",
    "_get_last_active_date",
    "_serialize_report",
    "cleanup_stale_knowledge",
    "format_report_detail",
    "list_reports",
    "load_report",
    "run_audit",
    "save_report",
    "scan_projects",
    "scan_skill_overlap",
    "scan_skill_scripts",
    "scan_skills",
    "scan_user_patterns",
    "touch_last_active_date",
]
