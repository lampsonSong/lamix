"""分层 System Prompt 构建器。

Lamix system prompt 分层加载：

Layer 1    Identity        - MEMORY.md（Agent 人格与行为准则）
Layer 1.5  User            - USER.md（用户画像与偏好，多用户基础）
Layer 2    Tool Guidance   - 记忆指引 + Skills 索引 + 工具使用规范
Layer 3    Project Index   - 动态扫描 projects/*.md 生成项目列表
Layer 4    Model Guidance  - 模型适配指引（如 GLM tool_calls 提示）
Layer 5    Channel Context - 消息来源标识（非 CLI 时注入）
"""
from __future__ import annotations

import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

import yaml
import logging
logger = logging.getLogger(__name__)


from src.core.config import LAMIX_DIR, SKILLS_DIR, PROJECTS_DIR, INFO_DIR

MEMORY_PATH = LAMIX_DIR / "MEMORY.md"
USER_PATH = LAMIX_DIR / "USER.md"

# 配置文件默认模板路径（仓库内）
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_DEFAULT_IDENTITY_PATH = _CONFIG_DIR / "default_memory.md"
_DEFAULT_USER_PATH = _CONFIG_DIR / "default_user.md"

# ── Tool Guidance 常量 ────────────────────────────────────────────────────────

MEMORY_GUIDANCE = (
    "你的记忆分为三层：\n"
    "- **skills**：工作流程，可重复的操作行为（如'代码审查'、'部署服务'）\n"
    "- **info**：零散信息，密码、key、地址、IP 等随时要查的东西\n"
    "- **projects**：归类到具体项目的所有上下文\n"
    "\n"
    "三层内容可能重叠，没关系，重复不影响，按需加载即可。优先记能减少未来重复沟通的内容。\n"
    "\n"
    "用户的性格、称呼、爱好、偏好、纠错等个人特征写在 ~/.lamix/USER.md。\n"
    "服务器地址、账号密码、API Key 等技术信息不要写 USER.md，写 info 目录。\n"
)

ACTION_GUIDANCE = (
    "完成任务后考虑沉淀为 skill；过时的 skill 及时更新。\n"
    "执行任务时用工具行动，不要只描述意图；聊天/确认等场景直接回复即可。"
)

# ── Frontmatter 解析 ─────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """解析 YAML frontmatter，返回 (meta_dict, body)。"""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    body = content[match.end():]
    return meta, body


# ── Skills 索引构建 ──────────────────────────────────────────────────────────

_skills_index_cache: tuple[frozenset[tuple[str, float]], str] | None = None


def write_skill_with_frontmatter(path: Path, meta: dict[str, Any], body: str) -> None:
    """将 YAML frontmatter + 正文写回 skill 文件（正文不变，仅更新 meta 时用）。"""
    dump = yaml.dump(meta, allow_unicode=True, default_flow_style=False).strip()
    path.write_text(f"---\n{dump}\n---\n{body}", encoding="utf-8")


def _ensure_skill_index_fields(path: Path) -> None:
    """若缺少 created_at / invocation_count，补填并写回（已有字段不覆盖）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    if not raw.strip():
        return
    meta, body = _parse_frontmatter(raw)
    changed = False
    today = date.today().isoformat()
    if "created_at" not in meta or meta.get("created_at") in (None, ""):
        meta["created_at"] = today
        changed = True
    if "invocation_count" not in meta:
        meta["invocation_count"] = 0
        changed = True
    if not changed:
        return
    write_skill_with_frontmatter(path, meta, body)


# ── 双层 Skill 结构 ─────────────────────────────────────────────────────────

_SKILL_ENTRY_FILE = "SKILL.md"


def _scan_skills_dir() -> list[dict[str, Any]]:
    """扫描 skills/ 目录，返回所有 skill 条目。

    双层优先：skills/<name>/SKILL.md
    向后兼容：skills/<name>.md（等价于 skills/<name>/SKILL.md）
    """
    if not SKILLS_DIR.exists():
        return []

    entries: list[dict[str, Any]] = []

    # 优先：双层目录结构 skills/<name>/SKILL.md
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        if skill_dir.name.startswith("."):
            continue
        entry_file = skill_dir / _SKILL_ENTRY_FILE
        if entry_file.is_file():
            _ensure_skill_index_fields(entry_file)
            sub_items = _collect_skill_sub_items(skill_dir)
            entries.append({
                "name": skill_dir.name,
                "path": entry_file,
                "sub_items": sub_items,
                "is_legacy": False,
            })

    # 向后兼容：平铺单文件 skills/<name>.md
    for md_file in sorted(SKILLS_DIR.rglob("*.md")):
        if ".archived" in str(md_file):
            continue
        # 如果同名目录已存在，跳过（双层优先）
        dir_path = SKILLS_DIR / md_file.stem
        if dir_path.is_dir():
            continue
        _ensure_skill_index_fields(md_file)
        entries.append({
            "name": md_file.stem,
            "path": md_file,
            "sub_items": [],
            "is_legacy": True,
        })

    return entries


def _collect_skill_sub_items(skill_dir: Path) -> dict[str, list[str]]:
    """收集 skill 目录下各子目录的文件清单。"""
    sub_items: dict[str, list[str]] = {}
    for subdir_name in ("references", "templates", "scripts", "assets"):
        subdir = skill_dir / subdir_name
        if not subdir.is_dir():
            continue
        files: list[str] = []
        if subdir_name == "scripts":
            files = sorted(f.name for f in subdir.glob("*.py") if not f.name.startswith("_"))
        elif subdir_name == "assets":
            files = sorted(f.name for f in subdir.iterdir() if f.is_file())
        else:
            files = sorted(f.stem for f in subdir.glob("*.md"))
        if files:
            sub_items[subdir_name] = files
    return sub_items


def _skill_entry_fingerprint(entries: list[dict[str, Any]]) -> frozenset[tuple[str, float]]:
    """生成 skill 条目的 mtime 指纹（用于缓存失效检测）。"""
    items: list[tuple[str, float]] = []
    for e in entries:
        try:
            items.append((str(e["path"].resolve()), e["path"].stat().st_mtime))
            # 子目录也加入指纹
            skill_dir = e["path"].parent
            for subdir in skill_dir.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("."):
                    for f in subdir.iterdir():
                        try:
                            items.append((str(f.resolve()), f.stat().st_mtime))
                        except OSError:
                            pass
        except OSError:
            items.append((str(e["path"]), 0.0))
    return frozenset(items)


def _skills_mtime_fingerprint(paths: list[Path]) -> frozenset[tuple[str, float]]:
    items: list[tuple[str, float]] = []
    for p in paths:
        try:
            items.append((str(p.resolve()), p.stat().st_mtime))
        except OSError:
            items.append((str(p), 0.0))
    return frozenset(items)


def build_skills_index() -> str:
    """扫描 skills/ 目录，生成注入 system prompt 的技能目录块。

    双层结构优先：skills/<name>/SKILL.md
    向后兼容平铺：skills/<name>.md
    """
    global _skills_index_cache
    entries = _scan_skills_dir()
    if not entries:
        _skills_index_cache = (frozenset(), "")
        return ""
    key = _skill_entry_fingerprint(entries)
    if _skills_index_cache is not None and _skills_index_cache[0] == key:
        return _skills_index_cache[1]

    lines: list[str] = [
        "## Skills（按需加载）",
        "以下是你已掌握的技能目录，每项包含名称和描述。",
        "**强制规则**：执行任何编码、调试、部署、代码审查等任务前，必须先调用 skill(action='view', name='对应技能名') 加载工作流全文，按工作流执行。不允许跳过这一步直接开始工作。",
        "如果任务不涉及任何已列出的技能，直接回答即可。",
        "",
    ]
    for e in entries:
        path = e["path"]
        sub_items = e["sub_items"]
        try:
            raw_file = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, _body = _parse_frontmatter(raw_file)
        if meta:
            name = str(meta.get("name", "") or path.stem)
            desc = str(meta.get("description", ""))
        else:
            name = e["name"]
            desc = ""

        # 子项摘要
        if sub_items:
            parts = []
            for subdir, items in sub_items.items():
                parts.append(f"{subdir}/[{len(items)}]")
            sub_label = f" ({', '.join(parts)})"
        else:
            sub_label = ""

        lines.append(f"- **{name}**: {desc}{sub_label}")

    text = "\n".join(lines)
    _skills_index_cache = (key, text)
    return text


# ── 遗留：保持旧的 glob 路径函数（供外部如 indexer 调用） ──────────────────


def _skill_md_paths_under_skills() -> list[Path]:
    """返回所有 skill 主文件的路径（SKILL.md 优先，否则 .md）。"""
    if not SKILLS_DIR.exists():
        return []
    out: list[Path] = []
    for e in _scan_skills_dir():
        out.append(e["path"])
    return out


# ── Project Index & Context ───────────────────────────────────────────────────

_projects_index_cache: tuple[frozenset[tuple[str, float]], str] | None = None


def _projects_mtime_fingerprint() -> frozenset[tuple[str, float]]:
    if not PROJECTS_DIR.exists():
        return frozenset()
    items: list[tuple[str, float]] = []
    for p in PROJECTS_DIR.glob("*.md"):
        try:
            items.append((str(p.resolve()), p.stat().st_mtime))
        except OSError:
            items.append((str(p), 0.0))
    return frozenset(items)


def _extract_project_info(path: Path) -> tuple[str, str]:
    """从 project md 文件提取项目名和一句话描述。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return path.stem, ""

    # 取第一行作为名字（去掉 # 号）
    name = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            name = stripped[2:].strip()
            break
    if not name:
        name = path.stem

    # 用 frontmatter 解析器跳过 meta，从 body 取第一段描述
    _meta, body = _parse_frontmatter(raw)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("|"):
            return name, stripped
    return name, ""


def build_project_index() -> str:
    """扫描 ~/.lamix/memory/projects/*.md，生成项目索引。"""
    global _projects_index_cache
    if not PROJECTS_DIR.exists():
        _projects_index_cache = (frozenset(), "")
        return ""

    key_before = _projects_mtime_fingerprint()
    if _projects_index_cache is not None and _projects_index_cache[0] == key_before:
        return _projects_index_cache[1]

    lines: list[str] = [
        "## Projects（按需加载）",
        "当用户提到或暗示与某个项目相关时，",
        "使用 project_context(name=\"项目名\") 加载完整上下文。",
        "",
    ]
    for p in sorted(PROJECTS_DIR.glob("*.md")):
        name, desc = _extract_project_info(p)
        lines.append(f"- **{name}**: {desc}")

    text = "\n".join(lines)
    _projects_index_cache = (key_before, text)
    return text


def _update_project_last_used(path: Path, date_str: str) -> None:
    """更新 project 文件的 last_used_at（写入第一行注释）。"""
    try:
        raw = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        meta["last_used_at"] = date_str
        import yaml
        fm = yaml.dump(meta, allow_unicode=True, default_flow_style=False).strip()
        path.write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")
    except Exception:
        pass


def load_project_context(name: str) -> str:
    """加载指定项目的完整上下文（projects/xxx.md 内容）。"""
    if not name:
        return "project_context 需要 name 参数，例如：project_context(name=\"Lamix\")"

    if not PROJECTS_DIR.exists():
        return f"[项目目录不存在：{PROJECTS_DIR}]"

    # 精确匹配
    for md_file in PROJECTS_DIR.glob("*.md"):
        if md_file.stem.lower() == name.lower():
            try:
                content = md_file.read_text(encoding="utf-8").strip()
                if content:
                    # 更新 last_used_at
                    from datetime import date
                    _update_project_last_used(md_file, str(date.today()))
                    return f"# {md_file.stem}\n\n{content}"
            except OSError:
                pass

    # 模糊匹配
    for md_file in PROJECTS_DIR.glob("*.md"):
        if name.lower() in md_file.stem.lower():
            try:
                content = md_file.read_text(encoding="utf-8").strip()
                if content:
                    from datetime import date
                    _update_project_last_used(md_file, str(date.today()))
                    return f"# {md_file.stem}\n\n{content}"
            except OSError:
                pass

    available = [f.stem for f in PROJECTS_DIR.glob("*.md")]
    avail_str = ", ".join(available) if available else "(none)"
    return f"[项目 '{name}' not found]\n\nAvailable projects: {avail_str}"


# ── Info Index ────────────────────────────────────────────────────────────────

_info_index_cache: tuple[frozenset[tuple[str, float]], str] | None = None


def _info_mtime_fingerprint() -> frozenset[tuple[str, float]]:
    if not INFO_DIR.exists():
        return frozenset()
    items: list[tuple[str, float]] = []
    for p in INFO_DIR.glob("*.md"):
        try:
            items.append((str(p.resolve()), p.stat().st_mtime))
        except OSError:
            items.append((str(p), 0.0))
    return frozenset(items)


def build_info_index() -> str:
    """扫描 ~/.lamix/memory/info/*.md，生成信息索引。"""
    global _info_index_cache
    if not INFO_DIR.exists():
        _info_index_cache = (frozenset(), "")
        return ""

    key_before = _info_mtime_fingerprint()
    if _info_index_cache is not None and _info_index_cache[0] == key_before:
        return _info_index_cache[1]

    lines: list[str] = [
        "## Info（按需加载）",
        "以下是你已记录的知识性信息，可通过 info(name=\"文件名\") 加载。",
        "",
    ]
    for p in sorted(INFO_DIR.glob("*.md")):
        try:
            meta, body = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        if meta:
            name = str(meta.get("name", "") or p.stem)
            desc = str(meta.get("description", ""))
        else:
            name = p.stem
            # 取第一段非空内容作为描述
            desc = ""
            for line in p.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    desc = stripped
                    break
        lines.append(f"- **{name}**: {desc}")

    text = "\n".join(lines)
    _info_index_cache = (key_before, text)
    return text


def load_info(name: str) -> str:
    """加载 info/xxx.md 内容。"""
    if not name:
        return "info 需要 name 参数，例如：info(name=\"api-reference\")"

    if not INFO_DIR.exists():
        return f"[info 目录不存在：{INFO_DIR}]"

    # 精确匹配
    for md_file in INFO_DIR.glob("*.md"):
        if md_file.stem.lower() == name.lower():
            try:
                content = md_file.read_text(encoding="utf-8").strip()
                # 去掉 frontmatter
                _meta, body = _parse_frontmatter(content)
                if body.strip():
                    return body.strip()
                return content
            except OSError:
                pass

    # 模糊匹配
    for md_file in INFO_DIR.glob("*.md"):
        if name.lower() in md_file.stem.lower():
            try:
                content = md_file.read_text(encoding="utf-8").strip()
                _meta, body = _parse_frontmatter(content)
                if body.strip():
                    return body.strip()
                return content
            except OSError:
                pass

    available = [f.stem for f in INFO_DIR.glob("*.md")]
    avail_str = ", ".join(available) if available else "(none)"
    return f"[info '{name}' not found]\n\nAvailable: {avail_str}"


def write_info_with_frontmatter(path: Path, meta: dict, body: str) -> None:
    """将 YAML frontmatter + 正文写入 info 文件。"""
    fm = yaml.dump(meta, allow_unicode=True, default_flow_style=False).strip()
    path.write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


# ── Tool Groups 索引 ────────────────────────────────────────────────────────

def build_tool_groups_index() -> str:
    """构建未激活工具组的索引（注入 system prompt）。"""
    from src.core import tools as tool_registry
    inactive = tool_registry.get_inactive_group_profiles()
    if not inactive:
        return ""
    lines: list[str] = [
        "## Tool Groups（按需激活）",
        "以下工具组当前未加载。当你判断任务需要相关能力时，调用 activate_tool_group(name=\"组名\") 激活，激活后该组所有工具即可使用。",
        "",
    ]
    for name, desc in inactive.items():
        lines.append(f"- **{name}**: {desc}")
    return "\n".join(lines)


def _read_config_template(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_identity() -> str:
    """加载 ~/.lamix/MEMORY.md，不存在则用 config/default_memory.md。"""
    if MEMORY_PATH.exists():
        try:
            return MEMORY_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return _read_config_template(_DEFAULT_IDENTITY_PATH)


# ── User ──────────────────────────────────────────────────────────────────────

# 硬编码的默认值（不可用户配置）
_IDENTITY_DEFAULTS = {
    "你是 Lamix。",
    "你是 Lamix",
    "你是 lamix。",
    "你是 lamix",
}

_fallback_identity = "\n".join([
    "你是 Lamix，一个 CLI 智能助手。",
    "你擅长自动化、代码、数据分析和系统运维。",
    "你的记忆分为三层：skills（工作流程）、info（零散信息）、projects（项目上下文）。",
    "三层内容可能重叠，没关系，重复不影响，按需加载即可。优先记能减少未来重复沟通的内容。",
    "用户的性格、称呼、爱好、偏好、纠错等个人特征写在 ~/.lamix/USER.md。\n"
    "服务器地址、账号密码、API Key 等技术信息不要写 USER.md，写 info 目录。\n"
    "完成任务后考虑沉淀为 skill；执行任务时用工具行动，不要只描述意图。",
])


def load_user() -> str:
    """加载 ~/.lamix/USER.md，不存在则从模板复制后读取。

    超过 2000 字符时，内容仍会加载（不截断），但会在头部插入警告，
    同时尝试通过飞书通知用户处理。
    """
    _ensure_user_file()
    if not USER_PATH.exists():
        return ""
    try:
        content = USER_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return ""
        if len(content) > USER_MD_MAX_LENGTH:
            warning = (
                f"⚠ USER.md 已达 {len(content)} 字符（上限 {USER_MD_MAX_LENGTH}），"
                "请精简内容，删除过时或冗余条目。"
            )
            # 尝试飞书通知（daemon 场景）
            _notify_user_md_oversize(len(content))
            return warning + "\n\n" + content
        return content
    except OSError:
        return ""


def _notify_user_md_oversize(length: int) -> None:
    """USER.md 超长时通过当前渠道通知用户。"""
    warning = f"⚠ USER.md 已达 {length} 字符（上限 {USER_MD_MAX_LENGTH}），请精简内容。"
    try:
        from src.tools import session as session_tool
        current_session = session_tool.get_current_session()
        if current_session and current_session.partial_sender:
            current_session.partial_sender(warning)
            return
    except Exception:
        pass
    # fallback: 打印到 stderr（CLI 场景）
    import sys
    print(warning, file=sys.stderr)


def build_model_guidance(model: str) -> list[str]:
    """根据模型返回对应指引（目前仅做占位符扩展）。"""
    # TODO: 可扩展为按模型注入特定提示，如 GLM 的 tool_calls 格式要求
    return []


def _ensure_user_file() -> None:
    """首次运行时将 default_user.md 模板复制为 ~/.lamix/USER.md。"""
    if USER_PATH.exists():
        return
    default = _read_config_template(_DEFAULT_USER_PATH)
    if not default:
        return
    try:
        USER_PATH.write_text(default, encoding="utf-8")
    except OSError:
        pass


USER_MD_MAX_LENGTH = 2000  # USER.md 最大字符数


# ── PromptBuilder ────────────────────────────────────────────────────────────

class PromptBuilder:
    """分层构建 system prompt。"""

    def __init__(self, model: str = "", channel: str = "cli") -> None:
        self.model = model
        self.channel = channel

    def build(self) -> str:
        """按层级拼装 system prompt。"""
        layers: list[str] = []

        # L1: Identity
        layers.append(load_identity())

        # L1.5: User
        user_block = load_user()
        if user_block.strip():
            layers.append(f"## 用户\n\n{user_block.strip()}")

        # L2: Tool guidance
        l2: list[str] = [MEMORY_GUIDANCE]
        skills_block = build_skills_index()
        if skills_block.strip():
            l2.append(skills_block)
        l2.append(ACTION_GUIDANCE)
        # Tool Groups 索引（仅未激活的组）
        tool_groups_block = build_tool_groups_index()
        if tool_groups_block:
            l2.append(tool_groups_block)
        layers.extend(l2)

        # L3: Project index
        project_index = build_project_index()
        if project_index:
            layers.append(project_index)

        # L3.5: Info index
        info_index = build_info_index()
        if info_index:
            layers.append(info_index)

        # L4: Model guidance
        layers.extend(build_model_guidance(self.model))

        # L5: Channel Context
        if self.channel != "cli":
            layers.append("# Channel Context\n\n当前消息来源: " + self.channel)

        return "\n\n".join(layer for layer in layers if layer.strip())


# ── 一次性清理旧 index 文件（2025-05 前无 .index_cache）────────────────────
def _cleanup_old_index_files():
    """删除旧的 .index 文件（已被 frozenset 缓存取代）。"""
    for dir_path in (SKILLS_DIR, PROJECTS_DIR, INFO_DIR):
        if not dir_path.exists():
            continue
        for f in dir_path.glob("*.index"):
            try:
                f.unlink()
            except OSError:
                pass
