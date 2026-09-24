"""Lamix macOS 安装脚本。

用法：
    python3 scripts/install_macos.py             # 安装
    python3 scripts/install_macos.py --uninstall # 卸载

## 工作流概览
1. 检查 Python 版本 (>=3.10) 与依赖
2. 检查 dist/Lamix.app 是否已构建（未构建则提示先跑 build_app.py）
3. 拷贝到 /Applications/Lamix.app —— 路径固定，TCC 授权稳定
4. 写 LaunchAgent（~/Library/LaunchAgents/com.lamix.gateway.plist）
5. launchctl bootstrap + kickstart 拉起 daemon
6. 打印首次授权指引（屏幕录制 / 辅助功能）

## 为什么必须放 /Applications
TCC（Transparency, Consent, and Control）会把授权和 `签名身份 + bundle id
+ 二进制路径` 三元组绑定。/Applications 是系统认定的应用目录，路径稳定；
把 .app 放桌面或临时目录，一旦移动就会导致授权失效。

## 权限
LaunchAgent 是用户级的（bootstrap gui/$(id -u)），不需要 sudo；
拷贝到 /Applications 通常也不需要 sudo（用户对 /Applications 有写权限）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


# —— 常量 —— #
APP_NAME = "Lamix"
BINARY_NAME = "lamix"
BUNDLE_ID = "com.lampson.lamix"
LAUNCHD_LABEL = "com.lamix.gateway"      # 必须和 src/platforms/posix_process_manager.py 一致
APPLICATIONS_DIR = Path("/Applications")
INSTALLED_APP = APPLICATIONS_DIR / f"{APP_NAME}.app"
INSTALLED_BINARY = INSTALLED_APP / "Contents" / "MacOS" / BINARY_NAME
LAUNCH_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCH_AGENT_PLIST = LAUNCH_AGENT_DIR / f"{LAUNCHD_LABEL}.plist"
LAMIX_DIR = Path.home() / ".lamix"
LOG_DIR = LAMIX_DIR / "logs"


def _print(msg: str) -> None:
    """带 flush 的 print。"""
    print(msg, flush=True)


# ────────────────────────────── 安装步骤 ────────────────────────────── #

def check_platform() -> None:
    """确保是 macOS。"""
    if sys.platform != "darwin":
        _print(f"❌ 当前平台 {sys.platform}，此脚本只在 macOS 上有意义。")
        sys.exit(1)


def check_python_version() -> None:
    """检查 Python 版本 >= 3.10。"""
    if sys.version_info < (3, 10):
        _print("❌ 需要 Python 3.10 或更高版本")
        _print(f"   当前：{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        sys.exit(1)
    _print(f"✓ Python 版本：{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


def install_dependencies(project_root: Path) -> None:
    """安装项目依赖。

    优先用 requirements.txt（如果存在），否则用 pip install -e . 走 pyproject。
    """
    req_file = project_root / "requirements.txt"
    if req_file.exists():
        _print(f"正在安装依赖（{req_file.name}）...")
        cmd = [sys.executable, "-m", "pip", "install", "-r", str(req_file)]
    else:
        _print("未发现 requirements.txt，通过 pyproject.toml 安装（pip install -e .）...")
        cmd = [sys.executable, "-m", "pip", "install", "-e", "."]

    try:
        subprocess.run(cmd, cwd=str(project_root), check=True)
        _print("✓ 依赖安装成功")
    except subprocess.CalledProcessError as e:
        _print(f"⚠ 依赖安装失败（继续，可能不影响 .app 运行）：{e}")


def check_app_built(project_root: Path) -> Path:
    """确认 dist/Lamix.app 已构建。"""
    built_app = project_root / "dist" / f"{APP_NAME}.app"
    if not built_app.exists():
        _print(f"❌ 未找到 {built_app}")
        _print("   请先运行：python3 scripts/build_app.py")
        sys.exit(1)
    _print(f"✓ 已发现构建产物：{built_app}")
    return built_app


def copy_to_applications(built_app: Path) -> None:
    """拷贝 .app 到 /Applications（已存在则先删除旧的）。

    路径必须固定在 /Applications，TCC 授权靠这个稳定路径。
    """
    _print(f"\n正在部署到 {INSTALLED_APP} ...")
    if INSTALLED_APP.exists():
        _print(f"    移除旧版：{INSTALLED_APP}")
        try:
            shutil.rmtree(INSTALLED_APP)
        except PermissionError:
            _print("❌ 无法删除旧版 .app（权限不足）")
            _print(f"   请手动删除：sudo rm -rf {INSTALLED_APP}")
            sys.exit(1)

    try:
        # 用 copytree 而不是 rsync，避免依赖外部工具；symlinks=True 保留符号链接。
        shutil.copytree(built_app, INSTALLED_APP, symlinks=True)
    except PermissionError:
        _print("❌ 拷贝失败：/Applications 无写权限")
        _print(f"   请手动执行：sudo cp -R {built_app} {INSTALLED_APP}")
        sys.exit(1)

    if not INSTALLED_BINARY.exists():
        _print(f"❌ 拷贝完成但主二进制缺失：{INSTALLED_BINARY}")
        sys.exit(1)

    _print(f"✓ 已部署：{INSTALLED_APP}")


def _launch_agent_plist_content() -> str:
    """生成 LaunchAgent plist 文本。

    - Label 必须和 posix_process_manager.py 中 _DAEMON_LAUNCHCTL_LABEL 一致，
      否则 restart_daemon 里的 kickstart 会找不到服务。
    - RunAtLoad=true：用户登录时自动拉起 daemon。
    - KeepAlive=false：daemon 自己有 watchdog，先关掉系统的 KeepAlive；
      如果希望崩溃后自动重启，可改为 true（下面注释里也写清楚了）。
    - StandardOutPath / StandardErrorPath 指到 ~/.lamix/logs/launchd.log，
      launchd 早期启动异常都会写这里，出问题第一时间看它。
    """
    stdout_path = LOG_DIR / "launchd.log"
    stderr_path = LOG_DIR / "launchd.log"
    # plist 里不能包含 ~，需要展开
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{INSTALLED_BINARY}</string>
        <string>gateway</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <!-- KeepAlive=false：daemon 自己有 watchdog。若希望系统级重启改成 <true/>。 -->
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>{stdout_path}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_path}</string>
    <key>WorkingDirectory</key>
    <string>{Path.home()}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
"""


def write_launch_agent() -> None:
    """写 LaunchAgent plist 到 ~/Library/LaunchAgents/。"""
    LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    _print(f"\n写 LaunchAgent 到 {LAUNCH_AGENT_PLIST} ...")
    LAUNCH_AGENT_PLIST.write_text(_launch_agent_plist_content(), encoding="utf-8")
    LAUNCH_AGENT_PLIST.chmod(0o644)
    _print("✓ plist 写入完成")


def _uid() -> int:
    """当前用户 UID。"""
    return os.getuid()


def _launchctl(*args: str, check: bool = False, quiet: bool = False) -> subprocess.CompletedProcess:
    """launchctl 包装：统一 timeout，可选忽略错误。"""
    result = subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if not quiet and result.returncode != 0:
        _print(f"    launchctl {' '.join(args)} → exit={result.returncode} "
               f"{result.stderr.strip()}")
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result


def _kill_lamix_leftovers() -> None:
    """兜底杀掉所有残留 lamix 进程（daemon + watchdog + 任何子进程）。

    Lamix 自建 watchdog + 单实例锁：launchctl bootout 只发 SIGTERM，若
    watchdog 抢先把 daemon 救回，或 daemon 忽略 SIGTERM，后续 kickstart
    的新实例会因为「已有 daemon 在跑」直接退出。这里兜底：
      1. pgrep -x lamix 按进程名精确匹配（daemon 用 setproctitle 改名为 lamix，
         watchdog 的 argv[0] basename 也是 lamix，两个都会被 -x 命中；
         pgrep -f 完整路径匹配不到 setproctitle 改过名的 daemon）
      2. SIGTERM，等 2s，还活着就 SIGKILL
    """
    result = subprocess.run(
        ["pgrep", "-x", "lamix"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return

    pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
    _print(f"    发现残留进程 {pids}，逐一终止...")

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    deadline = time.time() + 2.0
    while time.time() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive:
            _print(f"    残留进程已全部退出")
            return
        time.sleep(0.1)

    # 还没走的强杀
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            _print(f"    SIGKILL PID={pid}")
        except ProcessLookupError:
            pass


def bootstrap_and_start() -> None:
    """加载并启动 LaunchAgent。

    步骤：
      1. bootout 旧服务（忽略失败——首次安装时它本来就不存在）
      2. pgrep 兜底杀残留（防 lamix 自建 watchdog 救回旧 daemon）
      3. bootstrap 新 plist
      4. kickstart 立即启动
    """
    target = f"gui/{_uid()}"
    service_target = f"{target}/{LAUNCHD_LABEL}"

    _print("\n加载 LaunchAgent ...")
    # 1. bootout 旧版（首次安装会失败，安全忽略）
    _launchctl("bootout", service_target, quiet=True)

    # 2. 兜底：确保没有旧 daemon/watchdog 残留
    _kill_lamix_leftovers()

    # 3. bootstrap 新 plist
    result = _launchctl("bootstrap", target, str(LAUNCH_AGENT_PLIST))
    if result.returncode != 0:
        _print(f"❌ launchctl bootstrap 失败：{result.stderr.strip()}")
        _print("   常见原因：plist 语法错误 或 服务标签冲突。")
        sys.exit(1)

    # 4. kickstart 立即启动
    _launchctl("kickstart", service_target)
    _print("✓ LaunchAgent 已加载并启动")


def verify_daemon_running() -> bool:
    """等待几秒后用 pgrep 确认 daemon 起来了。"""
    _print("\n验证 daemon 是否存活...")
    pattern = f"{INSTALLED_APP}/Contents/MacOS/{BINARY_NAME}"
    for _ in range(6):  # 最多等 3 秒
        time.sleep(0.5)
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            _print(f"✓ daemon 运行中，PID: {', '.join(pids)}")
            return True
    _print("⚠ 未检测到 daemon 进程，请查看日志：")
    _print(f"    tail -f {LOG_DIR}/launchd.log")
    return False


def print_tcc_guidance() -> None:
    """打印首次授权指引。"""
    _print("\n" + "=" * 60)
    _print("首次授权指引（macOS 权限）")
    _print("=" * 60)
    _print(
        f"""
Lamix 需要以下系统权限才能正常工作：
  • 屏幕录制（Screen Recording）
  • 辅助功能（Accessibility）

请打开：
  系统设置 → 隐私与安全性 → 屏幕录制  → 添加 {INSTALLED_APP}
  系统设置 → 隐私与安全性 → 辅助功能  → 添加 {INSTALLED_APP}

添加后需要重启 daemon，让权限生效：
  launchctl kickstart -k gui/{_uid()}/{LAUNCHD_LABEL}

提示：只要 bundle id ({BUNDLE_ID}) 和安装路径 ({INSTALLED_APP}) 保持不变，
后续升级 .app（重跑本脚本）不需要重新授权。
"""
    )


# ────────────────────────────── 卸载步骤 ────────────────────────────── #

def _stop_daemon_process(timeout: float = 5.0) -> None:
    """通过 pgrep 找到 daemon 进程并终止。

    LaunchAgent 已 bootout 后进程通常会被 launchd 自己回收，这里做兜底。
    """
    pattern = f"{INSTALLED_APP}/Contents/MacOS/{BINARY_NAME}"
    result = subprocess.run(
        ["pgrep", "-f", pattern],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return

    pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        # 等待优雅退出
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            # 超时则强杀
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    _print(f"    已停止 daemon 进程：{pids}")


def uninstall() -> None:
    """卸载 Lamix：bootout + 删 plist + 停进程 + 删 .app。"""
    _print("=" * 60)
    _print("Lamix macOS 卸载")
    _print("=" * 60)

    # 1. bootout LaunchAgent
    service_target = f"gui/{_uid()}/{LAUNCHD_LABEL}"
    _print(f"\n1) 卸载 LaunchAgent（{service_target}）...")
    _launchctl("bootout", service_target, quiet=True)
    _print("   （bootout 完成，忽略「service not loaded」类错误）")

    # 2. 删 plist
    if LAUNCH_AGENT_PLIST.exists():
        try:
            LAUNCH_AGENT_PLIST.unlink()
            _print(f"   已删除 plist：{LAUNCH_AGENT_PLIST}")
        except OSError as e:
            _print(f"   ⚠ 删除 plist 失败：{e}")
    else:
        _print(f"   plist 不存在，跳过：{LAUNCH_AGENT_PLIST}")

    # 3. 停 daemon 进程（兜底）
    _print("\n2) 停止 daemon 进程...")
    _stop_daemon_process()

    # 4. 删 /Applications/Lamix.app
    _print(f"\n3) 删除 {INSTALLED_APP} ...")
    if INSTALLED_APP.exists():
        try:
            shutil.rmtree(INSTALLED_APP)
            _print("   已删除")
        except PermissionError:
            _print(f"   ⚠ 权限不足，请手动：sudo rm -rf {INSTALLED_APP}")
    else:
        _print("   .app 不存在，跳过")

    # 5. 询问是否删配置目录
    _print("")
    if LAMIX_DIR.exists():
        _print(f"配置目录 {LAMIX_DIR} 仍存在。")
        _print("  保留：下次安装可直接复用配置、记忆、技能。")
        _print("  删除：彻底清除所有个人数据。")
        try:
            choice = input("\n是否删除配置目录？(y/N): ").strip().lower()
        except EOFError:
            choice = ""
        if choice in ("y", "yes", "是"):
            try:
                shutil.rmtree(LAMIX_DIR)
                _print(f"✓ 已删除：{LAMIX_DIR}")
            except OSError as e:
                _print(f"❌ 删除失败：{e}")
                _print(f"   请手动：rm -rf {LAMIX_DIR}")
        else:
            _print(f"  已保留：{LAMIX_DIR}")

    _print("\n" + "=" * 60)
    _print("卸载完成")
    _print("=" * 60)
    _print("\n提示：TCC 授权（屏幕录制 / 辅助功能）不会随卸载自动清除。")
    _print("如需清理，请在 系统设置 → 隐私与安全性 里手动移除 Lamix 条目。")
    _print("")


# ────────────────────────────── main ────────────────────────────── #

def install() -> None:
    """完整安装流程。"""
    project_root = Path(__file__).resolve().parent.parent

    _print("=" * 60)
    _print("Lamix macOS 安装")
    _print("=" * 60)
    _print("")

    # 1. 环境检查
    check_python_version()

    # 2. 依赖安装
    install_dependencies(project_root)

    # 3. 检查 .app 是否已构建
    built_app = check_app_built(project_root)

    # 4. 部署到 /Applications
    copy_to_applications(built_app)

    # 5. 写 LaunchAgent
    write_launch_agent()

    # 6. 加载 + 启动
    bootstrap_and_start()

    # 7. 验证
    verify_daemon_running()

    # 8. 首次授权指引
    print_tcc_guidance()

    _print("=" * 60)
    _print("✓ 安装完成")
    _print("=" * 60)
    _print(f"\n日志目录：{LOG_DIR}")
    _print(f"配置目录：{LAMIX_DIR}")
    _print(f"手动重启 daemon：launchctl kickstart -k gui/{_uid()}/{LAUNCHD_LABEL}")
    _print("")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lamix macOS 安装/卸载脚本")
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="卸载 Lamix（bootout LaunchAgent、删除 .app、可选删除配置目录）",
    )
    args = parser.parse_args()

    check_platform()

    if args.uninstall:
        uninstall()
    else:
        install()


if __name__ == "__main__":
    main()
