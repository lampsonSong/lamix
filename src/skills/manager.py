"""技能管理器：扫描 ~/.lamix/memory/skills/，解析 skill 文件，提供加载和查询接口。

Skill 格式（平铺 md）：
    ~/.lamix/memory/skills/code-review.md
    ~/.lamix/memory/skills/code-review.md

    ---
    name: code-review
    description: 代码审查工作流
    ---
    ## 代码审查

    1. 看报错
    2. 看相关文件
    ...
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from src.core.config import SKILLS_DIR
BASE_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "default_skills"
# skill scripts（可执行 Python 代码）放这里，保持包结构
SKILL_SCRIPTS_DIR = SKILLS_DIR / "scripts"

# skill 文件 frontmatter 解析正则
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class Skill:
    """代表一个已加载的技能。"""

    def __init__(self, name: str, path: Path, meta: dict[str, Any], body: str) -> None:
        self.name = name
        self.path = path
        self.description: str = meta.get("description", "")
        self.body = body
        self.full_content = path.read_text(encoding="utf-8")


def _parse_skill_md(path: Path) -> Skill | None:
    """解析单个 skill md 文件，返回 Skill 对象；解析失败返回 None。"""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    match = _FRONTMATTER_RE.match(content)
    if not match:
        name = path.stem
        return Skill(name=name, path=path, meta={"description": ""}, body=content)

    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}

    body = content[match.end():]
    name = meta.get("name", path.stem)
    return Skill(name=name, path=path, meta=meta, body=body)


def load_all_skills() -> dict[str, Skill]:
    """扫描 base skills 和 user skills，加载并返回所有技能。

    加载顺序：先 base（随仓库），再 user（~/.lamix/memory/skills/）。
    同名 skill 以 user 版本为准（覆盖 base）。
    """
    skills: dict[str, Skill] = {}

    # 1. 加载 base skills（只读，随仓库版本）
    if BASE_SKILLS_DIR.exists():
        for skill_md in sorted(BASE_SKILLS_DIR.rglob("*.md")):
            skill = _parse_skill_md(skill_md)
            if skill:
                skill._source = "base"  # type: ignore[attr-defined]
                skills[skill.name] = skill

    # 2. 加载 user skills（可写，覆盖同名 base）
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for skill_md in sorted(SKILLS_DIR.rglob("*.md")):
        skill = _parse_skill_md(skill_md)
        if skill:
            skill._source = "user"  # type: ignore[attr-defined]
            skills[skill.name] = skill

    return skills


def get_skills_summary(skills: dict[str, Skill]) -> str:
    """生成技能概要，供 system prompt 注入。"""
    if not skills:
        return ""
    lines = ["已加载技能："]
    for skill in skills.values():
        desc = skill.description or "(无描述)"
        lines.append(f"- **{skill.name}**: {desc}")
    return "\n".join(lines)


def list_skills(skills: dict[str, Skill]) -> str:
    """返回技能列表文本，供 /skills list 展示。"""
    if not skills:
        return "暂无已安装的技能。"
    lines = ["已安装技能："]
    for skill in skills.values():
        desc = skill.description or "(无描述)"
        lines.append(f"  - {skill.name}: {desc}")
    return "\n".join(lines)


def show_skill(name: str, skills: dict[str, Skill]) -> str:
    """返回技能详情，供 /skills show <name> 展示。"""
    skill = skills.get(name)
    if not skill:
        return f"未找到技能：{name}"
    return skill.full_content


def create_skill(name: str, description: str = "") -> str:
    """在 SKILLS_DIR 中创建新的 skill 文件（子目录 SKILL.md 格式）。"""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-]", "_", name)
    skill_dir = SKILLS_DIR / safe_name
    skill_path = skill_dir / "SKILL.md"
    if skill_path.exists():
        return f"技能 '{name}' 已存在：{skill_path}"

    skill_dir.mkdir(exist_ok=True)
    content = f"""---
name: {name}
description: {description or name + ' 技能'}
---

## {name}

### 描述
{description or '在此填写技能描述。'}

### 步骤
1. 步骤一
2. 步骤二
3. 步骤三

### 注意事项
- 注意事项一
"""
    skill_path.write_text(content, encoding="utf-8")
    return f"已创建技能 '{name}'：{skill_path}"


def install_default_skills(default_skills_dir: Path) -> None:
    """将 config/default_skills/ 中的平铺 .md 技能文件复制到 SKILLS_DIR（如果不存在）。"""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    if not default_skills_dir.exists():
        return
    for skill_file in default_skills_dir.glob("*.md"):
        if not skill_file.is_file():
            continue
        dest = SKILLS_DIR / skill_file.name
        if not dest.exists():
            shutil.copy2(str(skill_file), str(dest))


# ── Consolidation ─────────────────────────────────────────────────────────────

@dataclass
class ConsolidationAction:
    """一次合并操作。"""

    keep: str  # 保留的 skill 名称
    delete: list[str]  # 要删除的 skill 名称
    merged_body: str  # 合并后的正文
    keep_invocation_count: int  # 取要合并的 skills 中最大的 invocation_count


def consolidate_skills(skills: dict[str, Skill], llm_client: Any) -> tuple[list[ConsolidationAction], str]:
    """让 LLM 分析所有 skills 的重复/耦合，返回合并建议列表和原始分析文本。

    返回 (actions, analysis)：actions 是可执行的合并操作列表；analysis 是 LLM 的分析文本。
    如果 skills 数量 < 2，直接返回空列表。
    """
    if len(skills) < 2:
        return [], ""

    # 构造 skill 摘要供 LLM 分析
    lines: list[str] = []
    for name, s in skills.items():
        lines.append(
            f"### {name}\n"
            f"description: {s.description}\n"
            f"invocation_count: {s.path.exists() and _get_invocation_count(s.path) or 0}\n"
            f"---\n"
            f"{s.body[:500]}"
        )

    skills_text = "\n\n".join(lines)

    prompt = (
        "你是一个技能库管理员，负责找出重复或功能重叠的技能并提出合并方案。\n\n"
        "## 判断标准\n"
        "- **A 包含 B**：如果 skill A 的正文几乎覆盖了 skill B 的所有功能点，B 应该被删除，A 保留并补充 B 的独特部分到正文。\n"
        "- **同功能不同实现**：如果 A 和 B 做的是同一件事但实现方式不同，合并成一个，用更好的描述。\n"
        "- **各自独立**：如果两个 skill 关注点完全不同，保留各自。\n\n"
        "## 当前 Skills\n\n"
        f"{skills_text}\n\n"
        "## 输出格式\n"
        "请仔细分析，输出以下 JSON 格式（只输出 JSON，不要其他文字）：\n\n"
        "```json\n"
        "{\n"
        '  "analysis": "你的分析说明（2-5句话），解释为什么这些技能需要或不需要合并",\n'
        '  "actions": [\n'
        '    {\n'
        '      "keep": "保留的 skill 名称",\n'
        '      "delete": ["要删除的 skill 名称列表"],\n'
        '      "merged_body": "合并后的完整正文（包含被删除 skill 的有价值内容）",\n'
        '      "keep_invocation_count": 保留的 invocation_count 值（取被合并 skills 中最大的）\n'
        '    }\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        "如果不需要任何合并，输出：\n"
        "```json\n"
        '{"analysis": "...", "actions": []}\n'
        "```"
    )

    try:
        from src.core.llm import LLMClient
        client = LLMClient(
            api_key=llm_client.client.api_key,
            base_url=str(llm_client.client.base_url),
            model=llm_client.model,
        )
        client.set_system_context()
        client.add_user_message(prompt)
        response = client.chat()
    except Exception as e:
        return [], f"[错误] 调用 LLM 失败：{e}"

    raw = (response.choices[0].message.content or "").strip()

    # 提取 JSON
    try:
        import json

        # 找 ```json ... ``` 或直接 {...}
        json_start = raw.find("```json")
        if json_start != -1:
            json_end = raw.find("```", json_start + 6)
            json_str = raw[json_start + 7 : json_end].strip()
        else:
            json_start = raw.find("{")
            json_end = raw.rfind("}") + 1
            json_str = raw[json_start:json_end]

        data = json.loads(json_str)
    except Exception:
        return [], f"[错误] 解析 LLM 返回失败：\n{raw}"

    analysis = data.get("analysis", "")
    raw_actions: list[dict] = data.get("actions", [])
    actions: list[ConsolidationAction] = []

    for raw_a in raw_actions:
        keep = str(raw_a.get("keep", ""))
        delete = [str(d) for d in raw_a.get("delete", [])]
        merged_body = str(raw_a.get("merged_body", ""))
        keep_ic = int(raw_a.get("keep_invocation_count", 0))

        if keep and keep in skills:
            actions.append(
                ConsolidationAction(
                    keep=keep,
                    delete=delete,
                    merged_body=merged_body,
                    keep_invocation_count=keep_ic,
                )
            )

    return actions, analysis


def _get_invocation_count(path: Path) -> int:
    """读取 skill 文件的 invocation_count。"""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return 0
    try:
        meta = yaml.safe_load(match.group(1)) or {}
        return int(meta.get("invocation_count", 0))
    except (yaml.YAMLError, ValueError, TypeError):
        return 0


def _write_skill(path: Path, name: str, body: str, invocation_count: int, description: str) -> None:
    """写回 skill 文件（保留 frontmatter，更新 body 和 invocation_count）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raw = ""

    match = _FRONTMATTER_RE.match(raw)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        old_body = raw[match.end() :]
    else:
        meta = {}
        old_body = raw

    # 更新 meta
    meta["name"] = name
    meta["description"] = description
    meta["invocation_count"] = invocation_count
    if "created_at" not in meta:
        meta["created_at"] = date.today().isoformat()

    dump = yaml.dump(meta, allow_unicode=True, default_flow_style=False).strip()
    path.write_text(f"---\n{dump}\n---\n{body}", encoding="utf-8")


def execute_consolidation(actions: list[ConsolidationAction]) -> str:
    """执行合并操作：删被合并的 skill，更新保留的 skill 文件。"""
    if not actions:
        return "没有需要合并的技能。"

    lines: list[str] = []
    for action in actions:
        keep_path = SKILLS_DIR / f"{action.keep}.md"

        if not keep_path.is_file():
            lines.append(f"[跳过] 保留的 skill '{action.keep}' 文件不存在")
            continue

        # 获取当前 skill 的 description
        try:
            raw = keep_path.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        match = _FRONTMATTER_RE.match(raw)
        if match:
            try:
                meta = yaml.safe_load(match.group(1)) or {}
            except yaml.YAMLError:
                meta = {}
        else:
            meta = {}
        description = meta.get("description", "")

        # 收集被合并 skill 的 description（去重追加）
        for del_name in action.delete:
            del_path = SKILLS_DIR / f"{del_name}.md"
            if del_path.is_file():
                try:
                    del_raw = del_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                del_match = _FRONTMATTER_RE.match(del_raw)
                if del_match:
                    try:
                        del_meta = yaml.safe_load(del_match.group(1)) or {}
                    except yaml.YAMLError:
                        del_meta = {}
                    del_desc = del_meta.get("description", "")
                    if del_desc and del_desc != description:
                        description = f"{description} / {del_desc}"

        # 写回
        _write_skill(
            keep_path,
            name=action.keep,
            body=action.merged_body,
            invocation_count=action.keep_invocation_count,
            description=description,
        )

        # 删除被合并的 skill
        deleted_names: list[str] = []
        for del_name in action.delete:
            del_flat = SKILLS_DIR / f"{del_name}.md"
            if del_flat.is_file():
                del_flat.unlink()
                deleted_names.append(del_name)

        lines.append(f"合并完成：保留 '{action.keep}'，删除 {deleted_names}，invocation_count={action.keep_invocation_count}")

    return "\n".join(lines)
