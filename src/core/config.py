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
SKILLS_DIR = LAMIX_DIR / "skills"
INDEX_DIR = LAMIX_DIR / "index"
PROJECTS_DIR = LAMIX_DIR / "projects"
INFO_DIR = LAMIX_DIR / "info"

# 旧路径（迁移前）
_OLD_SKILLS_DIR = LAMIX_DIR / "memory" / "skills"
_OLD_PROJECTS_DIR = LAMIX_DIR / "memory" / "projects"

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
            # 只修正指向旧路径（~/.lamix/memory/skills 等）的情况
            new_path = Path(new_value).expanduser()
            if expanded.resolve() != new_path.resolve():
                # 检查是否是旧路径（含 memory/ 子目录）
                if "memory" in str(expanded):
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
    old_skills = LAMIX_DIR / "memory" / "skills"
    old_projects = LAMIX_DIR / "memory" / "projects"
    old_info = LAMIX_DIR / "memory" / "info"
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
    if old_info.is_dir() and any(old_info.iterdir()):
        INFO_DIR.mkdir(parents=True, exist_ok=True)
        for item in old_info.iterdir():
            dest = INFO_DIR / item.name
            if not dest.exists():
                shutil.move(str(item), str(dest))
                moved = True
        if not any(old_info.iterdir()):
            old_info.rmdir()
    if moved:
        migrated.write_text("v2", encoding="utf-8")
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
    返回的 api_key 也必须显式在 embedding 段指定，否则为空（降级为 keyword matching）。
    """
    emb = config.get("embedding", {})
    if not isinstance(emb, dict):
        emb = {}
    return {
        "provider": str(emb.get("provider", "")),
        "model": str(emb.get("model", "")),
        "api_key": str(emb.get("api_key", "")),
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 优先。"""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config() -> dict[str, Any]:
    """加载配置。优先读 config.yaml，不存在则引导用户填写。"""
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            logger.exception("Failed to load config, resetting to empty config")
            data = {}
    else:
        data = {}

    # Merge with defaults (deep merge)
    merged = _deep_merge(dict(DEFAULT_CONFIG), data)
    return merged


def save_config(config: dict[str, Any]) -> None:
    """保存配置到 config.yaml。保留注释。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
    logger.info("Config saved to %s", CONFIG_PATH)


def setup_interactive() -> dict[str, Any]:
    """交互式引导用户填写 LLM 配置。"""
    print("=" * 60)
    print(_bold("Lamix 配置向导"))
    print("=" * 60)

    config = load_config()

    # LLM 配置
    print(f"\n{_cyan('[1/3] LLM 配置')}")
    print("（DeepSeek / 智谱 GLM / MiniMax 等 OpenAI 兼容 API）")
    api_key = input(f"API Key [{_yellow('跳过') if config['llm'].get('api_key') else _red('必填')}]：").strip()
    if not api_key and not config['llm'].get('api_key'):
        print(_red("✗ API Key 不能为空"))
        return {}
    if api_key:
        config["llm"]["api_key"] = api_key

    base_url = input(f"Base URL [默认: {_yellow(config['llm']['base_url'])}]：").strip()
    if base_url:
        config["llm"]["base_url"] = base_url

    model = input(f"模型 [默认: {_yellow(config['llm']['model'])}]：").strip()
    if model:
        config["llm"]["model"] = model

    # Feishu 配置
    print(f"\n{_cyan('[2/3] 飞书配置（可选）')}")
    print("（用于在飞书群聊中与 Lamix 交互，不配置则仅支持本地终端）")
    feishu_app_id = input("飞书 App ID（留空跳过）：").strip()
    if feishu_app_id:
        config["feishu"]["app_id"] = feishu_app_id
        feishu_app_secret = getpass("飞书 App Secret：")
        config["feishu"]["app_secret"] = feishu_app_secret

    # Embedding 配置
    print(f"\n{_cyan('[3/3] 向量检索配置（可选）')}")
    print("（配置后可用语义搜索 skills/projects/info）")
    emb_provider = input("Provider（deepseek / zhipu / minimax，留空跳过）：").strip()
    if emb_provider:
        config["embedding"] = {
            "provider": emb_provider,
            "model": input("模型名称：").strip(),
            "api_key": getpass("API Key："),
        }

    save_config(config)
    print(f"\n{_green('✓ 配置已保存')}")
    return config


def is_config_complete(config: dict[str, Any]) -> bool:
    """检查必填项是否已填写。用户必须至少配置过 api_key（说明走过 setup wizard）。"""
    if not CONFIG_PATH.exists():
        return False
    try:
        return bool(config.get("llm", {}).get("api_key", "").strip())
    except Exception:
        return False


def resolve_env_vars(config: dict[str, Any]) -> dict[str, Any]:
    """递归替换配置值中的 ${ENV_VAR} 为环境变量。"""
    if isinstance(config, dict):
        return {k: resolve_env_vars(v) for k, v in config.items()}
    if isinstance(config, list):
        return [resolve_env_vars(item) for item in config]
    if isinstance(config, str):
        match = _ENV_VAR_PATTERN.search(config)
        if match:
            env_var = match.group(1)
            return os.environ.get(env_var, config)
    return config


def get_llm_config(config: dict[str, Any]) -> dict[str, str]:
    """从完整配置中提取 LLM 调用所需字段。"""
    llm = config.get("llm", {})
    return {
        "api_key": os.environ.get("LLM_API_KEY") or llm.get("api_key", ""),
        "base_url": os.environ.get("LLM_BASE_URL") or llm.get("base_url", ""),
        "model": os.environ.get("LLM_MODEL") or llm.get("model", ""),
    }


def get_feishu_config(config: dict[str, Any]) -> dict[str, str]:
    """从完整配置中提取飞书相关字段。"""
    feishu = config.get("feishu", {})
    return {
        "app_id": os.environ.get("FEISHU_APP_ID") or feishu.get("app_id", ""),
        "app_secret": os.environ.get("FEISHU_APP_SECRET") or feishu.get("app_secret", ""),
    }


def get_provider_preset(provider: str) -> dict[str, Any] | None:
    """根据 provider 名称返回预设配置。"""
    for p in PROVIDER_PRESETS.values():
        if p["name"].lower().replace(" ", "") == provider.lower().replace(" ", ""):
            return p
    return None


def test_connection(config: dict[str, Any]) -> tuple[bool, str]:
    """测试 LLM API 连通性。返回 (success, message)。"""
    llm_cfg = get_llm_config(config)
    if not llm_cfg["api_key"]:
        return False, "API Key 未配置"
    if not llm_cfg["base_url"]:
        return False, "Base URL 未配置"
    if not llm_cfg["model"]:
        return False, "Model 未配置"

    try:
        client = httpx.Client(timeout=10)
        resp = client.post(
            f"{llm_cfg['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {llm_cfg['api_key']}"},
            json={
                "model": llm_cfg["model"],
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 5,
            },
        )
        if resp.status_code == 200:
            return True, "✓ 连接成功"
        else:
            return False, f"✗ HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, f"✗ 连接失败: {e}"


def update_config_path(config: dict[str, Any], key: str, path: str) -> None:
    """更新配置中的路径字段，同时写回文件。"""
    config[key] = path
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        data = {}
    data[key] = path
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
    except Exception as e:
        logger.warning("Failed to write config path %s: %s", key, e)


# Legacy stub for run_setup_wizard (used by CLI but functionality moved elsewhere)
def run_setup_wizard(title: str = "配置向导") -> dict:
    """Legacy stub - setup wizard functionality moved to CLI."""
    return load_config()
