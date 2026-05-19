"""Lamix Daemon 主进程。

职责：
1. 加载配置、初始化 SessionManager
2. 启动多平台消息网关（PlatformManager）
3. 启动心跳（HeartbeatManager）
4. 启动任务调度器（TaskScheduler）：自我审计
5. 启动后执行 boot_tasks（重启前指定的待办）
6. 主线程阻塞（signal 驱动优雅退出）
7. 退出时保存 session
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from pathlib import Path

from src.core.config import load_config, is_config_complete, LAMIX_DIR
from src.core.heartbeat import HeartbeatManager
from src.core.session_manager import get_session_manager
from src.core.self_audit import (
    run_audit,
    format_report_detail,
    save_report,
    _audit_log,
)
from src.core.constants import DEFAULT_AUDIT_HOUR, DEFAULT_AUDIT_MINUTE, REPORT_MAX_LENGTH
from src.core.task_scheduler import TaskScheduler, TaskType, TaskConfig, schedule, start as scheduler_start, shutdown as scheduler_shutdown
from src.core.tools import load_skill_scripts
import logging
import setproctitle

logger = logging.getLogger(__name__)

LOG_DIR = LAMIX_DIR / "logs"
_BOOT_TASKS_PATH = LAMIX_DIR / "boot_tasks.json"
_DAEMON_PID_PATH = LOG_DIR / "daemon.pid"
_shutdown = threading.Event()
_heartbeat_mgr: HeartbeatManager | None = None
_scheduler: TaskScheduler | None = None
SAFE_MODE_SCRIPT = Path(__file__).resolve().parent / "safe_mode.py"

# boot_tasks 限制
_MAX_TASKS = 20
_MAX_TOTAL_BYTES = 10 * 1024  # 10KB



def _notify_user(text: str, config: dict | None = None) -> None:
    """通过用户当前渠道发送通知。

    优先使用 session 的 partial_sender（自动匹配渠道），
    fallback 到 print（CLI 模式或无 session 时）。
    """
    # 优先走 session partial_sender
    try:
        from src.tools import session as session_tool
        current_session = session_tool.get_current_session()
        if current_session and current_session.partial_sender:
            current_session.partial_sender(text)
            return
    except Exception:
        pass
    # Fallback: 尝试飞书直发，失败则 print
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


# ── 任务回调 ────────────────────────────────────────────────────────────────


def _do_self_audit() -> None:
    """实际执行审计任务（在后台线程中运行）。"""
    from datetime import datetime

    now = datetime.now()

    try:
        report = run_audit()
        report_content = format_report_detail(report)
        if len(report_content) > REPORT_MAX_LENGTH:
            report_content = report_content[:REPORT_MAX_LENGTH] + "\n\n...（报告过长已截断）"
        _audit_log("[self_audit] 审计完成，开始发送报告")
        from src.core.config import load_config
        audit_config = load_config()
        _notify_user(f"[Lamix] 自我审计报告\n\n{report_content}", config=audit_config)
        report_path = save_report(report)
        _audit_log(f"[self_audit] 报告已保存至 {report_path}")
        logger.info("[self_audit] 每日审计完成")
        # 记录审计时间
        last_audit_file = LAMIX_DIR / "logs" / ".last_audit_time"
        last_audit_file.parent.mkdir(parents=True, exist_ok=True)
        last_audit_file.write_text(now.isoformat(), encoding="utf-8")
    except Exception as e:
        logger.error(f"[self_audit] 执行失败: {e}")


def _self_audit_callback() -> None:
    """审计任务：每天凌晨 4 点由 cron 触发，在后台线程中执行。"""
    thread = threading.Thread(target=_do_self_audit, daemon=True)
    thread.start()
    _audit_log("[self_audit] 审计任务已提交到后台执行")


def _register_tasks(session=None) -> None:
    """注册所有定时任务。"""
    global _scheduler
    _scheduler = TaskScheduler()
    if session is not None:
        from src.core.task_scheduler import set_session
        set_session(session)
    scheduler_start()

    # 自我审计：每天凌晨 4 点执行一次
    schedule(TaskConfig(
        task_id="self_audit_check",
        task_type=TaskType.CRON,
        cron_hour=DEFAULT_AUDIT_HOUR,
        cron_minute=DEFAULT_AUDIT_MINUTE,
        func=_self_audit_callback,
        description="每日审计（凌晨4点）",
    ))

    logger.info("[daemon] 任务调度器已启动（审计检查）")


# ── 信号与退出 ───────────────────────────────────────────────────────────────


def _signal_handler(signum: int, _frame: object | None) -> None:
    logger.info(f"\n[daemon] 收到信号 {signum}，准备退出...")
    _shutdown.set()



def _check_single_instance() -> None:
    """检查是否已有 daemon 实例在运行，若有则退出。

    两层检测：
    1. PID 文件 → 快速路径，检查文件中记录的进程是否存活
    2. 进程名扫描 → 扫描所有名为 "lamix" 的进程，杀掉孤儿后继续

    确保任何时刻只有一个 lamix daemon 在运行。
    """
    my_pid = os.getpid()

    # ── 第一层：PID 文件检查 ──
    if _DAEMON_PID_PATH.exists():
        try:
            old_pid = int(_DAEMON_PID_PATH.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            _DAEMON_PID_PATH.unlink(missing_ok=True)
        else:
            if old_pid != my_pid:
                from src.platforms.process_manager import get_process_manager
                pm = get_process_manager()
                if pm.is_alive(old_pid):
                    logger.error(f"[daemon] 已有 daemon 实例在运行 (PID={old_pid})，退出")
                    sys.exit(0)
                else:
                    logger.info(f"[daemon] 旧进程 {old_pid} 已死，清理 PID 文件")
                    _DAEMON_PID_PATH.unlink(missing_ok=True)

    # ── 第二层：按进程名扫描所有 "lamix" 进程 ──
    # 场景：PID 文件指向已死进程，但有其他 lamix 进程（孤儿/残留）还在跑
    _kill_other_lamix_processes(my_pid)


def _kill_other_lamix_processes(my_pid: int) -> None:
    """扫描并杀掉所有非本进程的 "lamix" daemon 进程。

    通过 pgrep 查找进程名精确匹配 "lamix" 的进程（setproctitle 设置的名称）。
    杀掉后等待最多 3 秒确认退出。
    """
    import signal as sig_module

    other_pids = _find_lamix_pids(exclude_pid=my_pid)
    if not other_pids:
        return

    logger.warning(f"[daemon] 发现残留 lamix 进程: {other_pids}，正在清理")

    # SIGTERM 优雅终止
    for pid in other_pids:
        try:
            os.kill(pid, sig_module.SIGTERM)
        except OSError:
            pass

    # 等待最多 3 秒
    for _ in range(30):
        still_alive = [p for p in other_pids if _pid_exists(p)]
        if not still_alive:
            break
        time.sleep(0.1)

    # 还没死就 SIGKILL
    still_alive = [p for p in other_pids if _pid_exists(p)]
    if still_alive:
        logger.warning(f"[daemon] SIGTERM 未杀掉 {still_alive}，发送 SIGKILL")
        for pid in still_alive:
            try:
                os.kill(pid, sig_module.SIGKILL)
            except OSError:
                pass
        time.sleep(0.3)

    # 最终确认
    final_alive = [p for p in other_pids if _pid_exists(p)]
    if final_alive:
        logger.error(f"[daemon] 无法杀掉残留进程 {final_alive}，仍然启动（可能为僵尸）")
    else:
        logger.info(f"[daemon] 已清理 {len(other_pids)} 个残留 lamix 进程")


def _find_lamix_pids(exclude_pid: int) -> list[int]:
    """查找所有名为 "lamix" 的进程 PID（排除 exclude_pid）。

    优先用 pgrep -x（精确匹配进程名），fallback 到 ps + grep。
    """
    # 方法 1: pgrep -x lamix（精确匹配进程名）
    try:
        result = subprocess.run(
            ["pgrep", "-x", "lamix"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            pids = [int(p.strip()) for p in result.stdout.strip().split("\n") if p.strip()]
            return [p for p in pids if p != exclude_pid]
    except Exception:
        pass

    # 方法 2 fallback: ps + grep
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,comm"],
            capture_output=True, text=True, timeout=5,
        )
        pids = []
        for line in result.stdout.strip().split("\n")[1:]:  # 跳过 header
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "lamix":
                pid = int(parts[0])
                if pid != exclude_pid:
                    pids.append(pid)
        return pids
    except Exception:
        return []


def _pid_exists(pid: int) -> bool:
    """检查 PID 是否存在（os.kill(pid, 0)）。"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_daemon_pid() -> None:
    """写 daemon pid 到文件，供 watchdog 查找。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _DAEMON_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _send_boot_notification(config: dict, pid: int) -> None:
    """常驻上线通知：优先发 open_id 私聊，失败则发 owner_chat_id 群聊。"""
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
        text = f"Lamix 已上线 (PID={pid})"

        # 优先尝试 user_open_id 私聊（一定可以发），再试 owner_chat_id 群聊
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

    # 优先直接用飞书 API 发送（解决 daemon 启动早期没有 session 的问题）
    feishu_cfg = (config or {}).get("feishu", {}) or {}
    owner_chat_id = feishu_cfg.get("owner_chat_id", "").strip()
    user_open_id = feishu_cfg.get("user_open_id", "").strip()
    app_id = feishu_cfg.get("app_id", "").strip()
    app_secret = feishu_cfg.get("app_secret", "").strip()

    if app_id and app_secret:
        try:
            from src.feishu.client import FeishuClient
            client = FeishuClient(app_id=app_id, app_secret=app_secret)
            # 优先 open_id 私聊
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

    # Fallback: session 可能已经存在的情况
    _notify_user(message)


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

    # 飞书 session 需要 partial_sender 才能把回复发出去
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

            # ─── 进度卡片 worker（通过 adapter 抽象层，支持多渠道扩展）───
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
                        if _progress_done.is_set():
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
            # 空结果也通知用户
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
        # 失败时通知用户
        if _feishu_sender:
            try:
                _feishu_sender(f"⚠️ 启动待办任务执行失败：{e}")
            except Exception:
                pass
    finally:
        # 清理进度回调
        if _progress_queue is not None:
            _progress_done.set()  # type: ignore
            try:
                _pw.join(timeout=3.0)  # type: ignore
            except Exception:
                pass
            session.agent.progress_callback = None
            session.agent.interim_sender = None

def _patch_websockets_ssl() -> None:
    """Monkey-patch websockets.connect 使用 certifi CA 证书。

    macOS launchd / Windows 环境下 Python 默认 SSL context 可能缺少中间 CA
    （尤其有 VPN/代理时），导致飞书 WebSocket 长连接 SSL 握手失败。
    用 certifi 的 CA bundle 更可靠。
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

        # 保留原始签名属性，避免 websockets 内部检查报错
        _patched_connect.__wrapped__ = _original_connect  # type: ignore[attr-defined]
        websockets.connect = _patched_connect
        logger.info("[daemon] websockets SSL patch 已应用 (certifi CA)")
    except ImportError:
        logger.warning("[daemon] certifi 未安装，跳过 websockets SSL patch")
    except Exception as e:
        logger.error(f"[daemon] websockets SSL patch 失败: {e}")


def main() -> None:
    global _heartbeat_mgr, _scheduler

    # 设置进程名为 lamix，方便 ps/任务管理器识别
    try:
        setproctitle.setproctitle("lamix")
    except Exception:
        pass

    # 单实例检测：防止多个 daemon 同时运行争抢飞书消息
    _check_single_instance()

    # 配置 logging：直接用 FileHandler 写文件，不依赖 stderr 重定向
    LOG_DIR = LAMIX_DIR / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    file_handler = logging.FileHandler(
        LOG_DIR / "daemon_error.log",
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[file_handler],
    )

    # 修复飞书 WebSocket SSL 证书验证失败（macOS launchd / Windows 均可能出现）
    _patch_websockets_ssl()

    parser = argparse.ArgumentParser(
        prog="lamix gateway",
        description="Lamix 常驻 daemon：多平台消息网关 + 飞书 WebSocket 长连接监听。",
    )
    parser.parse_known_args()

    config = load_config()
    if not is_config_complete(config):
        if sys.stdin.isatty():
            # 有交互终端：走安装引导
            logger.warning("[daemon] LLM 未配置，启动安装引导...")
            _write_daemon_pid()
            _heartbeat_mgr = HeartbeatManager(task_id="daemon")
            _heartbeat_mgr.start()
            try:
                from src.core.config import run_setup_wizard
                config = run_setup_wizard()
            except (KeyboardInterrupt, EOFError, SystemExit):
                logger.info("\n[daemon] 配置已取消，退出。")
                _heartbeat_mgr.stop(user_initiated=True)
                return
            if not is_config_complete(config):
                logger.info("[daemon] API Key 未填写，无法启动，退出。")
                _heartbeat_mgr.stop(user_initiated=True)
                return
            logger.info("[daemon] 安装引导完成，继续启动...")
            _heartbeat_mgr.stop(user_initiated=True)
            _heartbeat_mgr = None
        else:
            # 无交互终端（被 launchd 调用）：空转等配置变更
            logger.warning("[daemon] LLM 未配置，等待配置完成（可通过 'lamix cli' 或 'lamix model' 配置）...")
            _write_daemon_pid()
            _heartbeat_mgr = HeartbeatManager(task_id="daemon")
            _heartbeat_mgr.start()
            _shutdown.wait()
            _heartbeat_mgr.stop(user_initiated=True)
            return

    # ── 启动时强制刷新索引 ──────────────────────────────────────────────
    from src.core.indexer import ProjectIndex, SkillIndex
    from src.core.config import INDEX_DIR, SKILLS_DIR, PROJECTS_DIR

    try:
        skills_path = Path(str(config.get("skills_path", str(SKILLS_DIR)))).expanduser()
        projects_path = Path(str(config.get("projects_path", str(PROJECTS_DIR)))).expanduser()
        embedding_cfg = config.get("retrieval", {})

        skill_index = SkillIndex(skills_path, INDEX_DIR)
        skill_index.load_or_build()
        project_index = ProjectIndex(projects_path, INDEX_DIR, embedding_config=embedding_cfg)
        project_index.load_or_build()
        logger.info("[daemon] 索引已强制刷新 (skill=%d, project=%d)",
                     len(skill_index._entries), len(project_index._entries))
    except Exception as e:
        logger.warning(f"[daemon] 索引刷新失败: {e}")

    # ── 初始化 SessionManager ──────────────────────────────────────────────
    mgr = get_session_manager(config)
    session = mgr.get_or_create("cli", "default")

    # ── 初始化并启动 PlatformManager ──────────────────────────────────────
    from src.platforms.manager import PlatformManager
    from src.platforms.adapters.feishu import FeishuAdapter

    pm = PlatformManager(config)
    PlatformManager._instance = pm

    feishu_cfg = config.get("feishu", {})
    if feishu_cfg.get("app_id") and feishu_cfg.get("app_secret"):
        feishu_adapter = FeishuAdapter({
            "app_id": feishu_cfg["app_id"],
            "app_secret": feishu_cfg["app_secret"],
        })
        feishu_adapter.safe_mode_callback = lambda: _trigger_safe_mode(pm, mgr)
        feishu_adapter._shutdown_callback = lambda: _shutdown.set()
        pm.register(feishu_adapter)
        feishu_adapter.start()
        logger.info("[daemon] 飞书 adapter 已启动")

    # ── 写 pid ───────────────────────────────────────────────────────────
    pid = os.getpid()
    _write_daemon_pid()
    logger.info(f"[daemon] Lamix daemon 已启动 (PID={pid})")

    # ── 启动心跳 ──────────────────────────────────────────────────────────
    _heartbeat_mgr = HeartbeatManager(task_id="daemon")
    _heartbeat_mgr.start()
    logger.info("[daemon] 心跳已启动")

    # ── 任务调度器（自我审计）──────────────────────────────────────────
    _register_tasks(session)

    # ── 加载 skill scripts（延迟，避免循环导入）──────────────────────
    load_skill_scripts()
    logger.info("[daemon] skill scripts 已加载")

    # ── 上线通知 ─────────────────────────────────────────────────────────
    _send_boot_notification(config, pid)

    # ── boot_tasks ──────────────────────────────────────────────────────
    tasks = _load_and_clear_boot_tasks()
    if tasks:
        logger.info(f"[daemon] 发现 {len(tasks)} 条 boot_tasks，开始执行")
        # 飞书提示用户有 boot task 正在执行
        _notify_boot_tasks_running(config, tasks)
        boot_session = _get_boot_tasks_session(mgr, config)
        _inject_boot_tasks(boot_session, tasks, config=config)

    # ── Config 热重载检测 ────────────────────────────────────────────────
    _start_config_watcher(pm, config)

    # ── Memory 目录变更检测（自动刷新索引）─────────────────────────────
    _start_memory_watcher(mgr)

    # ── 主事件循环 ────────────────────────────────────────────────────────
    try:
        asyncio.run(pm.run())
    except KeyboardInterrupt:
        logger.info("[daemon] 收到 KeyboardInterrupt")

    # ── 优雅退出 ─────────────────────────────────────────────────────────
    if _heartbeat_mgr is not None:
        _heartbeat_mgr.stop(user_initiated=True)
        logger.info("[daemon] 心跳已停止")

    if _scheduler is not None:
        scheduler_shutdown()
        _scheduler = None
        logger.info("[daemon] 任务调度器已停止")

    try:
        mgr.close_all()
    except Exception as e:
        logger.error(f"[daemon] 保存会话时出错: {e}")

    if _DAEMON_PID_PATH.exists():
        try:
            _DAEMON_PID_PATH.unlink()
        except OSError:
            pass

    logger.info("[daemon] 已退出。")


# ── Safe Mode 切换 ─────────────────────────────────────────────────────────


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


import hashlib
import json


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

    每 30 秒检查 config.yaml，在任何关键配置（llm/models/feishu/retrieval/skills_management）变更时触发热重载。
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

                # 重新加载配置并计算指纹
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

    # 1. 标记旧 adapter 为 stopped（不关闭 WS，避免 event loop 冲突）
    old_adapter = pm._adapters.get("feishu")
    if old_adapter is not None:
        try:
            old_adapter._stopped = True
            del pm._adapters["feishu"]
            logger.info("[daemon] 旧飞书 adapter 已标记为 stopped")
        except Exception as e:
            logger.error(f"[daemon] 标记旧飞书 adapter 失败: {e}")

    # 2. 读新配置
    feishu_cfg = config.get("feishu", {})
    app_id = feishu_cfg.get("app_id", "").strip()
    app_secret = feishu_cfg.get("app_secret", "").strip()

    if not app_id or not app_secret:
        logger.info("[daemon] 新配置中无飞书凭证，不启动 adapter")
        return

    # 3. 创建并启动新的
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

    # 1. 检测 feishu 变更
    old_feishu = old_config.get("feishu", {})
    new_feishu = new_config.get("feishu", {})
    if old_feishu.get("app_id") != new_feishu.get("app_id") or \
       old_feishu.get("app_secret") != new_feishu.get("app_secret"):
        logger.info("[daemon] feishu 凭证变更，热重载飞书 adapter...")
        _reload_feishu_adapter(pm, new_config)

    # 2. 检测 llm/models 变更
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

    # 3. 检测 retrieval/skills_management 变更
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
    from src.core.compaction import _build_compaction_config

    primary_llm, primary_adapter = _create_llm(config, channel="cli")
    primary_name = config["llm"]["model"]
    primary_cw = config["llm"].get("context_window")

    # 构建多模型客户端字典
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
                "context_window": model_cfg.get("context_window"),
            }

    # 构建 fallback_models
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

def _trigger_safe_mode(pm, mgr) -> None:
    """由 FeishuAdapter 触发：切换到 safe_mode 后再恢复 daemon。"""
    logger.info("[daemon] 切换到 Safe Mode...")

    if _heartbeat_mgr is not None:
        try:
            _heartbeat_mgr.stop(user_initiated=True)
            logger.info("[daemon] 心跳已停止")
        except Exception as e:
            logger.error(f"[daemon] 停止心跳出错: {e}")

    global _scheduler
    if _scheduler is not None:
        scheduler_shutdown()
        _scheduler = None
        logger.info("[daemon] 任务调度器已停止")

    # 停止所有 adapter
    import asyncio
    for adapter in list(pm._adapters.values()):
        try:
            asyncio.run(adapter.shutdown())
            logger.info(f"[daemon] {adapter.platform} adapter 已关闭")
        except Exception as e:
            logger.error(f"[daemon] 关闭 {adapter.platform} 失败: {e}")

    # 保存 session
    try:
        mgr.close_all()
        logger.info("[daemon] Session 已保存")
    except Exception as e:
        logger.error(f"[daemon] 保存会话出错: {e}")

    # 启动 safe_mode.py（独立进程，阻塞等待它结束）
    if SAFE_MODE_SCRIPT.exists():
        logger.info(f"[daemon] 启动 safe_mode: {SAFE_MODE_SCRIPT}")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(SAFE_MODE_SCRIPT)],
                cwd=str(LAMIX_DIR.parent / "lamix"),
                stdout=open(LOG_DIR / "safe_mode.log", "a", encoding="utf-8"),
                stderr=open(LOG_DIR / "safe_mode.err.log", "a", encoding="utf-8"),
            )
            logger.info(f"[daemon] safe_mode 进程已启动 (PID={proc.pid})")
            proc.wait()
            logger.info(f"[daemon] safe_mode 已退出 (code={proc.returncode})")
        except Exception as e:
            logger.error(f"[daemon] safe_mode 启动失败: {e}")
    else:
        logger.warning(f"[daemon] safe_mode 脚本不存在: {SAFE_MODE_SCRIPT}")

    # safe_mode 结束，恢复 daemon
    logger.info("[daemon] Safe Mode 退出，重启 daemon...")
    _restore_daemon(pm, mgr)


def _restore_daemon(pm, mgr) -> None:
    """safe_mode 结束后，重新初始化 adapter、调度器和心跳。"""
    from src.core.config import load_config

    config = load_config()
    if not is_config_complete(config):
        logger.error("[daemon] 恢复失败：配置不完整")
        _shutdown.set()
        return

    # 重建 session
    session = mgr.get_or_create("cli", "default")

    # 重新启动所有 adapter
    import asyncio
    feishu_cfg = config.get("feishu", {})
    if feishu_cfg.get("app_id") and feishu_cfg.get("app_secret"):
        from src.platforms.adapters.feishu import FeishuAdapter
        feishu_adapter = FeishuAdapter({
            "app_id": feishu_cfg["app_id"],
            "app_secret": feishu_cfg["app_secret"],
        })
        feishu_adapter.safe_mode_callback = lambda: _trigger_safe_mode(pm, mgr)
        feishu_adapter._shutdown_callback = lambda: _shutdown.set()
        feishu_adapter.session_manager = mgr
        pm.register(feishu_adapter)
        try:
            feishu_adapter.start()
            logger.info("[daemon] 飞书 adapter 已恢复")
        except Exception as e:
            logger.error(f"[daemon] 恢复飞书 adapter 失败: {e}")

    # 恢复心跳
    global _heartbeat_mgr, _scheduler
    _heartbeat_mgr = HeartbeatManager(task_id="daemon")
    _heartbeat_mgr.start()
    logger.info("[daemon] 心跳已恢复")

    # 恢复任务调度器
    _register_tasks(session)
    load_skill_scripts()
    logger.info("[daemon] skill scripts 已加载")

    # 上线通知
    from src.feishu.client import FeishuClient
    owner_chat_id = config.get("feishu", {}).get("owner_chat_id", "").strip()
    app_id = config.get("feishu", {}).get("app_id", "").strip()
    app_secret = config.get("feishu", {}).get("app_secret", "").strip()
    if owner_chat_id and app_id and app_secret:
        try:
            client = FeishuClient(app_id=app_id, app_secret=app_secret)
            client.send_message(
                receive_id=owner_chat_id,
                text="✅ Safe Mode 已退出，主程序已恢复。",
                receive_id_type="chat_id",
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
