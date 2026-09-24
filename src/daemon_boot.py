"""daemon 启动通知与 boot_tasks 执行。

覆盖三块能力：
    1. 通知：发送上线通知 / boot_tasks 提示 / 通用 _notify_user
    2. Boot task 文件读写（原子）与限流
    3. Boot task 注入到 session 并执行一轮 agent（含飞书进度卡片）
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.config import LAMIX_DIR

logger = logging.getLogger(__name__)


LOG_DIR = LAMIX_DIR / "logs"
_BOOT_TASKS_PATH = LAMIX_DIR / "boot_tasks.json"

# boot_tasks 限制
_MAX_TASKS = 20
_MAX_TOTAL_BYTES = 10 * 1024

_NOTIFY_COOLDOWN_SECONDS = 600
_LAST_NOTIFY_PATH = LAMIX_DIR / "logs" / "last_online_notify.json"


# ── 通用通知 ─────────────────────────────────────────────────────────────────


def _notify_user(text: str, config: dict | None = None) -> None:
    """通过用户当前渠道发送通知。

    优先使用 session 的 partial_sender（自动匹配渠道），
    fallback 到飞书直发，再 fallback 到 print。
    """
    try:
        from src.tools import session as session_tool
        current_session = session_tool.get_current_session()
        if current_session and current_session.partial_sender:
            current_session.partial_sender(text)
            return
    except Exception:
        pass

    feishu_cfg = (config or {}).get("feishu", {})
    app_id = feishu_cfg.get("app_id", "").strip()
    app_secret = feishu_cfg.get("app_secret", "").strip()
    owner_chat_id = feishu_cfg.get("owner_chat_id", "").strip()
    if app_id and app_secret and owner_chat_id:
        try:
            from src.feishu.client import FeishuClient
            client = FeishuClient(app_id=app_id, app_secret=app_secret)
            client.send_message(receive_id=owner_chat_id, text=text, receive_id_type="chat_id")
            return
        except Exception as e:
            logger.warning(f"[daemon] 飞书发送失败: {e}")
    print(f"[daemon] {text}", flush=True)


# ── 上线通知 & 冷却 ─────────────────────────────────────────────────────────


def _check_notify_cooldown() -> bool:
    """检查上线通知是否在冷却中。返回 True 表示冷却中应跳过。"""
    try:
        if not _LAST_NOTIFY_PATH.exists():
            return False
        raw = _LAST_NOTIFY_PATH.read_text(encoding="utf-8").strip()
        data = json.loads(raw)
        last_sent = datetime.fromisoformat(data["last_sent"])
        elapsed = (datetime.now() - last_sent).total_seconds()
        if elapsed < _NOTIFY_COOLDOWN_SECONDS:
            logger.info(f"[daemon] 上线通知冷却中，跳过（距上次 {elapsed:.0f} 秒）")
            return True
        return False
    except Exception:
        return False


def _record_notify_sent() -> None:
    """记录上线通知发送时间。"""
    try:
        _LAST_NOTIFY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LAST_NOTIFY_PATH.write_text(
            json.dumps({"last_sent": datetime.now().isoformat()}),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"[daemon] 写入上线通知时间戳失败: {e}")


def _send_boot_notification(config: dict, pid: int, is_recovery: bool = False) -> None:
    """常驻上线通知：优先发 open_id 私聊，失败则发 owner_chat_id 群聊。"""
    if _check_notify_cooldown():
        return

    owner_chat_id = config.get("feishu", {}).get("owner_chat_id", "").strip()
    user_open_id = config.get("feishu", {}).get("user_open_id", "").strip()
    app_id = config.get("feishu", {}).get("app_id", "").strip()
    app_secret = config.get("feishu", {}).get("app_secret", "").strip()

    if not app_id or not app_secret:
        logger.warning("[daemon] 飞书凭证未配置，跳过上线通知")
        return

    try:
        from src.feishu.client import FeishuClient
        client = FeishuClient(app_id=app_id, app_secret=app_secret)
        if is_recovery:
            text = f"Lamix 已重启恢复 (PID={pid})"
        else:
            text = f"Lamix 已上线 (PID={pid})"

        targets = []
        if user_open_id:
            targets.append((user_open_id, "open_id"))
        if owner_chat_id:
            targets.append((owner_chat_id, "chat_id"))

        if not targets:
            logger.warning("[daemon] 未配置 feishu.user_open_id 或 feishu.owner_chat_id，跳过上线通知")
            return

        for receive_id, receive_id_type in targets:
            for attempt in range(2):
                try:
                    client.send_message(
                        receive_id=receive_id,
                        text=text,
                        receive_id_type=receive_id_type,
                    )
                    logger.info(f"[daemon] 上线通知已发送 (via {receive_id_type})")
                    _record_notify_sent()
                    return
                except Exception as e:
                    if attempt == 0:
                        logger.error(f"[daemon] 上线通知发送失败（{receive_id_type}），重试: {e}")
                    else:
                        logger.error(f"[daemon] 上线通知发送失败（{receive_id_type}）: {e}")
    except Exception as e:
        logger.error(f"[daemon] 上线通知异常: {e}")


def _notify_boot_tasks_running(config: dict, tasks: list[dict]) -> None:
    """boot_tasks 执行前发飞书提示。"""
    lines = [f"⚡ 正在执行 {len(tasks)} 条启动待办任务："]
    for i, t in enumerate(tasks, 1):
        desc = t.get("task", str(t))
        if len(desc) > 80:
            desc = desc[:77] + "..."
        lines.append(f"{i}. {desc}")

    message = "\n".join(lines)

    feishu_cfg = (config or {}).get("feishu", {}) or {}
    owner_chat_id = feishu_cfg.get("owner_chat_id", "").strip()
    user_open_id = feishu_cfg.get("user_open_id", "").strip()
    app_id = feishu_cfg.get("app_id", "").strip()
    app_secret = feishu_cfg.get("app_secret", "").strip()

    if app_id and app_secret:
        try:
            from src.feishu.client import FeishuClient
            client = FeishuClient(app_id=app_id, app_secret=app_secret)
            sent = False
            for receive_id, receive_id_type in [
                (user_open_id, "open_id"),
                (owner_chat_id, "chat_id"),
            ]:
                if not receive_id:
                    continue
                try:
                    client.send_message(
                        receive_id=receive_id,
                        text=message,
                        receive_id_type=receive_id_type,
                    )
                    logger.info(f"[daemon] boot_tasks 运行提示已发送到飞书 (via {receive_id_type})")
                    sent = True
                    break
                except Exception:
                    continue
            if sent:
                return
        except Exception as e:
            logger.error(f"[daemon] 飞书发送失败，fallback 到 _notify_user: {e}")

    _notify_user(message)


# ── boot_tasks 文件读写 ─────────────────────────────────────────────────────


def _write_boot_task(task: dict) -> None:
    """追加一条 boot_task 到 boot_tasks.json（原子写入）。"""
    tasks = []
    if _BOOT_TASKS_PATH.exists():
        try:
            raw = _BOOT_TASKS_PATH.read_text(encoding="utf-8").strip()
            if raw:
                tasks = json.loads(raw)
        except Exception:
            tasks = []
    tasks.append(task)
    fd, tmp = tempfile.mkstemp(dir=str(_BOOT_TASKS_PATH.parent), prefix=".boot_tasks_")
    with os.fdopen(fd, "w") as f:
        json.dump(tasks, f, ensure_ascii=False)
    os.replace(tmp, str(_BOOT_TASKS_PATH))


def _load_and_clear_boot_tasks() -> list[dict] | None:
    """读取 boot_tasks.json，清空文件，返回任务列表。"""
    tasks_path = _BOOT_TASKS_PATH
    if not tasks_path.exists():
        return None

    raw = tasks_path.read_text(encoding="utf-8").strip()
    if not raw or raw == "[]":
        return None

    try:
        tasks = json.loads(raw)
    except json.JSONDecodeError:
        bad_path = tasks_path.with_suffix(".json.bad")
        shutil.move(str(tasks_path), str(bad_path))
        logger.warning(f"[daemon] boot_tasks.json 损坏，已备份到 {bad_path.name}")
        return None

    if not isinstance(tasks, list) or not tasks:
        return None

    if len(tasks) > _MAX_TASKS:
        logger.warning(f"[daemon] boot_tasks 共 {len(tasks)} 条，截断为 {_MAX_TASKS} 条")
        tasks = tasks[:_MAX_TASKS]

    total = len(json.dumps(tasks, ensure_ascii=False).encode("utf-8"))
    if total > _MAX_TOTAL_BYTES:
        logger.warning(f"[daemon] boot_tasks 总长 {total}B 超过 {_MAX_TOTAL_BYTES}B，截断")
        while tasks and len(json.dumps(tasks, ensure_ascii=False).encode("utf-8")) > _MAX_TOTAL_BYTES:
            tasks.pop()

    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(tasks_path.parent), prefix=".boot_tasks_")
        with os.fdopen(fd, "w") as f:
            f.write("[]")
        os.replace(tmp_path, str(tasks_path))
    except Exception as e:
        logger.error(f"[daemon] 清空 boot_tasks.json 失败: {e}")

    return tasks


def _get_boot_tasks_session(mgr, config: dict):
    """获取用于执行 boot_tasks 的 session。

    优先使用飞书 owner session（保证 resume 等上下文在飞书渠道可见），
    如果未配置 user_open_id 则 fallback 到 CLI session。
    """
    owner_open_id = config.get("feishu", {}).get("user_open_id", "").strip()
    if owner_open_id:
        session = mgr.get_or_create("feishu", owner_open_id)
        logger.info(f"[daemon] boot_tasks 将在飞书 session 上执行 (owner_open_id={owner_open_id})")
        return session

    logger.warning("[daemon] 未配置 feishu.user_open_id，boot_tasks 将在 CLI session 上执行")
    return mgr.get_or_create("cli", "default")


def _inject_boot_tasks(session, tasks: list[dict], config: dict | None = None) -> None:
    """将 boot_tasks 注入 session 并主动执行一轮 agent。"""
    _feishu_sender = None
    _progress_queue: queue.Queue[dict[str, Any]] | None = None
    _progress_done: threading.Event | None = None
    _pw: threading.Thread | None = None

    if config and session.channel == "feishu":
        owner_chat_id = config.get("feishu", {}).get("owner_chat_id", "").strip()
        app_id = config.get("feishu", {}).get("app_id", "").strip()
        app_secret = config.get("feishu", {}).get("app_secret", "").strip()
        if owner_chat_id and app_id and app_secret:
            from src.feishu.client import FeishuClient
            client = FeishuClient(app_id=app_id, app_secret=app_secret)
            _feishu_sender = lambda t: client.send_message(
                receive_id=owner_chat_id, text=t, receive_id_type="chat_id"
            )
            session.partial_sender = _feishu_sender
            session._reply_callback = _feishu_sender

            # ─── 进度卡片 worker ───
            from src.platforms.adapters.feishu import FeishuAdapter
            _progress_adapter = FeishuAdapter({
                "app_id": app_id,
                "app_secret": app_secret,
            })
            _progress_queue = queue.Queue()
            _progress_done = threading.Event()

            def _progress_worker() -> None:
                progress_lines: list[str] = []
                last_update_ts = 0.0
                update_interval = 1.5
                progress_msg_id: str | None = None
                _fail_count = 0
                while True:
                    try:
                        event = _progress_queue.get(timeout=0.5)  # type: ignore
                    except queue.Empty:
                        if _progress_done.is_set():  # type: ignore
                            if progress_lines and _fail_count < 3:
                                try:
                                    if progress_msg_id is None:
                                        progress_msg_id = _progress_adapter._send_progress_card(owner_chat_id, progress_lines, finished=True)
                                    else:
                                        _progress_adapter._update_progress_card(progress_msg_id, progress_lines, finished=True)
                                except Exception:
                                    pass
                            return
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "model_switch":
                        progress_lines.append(f"**[模型切换]** {event.get('message', '')}")
                    elif event.get("type") == "tool_progress":
                        round_n = event["round"]
                        tool = event["tool"]
                        args_p = event["args_preview"]
                        result_p = event["result_preview"]
                        is_error = result_p.startswith("[错误]") or result_p.startswith("[网络错误]")
                        icon = "x" if is_error else ">"
                        progress_lines.append(f"**{round_n}.** `{tool}`({args_p})\n  {icon} {result_p}")
                    now = time.monotonic()
                    if now - last_update_ts >= update_interval and _fail_count < 3 and progress_lines:
                        try:
                            if progress_msg_id is None:
                                progress_msg_id = _progress_adapter._send_progress_card(owner_chat_id, progress_lines)
                                if progress_msg_id is None:
                                    _fail_count += 1
                            else:
                                _progress_adapter._update_progress_card(progress_msg_id, progress_lines)
                        except Exception:
                            _fail_count += 1
                        last_update_ts = time.monotonic()

            _pw = threading.Thread(target=_progress_worker, daemon=True, name="boot-progress")
            _pw.start()

            def _progress_cb(event: dict) -> None:
                _progress_queue.put(event)  # type: ignore

            session.agent.progress_callback = _progress_cb
            session.agent.interim_sender = _feishu_sender

    lines = ["[系统] 你刚完成重启，有以下待办任务需要执行："]
    for i, t in enumerate(tasks, 1):
        desc = t.get("task", str(t))
        lines.append(f"{i}. {desc}")
    lines.append("请逐一通过飞书通知 owner。")

    prompt = "\n".join(lines)
    try:
        result = session.handle_input(prompt)
        if result.reply:
            logger.info(f"[daemon] boot_tasks 执行完成: {result.reply[:100]}")
        else:
            msg = "boot_tasks 执行完成但返回为空，可能 context 过长导致 LLM 无法响应。"
            logger.info(f"[daemon] {msg}")
            if _feishu_sender:
                try:
                    _feishu_sender(msg)
                except Exception:
                    pass
    except Exception as e:
        err_msg = f"boot_tasks 执行失败: {e}"
        logger.info(f"[daemon] {err_msg}")
        if _feishu_sender:
            try:
                _feishu_sender(f"⚠️ 启动待办任务执行失败：{e}")
            except Exception:
                pass
    finally:
        if _progress_queue is not None:
            _progress_done.set()  # type: ignore
            try:
                if _pw is not None:
                    _pw.join(timeout=3.0)
            except Exception:
                pass
            session.agent.progress_callback = None
            session.agent.interim_sender = None
