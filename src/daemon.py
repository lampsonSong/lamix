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
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import setproctitle

from src.core.config import LAMIX_DIR, is_config_complete, load_config
from src.core.constants import DEFAULT_AUDIT_HOUR, DEFAULT_AUDIT_MINUTE, REPORT_MAX_LENGTH
from src.core.heartbeat import HeartbeatManager
from src.core.process_launch import (
    ROLE_DAEMON,
    is_frozen,
    pid_role,
    process_exists as _proc_exists,
    read_pid_record,
    safe_mode_launch_cmd,
    write_pid_record,
)
from src.core.self_audit import (
    _audit_log,
    format_report_detail,
    run_audit,
    save_report,
)
from src.core.session_manager import get_session_manager
from src.core.task_scheduler import (
    TaskConfig,
    TaskScheduler,
    TaskType,
    schedule,
    shutdown as scheduler_shutdown,
    start as scheduler_start,
)
from src.core.tools import load_skill_scripts
from src.daemon_boot import (
    _LAST_NOTIFY_PATH,
    _NOTIFY_COOLDOWN_SECONDS,
    _check_notify_cooldown,
    _get_boot_tasks_session,
    _inject_boot_tasks,
    _load_and_clear_boot_tasks,
    _notify_boot_tasks_running,
    _notify_user,
    _record_notify_sent,
    _send_boot_notification,
    _write_boot_task,
)
from src.daemon_reload import (
    _config_fingerprint,
    _get_feishu_credentials,
    _patch_websockets_ssl,
    _reload_config,
    _reload_feishu_adapter,
    _reload_llm_clients,
    _start_config_watcher,
    _start_memory_watcher,
)
from src.daemon_state import _shutdown

logger = logging.getLogger(__name__)

LOG_DIR = LAMIX_DIR / "logs"
_DAEMON_PID_PATH = LOG_DIR / "daemon.pid"
_RESTART_FLAG_PATH = LAMIX_DIR / ".restart_by_watchdog"

_heartbeat_mgr: HeartbeatManager | None = None
_scheduler: TaskScheduler | None = None
SAFE_MODE_SCRIPT = Path(__file__).resolve().parent / "safe_mode.py"


# ── watchdog 重启标志 ────────────────────────────────────────────────────────


def _check_restart_flag() -> tuple[int | None, bool]:
    """检查是否被 watchdog 重启。

    Returns:
        (old_pid, was_restarted): old_pid 为发起重启的旧进程 PID（可能是 None），
        was_restarted 表示是否检测到重启标志。
    """
    if not _RESTART_FLAG_PATH.exists():
        return None, False
    try:
        old_pid = int(_RESTART_FLAG_PATH.read_text(encoding="utf-8").strip())
        _RESTART_FLAG_PATH.unlink()
        return old_pid, True
    except (ValueError, OSError):
        return None, False


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
        audit_config = load_config()
        _notify_user(f"[Lamix] 自我审计报告\n\n{report_content}", config=audit_config)
        report_path = save_report(report)
        _audit_log(f"[self_audit] 报告已保存至 {report_path}")
        logger.info("[self_audit] 每日审计完成")
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


# ── 单实例检测 ───────────────────────────────────────────────────────────────


def _check_single_instance() -> None:
    """检查是否已有 daemon 实例在运行，若有则退出。

    两层检测：
    1. PID 文件 → 快速路径，读取记录的 PID 并校验其身份确为 daemon
       （防止 daemon.pid 里是 cli 或被复用的 PID 导致误判为“已在运行”）
    2. 进程名扫描 → 扫描所有真正的 daemon 进程（comm=lamix 且命令行含 daemon 标识），
       杀掉孤儿后继续。仅杀 daemon，不误伤 cli/watchdog。
    """
    my_pid = os.getpid()

    if _DAEMON_PID_PATH.exists():
        old_pid, _old_role = read_pid_record(_DAEMON_PID_PATH)
        if old_pid is None:
            _DAEMON_PID_PATH.unlink(missing_ok=True)
        elif old_pid != my_pid:
            if _proc_exists(old_pid) and pid_role(old_pid) == ROLE_DAEMON:
                logger.error(f"[daemon] 已有 daemon 实例在运行 (PID={old_pid})，退出")
                sys.exit(0)
            else:
                logger.info(
                    f"[daemon] pid 文件中的 {old_pid} 不是活着的 daemon（可能已死或身份不匹配），清理"
                )
                _DAEMON_PID_PATH.unlink(missing_ok=True)

    _kill_other_lamix_processes(my_pid)


def _kill_other_lamix_processes(my_pid: int) -> None:
    """扫描并杀掉所有非本进程的、且确认身份为 daemon 的残留 lamix 进程。"""
    import signal as sig_module
    import time

    candidate_pids = _find_lamix_pids(exclude_pid=my_pid)
    if not candidate_pids:
        return

    other_pids = [p for p in candidate_pids if pid_role(p) == ROLE_DAEMON]
    if not other_pids:
        return

    logger.warning(f"[daemon] 发现残留 daemon 进程: {other_pids}，正在清理")

    for pid in other_pids:
        try:
            os.kill(pid, sig_module.SIGTERM)
        except OSError:
            pass

    for _ in range(30):
        still_alive = [p for p in other_pids if _pid_exists(p)]
        if not still_alive:
            break
        time.sleep(0.1)

    still_alive = [p for p in other_pids if _pid_exists(p)]
    if still_alive:
        logger.warning(f"[daemon] SIGTERM 未杀掉 {still_alive}，发送 SIGKILL")
        for pid in still_alive:
            try:
                os.kill(pid, sig_module.SIGKILL)
            except OSError:
                pass
        time.sleep(0.3)

    final_alive = [p for p in other_pids if _pid_exists(p)]
    if final_alive:
        logger.error(f"[daemon] 无法杀掉残留进程 {final_alive}（可能为僵尸），拒绝启动")
        sys.exit(1)
    else:
        logger.info(f"[daemon] 已清理 {len(other_pids)} 个残留 lamix 进程")


def _find_lamix_pids(exclude_pid: int) -> list[int]:
    """查找所有名为 "lamix" 的进程 PID（排除 exclude_pid）。"""
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

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,comm"],
            capture_output=True, text=True, timeout=5,
        )
        pids = []
        for line in result.stdout.strip().split("\n")[1:]:
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
    """写 daemon pid（含 role 字段）到文件，供 watchdog / gateway 查找。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    write_pid_record(_DAEMON_PID_PATH, os.getpid(), ROLE_DAEMON)


# ── main ───────────────────────────────────────────────────────────────────


def main() -> None:
    global _heartbeat_mgr, _scheduler

    try:
        setproctitle.setproctitle("lamix")
    except Exception:
        pass

    _check_single_instance()

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

    _patch_websockets_ssl()

    parser = argparse.ArgumentParser(
        prog="lamix gateway",
        description="Lamix 常驻 daemon：多平台消息网关 + 飞书 WebSocket 长连接监听。",
    )
    parser.parse_known_args()

    config = load_config()
    if not is_config_complete(config):
        if sys.stdin.isatty():
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

    pid = os.getpid()
    _write_daemon_pid()
    logger.info(f"[daemon] Lamix daemon 已启动 (PID={pid})")

    _heartbeat_mgr = HeartbeatManager(task_id="daemon")
    _heartbeat_mgr.start()
    logger.info("[daemon] 心跳已启动")

    _register_tasks(session)

    load_skill_scripts()
    logger.info("[daemon] skill scripts 已加载")

    old_pid, is_recovery = _check_restart_flag()
    if is_recovery:
        logger.info(f"[daemon] 被 watchdog 重启恢复 (旧 PID={old_pid})")

    _send_boot_notification(config, pid, is_recovery=is_recovery)

    tasks = _load_and_clear_boot_tasks()
    if tasks:
        logger.info(f"[daemon] 发现 {len(tasks)} 条 boot_tasks，开始执行")
        _notify_boot_tasks_running(config, tasks)
        boot_session = _get_boot_tasks_session(mgr, config)
        _inject_boot_tasks(boot_session, tasks, config=config)

    _start_config_watcher(pm, config)
    _start_memory_watcher(mgr)

    try:
        asyncio.run(pm.run())
    except KeyboardInterrupt:
        logger.info("[daemon] 收到 KeyboardInterrupt")

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

    for adapter in list(pm._adapters.values()):
        try:
            asyncio.run(adapter.shutdown())
            logger.info(f"[daemon] {adapter.platform} adapter 已关闭")
        except Exception as e:
            logger.error(f"[daemon] 关闭 {adapter.platform} 失败: {e}")

    try:
        mgr.close_all()
        logger.info("[daemon] Session 已保存")
    except Exception as e:
        logger.error(f"[daemon] 保存会话出错: {e}")

    if is_frozen() or SAFE_MODE_SCRIPT.exists():
        cmd = safe_mode_launch_cmd(str(SAFE_MODE_SCRIPT))
        logger.info(f"[daemon] 启动 safe_mode: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
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

    logger.info("[daemon] Safe Mode 退出，重启 daemon...")
    _restore_daemon(pm, mgr)


def _restore_daemon(pm, mgr) -> None:
    """safe_mode 结束后，重新初始化 adapter、调度器和心跳。"""
    config = load_config()
    if not is_config_complete(config):
        logger.error("[daemon] 恢复失败：配置不完整")
        _shutdown.set()
        return

    session = mgr.get_or_create("cli", "default")

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

    global _heartbeat_mgr, _scheduler
    _heartbeat_mgr = HeartbeatManager(task_id="daemon")
    _heartbeat_mgr.start()
    logger.info("[daemon] 心跳已恢复")

    _register_tasks(session)
    load_skill_scripts()
    logger.info("[daemon] skill scripts 已加载")

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
