"""审计扫描器：skills / projects / skill scripts / user patterns。"""

from __future__ import annotations

import re

from src.core.config import LAMIX_DIR, SKILLS_DIR, PROJECTS_DIR
from src.core.self_audit.models import AuditFinding, logger


def scan_skills(auto_fix: bool = False) -> list[AuditFinding]:
    """扫描所有 skills（平铺 .md 格式 + 子目录 SKILL.md 格式），返回审计发现列表。

    注意：skill 的触发逻辑已改为 LLM 判断，不再检查 triggers 字段。
    auto_fix=True 时：缺少 frontmatter 自动生成（name 从 path.stem 取）。
    """
    findings: list[AuditFinding] = []

    if not SKILLS_DIR.exists():
        return findings

    for skill_file in SKILLS_DIR.glob("**/*.md"):
        if ".archived" in skill_file.parts or skill_file.name.startswith("."):
            continue

        try:
            raw = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue

        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
        if not fm_match:
            name = skill_file.stem
            if auto_fix:
                body_lines = raw.strip().splitlines()
                first_line = ""
                for line in body_lines:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        first_line = stripped
                        break
                    elif stripped.startswith("#"):
                        first_line = stripped.lstrip("# ").strip()
                        break
                if not first_line:
                    first_line = name
                fm_block = f"---\nname: {name}\ndescription: {first_line}\n---\n"
                new_content = fm_block + raw
                skill_file.write_text(new_content, encoding="utf-8")
                raw = new_content
                fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
                findings.append(AuditFinding(
                    severity="warning",
                    category="skill",
                    target=name,
                    message="skill 文件缺少 frontmatter",
                    suggestion="添加 YAML frontmatter（name、description）",
                    fixed=True,
                    fix_detail=f"已自动生成 frontmatter（name={name}）",
                ))
            else:
                findings.append(AuditFinding(
                    severity="warning",
                    category="skill",
                    target=name,
                    message="skill 文件缺少 frontmatter",
                    suggestion="添加 YAML frontmatter（name、description）",
                ))
            body = raw
        else:
            name = skill_file.stem
            try:
                import yaml
                meta = yaml.safe_load(fm_match.group(1)) or {}
                if not meta.get("name"):
                    findings.append(AuditFinding(
                        severity="info",
                        category="skill",
                        target=name,
                        message="frontmatter 缺少 name 字段",
                    ))
                if not meta.get("description"):
                    findings.append(AuditFinding(
                        severity="info",
                        category="skill",
                        target=name,
                        message="frontmatter 缺少 description 字段",
                    ))
            except yaml.YAMLError as e:
                findings.append(AuditFinding(
                    severity="error",
                    category="skill",
                    target=name,
                    message=f"frontmatter YAML 解析失败: {e}",
                    suggestion="修复 frontmatter 格式",
                ))
            body = raw[fm_match.end():]

        if len(body.strip()) < 50:
            findings.append(AuditFinding(
                severity="warning",
                category="skill",
                target=name,
                message="正文内容过短（<50 字符），可能是不完整的 skill",
                suggestion="补充完整的步骤描述和注意事项",
            ))

        if "步骤一" in body and "步骤二" in body and "步骤三" in body:
            findings.append(AuditFinding(
                severity="warning",
                category="skill",
                target=name,
                message="正文仍为模板占位内容（步骤一/步骤二/步骤三），未填写实际内容",
                suggestion="替换为具体的操作步骤",
            ))

    return findings


def scan_skill_overlap() -> list[AuditFinding]:
    """检测 skill 之间的职责重叠（平铺 .md 格式）。

    逻辑：
    1. 收集所有 skill 的 (name, description)
    2. 对每对 skill，用关键词重叠度判断是否职责重叠
    3. 仅在 description 有高度重叠（>60% 的词相同）时报告
    """
    findings: list[AuditFinding] = []

    if not SKILLS_DIR.exists():
        return findings

    _EN_STOP_WORDS = frozenset({
        "the", "a", "is", "for", "to", "of", "and", "in", "on", "with", "at",
        "an", "or", "it", "be", "as", "by", "this", "that", "are", "was",
    })

    def _extract_keywords(text: str) -> set[str]:
        keywords: set[str] = set()
        en_words = re.findall(r"[a-zA-Z]+", text)
        for w in en_words:
            w_lower = w.lower()
            if len(w_lower) >= 2 and w_lower not in _EN_STOP_WORDS:
                keywords.add(w_lower)
        chinese_text = re.sub(r"[a-zA-Z0-9\s\-_/\\.,;:!?(){}[\]\"'`~@#$%^&*+=|<>]", " ", text)
        if chinese_text.strip():
            try:
                import jieba
                for word in jieba.cut(chinese_text):
                    word = word.strip()
                    if len(word) >= 2:
                        keywords.add(word)
            except ImportError:
                for seg in re.findall(r"[一-鿿]{2,}", chinese_text):
                    keywords.add(seg)
        return keywords

    skill_infos: list[tuple[str, str, set[str]]] = []

    for skill_file in SKILLS_DIR.glob("**/*.md"):
        if ".archived" in skill_file.parts or skill_file.name.startswith("."):
            continue
        try:
            raw = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue

        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
        if not fm_match:
            continue
        try:
            import yaml
            meta = yaml.safe_load(fm_match.group(1)) or {}
        except Exception:
            continue

        sname = skill_file.parent.name if skill_file.parent != SKILLS_DIR else skill_file.stem
        name = meta.get("name", sname)
        description = meta.get("description", "")
        if not description:
            continue

        kws = _extract_keywords(description)
        if kws:
            skill_infos.append((name, description, kws))

    for i in range(len(skill_infos)):
        for j in range(i + 1, len(skill_infos)):
            name_a, desc_a, kws_a = skill_infos[i]
            name_b, desc_b, kws_b = skill_infos[j]

            overlap = kws_a & kws_b
            if len(overlap) <= 3:
                continue

            smaller = min(len(kws_a), len(kws_b))
            if smaller == 0:
                continue
            overlap_ratio = len(overlap) / smaller
            if overlap_ratio <= 0.6:
                continue

            findings.append(AuditFinding(
                severity="warning",
                category="skill",
                target=f"{name_a} / {name_b}",
                message=f"两个 skill 的 description 存在高度职责重叠（重叠词 {len(overlap)} 个，"
                        f"重叠率 {overlap_ratio:.0%}）：{', '.join(sorted(overlap)[:10])}",
                suggestion=f"建议检查 [{name_a}] 和 [{name_b}] 是否可以合并",
            ))

    return findings


def scan_projects(auto_fix: bool = False) -> list[AuditFinding]:
    """扫描所有 projects，返回审计发现列表。"""
    from datetime import date, datetime

    findings: list[AuditFinding] = []

    if not PROJECTS_DIR.exists():
        return findings

    for project_md in PROJECTS_DIR.glob("*.md"):
        try:
            content = project_md.read_text(encoding="utf-8")
        except OSError:
            continue

        name = project_md.stem

        lines = content.splitlines()
        if not lines:
            findings.append(AuditFinding(
                severity="error",
                category="project",
                target=name,
                message="文件为空",
            ))
            continue

        content_start_idx = 0
        if lines and lines[0].strip() == "---":
            for idx in range(1, len(lines)):
                if lines[idx].strip() == "---":
                    content_start_idx = idx + 1
                    break

        first_line = ""
        for idx in range(content_start_idx, len(lines)):
            if lines[idx].strip():
                first_line = lines[idx].strip()
                break

        if not first_line.startswith("# "):
            if auto_fix:
                new_content = "# " + name + "\n\n" + content
                project_md.write_text(new_content, encoding="utf-8")
                findings.append(AuditFinding(
                    severity="warning",
                    category="project",
                    target=name,
                    message="第一行不是 markdown 标题（# 项目名），格式不规范",
                    suggestion="第一行改为 # 项目名",
                    fixed=True,
                    fix_detail="已自动添加标题 '# " + name + "'",
                ))
            else:
                findings.append(AuditFinding(
                    severity="warning",
                    category="project",
                    target=name,
                    message="第一行不是 markdown 标题（# 项目名），格式不规范",
                    suggestion="第一行改为 # 项目名",
                ))

        if len(content.strip()) < len(first_line) + 5:
            findings.append(AuditFinding(
                severity="warning",
                category="project",
                target=name,
                message="文件几乎只有标题，内容为空或不完整",
            ))

        # 检查路径是否有效：只检查本地 macOS 路径（/Users/ 或 ~/）
        path_pattern = re.findall(r"(?:路径|Path|path)[:：]\s*([^\s\n]+)", content)
        for path_str in path_pattern:
            if path_str.startswith(('http://', 'https://', '//')):
                continue
            is_local = path_str.startswith("/Users/") or path_str.startswith("~/")
            if is_local:
                from pathlib import Path
                p = Path(path_str).expanduser()
                if not p.exists():
                    findings.append(AuditFinding(
                        severity="warning",
                        category="project",
                        target=name,
                        message=f"记录的项目路径不存在: {path_str}",
                        suggestion="确认路径是否正确，或更新为新路径",
                    ))

        try:
            file_mtime = datetime.fromtimestamp(project_md.stat().st_mtime).date()
            age_days = (date.today() - file_mtime).days
            if age_days > 180:
                findings.append(AuditFinding(
                    severity="info",
                    category="project",
                    target=name,
                    message=f"文件已 {age_days} 天未更新（mtime: {file_mtime}）",
                ))
        except OSError:
            pass

        code_blocks = re.findall(r"```", content)
        if len(code_blocks) % 2 != 0:
            if auto_fix:
                new_content = content + "\n```"
                project_md.write_text(new_content, encoding="utf-8")
                findings.append(AuditFinding(
                    severity="warning",
                    category="project",
                    target=name,
                    message="存在未闭合的代码块（``` 数量为奇数）",
                    suggestion="检查并修复代码块配对",
                    fixed=True,
                    fix_detail="已自动在文件末尾添加 ``` 闭合",
                ))
            else:
                findings.append(AuditFinding(
                    severity="warning",
                    category="project",
                    target=name,
                    message="存在未闭合的代码块（``` 数量为奇数）",
                    suggestion="检查并修复代码块配对",
                ))

    return findings


def scan_skill_scripts(auto_fix: bool = False) -> list[AuditFinding]:
    """扫描所有 skills/*/scripts/ 下的 Python 脚本，返回审计发现列表。"""
    findings: list[AuditFinding] = []

    if not SKILLS_DIR.exists():
        return findings

    scripts_dir = SKILLS_DIR / "scripts"
    if not scripts_dir.is_dir():
        return findings

    skill_name = "scripts"

    for py_file in sorted(scripts_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        script_name = py_file.stem
        target_label = f"{skill_name}/{script_name}"

        try:
            code = py_file.read_text(encoding="utf-8")
        except OSError:
            continue

        try:
            import py_compile
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            findings.append(AuditFinding(
                severity="error",
                category="module",
                target=target_label,
                message=f"语法错误: {e}",
                suggestion="修复语法错误",
            ))
            continue

        from src.tools.skill_scripts import BLOCKED_IMPORTS
        for line in code.splitlines():
            stripped = line.strip()
            m = re.match(r"^from\s+(\S+)", stripped)
            if m and m.group(1).split(".")[0] in BLOCKED_IMPORTS:
                findings.append(AuditFinding(
                    severity="error",
                    category="script",
                    target=target_label,
                    message=f"危险 import: {stripped}",
                    suggestion="移除此 import，禁止脚本调用 src 内部模块",
                ))
            m = re.match(r"^import\s+(\S+)", stripped)
            if m and m.group(1).split(".")[0] in BLOCKED_IMPORTS:
                findings.append(AuditFinding(
                    severity="error",
                    category="script",
                    target=target_label,
                    message=f"危险 import: {stripped}",
                    suggestion="移除此 import",
                ))

        has_schema = "TOOL_SCHEMA" in code
        has_runner = "TOOL_RUNNER" in code
        if has_schema and not has_runner:
            if auto_fix:
                schema_match = re.search(r'"name":\s*"(\w+)"', code)
                tool_name = schema_match.group(1) if schema_match else script_name
                stub = '\ndef TOOL_RUNNER(params: dict) -> str:\n    return "[TOOL_RUNNER] params={}".format(params)'
                new_code = code + stub
                py_file.write_text(new_code, encoding="utf-8")
                code = new_code
                has_runner = True
                findings.append(AuditFinding(
                    severity="warning",
                    category="module",
                    target=target_label,
                    message="定义了 TOOL_SCHEMA 但缺少 TOOL_RUNNER，工具不会被注册",
                    suggestion="添加 TOOL_RUNNER 函数: TOOL_RUNNER(params: dict) -> str",
                    fixed=True,
                    fix_detail="已自动生成 TOOL_RUNNER stub",
                ))
            else:
                findings.append(AuditFinding(
                    severity="warning",
                    category="module",
                    target=target_label,
                    message="定义了 TOOL_SCHEMA 但缺少 TOOL_RUNNER，工具不会被注册",
                    suggestion="添加 TOOL_RUNNER 函数: TOOL_RUNNER(params: dict) -> str",
                ))
        if has_runner and not has_schema:
            if auto_fix:
                tool_name = script_name.replace("_", " ")
                stub = '\nTOOL_SCHEMA = {\n    "type": "function",\n    "function": {\n        "name": "" + script_name + "",\n        "description": "Skill script: " + tool_name + "",\n        "parameters": {\n            "type": "object",\n            "properties": {},\n            "required": [],\n        },\n    },\n}'
                new_code = code + stub
                py_file.write_text(new_code, encoding="utf-8")
                code = new_code
                findings.append(AuditFinding(
                    severity="warning",
                    category="module",
                    target=target_label,
                    message="定义了 TOOL_RUNNER 但缺少 TOOL_SCHEMA，无法注册为工具",
                    suggestion="添加 TOOL_SCHEMA（OpenAI function calling schema）",
                    fixed=True,
                    fix_detail="已自动生成 TOOL_SCHEMA stub",
                ))
            else:
                findings.append(AuditFinding(
                    severity="warning",
                    category="module",
                    target=target_label,
                    message="定义了 TOOL_RUNNER 但缺少 TOOL_SCHEMA，无法注册为工具",
                    suggestion="添加 TOOL_SCHEMA（OpenAI function calling schema）",
                ))

        if has_runner:
            runner_match = re.search(r"def\s+TOOL_RUNNER\s*\([^)]*\)\s*(?:->\s*\w+)?\s*:", code)
            if not runner_match:
                if auto_fix:
                    new_code = re.sub(
                        r"def\s+TOOL_RUNNER\s*\([^)]*\)\s*(?:->\s*\w+)?\s*:",
                        "def TOOL_RUNNER(params: dict) -> str:",
                        code
                    )
                    if new_code != code:
                        py_file.write_text(new_code, encoding="utf-8")
                        findings.append(AuditFinding(
                            severity="warning",
                            category="module",
                            target=target_label,
                            message="TOOL_RUNNER 签名不符合规范，应为: def TOOL_RUNNER(params: dict) -> str:",
                            fixed=True,
                            fix_detail="已自动修正为标准签名",
                        ))
                    else:
                        findings.append(AuditFinding(
                            severity="warning",
                            category="module",
                            target=target_label,
                            message="TOOL_RUNNER 签名不符合规范，应为: def TOOL_RUNNER(params: dict) -> str:",
                        ))
                else:
                    findings.append(AuditFinding(
                        severity="warning",
                        category="module",
                        target=target_label,
                        message="TOOL_RUNNER 签名不符合规范，应为: def TOOL_RUNNER(params: dict) -> str:",
                    ))

        if len(code) > 50_000:
            findings.append(AuditFinding(
                severity="info",
                category="module",
                target=target_label,
                message=f"脚本代码 {len(code)} 字符，较大。建议拆分。",
            ))

    return findings


def scan_user_patterns(days: int = 1) -> list[AuditFinding]:
    """扫描近期 session 日志，检测用户高频重复操作，判断是否需要沉淀为 skill。

    检测逻辑：
    1. 统计近期 session 中用户请求的出现频次
    2. 用 SkillIndex 语义检索判断是否已被现有 skill 覆盖
    3. 过滤掉基本工具能力
    4. 出现 3 次以上的模式标记为建议沉淀
    """
    import json
    from collections import Counter
    from datetime import date, timedelta

    findings: list[AuditFinding] = []
    sessions_dir = LAMIX_DIR / "memory" / "sessions"
    if not sessions_dir.exists():
        return findings

    today = date.today()
    cutoff = today - timedelta(days=days)
    user_messages: list[str] = []

    for day_dir in sorted(sessions_dir.iterdir()):
        if not day_dir.is_dir() or day_dir.name.endswith(".md"):
            continue
        try:
            day_date = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if day_date < cutoff:
            continue

        for channel_dir in day_dir.iterdir():
            if not channel_dir.is_dir():
                continue
            for jsonl_file in channel_dir.glob("*.jsonl"):
                try:
                    with open(jsonl_file, encoding="utf-8") as f:
                        for line in f:
                            rec = json.loads(line)
                            if rec.get("role") == "user":
                                text = rec.get("content", "")
                                if isinstance(text, str) and len(text.strip()) > 3:
                                    stripped = text.strip()
                                    if stripped.startswith("/"):
                                        continue
                                    if len(stripped) < 5:
                                        continue
                                    user_messages.append(stripped)
                except Exception:
                    continue

    if not user_messages:
        return findings

    msg_counter = Counter(user_messages)

    from src.core.indexer import SkillIndex
    from src.core.config import INDEX_DIR

    skill_index = SkillIndex(SKILLS_DIR, INDEX_DIR)
    try:
        skill_index.load_or_build()
    except Exception as e:
        logger.warning(f"scan_user_patterns: SkillIndex 加载失败: {e}")
        skill_index = None

    from src.core.tools import get_all_schemas
    _TOOL_PATTERNS: dict[str, str] = {}
    for schema in get_all_schemas():
        func = schema.get("function", {})
        name = func.get("name", "")
        if not name:
            continue
        desc = func.get("description", "")
        auto_tokens = re.findall(r"[a-zA-Z]+", f"{name} {desc}")
        _TOOL_PATTERNS[name] = "|".join(set(t.lower() for t in auto_tokens if len(t) >= 2))

    # 补充常见中文口语变体（这些是工具能力但 description 里不会出现的词）
    _CAPABILITY_EXTENSIONS: dict[str, str] = {
        "shell": r"git|push|pull|提交代码|执行一下|运行.*脚本",
        "file_read": r"看看|查看|读取|读一下|看看日志",
        "file_write": r"写入|保存|创建文件",
        "search": r"搜索|查找|找.*文件",
        "web_search": r"搜一下|查一下|搜索.*网",
    }
    for tool_name, ext in _CAPABILITY_EXTENSIONS.items():
        if tool_name in _TOOL_PATTERNS:
            _TOOL_PATTERNS[tool_name] = _TOOL_PATTERNS[tool_name] + "|" + ext

    def _is_basic_capability(query: str) -> bool:
        for tool_name, pattern in _TOOL_PATTERNS.items():
            if pattern and re.search(pattern, query, re.IGNORECASE):
                return True
        return False

    sorted_msgs = msg_counter.most_common()

    for msg, count in sorted_msgs:
        if count < 3:
            break

        chat_patterns = {"你好", "咋样", "啥情况", "继续", "你在干啥", "谢谢",
                          "你刚才在做什么", "我上次在让你干啥", "刚刚你在",
                          "刚刚你", "上次我最后让你干的事儿", "我上次",
                          "你在做什么", "你刚才", "你好吗"}
        if any(p in msg for p in chat_patterns):
            continue

        # 检查 1：是否已被现有 skill 覆盖
        covered_by = ""
        if skill_index is not None:
            matched = skill_index.search(msg, top_k=5, similarity_threshold=0.5)
            if matched:
                for m in matched:
                    fm = re.match(r"^---\s*\n(.*?)\n---\s*\n", m, re.DOTALL)
                    if fm:
                        try:
                            import yaml
                            meta = yaml.safe_load(fm.group(1)) or {}
                            skill_name = meta.get("name", "")
                            skill_desc = meta.get("description", "")
                            if skill_name and len(skill_name) > 1:
                                name_words = set(re.findall(r"[\w]+", skill_name.lower()))
                                name_words = {w for w in name_words if len(w) >= 2}
                                msg_words = set(re.findall(r"[\w]+", msg.lower()))
                                if name_words and name_words & msg_words:
                                    covered_by = skill_name
                                    break
                        except Exception:
                            pass
                    if not covered_by and len(msg) < 40:
                        if msg in m:
                            covered_by = "(skill匹配)"
                            break

        if covered_by:
            continue

        if _is_basic_capability(msg):
            continue

        related = []
        msg_words = set(re.findall(r"[\w]+", msg.lower()))
        msg_words = {w for w in msg_words if len(w) >= 2}
        for other_msg, other_count in sorted_msgs:
            if other_msg == msg:
                continue
            other_words = set(re.findall(r"[\w]+", other_msg.lower()))
            other_words = {w for w in other_words if len(w) >= 2}
            if msg_words and other_words:
                overlap = msg_words & other_words
                if len(overlap) >= 2 and other_count >= 2:
                    related.append(other_msg)

        suggestions = [msg]
        if related:
            suggestions.extend(related[:4])

        findings.append(AuditFinding(
            severity="info",
            category="skill",
            target="高频操作模式",
            message=f"检测到高频操作（{count}次）：{msg}",
            suggestion=f"考虑沉淀为 skill。相关请求：{suggestions}",
        ))

    return findings
