"""配置管理模块：加载、保存、引导用户填写 ~/.lamix/config.yaml"""

from __future__ import annotations

import logging
import os
import re
from getpass import getpass
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

LAMIX_DIR = Path.home() / ".lamix"
CONFIG_PATH = LAMIX_DIR / "config.yaml"
MEMORY_DIR = LAMIX_DIR / "memory"
SKILLS_DIR = LAMIX_DIR / "memory" / "skills"
INDEX_DIR = LAMIX_DIR / "index"
PROJECTS_DIR = LAMIX_DIR / "memory" / "projects"
INFO_DIR = LAMIX_DIR / "memory" / "info"

# 旧路径（迁移前）
_OLD_SKILLS_DIR = LAMIX_DIR / "skills"
_OLD_PROJECTS_DIR = LAMIX_DIR / "projects"

_DEFAULT_RETRIEVAL: dict[str, Any] = {
    "skill_top_k": 3,
    "project_top_k": 2,
    "similarity_threshold": 0.3,
}

_DEFAULT_EMBEDDING: dict[str, Any] = {
    "provider": "",
    "model": "",
}

_DEFAULT_SKILLS_MANAGEMENT: dict[str, Any] = {
    "cleanup_max_skills": 300,
    "cleanup_age_days": 10,
    "cleanup_min_invocations": 0,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "api_key": "",
        "base_url": "https://api.deepseek.com/",
        "model": "deepseek-v4-flash",
    },
    "models": [],
    "feishu": {
        "app_id": "",
        "app_secret": "",
    },
    "memory_path": str(MEMORY_DIR),
    "skills_path": str(SKILLS_DIR),
    "projects_path": str(PROJECTS_DIR),
    "info_path": str(INFO_DIR),
    "retrieval": dict(_DEFAULT_RETRIEVAL),
    "skills_management": dict(_DEFAULT_SKILLS_MANAGEMENT),
}

# Pattern to match ${ENV_VAR} placeholders
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

# Provider presets for setup wizard
PROVIDER_PRESETS = {
    "1": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-v4-flash",
        "key_hint": "在 platform.deepseek.com 获取",
    },
    "2": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "models": ["glm-5.1", "glm-5-turbo", "glm-4-plus"],
        "default_model": "glm-5.1",
        "key_hint": "在 open.bigmodel.cn 获取",
    },
    "3": {
        "name": "MiniMax",
        "base_url": "https://api.minimaxi.com/v1/",
        "models": ["MiniMax-M2.7", "MiniMax-M2.5"],
        "default_model": "MiniMax-M2.7",
        "key_hint": "在 platform.minimaxi.com 获取",
    },
}


import sys as _sys

# Windows CMD 默认不支持 ANSI 转义码，跳过颜色
_SUPPORTS_COLOR = _sys.platform != "win32"


def _bold(text: str) -> str:
    """返回加粗文本（ANSI 转义码）。"""
    return f"\033[1m{text}\033[0m" if _SUPPORTS_COLOR else text


def _cyan(text: str) -> str:
    """返回青色文本（ANSI 转义码）。"""
    return f"\033[36m{text}\033[0m" if _SUPPORTS_COLOR else text


def _green(text: str) -> str:
    """返回绿色文本（ANSI 转义码）。"""
    return f"\033[32m{text}\033[0m" if _SUPPORTS_COLOR else text


def _red(text: str) -> str:
    """返回红色文本（ANSI 转义码）。"""
    return f"\033[31m{text}\033[0m" if _SUPPORTS_COLOR else text


def _yellow(text: str) -> str:
    """返回黄色文本（ANSI 转义码）。"""
    return f"\033[33m{text}\033[0m" if _SUPPORTS_COLOR else text


def ensure_dirs() -> None:
    """确保 ~/.lamix 及子目录存在。"""
    LAMIX_DIR.mkdir(exist_ok=True)
    MEMORY_DIR.mkdir(exist_ok=True)
    (MEMORY_DIR / "sessions").mkdir(exist_ok=True)
    (MEMORY_DIR / "sessions" / "tool_bodies").mkdir(exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    INFO_DIR.mkdir(exist_ok=True)
    INDEX_DIR.mkdir(exist_ok=True)

    _migrate_old_dirs()


def _fix_config_paths() -> None:
    """修正 config.yaml 中指向旧路径的配置项。

    迁移到 memory/ 子目录后，config.yaml 中用户显式配置的 skills_path / projects_path
    可能仍指向旧路径，导致索引扫描到空目录。此处自动更新为新路径。
    """
    if not CONFIG_PATH.exists():
        return
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return
    if not isinstance(data, dict):
        return

    changed = False
    path_fixes = {
        "skills_path": str(SKILLS_DIR),
        "projects_path": str(PROJECTS_DIR),
        "info_path": str(INFO_DIR),
        "memory_path": str(MEMORY_DIR),
    }
    for key, new_value in path_fixes.items():
        old_value = data.get(key)
        if isinstance(old_value, str) and old_value.strip():
            expanded = Path(old_value.strip()).expanduser()
            # 如果配置的路径既不是新路径，也不是旧路径的实际位置，跳过
            # 只修正指向旧路径（~/.lamix/skills 等不含 memory/）的情况
            new_path = Path(new_value).expanduser()
            if expanded.resolve() != new_path.resolve():
                # 检查是否是旧路径（不含 memory/ 子目录）
                if "memory" not in expanded.parts:
                    data[key] = new_value
                    changed = True
                    logger.info("Fixed config %s: %s -> %s", key, old_value, new_value)

    if changed:
        try:
            with CONFIG_PATH.open("w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            logger.info("Updated config.yaml with corrected paths")
        except Exception as ex:
            logger.warning("Failed to update config.yaml paths: %s", ex)


def _migrate_old_dirs() -> None:
    import shutil
    migrated = LAMIX_DIR / ".memory_migrated"
    if migrated.exists():
        # 即使已迁移，仍需检查 config.yaml 路径是否过时
        _fix_config_paths()
        return
    old_skills = LAMIX_DIR / "skills"
    old_projects = LAMIX_DIR / "projects"
    moved = False
    if old_skills.is_dir() and any(old_skills.iterdir()):
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        for item in old_skills.iterdir():
            dest = SKILLS_DIR / item.name
            if not dest.exists():
                shutil.move(str(item), str(dest))
                moved = True
        if not any(old_skills.iterdir()):
            old_skills.rmdir()
    if old_projects.is_dir() and any(old_projects.iterdir()):
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        for item in old_projects.iterdir():
            dest = PROJECTS_DIR / item.name
            if not dest.exists():
                shutil.move(str(item), str(dest))
                moved = True
        if not any(old_projects.iterdir()):
            old_projects.rmdir()
    if moved:
        migrated.write_text("v1", encoding="utf-8")
    # 迁移完成后修正 config.yaml 中的旧路径
    _fix_config_paths()

def get_skills_management_config(config: dict[str, Any]) -> dict[str, int]:
    """合并 skills_management 段，供 SkillIndex 清理逻辑使用。"""
    sm = config.get("skills_management")
    if not isinstance(sm, dict):
        sm = {}
    base = _deep_merge(dict(_DEFAULT_SKILLS_MANAGEMENT), sm)
    return {
        "cleanup_max_skills": int(
            base.get("cleanup_max_skills", _DEFAULT_SKILLS_MANAGEMENT["cleanup_max_skills"])
        ),
        "cleanup_age_days": int(
            base.get("cleanup_age_days", _DEFAULT_SKILLS_MANAGEMENT["cleanup_age_days"])
        ),
        "cleanup_min_invocations": int(
            base.get(
                "cleanup_min_invocations",
                _DEFAULT_SKILLS_MANAGEMENT["cleanup_min_invocations"],
            )
        ),
    }


def get_retrieval_config(config: dict[str, Any]) -> dict[str, Any]:
    """合并 retrieval 段，带默认值。字段均可被 user config 覆盖。"""
    r = config.get("retrieval")
    if not isinstance(r, dict):
        r = {}
    base = _deep_merge(dict(_DEFAULT_RETRIEVAL), r)
    return {
        "skill_top_k": int(base.get("skill_top_k", _DEFAULT_RETRIEVAL["skill_top_k"])),
        "project_top_k": int(
            base.get("project_top_k", _DEFAULT_RETRIEVAL["project_top_k"])
        ),
        "similarity_threshold": float(
            base.get("similarity_threshold", _DEFAULT_RETRIEVAL["similarity_threshold"])
        ),
    }


def get_embedding_config(config: dict[str, Any]) -> dict[str, str]:
    """
    合并 embedding 段。base_url 必须显式配置（不继承 llm 段），不配则 embedding 不可用。
    返回的 api_key 也必须显式在 embedding 段指定，否则为空（降级为纯关键词搜索）。
    """
    e = config.get("embedding")
    if not isinstance(e, dict):
        e = {}
    base = _deep_merge(dict(_DEFAULT_EMBEDDING), e)
    provider = str(base.get("provider", _DEFAULT_EMBEDDING["provider"]))
    model = str(base.get("model", _DEFAULT_EMBEDDING["model"]))
    base_url = str(base.get("base_url", "") or "").strip()
    # api_key: 只取 embedding 段显式配置的值，不继承 llm 段
    api_key = str(base.get("api_key", "") or "").strip()
    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
    }


def _expand_env_vars(value: str) -> str:
    """Expand ${ENV_VAR} patterns with environment variable values."""
    if not isinstance(value, str):
        return value
    
    def replacer(m: re.Match) -> str:
        var_name = m.group(1)
        return os.environ.get(var_name, "")
    
    return _ENV_VAR_PATTERN.sub(replacer, value)


def _expand_config(obj: Any) -> Any:
    """Recursively expand env vars in config values."""
    if isinstance(obj, str):
        return _expand_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _expand_config(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_config(item) for item in obj]
    return obj


def load_config() -> dict[str, Any]:
    """加载配置文件，不存在则返回默认配置。"""
    ensure_dirs()
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    merged = _deep_merge(dict(DEFAULT_CONFIG), data)
    expanded = _expand_config(merged)
    # 清理已废弃的 chat_ids 字段（WebSocket 自动接收所有会话，无需配置）
    if "chat_ids" in expanded.get("feishu", {}):
        del expanded["feishu"]["chat_ids"]
    return expanded


def save_config(config: dict[str, Any]) -> None:
    """将配置写入磁盘。"""
    ensure_dirs()
    # 清理已废弃的字段
    config.pop("chat_ids", None)
    if "feishu" in config:
        config["feishu"].pop("chat_ids", None)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)


def _install_feishu_skills() -> None:
    """如果 config/default_skills_feishu/ 存在，将其中的 skills 复制到用户的 skills 目录。"""
    import shutil
    feishu_skills_src = Path(__file__).resolve().parent.parent / "config" / "default_skills_feishu"
    if not feishu_skills_src.exists():
        return
    installed = []
    for skill_dir in feishu_skills_src.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        target = SKILLS_DIR / skill_dir.name
        if target.exists():
            continue  # 已存在则不覆盖
        shutil.copytree(skill_dir, target)
        installed.append(skill_dir.name)
    if installed:
        print(f"已自动安装飞书 skills：{', '.join(installed)}")


def _ensure_user_md() -> None:
    """确保 USER.md 存在（首次运行时从默认模板复制）。
    
    不再通过问答引导用户信息，改为在首次对话中自然获取。
    Agent 会根据对话内容自动更新 USER.md。
    """
    user_path = LAMIX_DIR / "USER.md"
    if user_path.exists():
        return
    default_path = Path(__file__).resolve().parent.parent / "config" / "default_user.md"
    try:
        default = default_path.read_text(encoding="utf-8")
    except OSError:
        default = "称呼：用户\n"
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(default, encoding="utf-8")


def _notify_daemon_restart() -> None:
    """如果 daemon 正在运行，提示用户需要重启才能让飞书配置生效。"""
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", r"(python.*src\.daemon|lamix.*gateway)"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            print()
            print(_green("✓ 检测到 daemon 正在运行，配置将在 30 秒内自动热重载生效。"))
    except Exception:
        pass


def _setup_fallback_models(config: dict) -> None:
    """引导用户配置 fallback 模型（主模型失败时的备选）。
    
    配完一个后询问是否继续配下一个，随时可跳过。
    """
    print()
    print(_bold("Fallback 模型配置") + "（可选，直接回车跳过）")
    print("  主模型请求失败时，会按顺序尝试 fallback 模型。")
    print()

    models = []
    idx = 1
    while True:
        label = f"第 {idx} 个 fallback" if idx > 1 else "Fallback 模型"
        
        # 选择供应商
        choice = _select(
            f"选择 {label} 的供应商（回车跳过）：",
            [
                ("1", "DeepSeek（推荐）"),
                ("2", "智谱 GLM"),
                ("3", "MiniMax"),
                ("4", "自定义"),
                ("skip", "跳过，不配置 fallback"),
            ],
        )
        if choice is None or choice == "skip":
            break

        if choice in PROVIDER_PRESETS:
            preset = PROVIDER_PRESETS[choice]
            provider_name = preset["name"]
            base_url = preset["base_url"]
            models_list = preset["models"]
            default_model = preset["default_model"]
        else:
            provider_name = "自定义"
            base_url = input("请输入 API Base URL: ").strip()
            if not base_url:
                print("  未输入 URL，跳过")
                continue
            models_list = []
            default_model = ""

        # 选择模型
        if models_list:
            model_options = [(m, f"{m}（默认）" if m == default_model else m) for m in models_list]
            model_options.append(("__manual__", "手动输入"))
            model_choice = _select(f"选择 {label} 的模型：", model_options)
            if model_choice is None:
                break
            if model_choice == "__manual__":
                selected_model = input("请输入模型名: ").strip()
                if not selected_model:
                    continue
            else:
                selected_model = model_choice
        else:
            selected_model = input(f"请输入模型名: ").strip()
            if not selected_model:
                continue

        # API Key（可选，默认继承主模型的）
        primary_key = config.get("llm", {}).get("api_key", "")
        primary_url = config.get("llm", {}).get("base_url", "")
        use_primary = (base_url == primary_url)
        
        if use_primary:
            api_key = primary_key
            print(f"  使用主模型的 API Key")
        else:
            api_key = input(f"请输入 {provider_name} 的 API Key（回车跳过）: ").strip()
            if not api_key:
                print("  未输入 Key，跳过")
                continue

        model_cfg = {
            "name": selected_model,
            "api_key": api_key,
            "base_url": base_url,
        }
        models.append(model_cfg)
        print(_green(f"✓ 已添加 fallback 模型：{selected_model}"))

        # 询问是否继续
        idx += 1
        if idx > 5:
            print("  最多配置 5 个 fallback 模型")
            break
        
        cont = _select("是否继续添加 fallback 模型？", [
            ("yes", "是，继续添加"),
            ("no", "否，完成配置"),
        ])
        if cont is None or cont != "yes":
            break

    if models:
        config["models"] = models
        names = ", ".join(m["name"] for m in models)
        print(f"\n已配置 {len(models)} 个 fallback 模型：{names}")
    else:
        print("  未配置 fallback 模型，主模型失败时将直接报错。")


def is_config_complete(config: dict[str, Any]) -> bool:
    """检查必填项是否已填写。用户必须至少配置过 api_key（说明走过 setup wizard）。"""
    if not CONFIG_PATH.exists():
        return False
    try:
        return bool(config.get("llm", {}).get("api_key", "").strip())
    except (KeyError, TypeError, AttributeError):
        return False


def _select(prompt_text: str, options: list[tuple[str, str]]) -> str | None:
    """用 prompt_toolkit 实现上下箭头选择菜单。

    当 stdin 不是真实终端时（管道/pty），fallback 到数字输入模式。
    """
    import sys

    # 非 tty：用数字输入 fallback
    if not sys.stdin.isatty():
        return _select_fallback(prompt_text, options)

    from prompt_toolkit import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    selected = [0]
    result: list[str | None] = [None]

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event: Any) -> None:
        selected[0] = (selected[0] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _down(event: Any) -> None:
        selected[0] = (selected[0] + 1) % len(options)

    @kb.add("enter")
    def _enter(event: Any) -> None:
        result[0] = options[selected[0]][0]
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event: Any) -> None:
        result[0] = None
        event.app.exit()

    def _get_text() -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []
        fragments.append(("bold", f"{prompt_text}\n"))
        fragments.append(("", "(↑/↓ 选择, Enter 确认, Esc 退出)\n\n"))
        for i, (_, label) in enumerate(options):
            if i == selected[0]:
                fragments.append(("fg:cyan bold", f"  ❯ {label}\n"))
            else:
                fragments.append(("", f"    {label}\n"))
        return fragments

    app: Application[None] = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(_get_text))])),
        key_bindings=kb,
        full_screen=False,
    )
    app.run()
    return result[0]


def _select_fallback(prompt_text: str, options: list[tuple[str, str]]) -> str | None:
    """非 tty 环境下的数字输入选择。"""
    print(f"\n{prompt_text}")
    for i, (_, label) in enumerate(options):
        print(f"  {i + 1}. {label}")
    try:
        choice = input("请输入序号: ").strip()
        if not choice:
            return None
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            return options[idx][0]
        return None
    except (ValueError, EOFError, KeyboardInterrupt):
        return None


def run_setup_wizard(*, title: str | None = None) -> dict[str, Any]:
    """首次运行引导用户填写配置，返回配置字典。"""
    _title = title or '欢迎使用 Lamix！首次运行需要配置 LLM 供应商信息。'
    print(f"\n{_bold(_title)}\n")

    config = load_config()

    try:
        # 1. 选择 Provider
        provider_choice = _select("请选择 LLM 供应商：", [
            ("1", "DeepSeek（推荐）"),
            ("2", "智谱 GLM"),
            ("3", "MiniMax"),
            ("4", "自定义（手动填写 URL）"),
        ])
        if provider_choice is None:
            print("\n\n配置已取消。")
            raise SystemExit(1)

        # 2. 设置预设或自定义配置
        if provider_choice in PROVIDER_PRESETS:
            preset = PROVIDER_PRESETS[provider_choice]
            provider_name = preset["name"]
            base_url = preset["base_url"]
            models = preset["models"]
            default_model = preset["default_model"]
            key_hint = preset["key_hint"]
        else:  # 自定义
            provider_name = "自定义"
            base_url = input("\n请输入 API Base URL: ").strip()
            if not base_url:
                print(_yellow("未输入 Base URL，使用默认值"))
                base_url = config["llm"]["base_url"]
            models = []
            default_model = ""
            key_hint = "请向供应商获取"

        # 3. 输入 API Key
        print(f"\n{_cyan('已选择：' + provider_name)}")
        print(f"{_cyan('API 地址：' + base_url)}")

        api_key = input(f"请输入 API Key（{key_hint}）: ").strip()

        config["llm"]["api_key"] = api_key
        config["llm"]["base_url"] = base_url

        # 4. 选择模型
        if models:
            model_options: list[tuple[str, str]] = []
            for model in models:
                label = f"{model}（默认）" if model == default_model else model
                model_options.append((model, label))
            model_options.append(("__manual__", "手动输入"))

            model_choice = _select("选择模型：", model_options)
            if model_choice is None:
                print("\n\n配置已取消。")
                raise SystemExit(1)

            if model_choice == "__manual__":
                selected_model = input("请输入模型名: ").strip()
                if not selected_model:
                    print(_yellow("模型名为空，使用默认模型"))
                    selected_model = default_model
            else:
                selected_model = model_choice
        else:
            # 自定义 provider，直接输入模型名
            selected_model = input(f"\n请输入模型名（回车使用默认 {config['llm']['model']}）: ").strip()
            if not selected_model:
                selected_model = config["llm"]["model"]

        config["llm"]["model"] = selected_model

        # 5. 连通性验证
        print(f"\n{_cyan('正在验证连接...')}")
        if _verify_connection(base_url, api_key):
            print(_green("✓ 连接成功"))
        else:
            print(_yellow("⚠ 连接验证失败，但您可以继续使用（请检查 API Key 和网络）"))
            retry = _select("是否重新配置？", [
                ("yes", "是，重新配置"),
                ("no", "否，继续使用"),
            ])
            if retry == "yes":
                return run_setup_wizard()

        # 6. 飞书配置（可选）
        print(f"\n{_bold('飞书配置')}（可选，直接回车跳过）：")
        app_id = input("飞书 App ID: ").strip()
        if app_id:
            config["feishu"]["app_id"] = app_id

        app_secret = input("飞书 App Secret: ").strip()
        if app_secret:
            config["feishu"]["app_secret"] = app_secret

# chat_ids 不再需要用户手动配置，listener 自动接收所有会话消息

        # 7. 保存配置
        save_config(config)
        print(f"\n{_green('配置已保存到')} {CONFIG_PATH}\n")

        # 8. Fallback 模型配置
        _setup_fallback_models(config)

        # 9. 保存配置（fallback 可能修改了 config）
        save_config(config)

        # 10. 如果配置了飞书，自动安装飞书专属 skills
        if config.get("feishu", {}).get("app_id"):
            _install_feishu_skills()

        # 11. 用户画像引导
        _ensure_user_md()

        # 12. 提示热重载
        _notify_daemon_restart()

        return config

    except (KeyboardInterrupt, EOFError):
        print("\n\n配置已取消。")
        raise SystemExit(1)


def _verify_connection(base_url: str, api_key: str) -> bool:
    """验证 API 连接是否正常。

    Args:
        base_url: API 基础 URL
        api_key: API 密钥

    Returns:
        连接成功返回 True，失败返回 False
    """
    if not api_key:
        # 如果没有 API Key（如内网模型），跳过验证
        return True

    try:
        # 尝试调用 /models 端点
        url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}

        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, headers=headers)
            return response.status_code == 200
    except Exception as e:
        logger.debug("Connection verification failed: %s", e)
        return False


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个字典，override 优先。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
