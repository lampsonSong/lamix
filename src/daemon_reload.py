"""daemon 配置热重载、watcher、safe mode 相关。

包括：
    - SSL 证书 patch（飞书 WS 长连接）
    - config.yaml watcher + 热重载分发
    - memory 目录 watcher（自动刷新索引）
    - 飞书 adapter / LLM 客户端热重载
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from typing import Any

from src.core.config import LAMIX_DIR
from src.core.session_manager import get_session_manager
from src.daemon_state import _shutdown

logger = logging.getLogger(__name__)


def _patch_websockets_ssl() -> None:
    """Monkey-patch websockets.connect 使用 certifi CA 证书。

    macOS launchd / Windows 环境下 Python 默认 SSL context 可能缺少中间 CA
    （尤其有 VPN/代理时），导致飞书 WebSocket 长连接 SSL 握手失败。
    """
    try:
        import ssl
        import certifi
        import websockets

        _original_connect = websockets.connect

        async def _patched_connect(*args, **kwargs):
            if "ssl" not in kwargs:
                ctx = ssl.create_default_context(cafile=certifi.where())
                kwargs["ssl"] = ctx
            return await _original_connect(*args, **kwargs)

        _patched_connect.__wrapped__ = _original_connect  # type: ignore[attr-defined]
        websockets.connect = _patched_connect
        logger.info("[daemon] websockets SSL patch 已应用 (certifi CA)")
    except ImportError:
        logger.warning("[daemon] certifi 未安装，跳过 websockets SSL patch")
    except Exception as e:
        logger.error(f"[daemon] websockets SSL patch 失败: {e}")


def _get_feishu_credentials(config_path) -> tuple[str, str]:
    """从配置文件读取飞书凭证（app_id, app_secret），用于变更比较。"""
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        feishu_cfg = cfg.get("feishu", {}) or {}
        return (
            feishu_cfg.get("app_id", "").strip(),
            feishu_cfg.get("app_secret", "").strip(),
        )
    except Exception:
        return ("", "")


def _start_memory_watcher(mgr) -> None:
    """监听 skills、projects、info 目录的 .md 文件变化，自动刷新索引。"""
    from src.core.config import SKILLS_DIR, PROJECTS_DIR, INFO_DIR

    watch_dirs = [SKILLS_DIR, PROJECTS_DIR, INFO_DIR]

    def _snapshot() -> dict:
        state: dict[str, float] = {}
        for d in watch_dirs:
            if d.exists():
                for p in d.rglob("*.md"):
                    state[str(p)] = p.stat().st_mtime
        return state

    def _watcher() -> None:
        last_state = _snapshot()
        while not _shutdown.is_set():
            _shutdown.wait(5)
            if _shutdown.is_set():
                break
            try:
                current = _snapshot()
                if current != last_state:
                    last_state = current
                    logger.info("[daemon] memory 目录变更，触发索引刷新")
                    mgr.refresh_all_indices()
            except Exception as e:
                logger.error(f"[daemon] memory watcher 异常: {e}")

    t = threading.Thread(target=_watcher, daemon=True, name="memory-watcher")
    t.start()
    logger.info("[daemon] memory watcher 已启动")


def _config_fingerprint(cfg: dict) -> str:
    """计算配置的指纹，用于检测变更。"""
    key_sections = {
        "llm": cfg.get("llm", {}),
        "models": cfg.get("models", []),
        "feishu": cfg.get("feishu", {}),
        "retrieval": cfg.get("retrieval", {}),
        "skills_management": cfg.get("skills_management", {}),
    }
    serialized = json.dumps(key_sections, sort_keys=True, default=str)
    return hashlib.md5(serialized.encode()).hexdigest()


def _start_config_watcher(pm, config: dict) -> None:
    """启动 config.yaml 热重载检测线程。

    每 30 秒检查 config.yaml，在任何关键配置（llm/models/feishu/retrieval/skills_management）
    变更时触发热重载。
    """
    from src.core.config import CONFIG_PATH, load_config

    def _watcher():
        last_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0
        last_fingerprint = _config_fingerprint(config)
        mgr = get_session_manager(config)
        while not _shutdown.is_set():
            _shutdown.wait(30)
            if _shutdown.is_set():
                break
            try:
                if not CONFIG_PATH.exists():
                    continue
                mtime = CONFIG_PATH.stat().st_mtime
                if mtime == last_mtime:
                    continue
                last_mtime = mtime

                new_config = load_config()
                new_fingerprint = _config_fingerprint(new_config)
                if new_fingerprint == last_fingerprint:
                    logger.debug("[daemon] 配置文件已变更，但关键字段未变，跳过热重载")
                    continue

                logger.info(f"[daemon] 配置文件已变更（fingerprint: {last_fingerprint[:8]} -> {new_fingerprint[:8]}），触发热重载...")
                last_fingerprint = new_fingerprint
                _reload_config(pm, mgr, config, new_config)
                config.clear()
                config.update(new_config)
            except Exception as e:
                logger.error(f"[daemon] config watcher 异常: {e}")

    t = threading.Thread(target=_watcher, daemon=True, name="config-watcher")
    t.start()
    logger.info("[daemon] config watcher 已启动")


def _reload_feishu_adapter(pm, config: dict | None = None) -> None:
    """热重载飞书 adapter：标记旧 adapter 为 stopped，创建新的替换。

    不关闭旧 WS client（lark SDK 内部用 asyncio.get_event_loop()，
    在新线程里 run_until_complete 会跟已有 running loop 冲突导致崩溃）。
    旧 adapter 被 _stopped=True 标记后不再处理新消息，自然被 GC 回收。
    """
    from src.core.config import load_config
    from src.platforms.adapters.feishu import FeishuAdapter

    if config is None:
        config = load_config()

    old_adapter = pm._adapters.get("feishu")
    if old_adapter is not None:
        try:
            old_adapter._stopped = True
            del pm._adapters["feishu"]
            logger.info("[daemon] 旧飞书 adapter 已标记为 stopped")
        except Exception as e:
            logger.error(f"[daemon] 标记旧飞书 adapter 失败: {e}")

    feishu_cfg = config.get("feishu", {})
    app_id = feishu_cfg.get("app_id", "").strip()
    app_secret = feishu_cfg.get("app_secret", "").strip()

    if not app_id or not app_secret:
        logger.info("[daemon] 新配置中无飞书凭证，不启动 adapter")
        return

    try:
        new_adapter = FeishuAdapter({
            "app_id": app_id,
            "app_secret": app_secret,
        })
        mgr = get_session_manager(config)
        new_adapter.session_manager = mgr
        pm.register(new_adapter)
        new_adapter.start()
        logger.info("[daemon] 新飞书 adapter 已启动，热重载完成")
    except Exception as e:
        logger.error(f"[daemon] 热重载飞书 adapter 失败: {e}")


def _reload_config(pm, mgr, old_config: dict, new_config: dict) -> None:
    """热重载配置变更。

    对比新旧配置，只处理实际变更的部分：
    - feishu: 凭证变更 → 重建 FeishuAdapter
    - llm/models: 模型配置变更 → 重建所有 session 的 LLM 客户端
    - retrieval/skills_management: 检索配置变更 → 更新 session 配置并刷新索引
    """
    from src.core.config import get_retrieval_config, get_embedding_config

    # 1. feishu 变更
    old_feishu = old_config.get("feishu", {})
    new_feishu = new_config.get("feishu", {})
    if old_feishu.get("app_id") != new_feishu.get("app_id") or \
       old_feishu.get("app_secret") != new_feishu.get("app_secret"):
        logger.info("[daemon] feishu 凭证变更，热重载飞书 adapter...")
        _reload_feishu_adapter(pm, new_config)

    # 2. llm/models 变更
    old_llm = old_config.get("llm", {})
    new_llm = new_config.get("llm", {})
    old_models = old_config.get("models", [])
    new_models = new_config.get("models", [])

    llm_changed = (
        old_llm.get("api_key") != new_llm.get("api_key") or
        old_llm.get("base_url") != new_llm.get("base_url") or
        old_llm.get("model") != new_llm.get("model") or
        old_models != new_models
    )

    if llm_changed:
        logger.info("[daemon] LLM/模型配置变更，热重载 LLM 客户端...")
        _reload_llm_clients(mgr, new_config)

    # 3. retrieval/skills_management 变更
    old_retrieval = old_config.get("retrieval", {})
    new_retrieval = new_config.get("retrieval", {})
    old_sm = old_config.get("skills_management", {})
    new_sm = new_config.get("skills_management", {})

    if old_retrieval != new_retrieval or old_sm != new_sm:
        logger.info("[daemon] retrieval/skills_management 配置变更，更新 session 配置...")
        retrieval_cfg = get_retrieval_config(new_config)

        with mgr._lock:
            sessions = list(mgr._sessions.values())
            if mgr._cli_session is not None:
                sessions.append(mgr._cli_session)

        for session in sessions:
            try:
                session.retrieval_config = retrieval_cfg
                if session.agent:
                    session.agent.retrieval_config = retrieval_cfg
            except Exception as e:
                logger.error(f"[daemon] 更新 session retrieval_config 失败: {e}")

        mgr.refresh_all_indices()
        logger.info("[daemon] retrieval/skills_management 配置已更新")


def _reload_llm_clients(mgr, config: dict) -> None:
    """重建所有 session 的 LLM 客户端（主模型 + fallback）。"""
    from src.core.session import _create_llm, _create_llm_from_model_config
    from src.core.compaction import _build_compaction_config, resolve_context_window

    primary_llm, primary_adapter = _create_llm(config, channel="cli")
    primary_name = config["llm"]["model"]
    primary_cw = resolve_context_window(
        config["llm"]["model"],
        explicit=config["llm"].get("context_window"),
    )

    llm_clients: dict[str, Any] = {
        primary_name: {
            "llm": primary_llm,
            "adapter": primary_adapter,
            "context_window": primary_cw,
        }
    }

    primary_api_key = config["llm"].get("api_key", "")
    primary_base_url = config["llm"].get("base_url", "")
    for model_cfg in config.get("models", []):
        name = model_cfg["name"]
        if name not in llm_clients:
            llm_i, adapter_i = _create_llm_from_model_config(
                model_cfg,
                fallback_api_key=primary_api_key,
                fallback_base_url=primary_base_url,
                channel="cli",
            )
            llm_clients[name] = {
                "llm": llm_i,
                "adapter": adapter_i,
                "context_window": resolve_context_window(
                    name, explicit=model_cfg.get("context_window")
                ),
            }

    fallback_models: list[tuple[Any, Any]] = []
    for name, cw in llm_clients.items():
        if name != primary_name:
            fallback_models.append((cw["llm"], cw["adapter"]))

    compaction_cfg = _build_compaction_config(config, model_context_window=primary_cw)

    with mgr._lock:
        sessions = list(mgr._sessions.values())
        if mgr._cli_session is not None:
            sessions.append(mgr._cli_session)

    for session in sessions:
        try:
            session.agent.llm = primary_llm
            session.agent.adapter = primary_adapter
            session.agent._compaction_config = compaction_cfg
            session.agent.fallback_models = fallback_models
            session.agent.set_context()

            session.llm_clients = llm_clients
            session._current_model_name = primary_name

            logger.info(f"[daemon] session {session.session_id} 的 LLM 客户端已重建")
        except Exception as e:
            logger.error(f"[daemon] 重建 session LLM 客户端失败: {e}")

    logger.info("[daemon] 所有 session 的 LLM 客户端已重建")
