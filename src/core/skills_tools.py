"""Skills / Project 相关工具：skill（合并 view+search）、search_projects、project_context；skill 解析供 indexer 等复用。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from src.core.config import SKILLS_DIR, PROJECTS_DIR, INFO_DIR


# Session 在启动时通过 set_retrieval_indices 注入，供 skill search/search_projects/info 使用
_active_skill_index: Any = None
_active_project_index: Any = None
_active_info_index: Any = None

# 双层结构入口文件名
_SKILL_ENTRY_FILE = "SKILL.md"


# ── 统一 Skill Schema ────────────────────────────────────────────────────────

SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": (
            "操作技能。action='view' 按名称加载技能全文，"
            "action='search' 在技能名称与描述中做关键词匹配查找。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["view", "search"],
                    "description": "操作类型：view 按名称加载技能，search 按关键词搜索技能",
                },
                "name": {
                    "type": "string",
                    "description": "技能名称（action='view' 时使用），例如 'code-writing', 'reverse-tracking'",
                },
                "query": {
                    "type": "string",
                    "description": "搜索关键词（action='search' 时使用），用自然语言描述需要的能力或工作流类型",
                },
                "top_k": {
                    "type": "integer",
                    "description": "search 时返回最多几个结果，默认 3",
                    "default": 3,
                },
            },
            "required": ["action"],
        },
    },
}

SEARCH_PROJECTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_projects",
        "description": (
            "根据自然语言描述搜索匹配的项目上下文。"
            "当你需要查找某个项目或仓库的背景信息时使用此工具。"
            "用自然语言描述你需要什么项目的什么信息，例如 '模型平台的工程目录'、'hermes 代码结构'。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用自然语言描述你需要哪类项目/仓库背景",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回最多几个结果，默认 2",
                    "default": 2,
                },
            },
            "required": ["query"],
        },
    },
}


PROJECT_CONTEXT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "project_context",
        "description": "加载指定项目的完整上下文（项目信息、状态、约定）。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "项目名称，例如 'Lamix'、'hermes'"
                }
            },
            "required": ["name"]
        }
    }
}

INFO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "info",
        "description": "加载知识性信息文件的内容，例如项目规范、API 文档、使用说明等。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "文件名（不含 .md），例如 'api-reference'、'deployment-guide'"
                }
            },
            "required": ["name"]
        }
    }
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def set_retrieval_indices(skill_index: Any, project_index: Any, info_index: Any = None) -> None:
    """由 Session 在索引构建后调用，供 skill search/search_projects/info 使用。"""
    global _active_skill_index, _active_project_index, _active_info_index
    _active_skill_index = skill_index
    _active_project_index = project_index
    _active_info_index = info_index



def _parse_skill(path: Path) -> dict[str, Any] | None:
    """解析 skill 文件。"""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    match = _FRONTMATTER_RE.match(content)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = content[match.end():]
    else:
        meta = {}
        body = content

    name = meta.get("name", "") or path.stem
    return {
        "name": name,
        "description": meta.get("description", ""),
        "body": body,
        "full_content": content,
    }


def project_context(params: dict[str, Any]) -> str:
    """加载指定项目的完整上下文。"""
    from src.core.prompt_builder import load_project_context as _load
    name = params.get("name", "")
    if not name:
        return "project_context 需要 name 参数，例如：project_context(name=\"Lamix\")"
    return _load(name)



def _get_skill_entry_by_name(name: str) -> dict[str, Any] | None:
    """根据 skill 名查找条目（支持双层路径）。返回条目或 None。

    双层结构优先：skills/<name>/SKILL.md
    向后兼容：skills/<name>.md
    """
    from src.core.prompt_builder import _scan_skills_dir

    entries = _scan_skills_dir()
    name_lower = name.lower()
    for e in entries:
        skill_name = str(e.get("name", "")).lower()
        if skill_name == name_lower:
            return e
    return None


def _resolve_skill_path(name: str) -> tuple[Path | None, bool]:
    """解析 skill 名称，返回 (文件路径, 是否子项)。

    - name="code-writing" → skills/code-writing/SKILL.md 或 skills/code-writing.md
    - name="code-writing/references/python-patterns" → skills/code-writing/references/python-patterns.md

    Returns: (path, is_sub_item)
    """
    if "/" in name:
        skill_name, sub_part = name.split("/", 1)
    else:
        skill_name, sub_part = name, ""

    if not SKILLS_DIR.exists():
        return None, False

    skill_dir = SKILLS_DIR / skill_name
    entry_file = skill_dir / _SKILL_ENTRY_FILE

    if sub_part:
        # 子项：skills/<name>/<sub_part>.md
        sub_path = skill_dir / (sub_part + ".md")
        if sub_path.is_file():
            return sub_path, True
        # 尝试不带扩展名（子目录形式）
        sub_dir = skill_dir / sub_part
        if sub_dir.is_dir():
            # 返回目录下的第一个 .md 文件
            md_files = list(sub_dir.glob("*.md"))
            if md_files:
                return md_files[0], True
        return None, True

    # 主入口：先尝试 SKILL.md，再尝试平铺 .md
    if entry_file.is_file():
        return entry_file, False
    legacy_file = SKILLS_DIR / f"{skill_name}.md"
    if legacy_file.is_file():
        return legacy_file, False
    return None, False


def _increment_invocation(skill_path: Path) -> int:
    """递增 skill 文件的 invocation_count 和 last_used_at，保留正文；返回新计数。"""
    from datetime import date
    from src.core.prompt_builder import _parse_frontmatter, write_skill_with_frontmatter

    try:
        raw = skill_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    meta, body = _parse_frontmatter(raw)
    try:
        ic = int(meta.get("invocation_count", 0))
    except (TypeError, ValueError):
        ic = 0
    meta["invocation_count"] = ic + 1
    meta["last_used_at"] = str(date.today())
    write_skill_with_frontmatter(skill_path, meta, body)
    return int(meta["invocation_count"])


def _run_skill_view(params: dict[str, Any]) -> str:
    """按名称加载 skill 文件全文，并递增 invocation_count。

    支持双层路径：
    - name="code-writing" → 加载 skills/code-writing/SKILL.md
    - name="code-writing/references/python-patterns" → 加载子项
    """
    name = str(params.get("name", "")).strip()
    if not name:
        return "[错误] name 参数不能为空"

    path, is_sub_item = _resolve_skill_path(name)
    if path is None or not path.is_file():
        # 提供可用的 skill 列表
        from src.core.prompt_builder import _scan_skills_dir
        entries = _scan_skills_dir()
        available = [str(e["name"]) for e in entries]
        avail_str = ", ".join(available) if available else "(none)"
        return f"[错误] 未找到名为「{name}」的技能\n\n可用技能: {avail_str}"

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as ex:
        return f"[错误] 读取技能文件失败：{ex}"

    # 只对主入口文件递增 invocation_count
    if not is_sub_item:
        new_c = _increment_invocation(path)
        # 同步更新 index 中的计数
        from src.core.prompt_builder import _scan_skills_dir
        entries = _scan_skills_dir()
        for e in entries:
            if str(e.get("path", "")) == str(path.resolve()):
                e["invocation_count"] = new_c
                break

        # 启动 skill 执行审计
        try:
            from src.core.skill_audit import start_audit
            body = text
            fm_match = __import__("re").match(r"^---\s*\n.*?\n---\s*\n", text, __import__("re").DOTALL)
            if fm_match:
                body = text[fm_match.end():]
            start_audit(name, body)
        except Exception as e:
            __import__("logging").getLogger(__name__).debug(f"审计启动失败: {e}")

    return text


def _run_skill_search(params: dict[str, Any]) -> str:
    """在 skill 内容中做关键词匹配，返回匹配的 skill 文件全文。

    搜索范围：SKILL.md + references/*.md + templates/*.md
    """
    query = params.get("query", "").strip()
    top_k = int(params.get("top_k", 3))
    if not query:
        return "[错误] query 参数不能为空"

    from src.core.prompt_builder import _scan_skills_dir, _parse_frontmatter

    entries = _scan_skills_dir()
    if not entries:
        return "[提示] 技能索引为空"

    q_lower = query.lower()
    results: list[tuple[float, str]] = []  # (score, content)

    for e in entries:
        skill_dir = e["path"].parent
        skill_name = str(e.get("name", ""))

        # 搜索所有相关文件
        files_to_search: list[Path] = [e["path"]]  # SKILL.md

        # references/*.md, templates/*.md
        for subdir in ("references", "templates"):
            subdir_path = skill_dir / subdir
            if subdir_path.is_dir():
                files_to_search.extend(subdir_path.glob("*.md"))

        all_text = ""
        for f in files_to_search:
            try:
                raw = f.read_text(encoding="utf-8")
                # 去 frontmatter
                _, body = _parse_frontmatter(raw)
                all_text += f"\n\n{body}"
            except OSError:
                pass

        score = 0.0
        if skill_name.lower() == q_lower:
            score = 2.0
        elif q_lower in skill_name.lower():
            score = 1.5
        elif q_lower in all_text.lower():
            score = 1.0

        if score > 0:
            results.append((score, all_text))

    results.sort(key=lambda x: -x[0])
    top = results[:top_k]

    if not top:
        return f"[提示] 没有找到与「{query}」相关的技能"

    lines = [f"--- 匹配度 {s:.1f} ---  {c[:500]}" for s, c in top]
    return "\n\n".join(lines)


# ── Skill 增删改 ────────────────────────────────────────────────────────────

def _create_skill(name: str, description: str, body: str = "") -> str:
    """在 skills/ 下创建子目录结构（SKILL.md）。"""
    skill_dir = SKILLS_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / _SKILL_ENTRY_FILE

    if skill_path.exists():
        return f"[错误] 技能「{name}」已存在"

    meta = {
        "name": name,
        "description": description,
        "invocation_count": 0,
        "created_at": str(__import__("datetime").date.today()),
    }
    _write_skill_with_frontmatter(skill_path, meta, body)
    _refresh_all_indices()
    return f"[成功] 创建技能「{name}」，路径：{skill_path}"


def _update_skill(name: str, body: str) -> str:
    """更新 skill 文件（保留 frontmatter）。"""
    path, is_sub_item = _resolve_skill_path(name)
    if path is None or not path.is_file():
        return f"[错误] 未找到技能「{name}」"

    from src.core.prompt_builder import _parse_frontmatter
    meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
    meta["updated_at"] = str(__import__("datetime").date.today())
    _write_skill_with_frontmatter(path, meta, body)
    _refresh_all_indices()
    return f"[成功] 更新技能「{name}」"


def _archive_skill(name: str) -> str:
    """归档 skill（移到 .archived/）。"""
    path, _ = _resolve_skill_path(name)
    if path is None or not path.is_file():
        return f"[错误] 未找到技能「{name}」"

    archive_dir = SKILLS_DIR / ".archived"
    archive_dir.mkdir(exist_ok=True)
    dest = archive_dir / path.name
    path.rename(dest)
    _refresh_all_indices()
    return f"[成功] 归档技能「{name}」"


# ── Archive ──────────────────────────────────────────────────────────────────

def _write_skill_with_frontmatter(path: Path, meta: dict[str, Any], body: str) -> None:
    """写 frontmatter + body 到文件。"""
    fm = yaml.dump(meta, allow_unicode=True, default_flow_style=False)
    path.write_text(f"---\n{fm}---\n{body}", encoding="utf-8")


def _refresh_all_indices() -> None:
    """skill 变更后刷新所有相关索引。"""
    try:
        # Skill 索引
        from src.core.prompt_builder import _scan_skills_dir
        _scan_skills_dir.cache_clear()  # type: ignore[attr-defined]

        # Project / Info 缓存
        from src.core import prompt_builder
        if hasattr(prompt_builder, "_project_index"):
            proj = getattr(prompt_builder, "_project_index")
            if proj and hasattr(proj, "load_or_build"):
                proj.load_or_build()
        prompt_builder._info_index_cache = None

        # Tools 注册
        from src.core import skills_tools as skills_tools_reg
        from src.tools import session as session_tool
        current_session = session_tool.get_current_session()
        if current_session:
            skills_tools_reg.set_retrieval_indices(
                getattr(current_session, "skill_index", None),
                getattr(current_session, "project_index", None),
            )
    except Exception:
        pass


# ── 统一 Skill 入口 ──────────────────────────────────────────────────────────

def skill(params: dict[str, Any]) -> str:
    """统一 skill 工具入口。"""
    action = (params.get("action") or "").strip()
    if action == "view":
        return _run_skill_view(params)
    elif action == "search":
        return _run_skill_search(params)
    else:
        return "[错误] action 参数必须为 'view' 或 'search'"


def info(params: dict[str, Any]) -> str:
    """加载 info 知识文件内容。"""
    name = params.get("name", "").strip()
    if not name:
        return "[错误] info 需要 name 参数，例如：info(name=\"api-reference\")"
    from src.core.prompt_builder import load_info as _load_info
    return _load_info(name)


def search_projects(params: dict[str, Any]) -> str:
    """语义搜索项目，返回匹配的项目全文。"""
    query = params.get("query", "").strip()
    top_k = int(params.get("top_k", 2))
    if not query:
        return "[错误] query 参数不能为空"
    global _active_project_index
    if _active_project_index is None:
        return "[提示] 项目索引未初始化"
    try:
        results = _active_project_index.search(query, top_k=top_k)  # type: ignore[union-attr]
    except Exception as e:
        return f"[错误] 搜索项目失败：{e}"
    if not results:
        return "未找到匹配的项目。"
    return "\n\n---\n\n".join(results)


# ── 归档查询与恢复 ──────────────────────────────────────────────────────────

ARCHIVE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "archive",
        "description": "归档管理。action='list' 列出归档内容；action='restore' 从归档恢复指定项。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "restore"],
                    "description": "操作类型",
                },
                "category": {
                    "type": "string",
                    "enum": ["skill", "info", "project", "all"],
                    "description": "要查看或恢复的类别",
                },
                "name": {
                    "type": "string",
                    "description": "要恢复的名称（restore 时必填）",
                },
            },
            "required": ["action"],
        },
    },
}


def archive(params: dict[str, Any]) -> str:
    """归档管理：list / restore。"""
    action = (params.get("action") or "").strip()
    if action == "list":
        return _list_archives(params.get("category", "all"))
    elif action == "restore":
        return _restore_archive(params)
    else:
        return "[错误] action 必须是 'list' 或 'restore'"


def _list_archives(category: str) -> str:
    """列出归档内容。"""
    from src.core.config import PROJECTS_DIR, INFO_DIR
    parts = []
    if category in ("skill", "all"):
        archive_dir = SKILLS_DIR / ".archived"
        if archive_dir.is_dir():
            files = sorted(archive_dir.glob("*.md"))
            parts.append(f"### 已归档的技能 ({len(files)} 个)\n" + "\n".join(f"- {f.stem}" for f in files) if files else "(none)")
    if category in ("project", "all"):
        archive_dir = PROJECTS_DIR / ".archived"
        if archive_dir.is_dir():
            files = sorted(archive_dir.glob("*.md"))
            parts.append(f"### 已归档的项目 ({len(files)} 个)\n" + "\n".join(f"- {f.stem}" for f in files) if files else "(none)")
    if category in ("info", "all"):
        archive_dir = INFO_DIR / ".archived"
        if archive_dir.is_dir():
            files = sorted(archive_dir.glob("*.md"))
            parts.append(f"### 已归档的信息 ({len(files)} 个)\n" + "\n".join(f"- {f.stem}" for f in files) if files else "(none)")
    if not parts:
        return "没有归档内容。"
    return "\n\n".join(parts)


def _restore_archive(params: dict[str, Any]) -> str:
    """从归档恢复。"""
    from src.core.config import PROJECTS_DIR, INFO_DIR
    category = (params.get("category") or "").strip()
    name = (params.get("name") or "").strip()
    if not name:
        return "[错误] restore 需要 name 参数"
    if category == "skill":
        src = SKILLS_DIR / ".archived" / f"{name}.md"
        dest = SKILLS_DIR / f"{name}.md"
    elif category == "project":
        src = PROJECTS_DIR / ".archived" / f"{name}.md"
        dest = PROJECTS_DIR / f"{name}.md"
    elif category == "info":
        src = INFO_DIR / ".archived" / f"{name}.md"
        dest = INFO_DIR / f"{name}.md"
    else:
        return "[错误] category 必须是 skill / project / info"

    if not src.exists():
        return f"[错误] 归档中未找到「{name}」"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    _refresh_all_indices()
    return f"[成功] 恢复「{name}」到 {dest.parent}"


# ── Skill 快速创建（用户对话触发）────────────────────────────────────────────

QUICK_CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "quick_create_skill",
        "description": "快速创建技能（用户对话触发）。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["name", "description"],
        },
    },
}


def quick_create_skill(params: dict[str, Any]) -> str:
    """快速创建技能（用户对话触发）。"""
    name = params.get("name", "").strip()
    description = params.get("description", "").strip()
    body = params.get("body", "").strip()
    if not name or not description:
        return "[错误] name 和 description 必填"
    return _create_skill(name, description, body)

# 向后兼容别名
def list_archived(params: dict[str, Any]) -> str:
    """list_archives 的别名。"""
    return _list_archives(params.get("category", "all"))


def restore_archived(params: dict[str, Any]) -> str:
    """_restore_archive 的别名。"""
    return _restore_archive(params)
