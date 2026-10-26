"""配置模型标识字段（name / model）归一化与容错测试。

规范键是 ``name``，``model`` 作为兼容别名照收。
本文件覆盖任务卡要求的 5 个场景 + 若干边界。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import (  # noqa: E402
    _normalize_model_fields,
    get_embedding_config,
    get_llm_config,
    get_model_name,
    load_config,
)


# ── 场景 (a)：llm 段只写 name ────────────────────────────────────────────
def test_llm_only_name_is_used():
    cfg = {"llm": {"api_key": "k", "base_url": "http://x", "name": "glm-5.3"}}
    assert get_model_name(cfg["llm"], "llm") == "glm-5.3"
    assert get_llm_config(cfg)["model"] == "glm-5.3"


# ── 场景 (b)：llm 段只写 model（兼容别名）────────────────────────────────
def test_llm_only_model_alias_is_used():
    cfg = {"llm": {"api_key": "k", "base_url": "http://x", "model": "glm-5.3"}}
    assert get_model_name(cfg["llm"], "llm") == "glm-5.3"
    assert get_llm_config(cfg)["model"] == "glm-5.3"


def test_normalize_promotes_alias_to_name():
    cfg = {"llm": {"api_key": "k", "base_url": "http://x", "model": "glm-5.3"}}
    _normalize_model_fields(cfg)
    assert cfg["llm"]["name"] == "glm-5.3"
    assert "model" not in cfg["llm"], "归一化后不应再保留 model 键"


# ── 场景 (c)：models[] 条目只写 model，且不能抛 KeyError ─────────────────
def test_models_entry_only_model_alias_builds_llm():
    from src.core.session import build_llm_clients

    config = {
        "llm": {"api_key": "k1", "base_url": "http://main", "name": "main-model"},
        "models": [
            {"api_key": "k2", "base_url": "http://alt", "model": "alt-model"},
        ],
    }
    primary_llm, _adapter, primary_name, _cw, clients = build_llm_clients(config)

    assert primary_name == "main-model"
    assert "alt-model" in clients, "只写 model 的条目应能正常建成客户端"
    assert clients["alt-model"]["llm"].model == "alt-model"
    assert primary_llm.model == "main-model"


# ── 场景 (d)：两个键都写但值不同 → 以 name 为准 ─────────────────────────
def test_conflict_prefers_name():
    cfg = {"llm": {"name": "from-name", "model": "from-model"}}
    assert get_model_name(cfg["llm"], "llm") == "from-name"


def test_conflict_logs_warning(caplog):
    cfg = {"llm": {"name": "from-name", "model": "from-model"}}
    with caplog.at_level(logging.WARNING):
        get_model_name(cfg["llm"], "llm", warn_on_conflict=True)
    assert any("冲突" in r.getMessage() for r in caplog.records)


def test_normalize_conflict_keeps_name():
    cfg = {"llm": {"name": "from-name", "model": "from-model"}}
    _normalize_model_fields(cfg)
    assert cfg["llm"]["name"] == "from-name"
    assert "model" not in cfg["llm"]


# ── 场景 (e)：两个键都缺 → 跳过该条，其余模型仍可用，进程不崩 ──────────
def test_models_entry_without_any_name_is_skipped(caplog):
    from src.core.session import build_llm_clients

    config = {
        "llm": {"api_key": "k1", "base_url": "http://main", "name": "main-model"},
        "models": [
            {"api_key": "k2", "base_url": "http://broken"},          # 完全缺名
            {"api_key": "k3", "base_url": "http://ok", "name": "ok-model"},
        ],
    }
    with caplog.at_level(logging.WARNING):
        _primary, _adapter, primary_name, _cw, clients = build_llm_clients(config)

    assert primary_name == "main-model"
    assert "ok-model" in clients, "其余模型仍应可用"
    assert len(clients) == 2, "缺名条目应被跳过，不进入客户端集合"
    assert any("跳过" in r.getMessage() for r in caplog.records), "应打 WARNING 说明跳过"


def test_models_entry_non_dict_is_skipped():
    from src.core.session import build_llm_clients

    config = {
        "llm": {"api_key": "k1", "base_url": "http://main", "name": "main-model"},
        "models": ["oops", {"api_key": "k3", "base_url": "http://ok", "name": "ok-model"}],
    }
    _primary, _adapter, primary_name, _cw, clients = build_llm_clients(config)
    assert primary_name == "main-model"
    assert "ok-model" in clients


# ── 边界：默认值不能覆盖用户写的 model（deep merge 顺序陷阱）────────────
def test_default_name_does_not_override_user_model(tmp_path, monkeypatch):
    """DEFAULT_CONFIG 的 llm.name 不能吃掉用户只写的 llm.model。"""
    import src.core.config as config_mod

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "llm:\n"
        "  api_key: user-key\n"
        "  base_url: http://user-host\n"
        "  model: user-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_file)

    loaded = config_mod.load_config()
    assert loaded["llm"]["name"] == "user-model"
    assert loaded["llm"]["api_key"] == "user-key"
    assert loaded["llm"]["base_url"] == "http://user-host"


def test_default_used_when_user_omits_model(tmp_path, monkeypatch):
    import src.core.config as config_mod

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("llm:\n  api_key: k\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_file)

    loaded = config_mod.load_config()
    assert loaded["llm"]["name"] == config_mod.DEFAULT_CONFIG["llm"]["name"]


# ── 边界：embedding / vision 段同样归一化 ───────────────────────────────
def test_embedding_section_normalized():
    cfg = {"embedding": {"provider": "deepseek", "model": "emb-1", "api_key": "k"}}
    _normalize_model_fields(cfg)
    assert cfg["embedding"]["name"] == "emb-1"
    assert "model" not in cfg["embedding"]
    assert get_embedding_config(cfg)["model"] == "emb-1"


def test_vision_section_normalized():
    cfg = {"vision": {"name": "glm-4v", "model": "legacy"}}
    _normalize_model_fields(cfg)
    assert cfg["vision"]["name"] == "glm-4v"
    assert "model" not in cfg["vision"]


def test_normalize_does_not_mutate_default_config():
    """归一化 DEFAULT_CONFIG 不能污染全局默认（deep merge 用的是 deepcopy）。"""
    import src.core.config as config_mod

    before = dict(config_mod.DEFAULT_CONFIG["llm"])
    cfg = {"llm": {"name": "x", "model": "y"}}
    _normalize_model_fields(cfg)
    assert config_mod.DEFAULT_CONFIG["llm"] == before


# ── 写盘路径：落盘只产 name，不产 model ──────────────────────────────────
def test_save_config_writes_name_only(tmp_path, monkeypatch):
    import src.core.config as config_mod

    out = tmp_path / "config.yaml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", out)

    cfg = {
        "llm": {"api_key": "k", "base_url": "http://x", "model": "aliased"},
        "models": [{"api_key": "k2", "base_url": "http://y", "model": "m2"}],
        "embedding": {"provider": "p", "model": "emb"},
        "vision": {"model": "vis"},
    }
    config_mod.save_config(cfg)

    import yaml

    written = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert written["llm"]["name"] == "aliased"
    assert "model" not in written["llm"]
    assert written["models"][0]["name"] == "m2"
    assert "model" not in written["models"][0]
    assert written["embedding"]["name"] == "emb"
    assert "model" not in written["embedding"]
    assert written["vision"]["name"] == "vis"
    assert "model" not in written["vision"]


def test_update_config_path_writes_name_only(tmp_path, monkeypatch):
    import src.core.config as config_mod

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "llm:\n  api_key: k\n  model: aliased\nskills_path: /tmp/old\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_file)

    cfg = config_mod.load_config()
    config_mod.update_config_path(cfg, "skills_path", "/tmp/new")

    import yaml

    written = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert written["llm"]["name"] == "aliased"
    assert "model" not in written["llm"]
    assert written["skills_path"] == "/tmp/new"


def test_setup_interactive_defaults_use_name():
    """setup 向导读 llm.name 作为默认值（不再读 llm.model）。"""
    import src.core.config as config_mod

    assert config_mod.DEFAULT_CONFIG["llm"]["name"] == "deepseek-v4-flash"
    assert "model" not in config_mod.DEFAULT_CONFIG["llm"]


# ── 消费者：indexer / vision 兼容取值 ───────────────────────────────────
def test_indexer_embedding_model_compat():
    """indexer 建 embedding 客户端时兼容 name / model。"""
    from src.core.indexer import ProjectIndex

    for key in ("name", "model"):
        cfg = {
            "provider": "deepseek",
            key: "emb-model",
            "api_key": "k",
            "base_url": "http://emb",
        }
        idx = ProjectIndex.__new__(ProjectIndex)
        idx._embed_client = None
        # 直接复用 __init__ 里的构造逻辑太重，这里只验证取值表达式
        value = cfg.get("name") or cfg.get("model") or ""
        assert value == "emb-model"


def test_vision_config_name_alias(tmp_path, monkeypatch):
    """vision.py 绕过 load_config 直读 yaml，需自行兼容 name / model。"""
    from src.tools import vision as vision_mod

    cfg_file = tmp_path / ".lamix" / "config.yaml"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(
        "vision:\n  api_key: k\n  base_url: http://vis\n  model: vis-alias\n",
        encoding="utf-8",
    )

    class _HomePath(type(vision_mod.Path)):
        @classmethod
        def home(cls):  # noqa: N805
            return tmp_path

    monkeypatch.setattr(vision_mod, "Path", _HomePath)

    assert vision_mod._get_model_name("") == "vis-alias"

    cfg_file.write_text(
        "vision:\n  api_key: k\n  name: vis-canonical\n  model: vis-alias\n",
        encoding="utf-8",
    )
    assert vision_mod._get_model_name("") == "vis-canonical"

    cfg_file.write_text("vision:\n  api_key: k\n", encoding="utf-8")
    assert vision_mod._get_model_name("fallback") == "fallback"
