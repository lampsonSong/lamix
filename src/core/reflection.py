"""任务完成后的反思与知识沉淀模块。

每次任务完成后，自动判断是否有值得持久化的知识：
- 项目事实 → projects/<名>.md（新建或更新）
- 新方法论 → skills/<名>.md（新建或更新）
- 无价值 → 跳过
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from src.planning.steps import Plan, StepStatus

logger = logging.getLogger(__name__)

LAMIX_DIR = Path.home() / ".lamix"
SKILLS_DIR = LAMIX_DIR / "memory" / "skills"
PROJECTS_DIR = LAMIX_DIR / "memory" / "projects"
INFO_DIR = LAMIX_DIR / "memory" / "info"

# 反思冷却时间（秒）：距上次反思不足此间隔则跳过
_REFLECT_COOLDOWN = 300  # 5 分钟
_last_reflect_time: float = 0.0

# Skill 内容最短长度（字符），低于此不创建
_MIN_SKILL_CONTENT_LEN = 80

# 全局 LLM Client（由 Session 初始化时注入）
_llm_client: Any = None

# 全局 SkillIndex（由 Session 初始化时注入）
_skill_index: Any = None

# 全局 Fallback 模型列表（由 Session 初始化时注入）
# 类型: list[tuple[LLMClient, BaseModelAdapter]]
_fallback_llms: list[Any] = []


def set_llm_client(client: Any) -> None:
    """由 Session 初始化时调用，注入当前 LLM Client。"""
    global _llm_client
    _llm_client = client


def set_fallback_llms(fallback_llms: list[Any]) -> None:
    """由 Session 初始化时调用，注入 fallback 模型列表。"""
    global _fallback_llms
    _fallback_llms = fallback_llms or []


def set_skill_index(index: Any) -> None:
    """由 Session 初始化时调用，注入当前 SkillIndex，skill 变更后自动刷新索引。"""
    global _skill_index
    _skill_index = index


def _refresh_all_indices() -> None:
    """skill/info/project 变更后重建所有索引 + 刷新 tools 注册。静默失败。"""
    global _skill_index
    try:
        # Skill 索引
        if _skill_index is not None:
            _skill_index.load_or_build()
            logger.info('[反思] Skill 索引已重建')
        else:
            logger.debug('[反思] Skill 索引未注入，跳过')

        # Project 索引
        from src.tools import session as session_tool
        current_session = session_tool.get_current_session()
        if current_session and current_session.project_index is not None:
            current_session.project_index.load_or_build()
            if current_session.agent:
                current_session.agent.project_index = current_session.project_index
            logger.info('[反思] Project 索引已重建')

        # Info 缓存：清掉 prompt_builder 的 _info_index_cache，下次构建时自动重建
        from src.core import prompt_builder
        prompt_builder._info_index_cache = None
        logger.info('[反思] Info 缓存已清除')

        # 统一刷新 skills_tools 的检索索引
        if current_session:
            from src.core import skills_tools as skills_tools_reg
            skills_tools_reg.set_retrieval_indices(
                _skill_index,
                current_session.project_index,
            )
            current_session.skill_index = _skill_index
            if current_session.agent:
                current_session.agent.skill_index = _skill_index

    except Exception as e:
        logger.warning('[反思] 索引重建失败: %s', e)


# ── 工具注册 ────────────────────────────────────────────────────────────────

REFLECT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "reflect_and_learn",
        "description": (
            "任务完成后反思沉淀知识。分析本轮对话内容，判断是否有值得持久化的知识"
            "（skill、info、project），并自动执行沉淀。"
            "适用场景：(1) 任务完成后 (2) 激活了某个技能并发现可改进之处 "
            "(3) 解决了复杂问题，过程中产生了可复用的方法 "
            "(4) 探索了新项目，发现了项目信息"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "本轮任务的简要描述（例如：'帮用户调试API接口'、'优化数据库查询性能'）",
                },
                "execution_summary": {
                    "type": "string",
                    "description": "执行过程摘要（包括主要步骤、遇到的问题、解决方案等）",
                },
                "skill_activated": {
                    "type": "string",
                    "description": "本轮激活的技能名（如果有）",
                },
            },
            "required": ["goal", "execution_summary"],
        },
    },
}


def tool_reflect_runner(params: dict[str, Any]) -> str:
    """反思工具的执行函数，供 tools.py 调用。

    Args:
        params: 包含 goal, execution_summary, skill_activated 的参数字典

    Returns:
        沉淀结果摘要
    """
    global _llm_client

    if _llm_client is None:
        return "[提示] 反思功能未初始化（LLM Client 未注入），跳过沉淀"

    goal = params.get("goal", "")
    execution_summary = params.get("execution_summary", "")
    skill_activated = params.get("skill_activated")

    if not goal or not execution_summary:
        return "[提示] 反思参数不完整（goal 和 execution_summary 为必填），跳过沉淀"

    try:
        # 获取当前项目上下文（如果有）
        active_project = ""
        try:
            from src.tools import session as session_tool
            current_session = session_tool.get_current_session()
            if current_session and hasattr(current_session, 'active_project'):
                active_project = current_session.active_project or ""
        except Exception:
            pass

        # 调用反思逻辑
        learnings = reflect_and_learn(
            goal=goal,
            execution_summary=execution_summary,
            llm_client=_llm_client,
            skill_activated=skill_activated,
            recent_context="",
            active_project=active_project,
        )

        if not learnings:
            return "✓ 反思完成：本轮任务无需沉淀新知识"

        # 执行沉淀
        hints = execute_learnings(learnings)

        if hints:
            summary = "\n".join(f"  - {h}" for h in hints)
            return f"✓ 反思沉淀完成：\n{summary}"
        else:
            return "✓ 反思完成：沉淀操作已跳过（可能因为内容重复或不符合要求）"

    except Exception as e:
        logger.exception("反思工具执行异常")
        return f"[错误] 反思沉淀失败: {e}"


# ── 反思 Prompt ──────────────────────────────────────────────────────────────

REFLECT_PROMPT = """你是一个知识管理助手。检查执行过程是否产生了新的、未记录的知识。

## 用户目标
{goal}

## 执行过程
{execution_summary}

## 已有 Skills
{existing_skills}

## 已有 Projects
{existing_projects}

## 已有 Info
{existing_info}

请依次检查以下三个问题，只要任意一个回答"是"就产生一条 learning：

1. **Skill 类型**：是否发现了新的、未记录的 5+ 步可复用工作流？→ skill_create
2. **Project 类型**：是否在项目中发现了新的、未记录的信息（配置、测试结论、容器地址等）？→ project_update
3. **Info 类型**：是否发现了新的、未记录的通用知识（服务地址、API 用法、踩坑记录）？→ info_create

判断标准：
- skill_create: 5+ 步骤的工作流，skills 里没有的
- project_update: 项目相关的新发现，直接追加到 project 文件
- info_create: 通用知识的新发现，info 里没有的

请只输出 JSON：
{{"learnings": []}}
或
{{"learnings": [{{"type": "project_update", "target": "项目名", "reason": "发现了新配置", "content": "具体内容"}}]}}

注意：
- 只要任意一个问题回答"是"就产生一条 learning
- project_update 的 content 是增量信息，直接追加到对应项目的 md 文件
- skill 的 content 是方法论（通用步骤），不是具体答案"""


# ── 是否需要反思（LLM 判断）─────────────────────────────────────────────────



# ── 公开接口 ─────────────────────────────────────────────────────────────────


def should_reflect(
    plan: Plan | None = None,
    *,
    is_fast_path: bool = False,
    tool_call_count: int = 0,
    skill_activated: str | None = None,
    user_input: str = "",
    llm_client: Any | None = None,
    recent_context: str = "",
) -> bool:
    """判断本次任务是否需要反思。

    触发条件：tool_call_count >= 3（任意 3 次以上工具调用）
    冷却控制：距上次反思不足 5 分钟则跳过。
    """
    import time
    global _last_reflect_time

    now = time.time()
    elapsed = now - _last_reflect_time

    # 冷却期内 → 跳过
    if elapsed < _REFLECT_COOLDOWN:
        logger.info(f"[反思] 跳过：冷却中（距上次 {elapsed:.0f}s < {_REFLECT_COOLDOWN}s）")
        return False

    # 工具调用 < 3 次 → 跳过
    if tool_call_count < 3:
        logger.info(f"[反思] 跳过：tool_call_count={tool_call_count} < 3")
        return False

    # 触发反思（冷却时间由 mark_reflection_done 在反思完成后更新）
    logger.info(f"[反思] 触发：tool_call_count={tool_call_count} >= 3")
    return True




def mark_reflection_done() -> None:
    """反思完成后更新冷却时间。由 agent.py 在反思线程结束时调用。"""
    global _last_reflect_time
    import time
    _last_reflect_time = time.time()
    logger.info(f"[反思] 冷却时间已更新（距上次 {time.time() - _last_reflect_time:.0f}s）")



def reflect_and_learn(
    goal: str,
    execution_summary: str,
    llm_client: Any,
    skill_activated: str | None = None,
    recent_context: str = "",
    active_project: str = "",
) -> list[dict[str, Any]]:
    """执行反思，返回 learnings 列表。调用方负责后续的沉淀执行。"""
    existing_skills = _get_existing_skills_summary()
    existing_projects = _get_existing_projects_summary()
    existing_info = _get_existing_info_summary()

    # 构建反思上下文
    extra_context = ""
    # 如果有 skill 被激活，补充 skill 全文
    if skill_activated:
        skill_summary = _get_skill_full_content(skill_activated)
        if skill_summary:
            extra_context += "\n## 本轮激活的技能 [{}]\n{}".format(skill_activated, skill_summary)
    # 补充最近对话上下文（让 LLM 看到用户反馈）
    if recent_context and recent_context != "（无对话记录）":
        extra_context += "\n## 最近对话\n{}".format(recent_context)

    # 注入当前操作的项目（防止沉淀到错误的项目）
    if active_project:
        extra_context += "\n## 当前操作的项目\n{}（内容应沉淀到该项目的 project 文件，不要串项目）".format(active_project)

    prompt = REFLECT_PROMPT.format(
        goal=goal,
        execution_summary=execution_summary,
        existing_skills=existing_skills,
        existing_projects=existing_projects,
        existing_info=existing_info,
    ) + extra_context

    def _call_reflect_llm(target_llm: Any) -> list[dict[str, Any]]:
        """调用单个 LLM 执行反思，返回 learnings 列表。"""
        import re

        resp = target_llm.client.chat.completions.create(
            model=target_llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2048,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # 移除 MiniMax 等模型的 <think>...</think> thinking 标签，防止污染 JSON
        raw = re.sub(r'<\|thinking\|>[\s\S]*?\x<\|think\|\x>', '', raw)
        raw = re.sub(r'《[\s\S]*?》', '', raw, flags=re.DOTALL)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        return result.get("learnings", [])

    # 主模型
    try:
        return _call_reflect_llm(llm_client)
    except Exception as e:
        logger.warning(f"反思 LLM {llm_client.model} 调用失败: {e}")

    # Fallback 降级
    global _fallback_llms
    all_failed = True
    for fb_llm, _ in _fallback_llms:
        try:
            logger.info(f"反思 fallback 使用: {fb_llm.model}")
            learnings = _call_reflect_llm(fb_llm)
            all_failed = False
            if learnings:
                return learnings
        except Exception as e2:
            logger.warning(f"反思 fallback {fb_llm.model} 失败: {e2}")

    if all_failed:
        # 所有模型都失败了，只记录日志，不阻塞主流程
        logger.warning("反思失败：主模型和所有 fallback 均调用失败")
        return []


def execute_learnings(learnings: list[dict[str, Any]]) -> list[str]:
    """执行沉淀操作，返回人类可读的提示列表。"""
    hints: list[str] = []

    for learning in learnings:
        ltype = learning.get("type", "")
        target = learning.get("target", "")
        content = learning.get("content", "")
        reason = learning.get("reason", "")

        if ltype == "project_create":
            hint = _create_project(target, content, reason)
            if hint:
                hints.append(hint)
                _refresh_all_indices()

        elif ltype == "project_update":
            hint = _update_project(target, content, reason)
            if hint:
                hints.append(hint)
                _refresh_all_indices()

        elif ltype == "skill_create":
            hint = _create_skill(target, content, reason)
            if hint:
                hints.append(hint)
                _notify_user(f"📝 **Skill 新建**\n\n- 名称：{target}\n- 原因：{reason}")
                _refresh_all_indices()

        elif ltype == "skill_update":
            hint = _update_skill(target, content, reason)
            if hint:
                hints.append(hint)
                _notify_user(f"🔧 **Skill 更新**\n\n- 名称：{target}\n- 原因：{reason}")
                _refresh_all_indices()

        elif ltype == "info_create":
            hint = _create_info(target, content, reason)
            if hint:
                hints.append(hint)
                _refresh_all_indices()

        elif ltype == "info_update":
            hint = _update_info(target, content, reason)
            if hint:
                hints.append(hint)
                _refresh_all_indices()

        else:
            logger.warning(f"未知的学习类型: {ltype}，跳过")

    return hints


# ── 飞书通知 ────────────────────────────────────────────────────────────────


def _notify_user(message: str) -> None:
    """通过用户当前渠道发送通知（skill 变更时调用）。静默失败，不阻塞主流程。"""
    try:
        from src.tools import session as session_tool
        current_session = session_tool.get_current_session()
        if current_session and current_session.partial_sender:
            current_session.partial_sender(message)
            logger.info("[反思] 通知已通过当前渠道发送")
            return
    except Exception:
        pass
    # Fallback: print
    logger.info(f"[反思] {message}")


# ── 沉淀执行 ─────────────────────────────────────────────────────────────────


def _create_project(target: str, content: str, reason: str) -> str | None:
    """创建新的项目文件。如果已存在则降级为 update。"""
    if not target or not content:
        return None

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project_file = PROJECTS_DIR / f"{target}.md"

    if project_file.exists():
        return _update_project(target, content, reason)

    project_file.write_text(content + f"\n\n> 创建于 {date.today()}\n", encoding="utf-8")
    logger.info(f"已创建项目: {target} ({reason})")
    return f"已创建项目: {target}（{reason}）"


def _update_project(target: str, content: str, reason: str) -> str | None:
    """追加内容到已有项目文件。"""
    if not target or not content:
        return None

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project_file = PROJECTS_DIR / f"{target}.md"

    if not project_file.exists():
        return _create_project(target, content, reason)

    existing = project_file.read_text(encoding="utf-8")
    # 简单追加：如果已有内容里已经包含这段新内容，跳过
    if content.strip() in existing:
        return None

    updated = existing.rstrip() + f"\n\n## 更新 {date.today()}\n" + content.strip()
    project_file.write_text(updated + "\n", encoding="utf-8")
    logger.info(f"已更新项目: {target} ({reason})")
    return f"已更新项目: {target}（{reason}）"


def _get_existing_info_summary() -> str:
    """获取已有 info 列表摘要。"""
    if not INFO_DIR.exists():
        return "(无)"
    lines = []
    for info_file in sorted(INFO_DIR.glob("*.md")):
        content = info_file.read_text(encoding="utf-8")
        first_line = content.split("\n")[0].lstrip("# ").strip()
        lines.append(f"- {info_file.stem}: {first_line}")
    return "\n".join(lines) if lines else "(无)"


def _create_info(target: str, content: str, reason: str) -> str | None:
    """创建新的 info 文件。如果已存在则降级为 update。"""
    if not target or not content:
        return None

    INFO_DIR.mkdir(parents=True, exist_ok=True)
    info_file = INFO_DIR / f"{target}.md"

    if info_file.exists():
        return _update_info(target, content, reason)

    info_file.write_text(content + f"\n\n> 创建于 {date.today()}\n", encoding="utf-8")
    logger.info(f"已创建 Info: {target} ({reason})")
    return f"已创建 Info: {target}（{reason}）"


def _update_info(target: str, content: str, reason: str) -> str | None:
    """追加内容到已有 info 文件。"""
    if not target or not content:
        return None

    INFO_DIR.mkdir(parents=True, exist_ok=True)
    info_file = INFO_DIR / f"{target}.md"

    if not info_file.exists():
        return _create_info(target, content, reason)

    existing = info_file.read_text(encoding="utf-8")
    # 简单追加：如果已有内容里已经包含这段新内容，跳过
    if content.strip() in existing:
        return None

    updated = existing.rstrip() + f"\n\n## 更新 {date.today()}\n" + content.strip()
    info_file.write_text(updated + "\n", encoding="utf-8")
    logger.info(f"已更新 Info: {target} ({reason})")
    return f"已更新 Info: {target}（{reason}）"


def _create_skill(
    target: str, content: str, reason: str
) -> str | None:
    """创建新的 skill 文件（平铺 .md 格式）。带格式校验。"""
    if not target or not content:
        return None

    # 内容至少 300 字符
    if len(content.strip()) < 300:
        logger.info(f"Skill {target} 内容过短（{len(content.strip())}字符 < 300），跳过创建")
        return None

    # 至少包含 3 个编号步骤或标题步骤
    numbered_steps = len(re.findall(r'^\s*\d+[.、）)]', content, re.MULTILINE))
    heading_steps = len(re.findall(r'^\s*##\s+步骤|^\s*##\s+Step|^\s*###\s+\d', content, re.MULTILINE))
    if numbered_steps + heading_steps < 3:
        logger.info(f"Skill {target} 步骤不足（{numbered_steps + heading_steps} < 3），跳过创建")
        return None

    # 名称校验：小写英文开头+小写字母/数字/连字符
    if not re.match(r'^[a-z][a-z0-9-]*$', target):
        logger.info(f"Skill 名称不规范: {target}，跳过创建")
        return None

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    skill_path = SKILLS_DIR / f"{target}.md"

    frontmatter = f"---\ncreated_at: '{date.today()}'\nname: {target}\ndescription: {content[:200]}\n---\n\n{content}"
    skill_path.write_text(frontmatter, encoding="utf-8")
    logger.info(f"已创建技能: {target} ({reason})")

    return f"已创建技能: {target}（以后遇到类似问题会自动使用）"


def _update_skill(target: str, content: str, reason: str) -> str | None:
    """更新已有 skill 文件（平铺 .md 格式）。"""
    if not target:
        return None

    skill_path = SKILLS_DIR / f"{target}.md"
    if not skill_path.exists():
        if content and len(content.strip()) >= _MIN_SKILL_CONTENT_LEN:
            return _create_skill(target, content, reason)
        return None

    existing = skill_path.read_text(encoding="utf-8")

    if content and _content_already_exists(existing, content):
        logger.debug(f"Skill {target} 已有相同信息，跳过更新")
        return None

    updated = existing
    if content:
        updated = existing + f"\n\n## 更新 ({datetime.now():%Y-%m-%d})\n{content}"

    if updated == existing:
        return None

    skill_path.write_text(updated, encoding="utf-8")
    logger.info(f"已更新技能: {target} ({reason})")
    return f"已更新技能: {target}（{reason}）"


def _content_already_exists(existing: str, new_content: str) -> bool:
    """检查新内容是否已在现有 skill 中。"""
    stripped = new_content.strip()
    # 简单检测：是否完全包含（允许空格差异）
    normalized_new = re.sub(r"\s+", "", stripped)
    normalized_existing = re.sub(r"\s+", "", existing)
    return normalized_new in normalized_existing


def _get_existing_skills_summary() -> str:
    """获取已有 skills 列表摘要。"""
    if not SKILLS_DIR.exists():
        return "(无)"
    lines = []
    for skill_file in sorted(SKILLS_DIR.glob("*.md")):
        if skill_file.name == ".archived":
            continue
        first_lines = skill_file.read_text(encoding="utf-8").split("\n")[:4]
        desc = " ".join(l.lstrip("-# ") for l in first_lines if l.strip())[:120]
        lines.append(f"- {skill_file.stem}: {desc}")
    return "\n".join(lines) if lines else "(无)"


def _get_skill_full_content(skill_name: str) -> str:
    """获取指定 skill 的完整内容。"""
    skill_file = SKILLS_DIR / f"{skill_name}.md"
    if not skill_file.exists():
        return ""
    return skill_file.read_text(encoding="utf-8")


def _get_existing_projects_summary() -> str:
    """获取已有 projects 列表摘要。"""
    if not PROJECTS_DIR.exists():
        return "(无)"
    lines = []
    for proj_file in sorted(PROJECTS_DIR.glob("*.md")):
        content = proj_file.read_text(encoding="utf-8")
        first_line = content.split("\n")[0].lstrip("# ").strip()
        lines.append(f"- {proj_file.stem}: {first_line}")
    return "\n".join(lines) if lines else "(无)"


def format_execution_summary(plan: Plan) -> str:
    """将 Plan 对象格式化为文字摘要。"""
    if plan is None:
        return "(无计划，纯 Fast Path)"
    lines = [f"计划: {plan.goal} (状态: {plan.status.value})"]
    for step in plan.steps:
        icon = "✓" if step.status == StepStatus.done else "✗" if step.status == StepStatus.failed else "○"
        lines.append(f"  {icon} {step.action}")
    return "\n".join(lines)


# ── Skill 自动合并 ─────────────────────────────────────────────────────────

