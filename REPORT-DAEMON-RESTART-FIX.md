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

1. ~~**daemon 触发 safe_mode 的 subprocess 尚未修复**~~ → 第二轮已修复（见下）。
2. **`_kill_other_lamix_processes` 现在只杀 `pid_role() == daemon` 的进程**：如果一个真的 daemon 命令行没能被 `ps` 拉到（macOS 极短命令行截断、权限阻碍等），会被 `pid_role()` 归为 None → 逃过清理。风险低，且保守（放过 vs. 误杀 cli 前者更安全）。
3. **hiddenimports 是否够全**：目前只显式声明了 `src.daemon` / `src.watchdog` / `src.safe_mode` / `src.core.process_launch`；其它模块靠 cli.py 的 top-level `import` 被静态分析覆盖。若之后有新增仅在 daemon 启动路径上首次触达的模块，可能需要追加。当前构建的 xref 已确认无缺失。
4. **`pid_role()` 依赖 `ps -p PID -o command=` 输出格式**：macOS 上正常，但如果日后走非 POSIX 或某些容器环境命令行被截断为进程名，`pid_role()` 可能失效并返回 None。此时 `is_running_as` 保守返回 False（可能造成 daemon 无法自我识别、允许重复启动）；这比误判「已在运行→退出」的 A 类错误可控。
5. **launchd 未重启**：本次修复不会生效在当前跑的 daemon/watchdog 上。用户需要自行 `launchctl kickstart -k gui/$(id -u)/com.lamix.gateway`（或重新登录 / 装新 .app），才能验证真实链路。

---

## 第二轮：心跳 freeze_support 修复（2026-09-08 19:xx）

### 根因（一句话）
`src/core/heartbeat.py::HeartbeatManager.start()` 用 `multiprocessing.Process` 拉起心跳子进程，macOS 默认 spawn 模式让子进程重新执行 `sys.executable`——frozen 下 `sys.executable=lamix` 二进制，入口未调用 `freeze_support()`，子进程重跑 CLI argparse 直接死掉，`~/.lamix/heartbeat/<pid>.json` 永远不生成，watchdog 每 10 秒判死重拉，形成 daemon 无限重启循环。

### 修法：**方案 B（统一改用 daemon 线程）**

**为什么选 B 而不是 A（freeze_support）**：
| 维度 | A: freeze_support() | B: threading.Thread |
| --- | --- | --- |
| 入口维护 | 必须在 CLI main 第一句加，改动横跨模块；argparse 之前 | 无需改入口 |
| 子进程冷启开销 | spawn 会重新加载整个 lamix bootloader（PyInstaller 二进制解压 + 全部 top-level import），~1s 起 | 线程共享进程 heap，零冷启 |
| 「绕过 GIL」的实际必要性 | 心跳工作 = 文件写 + sleep，两者都释放 GIL；LLM/网络 I/O 也释放 GIL —— 主线程阻塞不会拖住心跳线程 | 同左，线程足够 |
| 测试性 | mock spawn / freeze_support 行为很难 | 直接构造 Event 即可断言 |
| frozen/源码差异 | 需要在两端做不同分支处理 | 统一一份实现 |

线程实现要点（`src/core/heartbeat.py`）：
- `HeartbeatManager` 内部持 `threading.Thread` + `threading.Event`（stop_event / user_stop_event）
- `_heartbeat_worker` 循环里用 `stop_event.wait(interval)` 代替 `time.sleep(interval)`，`stop()` 触发时立即中断，无需等到下一次 tick
- 用户主动停止时线程写入 `user_stopped=True` 心跳并优雅退出（保留原语义供 watchdog 识别）
- 原有 `stop.flag` 兼容分支保留（Windows 优雅终止路径）

### 验证：打包 + 短时跑通

```
$ .venv/bin/python scripts/build_app.py       # 重新打包
...
✓ 构建完成：/Users/songyuhao/lamix/dist/Lamix.app
    体积：103M

# 隔离 HOME → daemon 走「无配置 → idle 等待」分支（仍会启动 heartbeat）
$ TMPHOME=$(mktemp -d)
$ HOME=$TMPHOME dist/Lamix.app/Contents/MacOS/lamix gateway daemon-run </dev/null \
    >$TMPHOME/out 2>$TMPHOME/err &
$ sleep 8
$ ls $TMPHOME/.lamix/heartbeat/
51812.json
$ cat $TMPHOME/.lamix/heartbeat/51812.json
{"pid": 51812, "task_id": "daemon", "user_stopped": false, "last_heartbeat": "2026-09-08T19:16:26"}
$ kill -TERM 51812  # 短时验证后立即退出，不留常驻
```

第二次跑等 13 秒验证「周期更新」：
```
TS1=2026-09-08T19:16:47
TS2=2026-09-08T19:16:57       # 相隔 10 秒（= HEARTBEAT_INTERVAL），已更新
PASS: heartbeat updated periodically
```

**核心断言**：frozen 二进制里心跳文件从「永远不生成」→「首条 <1s 生成、每 10s 定期更新」。第二轮修复到位。

### 同款坑扫描清单

| 模式 | 出现位置 | frozen 下是否有效 | 处理 |
| --- | --- | --- | --- |
| `mp.Process(target=_heartbeat_worker, ...)` | `src/core/heartbeat.py:164`（旧） | ❌ 失效——spawn 子进程重跑 CLI argparse 崩溃 | ✅ 改为 `threading.Thread`（daemon=True），源码/frozen 统一 |
| `[sys.executable, str(SAFE_MODE_SCRIPT)]` | `src/daemon.py::_trigger_safe_mode:1174`（旧） | ❌ 失效——`sys.executable=lamix` 会把 py 路径当成非法子命令 | ✅ 新增 `safe_mode_launch_cmd()` helper + `lamix gateway safe-mode-run` 内部子命令，frozen 走 `[lamix, gateway, safe-mode-run]`，源码走 `[python, safe_mode.py]` |
| `[sys.executable, "-m", "src.xxx"]` | `daemon_launch_cmd` / `watchdog_launch_cmd` | ✅ 已修（第一轮走 `daemon-run` / `watchdog-run` 内部子命令） | 无变化 |
| `[sys.executable, "-m", "pip", "install", "pyinstaller"]` | `scripts/build_app.py:82` | 不受影响（构建脚本仅在源码环境跑，不会打包） | 无变化 |
| `subprocess.run(["ps", ...])` / `["pgrep", ...]` / `["tasklist", ...]` | 多处 | ✅ 均是系统命令，与 `sys.executable` 无关 | 无变化 |
| `_get_lamix_bin()` shutil.which / sysconfig scripts 探测 | `src/watchdog.py:51`、`src/safe_mode.py::_resolve_daemon_cmd` | ✅ frozen 环境不会走这里（`is_frozen()` 分支已优先命中 helper） | 无变化 |

结论：全仓 grep 命中的启动子进程模式，只有心跳与 safe_mode 两处在 frozen 下失效，本轮全部修复。

### 具体改动

| 文件 | 说明 |
| --- | --- |
| `src/core/heartbeat.py` | `HeartbeatManager` 从 `mp.Process` 改为 `threading.Thread`；新增 `_stop_event` / `_user_stop_event` 让 `stop()` 能立即中断；`_heartbeat_worker` 循环用 `stop_event.wait(interval)` 替代 `time.sleep(interval)`；用户主动退出前写 `user_stopped=True` 心跳。 |
| `src/core/process_launch.py` | 新增 `safe_mode_launch_cmd(script_path)`：frozen → `[lamix, gateway, safe-mode-run]`，源码 → `[python, script_path]`。 |
| `src/daemon.py` | `_trigger_safe_mode()` 用 `safe_mode_launch_cmd(str(SAFE_MODE_SCRIPT))` 构造启动命令；import 补 `is_frozen`、`safe_mode_launch_cmd`。 |
| `src/cli.py` | `_build_parser()` 追加 `gateway safe-mode-run` 内部子命令；`main()` 分发到 `src.safe_mode.main()`（延迟 import）。 |
| `src/platforms/windows/process_manager.py` | 注释里「心跳 multiprocessing 子进程」表述已过时，删掉。 |
| `tests/test_heartbeat.py` | 集成测试从 `_process` 属性改到 `_thread`；用 `monkeypatch.setattr(hb_mod, "HEARTBEAT_INTERVAL", 1)` 把 10s 缩短到 1s，全部集成测试从 12+s 降到 <3s；新增 `test_frozen_mode_uses_thread_not_multiprocessing`：mock `sys.frozen=True` + 监视 `multiprocessing.Process` 构造，断言心跳不再走 mp。 |
| `tests/test_process_launch.py` | 新增 `test_safe_mode_cmd_source_mode` / `test_safe_mode_cmd_frozen_mode` / `test_safe_mode_run_subcommand_parses`。 |

### pytest 结果

```
$ .venv/bin/python -m pytest tests/ --tb=short
============= 666 passed, 2 skipped, 1 warning in 43.38s ==============
```

666/668 通过，0 失败（相比第一轮 663 通过：新增 3 个测试；跳过用例数不变）。

首次跑时出现一次 `test_watchdog_sleep_detection.py::test_multiple_sleep_cycles_reset_grace` 假失败（两次 `time.time()` 落在同一 tick 导致 `grace1 == grace2` 断言不满足），单独重跑立即通过；该测试与本轮改动无关，是既有 flaky 用例（用 `time.time()` 做严格 `>` 断言）。

### 打包产物

```
$ .venv/bin/python scripts/build_app.py
...
✓ 构建完成：/Users/songyuhao/lamix/dist/Lamix.app
    体积：103M

$ dist/Lamix.app/Contents/MacOS/lamix gateway --help
usage: lamix gateway [-h] {start,stop,restart,daemon-run,watchdog-run,safe-mode-run} ...
    daemon-run     [内部] 前台运行 daemon（供 frozen 环境 subprocess 使用）
    watchdog-run   [内部] 前台运行 watchdog（供 frozen 环境 subprocess 使用）
    safe-mode-run  [内部] 前台运行 safe_mode（供 frozen 环境 subprocess 使用）
```

**遵守纪律**：
- 未 kill/pkill 任何 lamix 进程（cli PID 7868 与残留 daemon 进程原封不动）
- 未做 launchctl 操作
- 验证 daemon-run 用隔离 `HOME` 目录跑，短时验证后 SIGTERM 立即退出，不留常驻
- 未装到 `/Applications`，产物只在 `dist/Lamix.app`

**建议下一步（不在本次范围）**：用户需要重启当前 launchd 管理的 daemon（`launchctl kickstart -k gui/$(id -u)/com.lamix.gateway` 或重新登录）才能让新逻辑生效于真实链路。届时应观察 `~/.lamix/logs/watchdog.log` 不再出现「心跳文件不存在，尝试重启」的循环。
