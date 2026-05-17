"""反思与知识沉淀模块的单元测试。"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import contextlib
import pytest

from src.core.reflection import (
    _content_already_exists,
    _create_project,
    _create_skill,
    _update_project,
    _update_skill,
    execute_learnings,
    format_execution_summary,
    reflect_and_learn,
    should_reflect,
)
from src.planning.steps import Plan, Step, StepStatus


# ── should_reflect ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_cooldown():
    """每个测试前重置反思冷却时间。"""
    import src.core.reflection as ref_mod
    ref_mod._last_reflect_time = 0.0
    yield
    ref_mod._last_reflect_time = 0.0


def _make_plan(n_steps: int, status: StepStatus = StepStatus.done) -> Plan:
    steps = []
    for i in range(n_steps):
        s = Step(id=i + 1, thought="", action=f"step {i+1}", args={}, status=status)
        steps.append(s)
    p = Plan(goal="test", steps=steps)
    p.status = status
    return p


def test_should_reflect_cooldown():
    """冷却期内即使满足条件也不反思。"""
    import src.core.reflection as ref_mod
    ref_mod._last_reflect_time = time.time()
    assert should_reflect(None, tool_call_count=3) is False


def test_should_reflect_tool_call_count_0():
    """tool_call_count=0 → 不反思。"""
    assert should_reflect(None, tool_call_count=0) is False


def test_should_reflect_tool_call_count_1():
    """tool_call_count=1 → 不反思。"""
    assert should_reflect(None, tool_call_count=1) is False


def test_should_reflect_tool_call_count_2():
    """tool_call_count=2 → 不反思。"""
    assert should_reflect(None, tool_call_count=2) is False


def test_should_reflect_tool_call_count_3():
    """tool_call_count=3 → 反思。"""
    assert should_reflect(None, tool_call_count=3) is True


def test_should_reflect_tool_call_count_5():
    """tool_call_count=5 → 反思。"""
    assert should_reflect(None, tool_call_count=5) is True


def test_should_reflect_with_plan_tool_count_3():
    """有 plan + tool_call_count=3 → 反思。"""
    plan = _make_plan(1)
    assert should_reflect(plan, tool_call_count=3) is True


def test_should_reflect_with_skill_tool_count_3():
    """有 skill + tool_call_count=3 → 反思。"""
    assert should_reflect(None, skill_activated="code-review", tool_call_count=3) is True


# ── Project 沉淀 ───────────────────────────────────────────────────────────


@pytest.fixture
def temp_projects(tmp_path: Path) -> Path:
    d = tmp_path / "projects"
    d.mkdir()
    return d


@pytest.fixture
def temp_skills(tmp_path: Path) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    return d


def test_create_project_new(temp_projects: Path):
    """project_create：新建项目文件。"""
    with patch("src.core.reflection.PROJECTS_DIR", temp_projects):
        hint = _create_project("myproj", "源码路径: /foo/bar", "首次探索")
    assert hint is not None
    assert "myproj" in hint
    assert (temp_projects / "myproj.md").exists()
    content = (temp_projects / "myproj.md").read_text()
    assert "源码路径: /foo/bar" in content
    assert "创建于" in content


def test_create_project_downgrade_to_update(temp_projects: Path):
    """project_create 遇已存在 → 降级为 update。"""
    with patch("src.core.reflection.PROJECTS_DIR", temp_projects):
        _create_project("myproj", "源码路径: /foo/bar", "首次")
        hint = _create_project("myproj", "新增了 cronjob 模块", "发现新模块")
    assert hint is not None
    assert "更新" in hint
    content = (temp_projects / "myproj.md").read_text()
    assert "cronjob" in content
    assert "更新" in content


def test_update_project_append(temp_projects: Path):
    """project_update：追加日期分节。"""
    with patch("src.core.reflection.PROJECTS_DIR", temp_projects):
        _create_project("myproj", "源码路径: /foo/bar", "首次")
        hint = _update_project("myproj", "新增模块: cronjob", "补充")
    assert hint is not None
    assert "更新" in hint
    content = (temp_projects / "myproj.md").read_text()
    assert "cronjob" in content


def test_update_project_duplicate(temp_projects: Path):
    """project_update：重复内容不写入。"""
    with patch("src.core.reflection.PROJECTS_DIR", temp_projects):
        _create_project("myproj", "源码路径: /foo/bar", "首次")
        hint = _update_project("myproj", "源码路径: /foo/bar", "重复")
    assert hint is None


def test_update_project_downgrade_to_create(temp_projects: Path):
    """project_update 遇不存在 → 降级为 create。"""
    with patch("src.core.reflection.PROJECTS_DIR", temp_projects):
        hint = _update_project("myproj", "源码路径: /foo/bar", "降级创建")
    assert hint is not None
    assert "myproj" in hint
    content = (temp_projects / "myproj.md").read_text()
    assert "源码路径: /foo/bar" in content


# ── Skill 沉淀 ─────────────────────────────────────────────────────────────


def test_create_skill(temp_skills: Path):
    # _create_skill 要求内容 >=300 字符且包含 >=3 个编号步骤
    content = "\n".join(
        [
            "这是一个测试技能内容，需要足够长度来满足 300 字符的最低要求。"
            "这段文字会反复填充以确保总字符数超过阈值。"
            "在实际使用中，skill 内容通常是一段完整的工作流程描述，"
            "包含环境准备、执行步骤、验证结果、错误排查等多个环节。"
            "下面列出具体的操作步骤，每一步都有明确的说明和可执行的命令。",
            "1. 第一步：执行初始化命令 `init --all`，确保所有依赖安装完毕且环境变量配置正确。",
            "2. 第二步：运行主程序 `run --config default.yaml`，使用默认配置文件启动服务并监听端口。",
            "3. 第三步：验证输出结果 `check --verbose`，确认服务健康检查通过且日志无异常。",
        ]
    )
    with patch("src.core.reflection.SKILLS_DIR", temp_skills):
        hint = _create_skill("my-skill", content, "测试创建")
    assert hint is not None
    assert (temp_skills / "my-skill.md").exists()


def test_create_skill_empty_content(temp_skills: Path):
    with patch("src.core.reflection.SKILLS_DIR", temp_skills):
        hint = _create_skill("empty", "", "空内容")
    assert hint is None


def test_update_skill_append(temp_skills: Path):
    skill_file = temp_skills / "existing.md"
    skill_file.write_text(
        "---\nname: existing\n---\n# Existing\nOld content.",
        encoding="utf-8",
    )
    with patch("src.core.reflection.SKILLS_DIR", temp_skills):
        hint = _update_skill("existing", "New findings here.", "补充")
    assert hint is not None
    updated = skill_file.read_text()
    assert "New findings" in updated


def test_update_skill_duplicate(temp_skills: Path):
    skill_file = temp_skills / "existing.md"
    skill_file.write_text(
        "---\nname: existing\n---\n# Existing\nOld content.",
        encoding="utf-8",
    )
    with patch("src.core.reflection.SKILLS_DIR", temp_skills):
        hint = _update_skill("existing", "Old content.", "重复")
    assert hint is None


# ── 去重 ─────────────────────────────────────────────────────────────────────


def test_content_already_exists():
    existing = "源码路径: /foo/bar\n其他信息"
    assert _content_already_exists(existing, "源码路径: /foo/bar") is True
    assert _content_already_exists(existing, "完全不同的内容") is False


# ── execute_learnings ───────────────────────────────────────────────────────


def test_execute_learnings_project_create(temp_projects: Path):
    learnings = [{
        "type": "project_create",
        "target": "test-proj",
        "content": "Some project info " + "x" * 100,
        "reason": "test",
        "triggers": [],
    }]
    with patch("src.core.reflection.PROJECTS_DIR", temp_projects):
        hints = execute_learnings(learnings)
    assert len(hints) == 1
    assert "test-proj" in hints[0]


def test_execute_learnings_project_update(temp_projects: Path):
    with patch("src.core.reflection.PROJECTS_DIR", temp_projects):
        execute_learnings([{
            "type": "project_create",
            "target": "test-proj",
            "content": "Initial info " + "x" * 100,
            "reason": "首次",
            "triggers": [],
        }])
        hints = execute_learnings([{
            "type": "project_update",
            "target": "test-proj",
            "content": "New module found " + "y" * 100,
            "reason": "发现新模块",
            "triggers": [],
        }])
    assert len(hints) == 1
    assert "更新" in hints[0]
    content = (temp_projects / "test-proj.md").read_text()
    assert "New module" in content


def test_execute_learnings_unknown_type(temp_projects: Path):
    """未知 type 不影响其他 learning。"""
    learnings = [
        {"type": "unknown_type", "target": "x", "content": "y", "reason": "z", "triggers": []},
        {
            "type": "project_create",
            "target": "valid-proj",
            "content": "Valid content " + "x" * 100,
            "reason": "test",
            "triggers": [],
        },
    ]
    with patch("src.core.reflection.PROJECTS_DIR", temp_projects):
        hints = execute_learnings(learnings)
    assert len(hints) == 1
    assert "valid-proj" in hints[0]


def test_execute_learnings_empty():
    assert execute_learnings([]) == []


# ── reflect_and_learn (mock LLM) ───────────────────────────────────────────


def test_reflect_and_learn_no_learning():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"learnings": []}'
    mock_client.client.chat.completions.create.return_value = mock_response
    mock_client.model = "test-model"

    result = reflect_and_learn("test goal", "test summary", mock_client)
    assert result == []


def test_reflect_and_learn_with_project_create():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = json.dumps({
        "learnings": [{
            "type": "project_create",
            "target": "myproj",
            "reason": "首次探索",
            "content": "项目路径: /foo/bar",
            "triggers": [],
        }],
    })
    mock_client.client.chat.completions.create.return_value = mock_response
    mock_client.model = "test-model"

    with patch("src.core.reflection._get_existing_skills_summary", return_value="（无）"), \
         patch("src.core.reflection._get_existing_projects_summary", return_value="（无）"):
        result = reflect_and_learn("探索 myproj", "执行了3步", mock_client)
    assert len(result) == 1
    assert result[0]["type"] == "project_create"


def test_reflect_and_learn_ignores_should_learn():
    """should_learn 已移除，以 learnings 为准。"""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"should_learn": true, "learnings": []}'
    mock_client.client.chat.completions.create.return_value = mock_response
    mock_client.model = "test-model"

    with patch("src.core.reflection._get_existing_skills_summary", return_value="（无）"), \
         patch("src.core.reflection._get_existing_projects_summary", return_value="（无）"):
        result = reflect_and_learn("test", "test", mock_client)
    assert result == []


# ── format_execution_summary ────────────────────────────────────────────────


def test_format_execution_summary():
    steps = [
        Step(id=1, thought="", action="find code", args={"pattern": "main"}, status=StepStatus.done, result="found"),
        Step(id=2, thought="", action="read file", args={"path": "/tmp/a.py"}, status=StepStatus.done, result="content"),
    ]
    plan = Plan(goal="test", steps=steps)
    plan.status = StepStatus.done
    summary = format_execution_summary(plan)
    assert "find code" in summary
    assert "done" in summary



# ── LLM 响应解析（各模型返回格式）──────────────────────────────────────────

_THINK_OPEN = "\u300a"  # <think>
_THINK_CLOSE = "\u300b"  # </think>


def _mock_llm(response_content: str):
    """构造返回指定内容的 mock LLM client。"""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = response_content
    mock_client.client.chat.completions.create.return_value = mock_response
    mock_client.model = "test-model"
    return mock_client


class TestLLMResponseParsing:
    """验证各模型返回格式的响应解析能力。"""

    def test_plain_json(self):
        """普通 JSON 响应（无 thinking 标签、无 markdown）。"""
        content = '{"learnings": []}'
        mock = _mock_llm(content)
        with _patch_reflect_context():
            result = reflect_and_learn("goal", "summary", mock)
        assert result == []

    def test_markdown_code_block(self):
        """Markdown ```json``` 包裹的响应（DeepSeek/GLM 常用）。"""
        content = '```json\n{"learnings": [{"type": "info", "target": "test", "content": "x", "reason": "y"}]}\n```'
        mock = _mock_llm(content)
        with _patch_reflect_context():
            result = reflect_and_learn("goal", "summary", mock)
        assert len(result) == 1
        assert result[0]["type"] == "info"

    def test_thinking_tag_single_line(self):
        """单行 thinking 标签（<think>...</think> 与 JSON 同在一行）。"""
        content = _THINK_OPEN + "分析中..." + _THINK_CLOSE + '{"learnings": []}'
        mock = _mock_llm(content)
        with _patch_reflect_context():
            result = reflect_and_learn("goal", "summary", mock)
        assert result == []

    def test_thinking_tag_cross_line(self):
        """跨行 thinking 标签（DeepSeek 的典型返回，必须用 re.DOTALL 匹配）。"""
        content = (_THINK_OPEN + "\n分析执行过程...\n" + _THINK_CLOSE +
                   '{"learnings": []}')
        mock = _mock_llm(content)
        with _patch_reflect_context():
            result = reflect_and_learn("goal", "summary", mock)
        assert result == []

    def test_thinking_tag_with_markdown(self):
        """thinking 标签 + markdown 代码块（组合格式）。"""
        content = (_THINK_OPEN + "\n分析中\n" + _THINK_CLOSE +
                   '\n```json\n{"learnings": []}\n```')
        mock = _mock_llm(content)
        with _patch_reflect_context():
            result = reflect_and_learn("goal", "summary", mock)
        assert result == []

    def test_thinking_tag_multi_line_complex(self):
        """多行复杂 thinking 标签，中间包含换行和特殊字符。"""
        content = (_THINK_OPEN + "\n让我逐步分析：\n1. 用户运行了多个 shell 命令\n2. 发现了一些配置问题\n结论：无新知识\n" +
                   _THINK_CLOSE +
                   '{"learnings": []}')
        mock = _mock_llm(content)
        with _patch_reflect_context():
            result = reflect_and_learn("goal", "summary", mock)
        assert result == []

    def test_thinking_tag_with_learnings(self):
        """带 thinking 标签且有实际 learnings 的响应。"""
        import json
        learnings_payload = json.dumps([{"type": "project_update", "target": "model-platform", "content": "新配置", "reason": "发现"}])
        content = _THINK_OPEN + "\n分析后发现了新知识\n" + _THINK_CLOSE + '{"learnings": ' + learnings_payload + '}'
        mock = _mock_llm(content)
        with _patch_reflect_context():
            result = reflect_and_learn("goal", "summary", mock)
        assert len(result) == 1
        assert result[0]["type"] == "project_update"


@contextlib.contextmanager
def _patch_reflect_context():
    """patch 掉 reflect_and_learn 中对外部文件的依赖。"""
    p1 = patch("src.core.reflection._get_existing_skills_summary", return_value="（无）")
    p2 = patch("src.core.reflection._get_existing_projects_summary", return_value="（无）")
    p3 = patch("src.core.reflection._get_existing_info_summary", return_value="（无）")
    with p1, p2, p3:
        yield
