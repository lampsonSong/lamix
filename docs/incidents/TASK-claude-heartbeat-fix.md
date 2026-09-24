# 任务：修复 frozen 环境心跳子进程失效（multiprocessing 坑）+ 扫清同款残留

## 背景（紧接上一轮 REPORT-DAEMON-RESTART-FIX.md 之后）
上一轮修复（commit d9df76d）后重新打包上线，发现新问题：**daemon 每 10 秒被 watchdog 杀掉重启，无限循环**。

根因（已定位，直接修）：`src/core/heartbeat.py` 的 `HeartbeatManager.start()` 用 `multiprocessing.Process` 启动心跳子进程。macOS 默认 spawn 模式下，子进程启动方式是重新执行 `sys.executable` 并带 multiprocessing 参数——**frozen 环境 sys.executable = lamix 二进制，入口没有调用 `multiprocessing.freeze_support()`，子进程重新跑 CLI argparse 直接死掉**，心跳文件 `~/.lamix/heartbeat/<pid>.json` 永远不生成，watchdog 每 10 秒判定 daemon 死亡并重启。

证据：
- `~/.lamix/logs/watchdog.log`：每 10 秒 "daemon (pid) 心跳文件不存在，尝试重启"（19:03:12 起约 18 轮）
- `~/.lamix/logs/daemon_error.log`：每轮都有 "[daemon] 心跳已启动" 但 heartbeat 目录无新文件
- heartbeat 目录 `~/.lamix/heartbeat/` 只有老残留 6204.json

## 修复要求

### 主修：心跳子进程 frozen 兼容
两个方案选一并说明理由：
- A（官方标准）：在 frozen 入口最早期调用 `multiprocessing.freeze_support()`（注意：必须在 `lamix` CLI 的入口 main/__main__ 处，即所有子命令分发之前；PyInstaller 官方要求位置在程序入口第一件事）
- B（绕开坑）：frozen 环境下心跳改用 `threading.Thread`（写入逻辑不变，只是载体从子进程变线程；源码环境保持 mp 不动或也统一改线程）
选能真正在打包版上工作的方案。若选 A，必须在报告中说明如何验证（如打包后跑 `dist/Lamix.app/Contents/MacOS/lamix gateway daemon-run` 数秒观察 heartbeat 目录生成 <pid>.json 再退出——允许做这个短时验证，跑完必须退出进程，不留常驻）。

### 同款坑全面扫清（重点！）
全仓 grep 以下模式，逐一审查是否在 frozen 下失效，失效的都修：
1. `mp.Process` / `multiprocessing` 其他用法（freeze_support 缺失同款）
2. `[sys.executable, str(xxx.py)]` 形式（脚本路径当参数传给二进制）：已知 `src/daemon.py` 的 `_trigger_safe_mode()` 用 `[sys.executable, str(SAFE_MODE_SCRIPT)]` 启动 `src/safe_mode.py`——上轮报告遗留风险 1 点名未修，这轮必须修（frozen 走 CLI 内部子命令或 process_launch helper）
3. `subprocess.*([sys.executable, "-m", ...])` 残留
修完在报告里列出：模式 → 出现位置 → frozen 下是否有效 → 怎么修的。

## 硬性纪律（同上轮）
1. 禁止 kill/pkill 任何 lamix 进程（cli PID 7868 在服务用户）
2. 禁止 launchctl 任何操作；daemon 不留常驻（短时验证后必须退出）
3. 全量 pytest 全过才交付：`cd /Users/songyuhao/lamix && .venv/bin/python -m pytest tests/ --tb=short`
4. 补心跳 frozen 场景测试（可 mock sys.frozen + 验证 freeze_support 调用/线程分支）
5. 用 `python3 scripts/build_app.py` 重新打包到 dist/Lamix.app，**不要装到 /Applications，不碰系统**
6. git 只 commit 不 push

## 交付物
更新 `/Users/songyuhao/lamix/REPORT-DAEMON-RESTART-FIX.md`（追加"第二轮：心跳 freeze_support 修复"章节）：
- 根因一句话
- 修法选择与理由、验证方式与结果（heartbeat 文件生成证据）
- 同款坑扫描清单表
- pytest 结果、打包产物
