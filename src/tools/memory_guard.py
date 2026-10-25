"""projects 记忆写入守卫：写入入口拦截流水账式追加（2026-10-25 用户定稿）。

背景
----
projects 的定位是「当前状态快照」，不是事件日志。2026-10-23 用户定稿四条规则
（UPDATE 优先 / 固定骨架 / 写入前三问 / 20KB 红线），但规则只落在系统提示里，
属于软约束，执行时会漂移：10-25 axon-agent-deep 从 7.8KB 涨回 18.5KB，内联
日期标记 26 处；scanner 事后也只认『## 更新』字面标题与 20KB 红线，抓不到
「下班断点（…替代…版）」这类变体，于是审计静默。

本模块把规则前移到写入入口：
- 硬指标（体积红线 / 单次增长超限）→ 拒绝落盘，返回违规报告，由当轮会话就地
  修正重写（写入上下文还在手边，修正成本最低，这正是「写入时处理」优于事后
  scanner 的地方）；
- 形态指标（新增日期式标题 / 内联日期标记密度超标）→ 放行但附告警，避免误伤
  合法的一次性锚点（如「2026-10-23 拍板」）。

守卫只作用于 ~/.lamix/memory/projects/ 下的直接子文件；.archived/ 等归档目录、
info/ 与 skills/ 不受限（过程细节有地方可去，正是守卫能落地的前提）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 硬指标：单次写入增长上限、文件体积红线（与 self_audit 的 20KB 红线保持一致）
PROJECT_GROWTH_LIMIT = 3 * 1024
PROJECT_SIZE_LIMIT = 20 * 1024

# 形态指标：全文内联日期标记密度上限（10-25 事故文件实测 26 处）
PROJECT_DATE_MARK_LIMIT = 15

# 追加式小节标题（更新/断点/增量…）——用于找相对旧版新增的小节
_UPDATE_SECTION_RE = re.compile(
    r"^##\s*(?:更新|update|断点|breakpoint|增量|inc|append)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
# 标题里带日期（如「## 下班断点（2026-10-25 更新，替代 10-24 01:30 版）」）
_DATE_TITLE_RE = re.compile(r"^##\s+\S.*(?:\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}).*$", re.MULTILINE)
# 内联日期标记（MM-DD 或 YYYY-MM-DD）
_DATE_MARK_RE = re.compile(r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2})")

_ARCHIVE_DIR_PREFIX = "."


def is_project_target(path: Path) -> bool:
    """判断路径是否为受守卫管辖的 projects 记录文件。

    只管 memory/projects/ 下的直接 .md 子文件；.archived/ 等隐藏子目录放行
    （过程细节的归档去处），其他目录（info/skills/普通文件）不在管辖范围。
    """
    projects_dir = (Path.home() / ".lamix" / "memory" / "projects").resolve()
    try:
        rel = path.resolve().relative_to(projects_dir)
    except (ValueError, OSError):
        return False
    if len(rel.parts) != 1:
        return False
    name = rel.parts[0]
    return name.endswith(".md") and not name.startswith(_ARCHIVE_DIR_PREFIX)


def _new_sections(old: str, new: str) -> list[str]:
    """找出相对旧版新增的追加式小节标题行（整行去重）。"""
    old_titles = {m.group(0).strip() for m in _UPDATE_SECTION_RE.finditer(old)}
    return [
        m.group(0).strip()
        for m in _UPDATE_SECTION_RE.finditer(new)
        if m.group(0).strip() not in old_titles
    ]


def _date_titles(content: str) -> list[str]:
    """找出标题里带日期的小节行。"""
    return [m.group(0).strip() for m in _DATE_TITLE_RE.finditer(content)]


def _format_violation(target: Path, old_size: int, new_size: int, hits: list[str]) -> str:
    """组装可操作的违规报告（agent 看到后应就地修正重写）。"""
    growth = new_size - old_size
    lines = [
        "[拒绝] projects 记忆写入被守卫拦截：违反「状态快照非流水账」定位。",
        f"- 目标：{target}",
        f"- 体积：旧 {old_size / 1024:.1f}KB → 新 {new_size / 1024:.1f}KB（增量 {growth / 1024:+.1f}KB）",
        f"- 命中：{'；'.join(hits)}",
        "正确做法：",
        "  1) 原地改写对应槽位（现状快照 / 环境配置 / 架构事实 / 铁律 / 进行中），不要新开『## 更新』『## 断点』式小节；",
        "  2) 过程事件（谁在何时做了什么、监控流水、单次 run 细节）留在 session 历史或 .archived/，主文件只留状态化结论；",
        "  3) 确需大幅改写：先备份到 /tmp，再按固定骨架整篇重写（折叠后通常更小）；",
        "  4) 大段原理性细节下沉到 ~/.lamix/memory/info/，主文件留一行指针。",
    ]
    return "\n".join(lines)


def check_project_write(path: Path, content: str) -> str | None:
    """检查一次 projects 写入是否违反「状态快照」定位。

    Args:
        path: 最终落盘路径（已做过路径改写）。
        content: 待写入全文。

    Returns:
        None 表示放行；非空字符串表示违规报告，调用方应拒绝落盘并原样返回。
    """
    if not is_project_target(path):
        return None

    new_size = len(content.encode("utf-8"))
    old = ""
    try:
        if path.exists():
            old = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("[memory_guard] read old content failed: %s (%s)", path, e)

    old_size = len(old.encode("utf-8"))
    hits: list[str] = []

    if new_size > PROJECT_SIZE_LIMIT:
        hits.append(f"体积 {new_size / 1024:.1f}KB 超 20KB 红线")
    if old and (new_size - old_size) > PROJECT_GROWTH_LIMIT:
        hits.append(f"单次增长 {(new_size - old_size) / 1024:+.1f}KB 超 +3KB 上限")

    if hits:
        return _format_violation(path, old_size, new_size, hits)

    # 形态指标只告警不拦截
    warnings: list[str] = []
    new_titles = _new_sections(old, content)
    if new_titles:
        warnings.append(f"新增追加式小节 {len(new_titles)} 个：{' / '.join(new_titles[:3])}")
    date_titles = _date_titles(content)
    if date_titles:
        warnings.append(f"标题带日期的小节 {len(date_titles)} 个：{date_titles[0][:40]}")
    marks = len(_DATE_MARK_RE.findall(content))
    if marks > PROJECT_DATE_MARK_LIMIT:
        warnings.append(f"内联日期标记 {marks} 处（>{PROJECT_DATE_MARK_LIMIT}），疑似流水账")
    if warnings:
        logger.warning("[memory_guard] project write warned: %s (%s)", path, "；".join(warnings))
        return (
            "[已写入·带告警] "
            + "；".join(warnings)
            + "\n提示：projects 应保持状态快照，过程事件请放 session 历史或 .archived/。"
        )
    return None
