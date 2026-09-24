# 任务：修复 PyInstaller 打包版 daemon 启动命令失效 + instance.lock 身份误判

## 背景
Lamix 是一个 AI Agent daemon（源码 /Users/songyuhao/lamix，Python），昨天 14:49 用 PyInstaller 打包为 /Applications/Lamix.app。打包后 daemon 无法启动：

打包环境下 `sys.executable` = lamix 二进制（不是 python），所有 `[sys.executable, "-m", "src.xxx"]` 形式的子进程启动命令失效，报：
```
lamix: error: argument command: invalid choice: 'src.watchdog' (choose from 'cli', 'gateway', 'model', 'update', 'config')
```
证据日志：~/.lamix/logs/watchdog.err.log 尾部、~/.lamix/logs/launchd.log。

## Bug 清单（都要修）

### Bug 1: frozen 环境子进程启动命令失效
涉及位置（grep 确认，可能不止这些）：
- src/watchdog.py 约 129-136 行：watchdog 拉起 daemon 用 `[sys.executable, "-m", "src.daemon"]`
- src/cli.py 约 430、435、448、617 行：gateway/watchdog 启动命令
- src/safe_mode.py 约 55 行：daemon 启动命令

修复要求：
- 检测 `getattr(sys, "frozen", False)`：
  - 源码环境：保持现有 `python -m src.xxx` 不变
  - frozen 环境：不能依赖 `-m src.xxx`。两个可选方案，你读完 gateway/daemon 入口代码后选一个并在报告里说明理由：
    - A. CLI 增加内部子命令（如 `lamix gateway daemon-run` / `lamix gateway watchdog-run`），frozen 时 watchdog 用 `[sys.executable, "gateway", "daemon-run"]` 拉起
    - B. frozen 时同进程直接调用 daemon 的入口函数（注意 watchdog 拉起的必须是独立子进程，daemon 崩溃不能带死 watchdog）
- 注意 PyInstaller 打包时 src.daemon/src.watchdog 模块必须在包内可导入（检查现有 build/spec 脚本是否已含，不足则补 hiddenimports）

### Bug 2: instance.lock 只查 PID 存活，不查进程身份
现象：lock 文件里是 cli 进程 PID（活着），gateway start 就误报 "Lamix 已在运行，无需重复启动"，daemon 永远起不来。
修复要求：
- "已在运行"判定必须验证 lock 中 PID 的进程身份确实是 daemon（如 lock 文件写入进程类型字段 daemon/cli，或校验 /proc 等价方式 ps command 包含 daemon 标识）
- cli 进程持锁不得阻止 daemon 启动（按需设计：daemon 与 cli 分锁，或 daemon 启动时可覆写 cli 的锁）
- 保持向后兼容：老格式 lock（纯 PID）按现逻辑处理但补上身份校验

## 硬性纪律
1. **禁止 kill/pkill 任何 lamix 进程**——当前 PID 7868 `lamix cli` 正在服务用户会话，杀了会断线
2. **禁止 launchctl load/unload/kickstart**，禁止重启 daemon/watchdog——重启由用户决定
3. 修改后必须跑全量测试：`cd /Users/songyuhao/lamix && .venv/bin/python -m pytest tests/ --tb=short`，**全部通过才算完成**（用户纪律：任何测试失败不可交付）
4. 为 Bug1/Bug2 补测试用例（frozen 检测逻辑、lock 身份判定逻辑，可 mock sys.frozen）
5. 重新打包 Lamix.app：先找现有打包脚本（查 build/、dist/、*.spec、Makefile、pyproject），用原脚本打包出新的 /Applications/Lamix.app。**打包后只验证产物存在 + `--help` 可跑，不要安装到系统、不要碰 launchd**
6. git：只 add/commit，**禁止 push**（push 前需用户测试确认，这是用户铁律）

## 交付物
写 /Users/songyuhao/lamix/REPORT-DAEMON-RESTART-FIX.md：
- 根因复述（一句话）
- 每个 bug 的修复方案与改动文件清单（git diff --stat）
- pytest 结果（通过数/总数）
- 打包产物路径与验证结果
- 遗留风险（如 hiddenimports 可能缺的模块）
