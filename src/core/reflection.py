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
import threading
import time as time_module
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.planning.steps import Plan, StepStatus

logger = logging.getLogger(__name__)

# 线程局部存储
_local = threading.local()

LAMIX_DIR = Path.home() / ".lamix"
SKILLS_DIR = LAMIX_DIR / "memory" / "skills"
PROJECTS_DIR = LAMIX_DIR / "memory" / "projects"
INFO_DIR = LAMIX_DIR / "memory" / "info"

# 反思冷却时间（秒）：距上次反思不足此间隔则跳过
_REFLECT_COOLDOWN = 300  # 5 分钟

# Skill 内容最短长度（字符），低于此不创建
_MIN_SKILL_CONTENT_LEN = 80

# 沉淀通知回调（由 Session 初始化时注入，全局共享）
_notify_callback: Any = None


def set_llm_client(client: Any) -> None:
    """由 Session 初始化时调用，注入当前 LLM Client。"""
    _local.llm_client = client


def set_fallback_llms(fallback_llms: list[Any]) -> None:
    """由 Session 初始化时调用，注入 fallback 模型列表。"""
    _local.fallback_llms = fallback_llms or []


def set_skill_index(index: Any) -> None:
    """由 Session 初始化时调用，注入当前 SkillIndex，skill 变更后自动刷新索引。"""
    _local.skill_index = index


def set_notify_callback(cb: Any) -> None:
    """由 Session 初始化时注入，用于向用户发送沉淀通知。"""
    global _notify_callback
    _notify_callback = cb


# ── 沉淀工具 Schemas ────────────────────────────────────────────────────────

_REFLECTION_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "skill_create",
            "description": (
                "创建新的可复用技能文档。适用：发现了 2+ 步骤的工作流（150-300 字符），"
                "或 3+ 步骤的工作流（300 字符以上），且 skills 目录中没有类似的。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能名称：小写英文开头+小写字母/数字/连字符，如 'docker-debug', 'api-testing'",
                    },
                    "content": {
                        "type": "string",
                        "description": "技能正文：包含 2+ 个明确步骤的方法论（150-300 字符），或 3+ 步骤（300 字符以上），是通用工作流，不是具体答案",
                    },
                    "reason": {
                        "type": "string",
                        "description": "创建原因：简短说明为什么值得创建这个技能",
                    },
                },
                "required": ["name", "content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_update",
            "description": "更新已有技能文档。适用：发现已有技能需要补充、修正或扩展。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要更新的技能名称（需已在 skills 目录存在）",
                    },
                    "content": {
                        "type": "string",
                        "description": "要追加的内容：方法论补充、踩坑记录、修正说明",
                    },
                    "reason": {
                        "type": "string",
                        "description": "更新原因：简短说明为什么更新",
                    },
                },
                "required": ["name", "content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_create",
            "description": (
                "为探索过的项目创建文档。适用：在某项目中发现了新的、未记录的配置、"
                "测试结论、容器地址、API 路由等事实。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "项目名称，如 'model-platform', 'im-asr', 'lamix'",
                    },
                    "content": {
                        "type": "string",
                        "description": "项目事实：配置、测试结论、部署信息等增量内容",
                    },
                    "reason": {
                        "type": "string",
                        "description": "创建原因",
                    },
                },
                "required": ["name", "content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_update",
            "description": (
                "追加内容到已有项目文档。适用：在已有项目中发现了新的事实。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "项目名称（需已存在）",
                    },
                    "content": {
                        "type": "string",
                        "description": "要追加的项目事实：配置变更、测试结论、踩坑记录等",
                    },
                    "reason": {
                        "type": "string",
                        "description": "追加原因",
                    },
                },
                "required": ["name", "content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "info_create",
            "description": (
                "创建通用知识文档。适用：发现了新的、未记录的通用知识，"
                "如服务地址、API 用法、踩坑记录，与特定项目无关。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Info 名称：小写英文开头，如 'jump-server-usage', 'feishu-api-pitfalls'",
                    },
                    "content": {
                        "type": "string",
                        "description": "通用知识内容",
                    },
                    "reason": {
                        "type": "string",
                        "description": "创建原因",
                    },
                },
                "required": ["name", "content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "info_update",
            "description": "追加内容到已有 Info 文档。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Info 名称（需已存在）",
                    },
                    "content": {
                        "type": "string",
                        "description": "要追加的通用知识内容",
                    },
                    "reason": {
                        "type": "string",
                        "description": "追加原因",
                    },
                },
                "required": ["name", "content", "reason"],
            },
        },
    },
]


# ── 工具执行函数 ───────────────────────────────────────────────────────────

def _skill_create(args: dict[str, Any]) -> str:
    name = args.get("name", "")
    content = args.get("content", "")
    reason = args.get("reason", "")
    hint = _create_skill(name, content, reason)
    if hint:
        _notify_user(f"📝 **Skill 新建**\n\n- 名称：{name}\n- 原因：{reason}")
        _refresh_all_indices()
    return hint or "无需创建"


def _skill_update(args: dict[str, Any]) -> str:
    name = args.get("name", "")
    content = args.get("content", "")
    reason = args.get("reason", "")
    hint = _update_skill(name, content, reason)
    if hint:
        _notify_user(f"🔧 **Skill 更新**\n\n- 名称：{name}\n- 原因：{reason}")
        _refresh_all_indices()
    return hint or "无需更新"


def _project_create(args: dict[str, Any]) -> str:
    name = args.get("name", "")
    content = args.get("content", "")
    reason = args.get("reason", "")
    hint = _create_project(name, content, reason)
    if hint:
        _refresh_all_indices()
    return hint or "无需创建"


def _project_update(args: dict[str, Any]) -> str:
    name = args.get("name", "")
    content = args.get("content", "")
    reason = args.get("reason", "")
    hint = _update_project(name, content, reason)
    if hint:
        _refresh_all_indices()
    return hint or "无需更新"


def _info_create(args: dict[str, Any]) -> str:
    name = args.get("name", "")
    content = args.get("content", "")
    reason = args.get("reason", "")
    hint = _create_info(name, content, reason)
    if hint:
        _refresh_all_indices()
    return hint or "无需创建"


def _info_update(args: dict[str, Any]) -> str:
    name = args.get("name", "")
    content = args.get("content", "")
    reason = args.get("reason", "")
    hint = _update_info(name, content, reason)
    if hint:
        _refresh_all_indices()
    return hint or "无需更新"


# 工具名 → 执行函数 映射
_TOOL_RUNNERS = {
    "skill_create": _skill_create,
    "skill_update": _skill_update,
    "project_create": _project_create,
    "project_update": _project_update,
    "info_create": _info_create,
    "info_update": _info_update,
}


# ── 反思主循环（tool-calling）─────────────────────────────────────────────

_REFLECTION_SYSTEM_PROMPT = """你是一个知识管理助手。根据对话内容判断是否有值得持久化的知识。

可用的工具：
- skill_create / skill_update：创建或更新可复用技能（2+ 步骤的工作流方法论）
- project_create / project_update：创建或追加项目事实（配置、测试结论、部署信息）
- info_create / info_update：创建或追加通用知识（服务地址、API 用法、踩坑记录）

判断标准：
- skill：2+ 步骤的工作流（150-300 字符），或 3+ 步骤（300 字符以上），且 skills 中没有类似的
- project：项目相关的新发现，直接追加
- info：通用知识的新发现，info 中没有的

如果没有值得保存的，直接用文字回复"无需沉淀"。
不要调用任何工具。"""


def run_reflection_loop(
    goal: str,
    execution_summary: str,
    llm_client: Any,
    adapter: Any,
    skill_activated: str | None = None,
    recent_context: str = "",
    active_project: str = "",
    max_rounds: int = 5,
    fallback_models: list[tuple[Any, Any]] | None = None,
) -> list[str]:
    """执行反思 tool-calling 循环，返回沉淀摘要列表。

    给 LLM 提供沉淀工具，LLM 自己决定调什么。
    支持主模型失败时自动 fallback 到备用模型。
    """
    # 构建 system message
    system_msg = _REFLECTION_SYSTEM_PROMPT

    # 构建 user message
    user_content = f"## 用户目标\n{goal}\n\n## 执行过程\n{execution_summary}\n"

    if skill_activated:
        skill_content = _get_skill_full_content(skill_activated)
        if skill_content:
            user_content += f"\n## 本轮激活的技能 [{skill_activated}]\n{skill_content[:2000]}\n"

    if recent_context and recent_context != "（无对话记录）":
        user_content += f"\n## 最近对话\n{recent_context[:3000]}\n"

    if active_project:
        user_content += f"\n## 当前操作的项目\n{active_project}（内容应沉淀到该项目的 project 文件，不要串项目）\n"

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content},
    ]

    reflection_results: list[str] = []
    no_tool_count = 0

    # 构建 fallback 列表
    fallback_list = fallback_models or []
    fb_idx = 0  # 当前尝试的 fallback 索引
    using_primary = True  # 是否仍使用主模型

    for round_num in range(max_rounds):
        # 确定本轮使用的 adapter
        if using_primary:
            current_adapter = adapter
            model_name = getattr(llm_client, 'model', 'unknown')
        else:
            # 使用 fallback
            if fb_idx >= len(fallback_list):
                # Fallback 耗尽，直接退出
                _notify_user("⚠️ 反思失败\n\n- 原因：所有 fallback 模型均已失败")
                return []
            fb_llm, current_adapter = fallback_list[fb_idx]
            model_name = getattr(fb_llm, 'model', 'unknown')

        try:
            resp = current_adapter.chat(messages, tools=_REFLECTION_TOOLS_SCHEMA, timeout=90)
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"[反思] LLM 调用失败（{model_name}, round {round_num + 1}）: {error_msg}")

            # 如果是主模型失败，切换到 fallback
            if using_primary and fallback_list:
                using_primary = False
                fb_idx = 0
                logger.info(f"[反思] 主模型失败，尝试 fallback...")
                continue  # 继续下一轮尝试 fallback

            # 如果是 fallback 失败，尝试下一个 fallback
            if not using_primary and fb_idx < len(fallback_list) - 1:
                fb_idx += 1
                logger.info(f"[反思] fallback {fb_idx - 1} 失败，尝试下一个...")
                continue  # 继续下一轮尝试下一个 fallback

            # Fallback 耗尽，直接失败，不回退到主模型
            _notify_user(f"⚠️ 反思失败\n\n- 原因：所有模型（主模型 + {len(fallback_list)} 个 fallback）均失败\n- 错误：{error_msg[:100]}")
            return []

        choice = resp.choices[0]
        msg_dict = choice.message.model_dump(exclude_none=True)
        messages.append(msg_dict)

        finish_reason = choice.finish_reason
        parsed_tcs = _parse_tool_calls(msg_dict)

        if not parsed_tcs:
            content = msg_dict.get("content", "").strip()
            if content and "无需沉淀" not in content:
                logger.info(f"[反思] LLM 文字回复: {content[:100]}")
            no_tool_count += 1
            if no_tool_count >= 1:
                break
            continue

        # 执行 tool calls
        for tc in parsed_tcs:
            tool_name = tc["name"]
            if tool_name not in _TOOL_RUNNERS:
                result = f"[错误] 未知工具：{tool_name}"
            else:
                try:
                    args = json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else tc["arguments"]
                except Exception:
                    result = f"[错误] 工具参数解析失败"
                else:
                    result = _TOOL_RUNNERS[tool_name](args)
                    if result and result not in ("无需创建", "无需更新", "无需沉淀"):
                        reflection_results.append(result)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        no_tool_count = 0

    return reflection_results


def _parse_tool_calls(msg_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """从 assistant message 中解析出 tool_calls 列表。"""
    tcs = msg_dict.get("tool_calls", [])
    if not tcs:
        return []
    result = []
    for tc in tcs:
        fn = tc.get("function", {})
        result.append({
            "id": tc.get("id", str(uuid.uuid4())),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "{}"),
        })
    return result


# ── 飞书通知 ───────────────────────────────────────────────────────────────


def _notify_user(message: str) -> None:
    """通过用户当前渠道发送通知（skill 变更时调用）。静默失败，不阻塞主流程。"""
    global _notify_callback
    if _notify_callback:
        try:
            _notify_callback(message)
            return
        except Exception:
            pass
    # Fallback: print
    logger.info(f"[反思] {message}")


# ── 公开接口 ─────────────────────────────────────────────────────────────────


def should_reflect(
    plan: Plan | None = None,
    *,
    tool_call_count: int = 0,
    skill_activated: str | None = None,
    user_input: str = "",
) -> bool:
    """判断本次任务是否需要反思。

    触发条件：tool_call_count >= 3（任意 3 次以上工具调用）
    冷却控制：距上次反思不足 5 分钟则跳过。

    注意：plan, skill_activated, user_input 参数保留以兼容调用方，但当前未使用。
    """
    last_reflect_time = getattr(_local, 'last_reflect_time', 0.0)

    now = time_module.time()
    elapsed = now - last_reflect_time

    # 冷却期内 → 跳过
    if elapsed < _REFLECT_COOLDOWN:
        logger.info(f"[反思] 跳过：冷却中（距上次 {elapsed:.0f}s < {_REFLECT_COOLDOWN}s）")
        return False

    # 工具调用 < 3 次 → 跳过
    if tool_call_count < 3:
        logger.info(f"[反思] 跳过：tool_call_count={tool_call_count} < 3")
        return False

    # 触发反思
    logger.info(f"[反思] 触发：tool_call_count={tool_call_count} >= 3")
    return True


def mark_reflection_done() -> None:
    """反思完成后更新冷却时间。由 agent.py 在反思线程结束时调用。"""
    _local.last_reflect_time = time_module.time()
    logger.info(f"[反思] 冷却时间已更新")


# ── 索引刷新 ────────────────────────────────────────────────────────────────


def _refresh_all_indices() -> None:
    """skill/info/project 变更后重建所有索引 + 刷新 tools 注册。静默失败。"""
    try:
        # Skill 索引
        skill_index = getattr(_local, 'skill_index', None)
        if skill_index is not None:
            skill_index.load_or_build()
            logger.info("[反思] Skill 索引已重建")
        else:
            logger.debug("[反思] Skill 索引未注入，跳过")

        # Project 索引
        from src.tools import session as session_tool
        current_session = session_tool.get_current_session()
        if current_session and current_session.project_index is not None:
            current_session.project_index.load_or_build()
            if current_session.agent:
                current_session.agent.project_index = current_session.project_index
            logger.info("[反思] Project 索引已重建")

        # Info 缓存：清掉 prompt_builder 的 _info_index_cache
        from src.core import prompt_builder
        prompt_builder._info_index_cache = None
        logger.info("[反思] Info 缓存已清除")

        # 统一刷新 skills_tools 的检索索引
        if current_session:
            from src.core import skills_tools as skills_tools_reg
            skill_index = getattr(_local, 'skill_index', None)
            skills_tools_reg.set_retrieval_indices(
                skill_index,
                current_session.project_index,
            )
            current_session.skill_index = skill_index
            if current_session.agent:
                current_session.agent.skill_index = skill_index

    except Exception as e:
        logger.warning("[反思] 索引重建失败: %s", e)


# ── 沉淀执行函数（复用原有实现）──────────────────────────────────────────────


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
    if content.strip() in existing:
        return None

    updated = existing.rstrip() + f"\n\n## 更新 {date.today()}\n" + content.strip()
    project_file.write_text(updated + "\n", encoding="utf-8")
    logger.info(f"已更新项目: {target} ({reason})")
    return f"已更新项目: {target}（{reason}）"


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
    if content.strip() in existing:
        return None

    updated = existing.rstrip() + f"\n\n## 更新 {date.today()}\n" + content.strip()
    info_file.write_text(updated + "\n", encoding="utf-8")
    logger.info(f"已更新 Info: {target} ({reason})")
    return f"已更新 Info: {target}（{reason}）"


def _create_skill(
    target: str,
    content: str,
    reason: str,
) -> str | None:
    """创建新的 skill 文件（平铺 .md 格式）。带格式校验。"""
    if not target or not content:
        return None

    content_len = len(content.strip())

    # 最低门槛：150 字符以下直接拒绝
    if content_len < 150:
        logger.info(f"Skill {target} 内容过短（{content_len} 字符 < 150），跳过创建")
        return None

    # 计算步骤数
    numbered_steps = len(re.findall(r"^\s*\d+[.、）)]", content, re.MULTILINE))
    heading_steps = len(re.findall(r"^\s*##\s+步骤|^\s*##\s+Step|^\s*###\s+\d", content, re.MULTILINE))
    total_steps = numbered_steps + heading_steps

    if content_len < 300:
        # 简短技能：150-300 字符，至少 2 个步骤
        if total_steps < 2:
            logger.info(f"Skill {target} 步骤不足（{total_steps} < 2，内容 {content_len} 字符 < 300），跳过创建")
            return None
    else:
        # 标准技能：>= 300 字符，至少 3 个步骤
        if total_steps < 3:
            logger.info(f"Skill {target} 步骤不足（{total_steps} < 3，内容 {content_len} 字符 >= 300），跳过创建")
            return None

    # 名称校验：小写英文开头+小写字母/数字/连字符
    if not re.match(r"^[a-z][a-z0-9-]*$", target):
        logger.info(f"Skill 名称不规范: {target}，跳过创建")
        return None

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    skill_path = SKILLS_DIR / f"{target}.md"

    # 安全生成 YAML frontmatter：清理换行和特殊字符，用引号包裹
    description = content[:200].replace('\n', ' ').replace('\r', ' ').replace('\\', '\\\\').replace('"', '\\"')
    frontmatter = f"---\ncreated_at: '{date.today()}'\nname: {target}\ndescription: \"{description}\"\n---\n\n{content}"
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
    normalized_new = re.sub(r"\s+", "", stripped)
    normalized_existing = re.sub(r"\s+", "", existing)
    return normalized_new in normalized_existing


def _get_skill_full_content(skill_name: str) -> str:
    """获取指定 skill 的完整内容。"""
    skill_file = SKILLS_DIR / f"{skill_name}.md"
    if not skill_file.exists():
        return ""
    return skill_file.read_text(encoding="utf-8")


def format_execution_summary(plan: Plan) -> str:
    """将 Plan 对象格式化为文字摘要。"""
    if plan is None:
        return "(无计划，纯 Fast Path)"
    lines = [f"计划: {plan.goal} (状态: {plan.status.value})"]
    for step in plan.steps:
        icon = "✓" if step.status == StepStatus.done else "✗" if step.status == StepStatus.failed else "○"
        lines.append(f"  {icon} {step.action}")
    return "\n".join(lines)
