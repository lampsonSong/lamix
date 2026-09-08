# 修复报告：PyInstaller 打包版 daemon 启动失效 + instance.lock 身份误判

## 根因（一句话）
PyInstaller frozen 环境下 `sys.executable` 已是 `lamix` 二进制，所有 `[sys.executable, "-m", "src.xxx"]` 形式的子进程启动会被 argparse 当成非法子命令而失败；同时 `daemon.pid` / `instance.lock` 只按 PID 存活判定，遇到 PID 复用或持有者本就是 cli 时会把 cli 误判为 daemon 已在运行，进一步阻塞真正的 daemon 启动。

---

## Bug 1：frozen 环境子进程启动命令失效

### 方案：Option A（CLI 增加内部子命令）
新增 `lamix gateway daemon-run` / `lamix gateway watchdog-run` 两个「内部子命令」，frozen 环境下 watchdog/CLI 用它们通过 subprocess 拉起 daemon/watchdog。

**为什么选 A 不选 B（同进程直接调用入口函数）**：
- Watchdog 的整个价值就在于「独立进程」——daemon 崩溃时 watchdog 必须存活才能重拉。
- CLI 拉起 watchdog 也必须是独立进程，否则 CLI 退出就会带死 watchdog。
- 直接 Python 层 `subprocess.Popen([sys.executable, ...])` 在 frozen 里已经会走 CLI 分发（bootloader 加载）；把子命令挂在 `gateway` 下语义最贴切（daemon/watchdog 天然属于 gateway 管理），argparse 也无需为「不可见」的子命令做特殊隐藏——`--help` 里显式标注 `[内部]` 即可。

### 具体改动
| 文件 | 说明 |
| --- | --- |
| `src/core/process_launch.py` (新) | 集中定义 `is_frozen()`、`daemon_launch_cmd()`、`watchdog_launch_cmd()`；frozen → `[lamix, gateway, daemon-run\|watchdog-run]`，源码 → `[python, -m, src.xxx]`。避免在多处重复写分支。 |
| `src/cli.py` | `_daemon_start_cmd()` / `_start_watchdog()` / `_ensure_watchdog_on_windows()` 全部走 helper；`_build_parser()` 追加 `daemon-run` / `watchdog-run` 两个子命令；`main()` 分发到 `src.daemon.main()` / `src.watchdog.main()`（延迟 import）。 |
| `src/watchdog.py` | `_restart_daemon()` 用 helper 构造 daemon 命令；`main()` 从 `parse_args()` 改为 `parse_known_args()`，防止 frozen 下 `lamix gateway watchdog-run` 的外层位置参数被当成非法参数报错；`Watchdog.run()` 启动/退出时写/清 `watchdog.pid`（原来无人写）。 |
| `src/safe_mode.py` | `_resolve_daemon_cmd()` frozen 分支走 helper，源码分支保留 lamix + `gateway daemon-run` 兜底。 |
| `scripts/build_app.py` | 增加 `--hidden-import src.{daemon,watchdog,safe_mode,core.process_launch}`，防 PyInstaller 静态分析漏收（lazy import 场景）。 |

### git diff --stat（仅本次改动）
```
 scripts/build_app.py            |   9 +-
 src/cli.py                      | 222 ++++++++++++++++++++--------------------
 src/core/process_launch.py      | 176 +++++++++++++++++++++++++++++++
 src/daemon.py                   |  77 +++++++++-----
 src/safe_mode.py                |  18 +++-
 src/watchdog.py                 |  58 +++++++----
 tests/test_process_launch.py    | 415 +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
 7 files changed
```
（其他 modified/untracked 文件与本任务无关，未提交。）

---

## Bug 2：instance.lock / daemon.pid 只查 PID 存活，不查身份

### 方案
1. **文件格式**：所有 pid/lock 文件由「纯 PID」升级为 JSON `{"pid": ..., "role": "daemon|watchdog|cli"}`；读端兼容老纯整数（视为「未知 role」）。
2. **身份校验**：新增 `pid_role(pid)`，用 `ps -p PID -o command=` 匹配特征子串（`src.daemon` / `daemon-run` / `src.watchdog` / `watchdog-run` / 独立的 `cli` 参数）。`is_running_as(pid, expected_role)` 只有 PID 存活且身份匹配才返回 True。
3. **`_is_daemon_running()` / `_is_watchdog_running()`**：改为「pid 存在 → 校验 role」；即使 `daemon.pid` 里是被复用的 cli PID，也会返回 False。
4. **`_acquire_instance_lock(role)`**：
   - 现有锁 role ≠ 当前 role → **允许覆写**（cli 持锁不再阻挡 daemon 启动）。
   - 现有锁 role == 当前 role 且持有者存活 → 拒绝（保留原单实例语义）。
   - 老格式（纯 PID）+ 持有者已死 or 实际身份不同 → 覆写。
5. **daemon 侧 `_check_single_instance()`**：读 pid 用 `read_pid_record`，只在真身份 == daemon 时才「已在运行→退出」；`_kill_other_lamix_processes()` 只杀 `pid_role() == daemon` 的进程，不再误伤 cli/watchdog。
6. **daemon 自己写 pid**：`_write_daemon_pid()` 改用 `write_pid_record(_, ROLE_DAEMON)`；Windows frozen self-注册 `daemon.pid` 也用同一接口。

### 改动文件
| 文件 | 说明 |
| --- | --- |
| `src/core/process_launch.py` (新) | `pid_role`, `is_running_as`, `read_pid_record`, `write_pid_record`, `process_exists`。 |
| `src/cli.py` | `_is_daemon_running` / `_is_watchdog_running` / `_acquire_instance_lock(role=ROLE_CLI)` / `_release_instance_lock` / `_wait_daemon_ready` / `gateway_stop` / `run_update` 全部改成用新的 reader/身份校验。 |
| `src/daemon.py` | `_check_single_instance`, `_kill_other_lamix_processes`, `_write_daemon_pid` 使用新格式并做身份校验。 |
| `src/watchdog.py` | `Watchdog.run()` 写 `watchdog.pid`（带 role），退出时清理；`_find_daemon_pid` 用新 reader。 |

### 向后兼容
- 老格式 `daemon.pid` / `watchdog.pid` / `instance.lock`（内容为纯整数）依然能读；`role` 字段返回 None，走 `pid_role()` 实时判定身份。
- 老锁被本次进程覆写后自动升级成 JSON 格式。

---

## 测试结果

新增测试：`tests/test_process_launch.py`（38 个用例，全部通过），覆盖：
- Bug 1：frozen / 源码两种模式下 daemon/watchdog 启动命令；mock `sys.frozen` 验证 `-m` / `gateway ...-run` 的分支。
- Bug 2：pid 文件 JSON 读写、老纯整数兼容、`pid_role` 各种角色识别（含 daemon 优先命中）、cli PID 不能被误判为 daemon、cli 锁不阻挡 daemon 启动、老锁陈旧/身份不匹配时可覆写。
- argparse：`gateway daemon-run` / `gateway watchdog-run` 能被解析；`start/stop/restart` 无回归。

全量测试：
```
$ .venv/bin/python -m pytest tests/ --tb=short
============= 663 passed, 2 skipped, 1 warning in 82.20s (0:01:22) =============
```
**663/665 通过，0 失败，2 skip（预存跳过用例）。**

---

## 打包产物验证

```
$ python3 scripts/build_app.py    # 通过原打包脚本
...
✓ 构建完成：/Users/songyuhao/lamix/dist/Lamix.app
    体积：103M

$ /Users/songyuhao/lamix/dist/Lamix.app/Contents/MacOS/lamix --help
usage: lamix [-h] [-V] {cli,gateway,model,update,config} ...
...

$ /Users/songyuhao/lamix/dist/Lamix.app/Contents/MacOS/lamix gateway --help
usage: lamix gateway [-h] {start,stop,restart,daemon-run,watchdog-run} ...
    daemon-run          [内部] 前台运行 daemon（供 frozen 环境 subprocess 使用）
    watchdog-run        [内部] 前台运行 watchdog（供 frozen 环境 subprocess 使用）
```

同时通过 `build/lamix/xref-lamix.html` 交叉引用文件确认 `src.daemon`、`src.watchdog`、`src.safe_mode`、`src.core.process_launch` 都被 PyInstaller 收录。

**未安装到 /Applications，未触碰 launchd，未 kill 任何进程。** 现有 `/Applications/Lamix.app`（昨天 14:49 的旧版本）保持原状；新产物在 `dist/Lamix.app`。

---

## 遗留风险

1. **daemon 触发 safe_mode 的 subprocess 尚未修复**：`src/daemon.py` 里 `_trigger_safe_mode()` 用 `[sys.executable, str(SAFE_MODE_SCRIPT)]` 启动 `src/safe_mode.py`。frozen 环境下 `sys.executable` = `lamix` 二进制，会同样报「invalid choice」。本次未改是因为任务书未点名，且 safe_mode 触发路径较冷；建议下轮统一改成 `lamix gateway safe-mode-run` 内部子命令。
2. **`_kill_other_lamix_processes` 现在只杀 `pid_role() == daemon` 的进程**：如果一个真的 daemon 命令行没能被 `ps` 拉到（macOS 极短命令行截断、权限阻碍等），会被 `pid_role()` 归为 None → 逃过清理。风险低，且保守（放过 vs. 误杀 cli 前者更安全）。
3. **hiddenimports 是否够全**：目前只显式声明了 `src.daemon` / `src.watchdog` / `src.safe_mode` / `src.core.process_launch`；其它模块靠 cli.py 的 top-level `import` 被静态分析覆盖。若之后有新增仅在 daemon 启动路径上首次触达的模块，可能需要追加。当前构建的 xref 已确认无缺失。
4. **`pid_role()` 依赖 `ps -p PID -o command=` 输出格式**：macOS 上正常，但如果日后走非 POSIX 或某些容器环境命令行被截断为进程名，`pid_role()` 可能失效并返回 None。此时 `is_running_as` 保守返回 False（可能造成 daemon 无法自我识别、允许重复启动）；这比误判「已在运行→退出」的 A 类错误可控。
5. **launchd 未重启**：本次修复不会生效在当前跑的 daemon/watchdog 上。用户需要自行 `launchctl kickstart -k gui/$(id -u)/com.lamix.gateway`（或重新登录 / 装新 .app），才能验证真实链路。
