"""工具注册与调度：统一管理所有工具的 schema 和执行函数。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from src.tools import shell as shell_tool
from src.tools import fileops as fileops_tool
from src.tools import search as search_tool
from src.tools import session as session_tool
from src.feishu import client as feishu_client
from src.core import skills_tools
from src.core.skills_tools import (
    ARCHIVE_SCHEMA,
    archive as _archive,
)

logger = logging.getLogger(__name__)

ToolRunner = Callable[[dict[str, Any]], str]

_REGISTRY: dict[str, tuple[dict[str, Any], ToolRunner]] = {}

# ── 工具组 ──────────────────────────────────────────────────────────────────
# auto=True 的组默认加载，auto=False 的组按需激活
# 前缀匹配：desktop_* → desktop, vision_* → vision, 其余 → core
GROUP_PROFILES: dict[str, dict[str, Any]] = {
    "desktop": {
        "description": "桌面控制（截图、点击、输入、滚动等 GUI 操作）",
        "auto": False,
    },
    "vision": {
        "description": "图片分析（用视觉模型分析图片内容）",
        "auto": False,
    },
}

# 当前 session 中已激活的组名集合（从 auto=True 的组开始）
_active_groups: set[str] = {name for name, cfg in GROUP_PROFILES.items() if cfg["auto"]}


def _tool_group(name: str) -> str:
    """根据工具名返回所属组名。前缀匹配，未匹配的归 core。"""
    prefix = name.split("_")[0]
    return prefix if prefix in GROUP_PROFILES else "core"


def _register(schema: dict[str, Any], runner: ToolRunner) -> None:
    name = schema["function"]["name"]
    _REGISTRY[name] = (schema, runner)


# ── 核心工具（缺了就起不来） ─────────────────────────────────────────────
_register(shell_tool.SCHEMA, shell_tool.run)
_register(search_tool.SEARCH_SCHEMA, search_tool.run)
_register(fileops_tool.FILE_READ_SCHEMA, fileops_tool.run_file_read)
_register(fileops_tool.FILE_WRITE_SCHEMA, fileops_tool.run_file_write)
_register(feishu_client.FEISHU_SEND_SCHEMA, feishu_client.tool_feishu_send)
_register(feishu_client.FEISHU_READ_SCHEMA, feishu_client.tool_feishu_read)
_register(skills_tools.PROJECT_CONTEXT_SCHEMA, skills_tools.project_context)
_register(skills_tools.SKILL_SCHEMA, skills_tools.skill)
_register(skills_tools.SEARCH_PROJECTS_SCHEMA, skills_tools.search_projects)
_register(skills_tools.INFO_SCHEMA, skills_tools.info)
_register(session_tool.SESSION_SCHEMA, session_tool.run)
_register(ARCHIVE_SCHEMA, _archive)


# ── 可选工具：schema 硬编码（无外部依赖），runner 延迟导入 ───────────────
# 注册时只写 schema，runner 在 dispatch() 首次调用时才 import
# 这样新用户 `pip install -e .` 时不会有任何 warning


def _run_web(params: dict) -> str:
    from src.tools.web import run as _run
    return _run(params)


def _run_schedule(params: dict) -> str:
    from src.tools.task_scheduler_tool import run_dispatch as _run
    return _run(params)


def _run_list_tasks(params: dict) -> str:
    from src.tools.task_scheduler_tool import run_list_tool as _run
    return _run(params)


def _run_cancel_task(params: dict) -> str:
    from src.tools.task_scheduler_tool import run_cancel_tool as _run
    return _run(params)


def _run_desktop(name: str, params: dict) -> str:
    from src.tools.desktop import run as _run
    return _run(name, params)


def _run_vision(params: dict) -> str:
    from src.tools.vision import run as _run
    return _run(params)


# web_search
_register(
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网，返回相关网页标题、链接和摘要。适用于查找最新信息、技术文档等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词或问题"},
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回几条结果，默认 5",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    _run_web,
)

# task_schedule / task_list / task_cancel
_register(
    {
        "type": "function",
        "function": {
            "name": "task_schedule",
            "description": (
                "动态注册定时任务。支持 interval（固定间隔）、cron（定时）、delayed（一次性延迟）。"
                "执行方式：用 prompt（自然语言，推荐）指定任务内容，或用 module 引用 skill scripts。"
                "注册后立即生效，无需重启。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["schedule", "cancel", "list"],
                        "description": "操作类型：schedule 注册任务、cancel 取消任务、list 查看所有任务",
                    },
                    "task_id": {"type": "string", "description": "任务 ID（schedule 时必填，cancel 时必填）"},
                    "task_type": {
                        "type": "string",
                        "enum": ["interval", "cron", "delayed"],
                        "description": "触发类型（schedule 时必填）",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "自然语言任务描述。触发时注入 agent session 由 LLM 用工具执行。推荐用于大多数场景。",
                    },
                    "module": {"type": "string", "description": "skills 下 scripts 目录中的模块名。与 prompt 二选一。"},
                    "func_name": {"type": "string", "description": "模块中要调用的函数名（默认 'run'）"},
                    "func_args": {"type": "object", "description": "传给函数的额外参数（可选）"},
                    "interval_seconds": {"type": "integer", "description": "interval 模式的间隔秒数"},
                    "cron_hour": {"type": "integer", "description": "cron 模式的小时（0-23）"},
                    "cron_minute": {"type": "integer", "description": "cron 模式的分钟（0-59）"},
                    "cron_day_of_week": {"type": "string", "description": "cron 模式的星期（如 'mon-fri'）"},
                    "delay_seconds": {"type": "integer", "description": "delayed 模式的延迟秒数"},
                    "description": {"type": "string", "description": "任务描述（显示在 list 中）"},
                },
                "required": ["action"],
            },
        },
    },
    _run_schedule,
)

_register(
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "查看当前所有已注册的定时任务，包括下次触发时间。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    _run_list_tasks,
)

_register(
    {
        "type": "function",
        "function": {
            "name": "task_cancel",
            "description": "取消一个已注册的定时任务。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "要取消的任务 ID"}},
                "required": ["task_id"],
            },
        },
    },
    _run_cancel_task,
)

# desktop_* 工具
_desktop_schemas = [
    ("desktop_screenshot", "截取当前屏幕，保存为 PNG 文件并返回路径。用于获取屏幕内容后配合视觉模型分析。", {}),
    (
        "desktop_screenshot_region",
        "截取屏幕指定区域，保存为 PNG 文件并返回路径。",
        {"x": {"type": "integer", "description": "左上角 X 坐标（像素）"}, "y": {"type": "integer", "description": "左上角 Y 坐标（像素）"}, "width": {"type": "integer", "description": "区域宽度（像素）"}, "height": {"type": "integer", "description": "区域高度（像素）"}},
        ["x", "y", "width", "height"],
    ),
    ("desktop_click", "在指定坐标点击鼠标左键。", {"x": {"type": "integer", "description": "X 坐标"}, "y": {"type": "integer", "description": "Y 坐标"}}, ["x", "y"]),
    ("desktop_type", "在当前焦点位置输入文本。", {"text": {"type": "string", "description": "要输入的文本"}}, ["text"]),
    ("desktop_press", "按下按键，如 enter, esc, space, tab, delete, cmd, shift, ctrl, alt", {"key": {"type": "string", "description": "按键名称"}}, ["key"]),
    ("desktop_hotkey", "按组合键，如 cmd+c, cmd+v, cmd+w, cmd+tab, ctrl+c 等", {"keys": {"type": "array", "items": {"type": "string"}, "description": "按键列表，如 ['cmd', 'c'] 表示 Cmd+C"}}, ["keys"]),
    ("desktop_scroll", "滚动鼠标。正数向上，负数向下。", {"clicks": {"type": "integer", "description": "滚动格数，正=上，负=下"}}, ["clicks"]),
    ("desktop_query_ui", "查询应用中的 UI 元素（需要应用开启 Accessibility 权限）。返回匹配元素的角色、名称、位置和大小。macOS 和 Windows 均支持。", {"app_name": {"type": "string", "description": "应用名称，如 Firefox, Google Chrome, Safari, Finder"}, "element_role": {"type": "string", "description": "元素角色，如 button, textfield, statictext, checkbox, menuitem"}, "element_title": {"type": "string", "description": "元素标题关键词（模糊匹配）"}}, ["app_name"]),
    ("desktop_info", "获取屏幕分辨率和鼠标位置等基本信息。", {}, []),
]

for _name, _desc, _props, *_required in _desktop_schemas:
    _reqs = _required[0] if _required else []
    _schema = {
        "type": "function",
        "function": {"name": _name, "description": _desc, "parameters": {"type": "object", "properties": _props, "required": _reqs}},
    }
    _register(_schema, lambda p, n=_name: _run_desktop(n, p))

# vision_analyze
_register(
    {
        "type": "function",
        "function": {
            "name": "vision_analyze",
            "description": (
                "用视觉模型分析一张图片。推荐传入 image_path（文件路径），由 Python 层面处理读取和压缩。"
                "也可传入 image_base64（向后兼容，不推荐大图片使用）。image_path 和 image_base64 二选一。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "图片文件路径，如 ~/.lamix/screenshots/screenshot_xxx.png（推荐）"},
                    "image_base64": {"type": "string", "description": "图片的 base64 编码字符串（不含 data:image/... 前缀）。大图片请改用 image_path。"},
                    "prompt": {"type": "string", "description": "对图片的提问，例如 '屏幕上有哪些按钮？' 或 '描述当前桌面'", "default": "描述这张图片的内容"},
                },
            },
        },
    },
    _run_vision,
)


# ── skill scripts 延迟加载 ──────────────────────────────────────────────

def load_skill_scripts() -> None:
    """扫描 ~/.lamix/memory/skills/*/scripts/，注册所有包含 TOOL_SCHEMA 的脚本为工具。

    必须在 daemon 启动完成后调用，不能在模块初始化时调用，否则会产生循环导入：
    tools.py → skill_scripts.py → tools.py（tools 模块还未初始化完成）。
    """
    try:
        from src.tools import skill_scripts
        registered = skill_scripts.scan_and_register()
        if registered:
            logger.info(f"已加载 {len(registered)} 个 skill script 工具: "
                        f"{[s['function']['name'] for s in registered]}")
        else:
            logger.debug("未发现 skill script 工具（skills/*/scripts/ 下无有效脚本）")
    except Exception as e:
        logger.warning(f"加载 skill scripts 失败: {e}")


# ─── 飞书客户端懒加载初始化 ────────────────────────────────────────────────

_feishu_initialized = False


def _ensure_feishu_client() -> bool:
    """确保飞书客户端已初始化（懒加载）。"""
    global _feishu_initialized
    if _feishu_initialized:
        return True

    config_paths = [
        Path("~/.lamix/config.yaml").expanduser(),
        Path("config/default.yaml").expanduser(),
    ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                import yaml
                with open(config_path, encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                feishu_cfg = config.get("feishu", {})
                app_id = feishu_cfg.get("app_id", "").strip()
                app_secret = feishu_cfg.get("app_secret", "").strip()
                if app_id and app_secret:
                    feishu_client.init_client(app_id=app_id, app_secret=app_secret)
                    _feishu_initialized = True
                    return True
            except Exception:
                pass

    return False


def validate_tool_schema(schema: dict[str, Any]) -> list[str]:
    """校验工具 schema 格式，返回错误列表（空列表 = 通过）。"""
    errors: list[str] = []
    if schema.get("type") != "function":
        errors.append(f"缺少或错误的 type 字段: {schema.get('type')!r}, 期望 'function'")
    func = schema.get("function")
    if not isinstance(func, dict):
        errors.append("缺少 function 字段或类型不是 dict")
    else:
        if not func.get("name"):
            errors.append("缺少 function.name")
        if "parameters" not in func:
            errors.append("缺少 function.parameters")
    return errors


def get_all_schemas() -> list[dict[str, Any]]:
    """返回所有已激活工具的 schema 列表（core + 已激活组）。"""
    return [
        schema for name, (schema, _) in _REGISTRY.items()
        if _tool_group(name) == "core" or _tool_group(name) in _active_groups
    ]


def activate_tool_group(name: str) -> str:
    """激活一个工具组，使其工具可用于当前 session。

    Returns:
        组内工具的描述文本（供 LLM 了解可用工具）。
    """
    if name not in GROUP_PROFILES:
        available = ", ".join(
            f"{n}（{cfg['description']}）"
            for n, cfg in GROUP_PROFILES.items()
            if n not in _active_groups
        )
        return f"[未知工具组: {name}]\n\n可激活的组: {available or '(全部已激活)'}"

    if name in _active_groups:
        return f"[工具组 '{name}' 已处于激活状态]"

    _active_groups.add(name)

    # 收集该组所有工具的名称和描述
    tool_lines: list[str] = []
    for tool_name, (schema, _) in _REGISTRY.items():
        if _tool_group(tool_name) == name:
            func = schema.get("function", {})
            desc = func.get("description", "")
            tool_lines.append(f"- **{tool_name}**: {desc}")

    tools_text = "\n".join(tool_lines) if tool_lines else "(无工具)"
    return (
        f"工具组 '{name}' 已激活。以下工具现在可用：\n\n"
        f"{tools_text}\n\n"
        f"你现在可以直接调用这些工具。"
    )


def get_inactive_group_profiles() -> dict[str, str]:
    """返回尚未激活的组的 {组名: 描述}（供 prompt_builder 写索引用）。"""
    return {
        name: cfg["description"]
        for name, cfg in GROUP_PROFILES.items()
        if name not in _active_groups
    }


# ── activate_tool_group 工具注册 ─────────────────────────────────────────

_ACTIVATE_TOOL_GROUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "activate_tool_group",
        "description": (
            "按需激活一个工具组。当前只有默认工具可用；"
            "当你判断任务需要特定能力（如桌面控制、图片分析）时，先调用此工具激活对应组，"
            "激活后该组所有工具即可在当前会话中使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要激活的工具组名称，如 desktop、vision",
                },
            },
            "required": ["name"],
        },
    },
}


def _run_activate_tool_group(params: dict) -> str:
    return activate_tool_group(params.get("name", ""))


_register(_ACTIVATE_TOOL_GROUP_SCHEMA, _run_activate_tool_group)


def dispatch(tool_name: str, arguments_raw: str | dict[str, Any]) -> str:
    """根据工具名分发执行，arguments_raw 可以是 JSON 字符串或字典。"""
    if tool_name not in _REGISTRY:
        return f"[错误] 未知工具：{tool_name}"

    if tool_name.startswith("feishu_"):
        _ensure_feishu_client()

    if isinstance(arguments_raw, str):
        try:
            params = json.loads(arguments_raw)
        except json.DecodeError as e:
            return f"[错误] 工具参数解析失败：{e}"
    else:
        params = arguments_raw

    _, runner = _REGISTRY[tool_name]
    try:
        return runner(params)
    except ImportError as e:
        logger.warning(f"可选工具 {tool_name} 运行时导入失败（缺少依赖: {e}），已跳过")
        return f"[错误] 工具 {tool_name} 缺少依赖：{e}。请安装相关依赖后重试。"
    except Exception as e:
        return f"[错误] 工具 {tool_name} 执行异常：{e}"


def register_external(schema: dict[str, Any], runner: ToolRunner) -> bool:
    """注册外部工具（供飞书、自更新等模块动态注册）。"""
    errors = validate_tool_schema(schema)
    if errors:
        name_hint = schema.get("function", {}).get("name", "<未知>")
        logger.warning(f"工具 {name_hint} schema 校验失败，跳过注册: {'; '.join(errors)}")
        return False
    _register(schema, runner)
    return True
