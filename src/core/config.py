"""配置管理模块：加载、保存、引导用户填写 ~/.lamix/config.yaml"""

from __future__ import annotations

import copy
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
SKILLS_DIR = MEMORY_DIR / "skills"
INDEX_DIR = LAMIX_DIR / "index"
PROJECTS_DIR = MEMORY_DIR / "projects"
INFO_DIR = MEMORY_DIR / "info"


_DEFAULT_RETRIEVAL: dict[str, Any] = {
    "skill_top_k": 3,
    "project_top_k": 2,
    "similarity_threshold": 0.3,
}

_DEFAULT_EMBEDDING: dict[str, Any] = {
    "provider": "",
    "name": "",
}

# 模型标识字段：规范键是 "name"，"model" 作为兼容别名照收
MODEL_NAME_KEY = "name"
MODEL_ALIAS_KEY = "model"

# 需要做 name/model 归一化的配置段
_MODEL_SECTIONS = ("llm", "embedding", "vision")

_DEFAULT_SKILLS_MANAGEMENT: dict[str, Any] = {
    "cleanup_max_skills": 300,
    "cleanup_age_days": 10,
    "cleanup_min_invocations": 0,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "api_key": "",
        "base_url": "https://api.deepseek.com/",
        "name": "deepseek-v4-flash",
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


_MIGRATION_VERSION = "v3"


def _migrate_old_dirs() -> None:
    """把顶层 ~/.lamix/{skills,projects,info}/ 下的遗留文件搬进 memory/ 子目录。

    这些顶层目录是 memory/ 迁移（2026-05）之前的旧位置，且 LLM 用 file_write 时
    可能又往里写过——同名冲突时给源文件加 -legacy-YYYYMMDD 后缀移入，不覆盖已有。
    """
    import shutil
    from datetime import date

    migrated = LAMIX_DIR / ".memory_migrated"
    if migrated.exists() and migrated.read_text(encoding="utf-8").strip() == _MIGRATION_VERSION:
        _fix_config_paths()
        return

    old_skills = LAMIX_DIR / "skills"
    old_projects = LAMIX_DIR / "projects"
    old_info = LAMIX_DIR / "info"

    moved_any = False

    def _move_all(src_dir: Path, dst_dir: Path) -> bool:
        if not src_dir.is_dir():
            return False
        items = list(src_dir.iterdir())
        if not items:
            try:
                src_dir.rmdir()
            except OSError:
                pass
            return False
        dst_dir.mkdir(parents=True, exist_ok=True)
        stamp = date.today().strftime("%Y%m%d")
        moved = False
        for item in items:
            dest = dst_dir / item.name
            if dest.exists():
                # 冲突：源文件加 -legacy-YYYYMMDD 后缀
                stem, dot, suffix = item.name.rpartition(".")
                if dot:
                    legacy_name = f"{stem}-legacy-{stamp}.{suffix}"
                else:
                    legacy_name = f"{item.name}-legacy-{stamp}"
                dest = dst_dir / legacy_name
                # 冲突升级到 -legacy-YYYYMMDD-N
                n = 1
                while dest.exists():
                    if dot:
                        dest = dst_dir / f"{stem}-legacy-{stamp}-{n}.{suffix}"
                    else:
                        dest = dst_dir / f"{item.name}-legacy-{stamp}-{n}"
                    n += 1
            try:
                shutil.move(str(item), str(dest))
                moved = True
                logger.info("[migration] moved %s -> %s", item, dest)
            except OSError as e:
                logger.warning("[migration] move failed for %s: %s", item, e)
        # 空目录清理
        try:
            if not any(src_dir.iterdir()):
                src_dir.rmdir()
        except OSError:
            pass
        return moved

    if _move_all(old_skills, SKILLS_DIR):
        moved_any = True
    if _move_all(old_projects, PROJECTS_DIR):
        moved_any = True
    if _move_all(old_info, INFO_DIR):
        moved_any = True

    if moved_any or not migrated.exists():
        migrated.write_text(_MIGRATION_VERSION, encoding="utf-8")

    _fix_config_paths()


def get_model_name(
    section: Any,
    section_label: str = "config",
    warn_on_conflict: bool = False,
) -> str:
    """从配置段中取模型标识。

    规范键是 ``name``，``model`` 作为兼容别名照收：
      - 只写 ``name`` → 用 ``name``
      - 只写 ``model`` → 用 ``model``
      - 两个都写且值不同 → 以 ``name`` 为准（可选打 WARNING）
      - 两个都没有 → 返回空字符串（调用方应跳过该条目）

    Args:
        section: 配置段（dict）
        section_label: 段名，用于日志（如 llm / models[0]）
        warn_on_conflict: 是否在冲突时打 WARNING
    """
    if not isinstance(section, dict):
        return ""
    raw_name = section.get(MODEL_NAME_KEY)
    raw_alias = section.get(MODEL_ALIAS_KEY)
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    alias = raw_alias.strip() if isinstance(raw_alias, str) else ""
    if name and alias and name != alias:
        if warn_on_conflict:
            logger.warning(
                "配置段 %s 中 'name' (%s) 与 'model' (%s) 冲突，以 'name' 为准",
                section_label,
                name,
                alias,
            )
    return name or alias


def _normalize_model_fields(cfg: dict[str, Any]) -> dict[str, Any]:
    """把 model/name 别名归一化为 name（就地修改并返回同一 dict）。

    适用范围：llm 段、embedding 段、vision 段、models[] 列表元素。
    归一化后：规范键 ``name`` 存在，别名键 ``model`` 被移除（写盘只落 name）。

    语义约定：
      - 同时存在 name 与 model 且值不同 → 以 name 为准并打 WARNING
      - 只有 model → 提升为 name
      - 显式写空值（name: "" / model: ""）→ 保留为空（表示“未配置”），不回落默认值
      - 两个键都没有 → 不补键（models[] 条目在消费处被跳过并打 WARNING）
    """
    if not isinstance(cfg, dict):
        return cfg

    for key in _MODEL_SECTIONS:
        section = cfg.get(key)
        if not isinstance(section, dict):
            continue
        _promote_alias(section, key)

    models = cfg.get("models")
    if isinstance(models, list):
        for idx, entry in enumerate(models):
            if not isinstance(entry, dict):
                continue
            _promote_alias(entry, f"models[{idx}]")

    return cfg


def _promote_alias(section: dict[str, Any], section_label: str) -> None:
    """把单个配置段里的 ``model`` 别名提升为规范键 ``name``（就地修改）。"""
    has_name = MODEL_NAME_KEY in section
    has_alias = MODEL_ALIAS_KEY in section
    if not has_name and not has_alias:
        return
    value = get_model_name(section, section_label, warn_on_conflict=True)
    section.pop(MODEL_ALIAS_KEY, None)
    # 有值或显式写过键（哪怕是空串）→ 写回规范键；两个键都没写则不补键
    section[MODEL_NAME_KEY] = value


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

    模型标识兼容 ``name``（规范键）与 ``model``（兼容别名），对外仍返回 ``model`` 键。
    """
    emb = config.get("embedding", {})
    if not isinstance(emb, dict):
        emb = {}
    name = get_model_name(emb, "embedding")
    return {
        "provider": str(emb.get("provider", "")),
        "model": name,
        "name": name,
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

    if not isinstance(data, dict):
        data = {}

    # 先归一化用户原始配置（name 优先 / model 兼容别名），
    # 再与默认值合并，避免默认 llm.name 覆盖用户写的 model。
    data = _normalize_model_fields(data)

    # Merge with defaults (deep merge)
    merged = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), data)
    return _normalize_model_fields(merged)


def save_config(config: dict[str, Any]) -> None:
    """保存配置到 config.yaml。保留注释。"""
    # 写盘前归一化：落盘字段名统一为 name，不再产出 model 键
    _normalize_model_fields(config)
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

    model = input(f"模型 [默认: {_yellow(config['llm'].get('name', ''))}]：").strip()
    if model:
        config["llm"]["name"] = model

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
            "name": input("模型名称：").strip(),
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
    """从完整配置中提取 LLM 调用所需字段。

    模型标识统一用 ``name``（``model`` 作为兼容别名）。为兼容下游调用方，
    返回的 dict 仍保留 ``model`` 键（值来自归一化后的 name）。
    """
    llm = config.get("llm", {})
    if not isinstance(llm, dict):
        llm = {}
    return {
        "api_key": os.environ.get("LLM_API_KEY") or llm.get("api_key", ""),
        "base_url": os.environ.get("LLM_BASE_URL") or llm.get("base_url", ""),
        "model": os.environ.get("LLM_MODEL") or get_model_name(llm, "llm"),
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
    # 写盘前归一化：落盘字段名统一为 name
    _normalize_model_fields(data)
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
    except Exception as e:
        logger.warning("Failed to write config path %s: %s", key, e)


# Legacy stub for run_setup_wizard (used by CLI but functionality moved elsewhere)
def run_setup_wizard(title: str = "配置向导") -> dict:
    """Legacy stub - setup wizard functionality moved to CLI."""
    return load_config()
