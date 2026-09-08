"""构建 macOS .app bundle。

用 PyInstaller (--onedir --windowed) 打包 src/cli.py。--windowed 在
macOS 上会额外生成一个符合 Apple 规范的 dist/lamix.app（正确的
Contents/{MacOS,Frameworks,Resources} 布局，已由 PyInstaller 完成 ad-hoc
签名）。本脚本以那个 .app 为基座，做三件事：
  1. 重命名 dist/lamix.app → dist/Lamix.app
  2. 覆盖 Contents/Info.plist：把 bundle id 固化为 com.lampson.lamix，
     写入 LSUIElement=true（后台 app）、版本号、最低系统版本
  3. 再次 ad-hoc 重签，让 Info.plist 的改动进签名封条

## 关键设计决策
1. **onedir 而不是 onefile**：onefile 每次启动都会把内部资源解压到
   /var/folders/.../_MEIxxxxx 这种随机目录，TCC 每次都把它当成新的
   二进制路径，授权无法保留。onedir 的主二进制固定在
   Contents/MacOS/lamix，路径永远不变。
2. **--windowed 走 Apple bundle 规范**：PyInstaller 在这个模式下会把
   数据文件放 Contents/Resources、把 .dylib/.so 放 Contents/Frameworks，
   主可执行文件放 Contents/MacOS。这样 codesign 才能正确地为整个 bundle
   建 CodeResources 封条；把 onedir 内容平铺到 Contents/MacOS 会让
   codesign 报「code object is not signed at all — In subcomponent:
   xxx.yaml」，因为数据文件不应该在 MacOS/ 里。
3. **ad-hoc 签名（--sign -）**：没有 Apple Developer 证书时的标准
   做法。TCC 认的是「签名身份 + bundle id + 路径」的组合，ad-hoc
   签名也是有效身份，只要 bundle id 和 /Applications 路径固定即可。

依赖：pip install pyinstaller

用法：
    python3 scripts/build_app.py
    # 产出：dist/Lamix.app
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


APP_NAME = "Lamix"                   # .app 显示名称
BINARY_NAME = "lamix"                # 主二进制文件名（PyInstaller --name）
BUNDLE_ID = "com.lampson.lamix"      # 固定 bundle id，TCC 授权靠它绑定
MIN_MACOS = "12.0"                   # 最低 macOS 版本


def _print(msg: str) -> None:
    """带 flush 的 print（避免子进程输出错序）。"""
    print(msg, flush=True)


def _read_version(project_root: Path) -> str:
    """从 pyproject.toml 读版本号，读取失败则回退到 0.2.0。"""
    pyproject = project_root / "pyproject.toml"
    try:
        # Python 3.11+ 自带 tomllib
        import tomllib  # type: ignore[import-not-found]
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return data.get("project", {}).get("version", "0.2.0")
    except Exception:
        # 兜底：无 tomllib 时用简单正则扫描
        try:
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("version") and "=" in stripped:
                    return stripped.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
        return "0.2.0"


def _ensure_pyinstaller() -> None:
    """确保 PyInstaller 已安装，未装则自动 pip install。"""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        _print("PyInstaller 未安装，正在安装...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            check=True,
        )


def _run_pyinstaller(project_root: Path) -> Path:
    """调用 PyInstaller onedir + windowed 打包，返回 PyInstaller 自建的 .app 路径。

    --windowed 在 macOS 上让 PyInstaller 同时产出：
      dist/lamix/       — onedir 平铺目录（也可用，但布局不符合 Apple 规范）
      dist/lamix.app/   — 符合 Apple 规范的 bundle（本脚本基座）
    我们只用后者。
    """
    entry_script = str(project_root / "src" / "cli.py")
    add_data = f"{project_root / 'config'}{os.pathsep}config"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",          # 关键：不用 onefile，路径固定 TCC 才稳定
        "--windowed",        # 生成 Apple 规范的 .app（Frameworks/Resources 分离）
        "--name", BINARY_NAME,
        "--clean",
        "--noconfirm",
        "--add-data", add_data,
        # frozen 环境下 daemon/watchdog/safe_mode 靠 cli.py 里延迟 import + subprocess
        # 内部子命令启动。虽然 PyInstaller 通常能静态分析出来，这里显式声明以防漏收。
        "--hidden-import", "src.daemon",
        "--hidden-import", "src.watchdog",
        "--hidden-import", "src.safe_mode",
        "--hidden-import", "src.core.process_launch",
        entry_script,
    ]

    _print("\n>>> PyInstaller 打包中...")
    _print("    " + " ".join(cmd))
    subprocess.run(cmd, cwd=str(project_root), check=True, encoding="utf-8", errors="replace")

    pyi_app = project_root / "dist" / f"{BINARY_NAME}.app"
    if not pyi_app.is_dir():
        # 兜底：某些老版本 PyInstaller 布局不同，抛清晰错误
        raise RuntimeError(
            f"PyInstaller 未生成 .app（预期 {pyi_app}）。请检查 --windowed 是否受支持。"
        )
    return pyi_app


def _finalize_app_bundle(pyi_app: Path, final_app: Path, version: str) -> None:
    """把 PyInstaller 生成的 lamix.app 转成最终的 Lamix.app。

    - 重命名（Finder 里显示为 Lamix）
    - 用我们自定义的 Info.plist 覆盖 PyInstaller 版本，写入固定 bundle id
      和 LSUIElement=true 等关键字段
    """
    # 处理大小写不敏感的文件系统（APFS 默认就是不敏感）：pyi_app 和 final_app
    # 只差大小写时，final_app.exists() 会返回 True 并 rmtree 掉 PyInstaller 产物。
    # 所以先判断两条路径是否指向同一 inode，同 inode 就不删。
    same_inode = False
    try:
        if final_app.exists() and pyi_app.exists():
            same_inode = final_app.samefile(pyi_app)
    except OSError:
        pass

    if final_app.exists() and not same_inode:
        _print(f"    清理旧 bundle：{final_app}")
        shutil.rmtree(final_app)

    if same_inode:
        # 大小写不敏感 FS 下用 rename 强制改成目标大小写
        pyi_app.rename(final_app)
    else:
        shutil.move(str(pyi_app), str(final_app))
    _print(f"    重命名：{pyi_app.name} → {final_app.name}")

    contents = final_app / "Contents"

    # 主二进制必须存在
    main_binary = contents / "MacOS" / BINARY_NAME
    if not main_binary.exists():
        raise RuntimeError(f"主二进制缺失：{main_binary}")
    main_binary.chmod(0o755)

    # 覆盖 Info.plist：固定 bundle id + LSUIElement + 版本
    info_plist = {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": BINARY_NAME,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": MIN_MACOS,
        # LSUIElement=true：后台 app，不在 Dock 显示图标，双击也不抢焦点。
        # daemon 长期驻留时这是必需的，否则 Dock 会出现一个僵尸图标。
        "LSUIElement": True,
        # 声明支持高分屏，避免文字模糊。
        "NSHighResolutionCapable": True,
        # LSBackgroundOnly=false + LSUIElement=true 的组合允许 `lamix cli`
        # 在终端里正常运行（有 stdio），后台启动时又不抢焦点。
        "LSBackgroundOnly": False,
    }
    plist_path = contents / "Info.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(info_plist, f)
    _print(f"    写入 Info.plist（bundle id = {BUNDLE_ID}）")


def _codesign_adhoc(app_path: Path) -> None:
    """ad-hoc 重签 .app。

    改过 Info.plist 后，PyInstaller 原有的封条 CodeResources 已失效，
    必须重新签名。这里直接对外层 bundle codesign，不用 --deep：
    PyInstaller 在打包时已对每个内部 Mach-O 单独签过了，外层封条只需
    覆盖 CodeResources 即可。
    """
    _print("\n>>> ad-hoc 代码签名...")
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(app_path)],
        check=True,
    )
    _print("    签名完成，开始校验...")
    subprocess.run(
        ["codesign", "--verify", "--verbose", str(app_path)],
        check=True,
    )
    # 附加打印签名摘要
    result = subprocess.run(
        ["codesign", "-dvv", str(app_path)],
        capture_output=True, text=True,
    )
    for line in (result.stderr or "").splitlines():
        if any(k in line for k in ("Identifier", "Signature", "TeamIdentifier")):
            _print(f"    {line.strip()}")
    _print("    ✓ 签名校验通过")


def _report_size(app_path: Path) -> None:
    """打印 .app 体积。"""
    try:
        result = subprocess.run(
            ["du", "-sh", str(app_path)],
            capture_output=True, text=True, timeout=10,
        )
        size = result.stdout.strip().split("\t")[0] if result.returncode == 0 else "?"
    except Exception:
        size = "?"
    _print(f"    体积：{size}")


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent

    # 仅在 macOS 上运行才有意义
    if sys.platform != "darwin":
        _print(f"⚠ 当前平台 {sys.platform}，此脚本只在 macOS 上有意义。")
        _print("  Windows 请用 scripts/build_exe.py。")
        sys.exit(1)

    _print("=" * 60)
    _print(f"Lamix macOS .app 打包（bundle id = {BUNDLE_ID}）")
    _print("=" * 60)

    _ensure_pyinstaller()

    version = _read_version(project_root)
    _print(f"版本号：{version}")

    # 1. PyInstaller → dist/lamix.app
    pyi_app = _run_pyinstaller(project_root)

    # 2. 定型 → dist/Lamix.app
    final_app = project_root / "dist" / f"{APP_NAME}.app"
    _print(f"\n>>> 定型 {final_app.name}...")
    _finalize_app_bundle(pyi_app, final_app, version)

    # 3. ad-hoc 签名 + 校验
    _codesign_adhoc(final_app)

    # 4. 总结
    _print("\n" + "=" * 60)
    _print(f"✓ 构建完成：{final_app}")
    _report_size(final_app)
    _print("=" * 60)
    _print("\n下一步：")
    _print("  python3 scripts/install_macos.py")
    _print("  （会把 .app 拷到 /Applications 并注册 LaunchAgent）")
    _print("")


if __name__ == "__main__":
    main()
