"""审计发现的 LLM 自动修复：记忆类发现的修复 prompt 构造。

设计原则（与主控约定一致）：
- 只修复记忆类内容（skills / projects / infos），绝不触碰代码
- LLM 修复以独立 session 注入执行，结果回写到审计通知
"""

from __future__ import annotations

from src.core.self_audit.models import AuditFinding

# 记忆类 category（对应 memory/ 三层目录）
_MEMORY_CATEGORIES = frozenset({"skill", "project", "info"})


def is_memory_finding(finding: AuditFinding) -> bool:
    """判断一条审计发现是否属于记忆类（skill / project / info）。"""
    return finding.category in _MEMORY_CATEGORIES


def build_repair_prompt(findings: list[AuditFinding]) -> str:
    """把未修复的记忆类发现构造成 LLM 修复任务 prompt。

    规则：
    - 空列表 / 无记忆类未修复发现 → 返回空字符串（不注入修复）
    - 只纳入记忆类且未 fixed 的发现
    - prompt 内置安全铁律，禁止 LLM 借修复之名改代码 / 动 git / 删文件
    """
    pending = [
        f for f in findings
        if is_memory_finding(f) and not f.fixed
    ]
    if not pending:
        return ""

    lines = [
        "以下是自我审计发现的记忆库问题清单，请逐条修复记忆文件（memory/ 目录下的 skills / projects / infos）：",
        "",
        "修复铁律（必须严格遵守）：",
        "- 禁止修改任何代码文件（src/、tests/ 等一律不碰）",
        "- 禁止执行任何 git 操作（不 add / commit / push / checkout）",
        "- 禁止用 rm 删除文件（过时内容归档到 .archived/ 而不是删除）",
        "",
        "待修复清单：",
    ]
    for f in pending:
        lines.append(f"- [category={f.category}] target={f.target}：{f.message}")
        if f.suggestion:
            lines.append(f"  建议：{f.suggestion}")

    lines += [
        "",
        "输出要求：按「finding → 动作 → 结果」逐条汇报，没有把握的条目明确说明跳过原因。",
    ]
    return "\n".join(lines)
