"""Lamix 顶层命令分发器。

lamix              → 显示帮助（macOS/开发环境）/ 直接启动 CLI（Windows exe）
lamix cli [query]  → 交互式 CLI（启动 daemon + REPL）
lamix gateway      → 仅启动 daemon
lamix model        → 模型管理（占位）
lamix update       → 自更新（源码: git pull / Windows exe: 下载新版本）
lamix config       → 显示当前配置
lamix -V/--version → 版本号

Windows 上双击 lamix.exe（无参数）自动进入交互式 CLI 模式。
macOS/Linux 上无参数时显示帮助信息。
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
import sys
import textwrap
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.styles import Style

from src.core.config import (
    load_config,
    is_config_complete,
    run_setup_wizard,
    LAMIX_DIR,
)
from src.core.session_manager import get_session_manager
from src.memory import session_store

# 导入 CLI 样式和补全模块
from src.cli_ui.styled import (
    C, print_bot, print_command, print_info, print_success,
    print_warning, print_error, print_divider, print_banner,
    create_progress,
)
from src.cli_ui.completion import LamixCompleter, create_key_bindings

PROMPT_STYLE = Style.from_dict({
    "prompt": "ansigreen bold",
    "command": "ansigreen",
})

# ── 全局中断标志（用于 Ctrl+C 退出正在执行的命令） ──
_cli_interrupt_flag = threading.Event()
_cli_interrupted = False

def _set_interrupt():
    """由 signal handler 调用，设置中断标志。"""
    global _cli_interrupted
    _cli_interrupted = True
    _cli_interrupt_flag.set()

def _clear_interrupt():
    """清除中断标志（每次 prompt 前调用）。"""
    global _cli_interrupted
    _cli_interrupted = False
    _cli_interrupt_flag.clear()

def _check_interrupt():
    """检查是否收到中断信号。"""
    return _cli_interrupted




def _get_version() -> str:
    """从 pyproject.toml 的 importlib.metadata 读取版本号。"""
    try:
        from importlib.metadata import version
        return version("lamix")
    except Exception:
        return "0.2.0"


def _cli_partial_sender(text: str) -> None:
    """CLI 下 compaction 等进度文案即时打印。"""
    from src.cli_ui.styled import console
    console.print(f"[dim]{text}[/dim]", end="", flush=True)


def _cli_progress_callback(event: dict) -> None:
    """CLI 模式下的工具调用进度回调，实时打印到终端。"""
    if event.get("type") != "tool_progress":
        return
    tool = event.get("tool", "?")
    args_preview = event.get("args_preview", "")
    result_preview = event.get("result_preview", "")
    round_num = event.get("round", "?")
    from src.cli_ui.styled import print_tool, C
    print_tool(f"[{C.TOOL}]  [工具 {round_num}] {tool}({args_preview}) → {result_preview}")


def _maybe_greet_first_run(session) -> None:
    """首次运行时固定问候语。"""
    user_path = LAMIX_DIR / "USER.md"
    if not user_path.exists():
        return
    try:
        user_content = user_path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    # 判断是否仍是默认内容（只包含"称呼：用户"之类的占位）
    if "称呼：用户" in user_content or len(user_content) < 30:
        print()
        print_bot("你好！我是 Lamix，随时帮你探索这个世界的 AI 伙计 🚀")


def _run_repl(config: dict) -> None:
    """交互式 REPL 循环。"""
    # 打印 banner
    print_banner()
    print_divider()
    
    mgr = get_session_manager(config)
    session = mgr.get_or_create("cli", "default")
    # CLI 模式下设置 progress_callback，工具调用时实时打印进度
    session.agent.progress_callback = _cli_progress_callback
    session.partial_sender = _cli_partial_sender
    
    skill_count = len(session.skills)
    feishu_status = "已连接" if session.feishu_ready else "未配置"
    
    # 启动信息
    print_info(f"技能: {skill_count} 个  |  飞书: {feishu_status}")
    print_info("输入 /help 查看命令，Ctrl+C 或 /exit 退出。")
    print_divider()
    print()

    # 首次运行检测：USER.md 仍是默认内容时，自动发问候让对话自然开始
    _maybe_greet_first_run(session)

    # 加载历史记录用于补全
    history_file = LAMIX_DIR / ".repl_history"
    history_content = []
    if history_file.exists():
        try:
            history_content = history_file.read_text(encoding="utf-8").strip().splitlines()
        except Exception:
            pass
    
    # 创建补全器
    completer = LamixCompleter(history=history_content)
    # Ctrl+C 中断回调
    def _on_ctrl_c():
        _set_interrupt()
    
    key_bindings = create_key_bindings(interrupt_callback=_on_ctrl_c)
    
    # 设置 signal handler（用于 shell 命令执行期间）
    if sys.platform != "win32":
        signal.signal(signal.SIGINT, lambda s, f: _set_interrupt())

    prompt_session: PromptSession = PromptSession(
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
        style=PROMPT_STYLE,
        completer=completer,
        key_bindings=key_bindings,
        complete_while_typing=True,
        reserve_space_for_menu=8,
    )

    try:
        while True:
            _clear_interrupt()
            try:
                user_input = prompt_session.prompt(
                    [("class:prompt", "you> ")],
                ).strip()
            except (KeyboardInterrupt, EOFError):
                break

            if _check_interrupt():
                break
            if not user_input:
                continue

            # 打印用户输入（带样式）
            from src.cli_ui.styled import console
            console.print(f"[{C.USER_INPUT}]{user_input}[/{C.USER_INPUT}]")

            result = session.handle_input(user_input)

            if result.is_exit or _check_interrupt():
                break

            if result.is_new:
                # 通过 SessionManager 统一重置
                session = mgr.reset_session("cli", "default")
                session.agent.progress_callback = _cli_progress_callback
                session.partial_sender = _cli_partial_sender
                print_divider()
                print_info("新 session 已开始")
                print_divider()
                print()
                continue

            if result.reply:
                if result.is_command:
                    print_command(result.reply)
                else:
                    print_bot(result.reply)

                # 计划待确认时由用户选择是否执行
                if (
                    not result.is_command
                    and result.reply
                    and "请确认是否执行此计划" in result.reply
                ):
                    try:
                        confirm_input = prompt_session.prompt(
                            [("class:prompt", "确认执行？(y/n): ")],
                        ).strip().lower()
                    except (KeyboardInterrupt, EOFError):
                        confirm_input = "n"
                    if confirm_input in ("y", "yes", "是"):
                        exec_result = session.agent.confirm_and_execute()
                        if exec_result:
                            print()
                            print_bot(exec_result)
                    else:
                        cancel_result = session.agent.cancel_plan()
                        print_info(f"已取消: {cancel_result}")
                    
                    # 与 handle_input 一致：确认/取消后的回合也尝试压缩
                    try:
                        cr = session.agent.maybe_compact(
                            session_store=session_store,
                            session_id=session.session_id or "",
                            progress_callback=_cli_partial_sender,
                        )
                        if cr is not None:
                            if cr.success:
                                print_success(
                                    f"上下文压缩完成：归档 {cr.archived_count} 条内容，"
                                    f"{cr.tokens_before} → {cr.tokens_after} token"
                                )
                            else:
                                print_error(f"上下文压缩失败: {cr.error}")
                    except Exception:
                        pass

            if result.compaction_msg:
                print_info(result.compaction_msg)

    finally:
        print_divider()
        print_info("正在清理会话...")
        session.cleanup()
        print_success("再见！")


def _process_exists(pid: int) -> bool:
    """跨平台检查 PID 是否存活。
    
    Windows 上用 tasklist（os.kill(pid, 0) 在该平台不可靠，
    进程已死仍可能返回 True），其他平台用 os.kill(pid, 0)。
    """
    if sys.platform == "win32":
        import subprocess
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        except (subprocess.SubprocessError, OSError):
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _is_daemon_running() -> bool:
    """检查 daemon 是否在运行。"""
    pid_path = Path.home() / ".lamix" / "logs" / "daemon.pid"
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        return _process_exists(pid)
    except ValueError:
        return False


def _is_watchdog_running() -> bool:
    """检查 watchdog 是否在运行。"""
    # 先看 pid 文件
    pid_path = Path.home() / ".lamix" / "logs" / "watchdog.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            if _process_exists(pid):
                return True
        except ValueError:
            pass
    # 兜底：看进程名
    import subprocess

    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe"], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if "watchdog" in line.lower():
                return True
    else:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if "src.watchdog" in line and "grep" not in line:
                return True
    return False


_INSTANCE_LOCK_PATH = LAMIX_DIR / "instance.lock"


def _release_instance_lock() -> None:
    """释放单实例锁（仅当锁文件属于当前进程时）。"""
    try:
        if (
            _INSTANCE_LOCK_PATH.exists()
            and _INSTANCE_LOCK_PATH.read_text().strip() == str(os.getpid())
        ):
            _INSTANCE_LOCK_PATH.unlink()
    except OSError:
        pass


def _acquire_instance_lock() -> bool:
    """单实例锁：防止多个实例同时连接飞书/写入共享状态。

    锁文件写入当前 pid；若锁文件存在但对应进程已死（崩溃残留），
    自动接管；正常退出时通过 atexit 释放。
    """
    import atexit

    try:
        LAMIX_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True  # 无法创建目录时不阻断启动

    for _ in range(2):
        try:
            fd = os.open(
                _INSTANCE_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            atexit.register(_release_instance_lock)
            return True
        except FileExistsError:
            try:
                pid = int(_INSTANCE_LOCK_PATH.read_text().strip())
            except (ValueError, OSError):
                pid = -1
            # 锁持有者还活着（且不是自己）→ 拒绝启动
            if pid > 0 and pid != os.getpid() and _process_exists(pid):
                return False
            # 陈旧锁（进程已崩溃/被杀）：删除后重试
            try:
                _INSTANCE_LOCK_PATH.unlink()
            except OSError:
                return False
    return False


def _daemon_start_cmd() -> list[str]:
    """构造 daemon 启动命令。"""
    import importlib.util, shutil

    _is_frozen = getattr(sys, "frozen", False)
    python_exe = sys.executable

    if _is_frozen:
        return [python_exe, "gateway"]
    elif sys.platform == "win32":
        pythonw = python_exe.replace("python.exe", "pythonw.exe")
        if Path(pythonw).exists():
            python_exe = pythonw
        return [python_exe, "-m", "src.daemon"]
    else:
        lamix_path = shutil.which("lamix")
        if lamix_path:
            return [lamix_path, "gateway"]
        return [python_exe, "-m", "src.daemon"]


def _start_watchdog() -> None:
    """启动 watchdog（后台常驻）。"""
    import subprocess

    log_dir = Path.home() / ".lamix" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    watchdog_log = log_dir / "watchdog.log"
    watchdog_err = log_dir / "watchdog.err.log"

    subprocess.Popen(
        [sys.executable, "-m", "src.watchdog"],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=open(watchdog_log, "a", encoding="utf-8"),
        stderr=open(watchdog_err, "a", encoding="utf-8"),
        start_new_session=True,
    )
    import time
    time.sleep(3)


def _wait_daemon_ready(timeout: int = 15) -> bool:
    """等待 daemon 心跳文件出现。"""
    import time

    log_dir = Path.home() / ".lamix" / "logs"
    daemon_pid_path = log_dir / "daemon.pid"
    for _ in range(timeout):
        time.sleep(1)
        if daemon_pid_path.exists():
            try:
                pid = int(daemon_pid_path.read_text().strip())
                if _process_exists(pid):
                    return True
            except ValueError:
                pass
    return False


def gateway_start() -> None:
    """lamix gateway start: 启动 watchdog + daemon，不进 CLI。"""
    import subprocess

    # 检查是否已在跑
    if _is_daemon_running() or _is_watchdog_running():
        print_info("Lamix 已在运行，无需重复启动。")
        return

    # 配置检查：LLM 未配置则走 setup wizard（含 LLM + fallback + 飞书全套引导）
    config = load_config()
    if not is_config_complete(config):
        print_info("Lamix 未配置，开始引导配置...\n")
        try:
            config = run_setup_wizard()
        except (KeyboardInterrupt, EOFError):
            print_info("配置已取消，退出。")
            sys.exit(0)
        if not is_config_complete(config):
            print_error("API Key 未填写，退出。")
            sys.exit(1)

    # 启动 watchdog（由 watchdog 管理 daemon）
    print_info("正在启动 watchdog...")
    _start_watchdog()

    # 4. 等待 daemon 就绪
    print_info("正在等待 daemon 启动...")
    if _wait_daemon_ready():
        print_success("Lamix 已就绪 (PID 见 ~/.lamix/logs/daemon.pid)")
        print_info("可通过 'lamix cli' 或飞书私聊开始对话。")
    else:
        print_warning(
            "daemon 启动超时，请检查日志: ~/.lamix/logs/daemon_error.log"
        )


def gateway_stop() -> None:
    """lamix gateway stop: 停止 daemon + watchdog。"""
    import subprocess, signal, time

    log_dir = Path.home() / ".lamix" / "logs"

    # 停止 daemon
    daemon_pid_path = log_dir / "daemon.pid"
    if daemon_pid_path.exists():
        try:
            pid = int(daemon_pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print_info(f"daemon (PID={pid}) 正在停止...")
        except (ProcessLookupError, OSError, ValueError):
            pass

    # 停止 watchdog
    watchdog_pid_path = log_dir / "watchdog.pid"
    if watchdog_pid_path.exists():
        try:
            pid = int(watchdog_pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print_info(f"watchdog (PID={pid}) 正在停止...")
        except (ProcessLookupError, OSError, ValueError):
            pass

    # 等一下让进程自己退出，再强杀
    time.sleep(3)

    # 强杀残留进程
    if sys.platform == "win32":
        for name in ["src.daemon", "src.watchdog"]:
            try:
                result = subprocess.run(
                    ["wmic", "process", "where", f"(CommandLine like '%{name}%')", "get", "ProcessId"],
                    capture_output=True, text=True, timeout=10
                )
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line.isdigit():
                        subprocess.run(["taskkill", "/F", "/PID", line], capture_output=True)
            except Exception:
                pass
    else:
        for name in ["src.daemon", "src.watchdog"]:
            result = subprocess.run(
                ["pgrep", "-f", name], capture_output=True, text=True
            )
            for pid_str in result.stdout.strip().split("\n"):
                if pid_str:
                    try:
                        subprocess.run(["kill", "-9", pid_str])
                    except Exception:
                        pass

    # 清理 PID 文件，避免 stale pid 导致 _is_daemon_running() 误判
    for pid_file in ["daemon.pid", "watchdog.pid"]:
        p = log_dir / pid_file
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    print_success("Lamix 已停止。")


def gateway_restart() -> None:
    """lamix gateway restart: 停止后再启动 daemon + watchdog。"""
    import time

    # 先停止
    print_info("正在停止 Lamix...")
    gateway_stop()

    # 等待进程完全退出
    print_info("等待进程退出...")
    time.sleep(2)

    # 再启动
    print_info("正在启动 Lamix...")
    gateway_start()

def _ensure_watchdog_on_windows() -> None:
    """Windows 上确保 watchdog 在运行。"""
    import subprocess
    from pathlib import Path

    watchdog_pid_path = Path.home() / ".lamix" / "logs" / "watchdog.pid"
    if watchdog_pid_path.exists():
        try:
            pid = int(watchdog_pid_path.read_text().strip())
            if _process_exists(pid):
                return
        except ValueError:
            pass

    # 启动 watchdog
    python_exe = sys.executable
    log_dir = Path.home() / ".lamix" / "logs"
    watchdog_log = log_dir / "watchdog.log"

    try:
        subprocess.Popen(
            [python_exe, "-m", "src.watchdog"],
            stdout=open(watchdog_log, "a"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        import time
        time.sleep(0.5)
    except Exception as e:
        print_error(f"启动 watchdog 失败: {e}")


def _handle_gateway_predicate(predicate: str, config: dict) -> None:
    """处理 gateway 的谓词参数（如 --no-repl, --no-sandbox）。"""
    actions = predicate.split(",")
    feishu_enabled = "no-feishu" not in actions
    repl_enabled = "no-repl" not in actions
    sandbox_enabled = "no-sandbox" not in actions

    if feishu_enabled and config.get("feishu", {}).get("enabled", False):
        mgr = get_session_manager(config)
        session = mgr.get_or_create("gateway", "default")
        session.agent.progress_callback = _cli_progress_callback
        session.partial_sender = _cli_partial_sender

    if repl_enabled:
        print_info("启动交互式 CLI...")
        _run_repl(config)


def run_cli(args: argparse.Namespace) -> None:
    """'lamix cli' 子命令：继承原 main() 的所有逻辑。"""
    # 抑制第三方库的启动日志（jieba / lark_oapi），避免污染 CLI 输出
    import logging
    for _lib_logger in ("jieba", "Lark"):
        _log = logging.getLogger(_lib_logger)
        _log.setLevel(logging.WARNING)
        for _h in _log.handlers[:]:
            _log.removeHandler(_h)
        _sh = logging.StreamHandler()
        _sh.setLevel(logging.WARNING)
        _log.addHandler(_sh)

    # 确定非交互输入
    non_interactive_input: str | None = None
    is_slash_command = False

    if args.help_cmd:
        non_interactive_input, is_slash_command = "/help", True
    elif args.memory:
        non_interactive_input, is_slash_command = "/memory " + " ".join(args.memory), True
    elif args.skills:
        non_interactive_input, is_slash_command = "/skills " + " ".join(args.skills), True
    elif args.feishu:
        non_interactive_input, is_slash_command = "/feishu " + " ".join(args.feishu), True
    elif args.update:
        non_interactive_input, is_slash_command = "/update " + " ".join(args.update), True
    else:
        query = args.query_c or args.query
        if query:
            non_interactive_input = query
        elif not sys.stdin.isatty():
            piped = sys.stdin.read().strip()
            if piped:
                non_interactive_input = piped

    config = load_config()
    if not is_config_complete(config):
        if non_interactive_input is not None:
            print_error("Lamix 未配置，请先运行 lamix cli 进入交互模式完成配置。")
            sys.exit(1)
        try:
            config = run_setup_wizard()
        except (KeyboardInterrupt, EOFError):
            print_info("配置已取消，退出。")
            sys.exit(0)
        if not is_config_complete(config):
            print_error("API Key 未填写，无法启动。")
            sys.exit(1)

    # 检查 daemon 是否在跑，没跑则提示先启动
    if not _is_daemon_running():
        print_error("daemon 未运行，请先执行: lamix gateway start")
        sys.exit(1)

    _init_platform(config)

    # 单条查询模式
    if non_interactive_input:
        mgr = get_session_manager(config)
        session = mgr.get_or_create("cli", "default")
        session.agent.progress_callback = _cli_progress_callback
        session.partial_sender = _cli_partial_sender

        if is_slash_command:
            result = session.handle_input(non_interactive_input)
        else:
            result = session.handle_input(non_interactive_input)
        if result.reply:
            print(result.reply)
        return

    # 交互模式
    _run_repl(config)


def _init_platform(config: dict) -> None:
    """初始化平台相关组件：创建 PlatformManager 单例，注册并启动飞书 adapter。"""
    from src.platforms.manager import PlatformManager
    from src.platforms.adapters.feishu import FeishuAdapter

    # 如果已有实例，跳过（同进程内重复调用）
    if PlatformManager._instance is not None:
        return

    # 单实例保护：已有其它活实例在运行则拒绝启动，避免飞书重复回复/数据竞争
    if not _acquire_instance_lock():
        print_error(
            "检测到另一个 Lamix 实例正在运行（单实例模式），本次启动已取消。\n"
            "如需重启，请先停止已有实例（lamix gateway stop）。"
        )
        sys.exit(0)

    # 创建并设置 PlatformManager 单例
    pm = PlatformManager(config)
    PlatformManager._instance = pm

    # 初始化飞书
    feishu_config = config.get("feishu", {})
    if feishu_config.get("app_id") and feishu_config.get("app_secret"):
        try:
            adapter = FeishuAdapter({
                "app_id": feishu_config["app_id"],
                "app_secret": feishu_config["app_secret"],
            })
            # 注册 adapter（注入 session_manager）
            pm.register(adapter)
            # 启动 adapter（后台线程）
            import threading
            threading.Thread(target=adapter.start, daemon=True).start()
        except Exception as e:
            print_error(f"飞书初始化失败: {e}")


def run_gateway(args: argparse.Namespace) -> None:
    """'lamix gateway' 子命令：仅启动 daemon（后台常驻），启动 CLI。"""
    config = load_config()
    if not is_config_complete(config):
        print_info("Lamix 未配置，将进行首次配置。\n")
    try:
        config = run_setup_wizard()
    except (KeyboardInterrupt, EOFError):
        print_info("配置已取消，退出。")
        sys.exit(0)
    if not is_config_complete(config):
        print_error("API Key 未填写，退出。")
        sys.exit(1)

    _init_platform(config)
    print_info("daemon 已就绪，启动交互式 CLI...")
    _run_repl(config)


def run_model(args: argparse.Namespace) -> None:
    """'lamix model' 子命令：重新配置 LLM 模型。"""
    from src.core.config import load_config, is_config_complete, run_setup_wizard

    config = load_config()
    if not is_config_complete(config):
        print_info("Lamix 未配置，将进行首次配置。\n")
    try:
        run_setup_wizard(title="模型配置 - 重新选择 LLM 供应商和模型")
    except (KeyboardInterrupt, EOFError):
        print_info("配置已取消。")
        return
    except SystemExit:
        return
    print_success("模型配置完成。若 daemon 正在运行，配置将在 30 秒内自动热重载生效。")


def run_update(args: argparse.Namespace) -> None:
    """从 GitHub 拉取最新代码/下载最新 exe 并重启。"""
    # Windows exe 模式：走 exe 自更新
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        from src.selfupdate.exe_updater import run_exe_update
        result = run_exe_update()
        print(result)
        return

    import os
    import signal
    import subprocess
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent
    print_info("正在从 GitHub 拉取最新代码...")
    try:
        result = subprocess.run(
            ["git", "pull"], cwd=str(project_root),
            capture_output=True, text=True, check=True,
        )
        print_success(result.stdout.strip() or "代码已更新")
    except subprocess.CalledProcessError as e:
        print_error(f"git pull 失败: {e.stderr}")
        sys.exit(1)
    except FileNotFoundError:
        print_error("未找到 git 命令，请确保已安装 Git。")
        sys.exit(1)

    # 重启 daemon
    print_info("正在重启 daemon...")
    pid_path = Path.home() / ".lamix" / "logs" / "daemon.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, ValueError, PermissionError):
            pass

    print_success("更新完成，请重新运行 lamix。")


def run_config(args: argparse.Namespace) -> None:
    """'lamix config' 子命令：显示当前配置。"""
    from src.cli_ui.styled import print_table, C
    from src.core.config import load_config

    config = load_config()
    if not config:
        print_warning("未找到配置文件，请先运行 lamix cli 完成配置。")
        return

    # 基本配置
    basic_info = [
        ["项目", "值"],
        ["API 供应商", config.get("provider", "unknown")],
        ["模型", config.get("model", "unknown")],
        ["API Base URL", config.get("api_base", "-")],
    ]
    print_table("📋 基本配置", basic_info[0], basic_info[1:])

    # 飞书配置
    feishu = config.get("feishu", {})
    if feishu.get("enabled"):
        feishu_info = [
            ["项目", "值"],
            ["启用", "✓"],
            ["App ID", feishu.get("app_id", "-")[:20] + "..." if len(feishu.get("app_id", "")) > 20 else feishu.get("app_id", "-")],
        ]
        print_table("📱 飞书配置", feishu_info[0], feishu_info[1:])
    else:
        print_warning("飞书未配置（使用 --feishu 参数启用）")

    # 目录信息
    print_info(f"数据目录: {LAMIX_DIR}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lamix",
        description="一起认识这个世界的 AI 伙计",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例:
              lamix cli                    # 启动交互式 CLI
              lamix cli 你好                # 单次对话
              lamix gateway                # 仅启动 daemon
              lamix config                 # 显示配置
              lamix -V                     # 显示版本号

            首次运行会自动引导配置。
        """),
    )
    parser.add_argument("-V", "--version", action="version", version=f"lamix {_get_version()}")

    subparsers = parser.add_subparsers(dest="command", title="子命令", description="可用子命令")

    # lamix cli
    cli_parser = subparsers.add_parser("cli", help="启动交互式 CLI（默认模式）")
    cli_parser.add_argument("query", nargs="*", help="直接执行的查询")
    cli_parser.add_argument("-c", dest="query_c", help="单条查询（内部使用）")
    cli_parser.add_argument("--help-cmd", action="store_true", help="显示帮助")
    cli_parser.add_argument("--memory", nargs="*", help="查看/操作记忆")
    cli_parser.add_argument("--skills", nargs="*", help="查看/操作技能")
    cli_parser.add_argument("--feishu", nargs="*", help="飞书命令")
    cli_parser.add_argument("--update", nargs="*", help="自更新命令")

    # lamix gateway
    gateway_parser = subparsers.add_parser("gateway", help="管理 daemon")
    gateway_sub = gateway_parser.add_subparsers(dest="gateway_action", title="gateway 子命令")
    start_parser = gateway_sub.add_parser("start", help="启动 watchdog + daemon")
    stop_parser = gateway_sub.add_parser("stop", help="停止 daemon + watchdog")
    restart_parser = gateway_sub.add_parser("restart", help="重启 daemon + watchdog")

    # lamix model
    model_parser = subparsers.add_parser("model", help="重新配置 LLM 模型")

    # lamix update
    update_parser = subparsers.add_parser("update", help="检查并执行自更新")

    # lamix config
    config_parser = subparsers.add_parser("config", help="显示当前配置")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # 无子命令：Windows exe 或开发环境行为
    if args.command is None:
        _is_frozen = getattr(sys, "frozen", False)

        # Windows exe 双击：无参数直接进 CLI
        if _is_frozen and sys.platform == "win32":
            config = load_config()
            if not is_config_complete(config):
                try:
                    config = run_setup_wizard()
                except (KeyboardInterrupt, EOFError):
                    sys.exit(0)
                if not is_config_complete(config):
                    sys.exit(1)
            _init_platform(config)
            if not _is_daemon_running() and not _is_watchdog_running():
                print_error("daemon 未运行，请先执行: lamix gateway start")
                sys.exit(1)
            _run_repl(config)
        else:
            # macOS/Linux 开发环境：显示帮助
            parser.print_help()
        return

    # 分发子命令
    if args.command == "cli":
        run_cli(args)
    elif args.command == "gateway":
        action = getattr(args, "gateway_action", None)
        if action == "start":
            gateway_start()
        elif action == "stop":
            gateway_stop()
        elif action == "restart":
            gateway_restart()
        else:
            # 无子命令：显示 gateway 子命令帮助
            print("用法: lamix gateway <子命令>")
            print("  start  启动 watchdog + daemon")
            print("  stop   停止 daemon + watchdog")
            print("  restart 重启 daemon + watchdog")
    elif args.command == "model":
        run_model(args)
    elif args.command == "update":
        run_update(args)
    elif args.command == "config":
        run_config(args)


if __name__ == "__main__":
    main()
