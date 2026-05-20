# Lamix 项目文档

> 自主运行的 AI Agent daemon，帮用户把事情做完、做好。

---

## 一、项目概述

**技术栈**：Python 3.11+，智谱 GLM（OpenAI 兼容接口），飞书开放平台 REST API

**核心能力**：
- 多工具调用循环（30 轮自动继续）
- 任务规划与执行（Plan-and-Execute）
- 上下文压缩（轮次分段 + LLM 摘要）
- 反思沉淀（自动创建/更新 skill 和 project）
- 飞书 WebSocket 长连接 + 中断抢占
- 自更新（git 分支 + 回滚）

---

## 二、架构

```
launchd ──→ daemon.py / cli.py
              └── PlatformManager
                  ├── FeishuAdapter（WebSocket 长连接）
                  └── CliAdapter（stdin/stdout）
              └── SessionManager（按 channel+sender_id 隔离）

lamix 命令 ──→ cli.py（独立 REPL，内嵌 PlatformManager）
```

每个渠道拥有独立 Session（Agent + LLM messages），互不干扰。Session 3 小时 idle 自动重置。

### 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| Daemon | `src/daemon.py` | 后台主进程，启动飞书监听 + boot_tasks |
| CLI | `src/cli.py` | 纯 REPL 入口，不启动飞书监听 |
| SessionManager | `src/core/session_manager.py` | 按 channel+sender_id 路由 Session，idle 超时重置 |
| Session | `src/core/session.py` | Agent 生命周期 + 命令路由 + 压缩触发 + 飞书初始化 |
| Agent | `src/core/agent.py` | LLM 主循环，工具分发，规划执行，fallback，中断检查 |
| Planning | `src/planning/` | Planner + Executor + Plan 状态机 |
| LLM | `src/core/llm.py` | OpenAI SDK 封装，消息管理 |
| PromptBuilder | `src/core/prompt_builder.py` | 分层 system prompt（L1-L5） |
| Tools | `src/core/tools.py` | 工具注册表 + dispatch |
| Config | `src/core/config.py` | 配置加载/保存，路径迁移 |
| Memory | `src/memory/manager.py` | MEMORY.md 读写（load/add/search/forget） |
| Skills Tools | `src/core/skills_tools.py` | skill / info / project_context / search_projects |
| Feishu Client | `src/feishu/client.py` | 飞书 REST API（发送/读取/卡片） |
| Feishu Adapter | `src/platforms/adapters/feishu.py` | WebSocket 长连接，消息路由，发送回复 |
| Compaction | `src/core/compaction.py` | 轮次压缩：split_into_turns → 策略 A/B → LLM 摘要 |
| Reflection | `src/core/reflection.py` | 反思沉淀（skill/project create/update） |
| Indexer | `src/core/indexer.py` | SkillIndex / ProjectIndex，增量构建 |
| Interrupt | `src/core/interrupt.py` | AgentInterrupted 异常 + 中断标志位 |
| Platform Adapters | `src/platforms/adapters/` | 平台适配（feishu.py / cli.py） |
| Model Adapters | `src/core/adapters/` | 模型适配（OpenAI 兼容 / MiniMax） |
| Heartbeat | `src/core/heartbeat.py` | 心跳管理器 |
| Watchdog | `src/watchdog.py` | 独立看门狗进程，监控 daemon |
| Self-update | `src/selfupdate/updater.py` | git 分支自更新 |
| Self Audit | `src/core/self_audit.py` | 自我审计（skills/projects/scripts/patterns 扫描，自动修复） |
| Skill Audit | `src/core/skill_audit.py` | Skill 执行步骤追踪审计 |
| Safe Mode | `src/safe_mode.py` | 安全模式（备份/回滚/飞书消息处理） |

---

## 三、Setup Wizard 流程

首次运行 `lamix cli` 时自动进入配置向导：

| 步骤 | 内容 | 可跳过 |
|------|------|--------|
| 1 | 选择 LLM 供应商（GLM/MiniMax/DeepSeek/自定义），上下键选择 | 否 |
| 2 | 输入 API Key | 否 |
| 3 | 选择模型（上下键选择） | 否 |
| 4 | 连通性验证 | 自动 |
| 5 | 飞书配置（App ID、App Secret） | 是 |
| 7 | 保存配置 | 自动 |
| 8 | 用户画像（称呼、偏好、主渠道） | 是 |

配完后自动安装飞书专属 skills（如果配了飞书），daemon 运行中则 30 秒内热重载生效。

## 四、工具列表

| 工具名 | 功能 | 文件 |
|--------|------|------|
| `shell` | 执行 shell 命令（危险命令拦截，支持 Ctrl+C 中断） | `src/tools/shell.py` |
| `search` | ripgrep 文件名/内容搜索（mode=files/content） | `src/tools/search.py` |
| `file_read` | 读文件（100KB 限制） | `src/tools/fileops.py` |
| `file_write` | 写文件（自动创建父目录） | `src/tools/fileops.py` |
| `feishu_send` | 发送飞书消息（text/card） | `src/feishu/client.py` |
| `feishu_read` | 读取飞书会话消息 | `src/feishu/client.py` |
| `skill` | 加载/搜索技能（action=view/search，name 支持 `skill-name` 和 `skill-name/sub-item`） | `src/core/skills_tools.py` |
| `skill_scripts` | 扫描 skills/*/scripts/，动态注册脚本为工具 | `src/tools/skill_scripts.py` |
| `info` | 加载 info 知识文件 | `src/core/skills_tools.py` |
| `project_context` | 加载项目上下文 | `src/core/skills_tools.py` |
| `search_projects` | 语义搜索项目 | `src/core/skills_tools.py` |
| `session` | 搜索/加载历史会话 | `src/tools/session.py` |
| `web_search` | 网页搜索 | `src/tools/web.py` |
| `task_schedule` | 注册定时任务（interval/cron/delayed） | `src/tools/task_scheduler_tool.py` |
| `task_list` | 查看定时任务 | `src/tools/task_scheduler_tool.py` |
| `task_cancel` | 取消定时任务 | `src/tools/task_scheduler_tool.py` |
| `activate_tool_group` | 按需激活工具组（desktop/vision 等） | `src/core/tools.py` |
| `archive` | 归档管理（list/restore） | `src/core/skills_tools.py` |

### 工具组（Tool Groups）

核心工具（shell、file、feishu、skill、info 等）默认加载。部分工具按组管理，仅索引（名称+描述）写入 system prompt，LLM 判断需要时调用 `activate_tool_group(name="组名")` 激活。

| 工具组 | 包含工具 | 默认加载 | 触发方式 |
|--------|---------|---------|---------|
| `desktop` | desktop_* × 9（截图/点击/输入/滚动/查询UI等） | 否 | LLM 看到 GUI 相关任务时主动激活 |
| `vision` | vision_analyze | 否 | LLM 看到图片分析需求时主动激活 |

激活流程：
1. system prompt 只展示组名+描述（如 `desktop: 桌面控制（截图、点击、输入、滚动等 GUI 操作）`）
2. LLM 判断任务需要该能力 → 调用 `activate_tool_group(name="desktop")`
3. 系统返回组内所有 tool schema 的描述，LLM 再选择具体工具执行
4. 激活后该组工具在当前 session 内持续可用

与 skills/info/projects 统一的按需加载模式：
- **skills** → `skill(view)` 按需加载知识文档
- **info** → `info(name)` 按需加载信息
- **projects** → `project_context(name)` 按需加载上下文
- **tool groups** → `activate_tool_group(name)` 按需激活能力

动态工具（如 skill_scripts 沉淀的脚本）注册后自动归入 core 组，始终加载。

---

## 五、PromptBuilder 分层

| 层 | 内容 |
|----|------|
| L1 | Identity（MEMORY.md）+ User（USER.md） |
| L2 | Tool Guidance + Skills 索引（名称+描述） + Tool Groups 索引（名称+描述） |
| L3 | Project Index + Info Index（动态扫描） |
| L4 | Model Guidance（模型适配提示） |
| L5 | Channel Context（非 CLI 时注入消息来源） |

Skills 索引展示 skill 名称、描述、以及子项数量。详情通过 `skill(action='view')` 按需加载，支持加载子项（如 `skill(name="code-writing/references/python-patterns")`）。
Tool Groups 索引只展示组名和描述。需要时通过 `activate_tool_group(name)` 激活组内工具。

---

## 六、数据目录

```
~/.lamix/
├── config.yaml              # 主配置
├── MEMORY.md                # Agent 身份 + 行为准则（500字符限制）
├── USER.md                  # 用户画像 + 偏好
├── boot_tasks.json          # 重启前待办
├── .last_active_date        # 用户最后活跃日期（用于知识归档基准）
├── memory/
│   ├── skills/              # 技能知识文档（双层目录结构）
│   │   ├── <skill-name>/     # 每个技能一个目录
│   │   │   ├── SKILL.md      # 入口文件（技能名称、描述、工作流）
│   │   │   ├── references/   # 参考资料（*.md）
│   │   │   ├── templates/    # 模板片段（*.md）
│   │   │   ├── scripts/     # 可执行脚本（*.py，动态注册为工具）
│   │   │   └── assets/       # 静态资源（图片、配置文件等）
│   │   └── *.md              # 向后兼容：旧格式单文件 skill
│   ├── projects/            # 项目信息（*.md）
│   ├── info/                # 知识文件（*.md）
│   ├── sessions/            # 会话 JSONL
│   │   └── tool_bodies/     # 大型工具结果
│   └── errors.jsonl          # 错误日志
├── index/                   # 索引文件（skills.jsonl, projects.jsonl）
├── search.db                # SQLite FTS5 搜索
├── task_scheduler.db        # APScheduler 持久化
├── metrics.jsonl            # 任务指标
├── audit_reports/           # 审计报告历史
├── archived/                # 归档的 skills/info/projects
│   ├── skills/
│   ├── projects/
│   └── info/
└── logs/                    # daemon 日志
```

---

## 七、配置

`~/.lamix/config.yaml`：

```yaml
llm:
  api_key: ""                                        # LLM API Key（必填）
  base_url: "https://api.deepseek.com/"              # LLM API 地址
  model: "deepseek-v4-flash"                         # 模型名称
  context_window: 131072                           # 模型 context window（token 数）

feishu:
  app_id: ""      # 飞书应用 App ID（可选）
  app_secret: ""  # 飞书应用 App Secret（可选）
memory_path: "~/.lamix/memory"
skills_path: "~/.lamix/memory/skills"
projects_path: "~/.lamix/memory/projects"
info_path: "~/.lamix/memory/info"
retrieval:
  skill_top_k: 3
  project_top_k: 2
  similarity_threshold: 0.3

# Embedding 配置（语义检索 & session 搜索）
# 配置有效的 provider 启用语义检索；留空则降级为纯关键词搜索。
embedding:
  provider: ""
  model: ""
  # base_url: ""
  # api_key: ""

# 上下文压缩配置
compaction:
  enabled: true                    # 是否启用自动压缩
  trigger_threshold: 0.8           # 触发阈值（占 context_window 的百分比）
  end_threshold: 0.3               # 压缩目标（占 context_window 的百分比）
  max_iterations: 3                # 最大压缩迭代次数
  enable_archive: true             # 是否归档有价值内容到文件

# MCP 服务器配置（Phase 2 预留）
# mcp:
#   servers:
#     - name: filesystem
#       command: "npx"
#       args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"]
#       enabled: true

skills_management:
  cleanup_max_skills: 300
  cleanup_age_days: 10
  cleanup_min_invocations: 0

# 视觉模型配置（可选，用于视觉分析工具）
# vision:
#   model: "glm-4.6v"
```

---

## 八、命令

### CLI 子命令（shell 入口）

| 命令 | 功能 |
|------|------|
| `lamix cli [query]` | 交互式 CLI（含 daemon），可跟查询内容直接对话 |
| `lamix gateway` | 仅启动 daemon（后台常驻，飞书消息接收） |
| `lamix gateway start` | 启动 gateway daemon |
| `lamix gateway stop` | 停止 gateway daemon |
| `lamix gateway restart` | 重启 gateway daemon |
| `lamix model` | 重新配置 LLM 模型（供应商/模型/API Key） |
| `lamix update` | 从 GitHub 拉取最新代码，自动重启 daemon |
| `lamix config` | 显示当前配置 |
| `lamix -V` | 显示版本号 |

### REPL 内部命令（`lamix cli` 交互模式下）

| 命令 | 功能 |
|------|------|
| `/help` | 帮助信息 |
| `/config` | 查看/编辑配置 |
| `/model` | 切换/对比模型 |
| `/memory` | 记忆管理（show/add/search/forget） |
| `/skills` | 技能管理 |
| `/feishu` | 飞书操作 |
| `/update` | 自更新 |
| `/compact` | 手动触发压缩 |
| `/context-size` | 查看当前上下文长度和使用率 |
| `/search` | 跨 session 搜索历史 |
| `/resume` | 加载历史 session |
| `/new` | 结束当前 session，创建空白 |
| `/background` | 后台任务 |
| `/tasks` | 查看后台任务 |
| `/cancel` | 取消任务 |
| `/metrics` | 任务统计 |
| `/safemode` | 安全模式 |
| `/self-audit` | 立即触发自我审计 |
| `/audit-report` | 查看历史审计报告 |
| `/exit` | 退出 |

### Shell 工具特性

| 特性 | 说明 |
|------|------|
| 命令长度限制 | 最大 100KB，防止超长命令注入 |
| 危险命令拦截 | `rm -rf /`、`mkfs`、`dd` 等命令执行前拦截 |
| 通配符滥用检测 | `cat *.py`、`rm src/*` 等命令被拦截，引导使用 `search`/`file_read` |
| Ctrl+C 中断 | CLI 模式下支持中断正在执行的命令 |

---

## 九、特色功能

### 9.1 自我审计（Self Audit）

daemon **每天凌晨 4 点**自动执行一次审计（通过 APScheduler cron 触发）。

**审计内容**：
- `scan_skills`: 检查 skills 目录结构、frontmatter、散落文件，自动修复
- `scan_skill_overlap`: 检测 skill 职责重叠（关键词重叠率）
- `scan_projects`: 检查 projects 目录结构
- `scan_skill_scripts`: 验证脚本签名规范（`def TOOL_RUNNER(params: dict) -> str:`）
- `scan_user_patterns`: 分析用户使用习惯
- `cleanup_stale_knowledge`: 清理过期 skill/info/project

**自动修复**：
- 空目录删除
- 散落 .md 合并到 SKILL.md
- 缺失 frontmatter 自动生成
- TOOL_RUNNER 签名修正

**知识归档规则**：
- 归档基准日期为用户最后活跃日期（`.last_active_date`）
- 7 天未使用且调用次数为 0 → 自动归档
- 30 天未使用 → 自动归档
- skill / info / project 三类统一归档规则
- 归档不删除，移入 `archived/` 子目录，可恢复

**报告推送**：
- 审计结果推送飞书群聊
- 报告持久化到 `~/.lamix/audit_reports/` 目录

**可用命令**：
- `/self-audit` — 立即触发审计
- `/audit-report` — 列出最近 10 份历史报告
- `/audit-report <path>` — 查看指定报告详情

### 9.2 Skill 审计（Skill Audit）

追踪 Skill 执行过程中的步骤完成情况，帮助发现 skill 定义的缺失或不完善。

**工作原理**：
- 在 skill view 时解析 SKILL.md 中的步骤（如 `1. 理解需求`、`## 工作流` 等）
- 在 Agent 执行时记录 tool_call 和 LLM output
- 结束时对比步骤关键词和实际执行，生成审计报告

**用途**：
- 检验 skill 是否按预期工作流执行
- 发现遗漏步骤或多余步骤
- 持续改进 skill 质量

### 9.3 Boot Tasks（重启验证）

改了 daemon 代码后需要重启才能生效。Boot Task 机制让你在重启前写好验证任务，daemon 重启后自动执行并汇报结果。

**使用场景**：改了 compaction 逻辑，重启后自动验证压缩是否正常。

```bash
# 写入 boot task（重启前执行）
file_write("~/.lamix/boot_tasks.json", '[{"task": "验证改动：发几条消息测试交互，检查日志无异常"}]')

# 重启 daemon
/restart
```

daemon 启动后读取 boot_tasks.json，把任务注入 session 执行，完成后清空文件。

### 9.4 定时任务

通过 `task_schedule` 工具注册定时任务，支持三种触发方式：

| 类型 | 说明 | 示例 |
|------|------|------|
| `interval` | 固定间隔 | 每 30 分钟检查一次 |
| `cron` | 定时执行 | 每天凌晨 4 点触发（需配合触发逻辑） |
| `delayed` | 一次性延迟 | 5 分钟后提醒 |

```bash
# 注册间隔任务
task_schedule(action="schedule", task_id="monitor", task_type="interval", interval_seconds=1800, prompt="检查服务状态")

# 注册 cron 任务
task_schedule(action="schedule", task_id="daily_report", task_type="cron", cron_hour=9, cron_minute=0, prompt="发送昨日工作汇总")

# 查看所有任务
task_schedule(action="list")

# 取消任务
task_schedule(action="cancel", task_id="monitor")
```

### 9.5 Safe Mode（安全模式）

安全模式提供备份、回滚和最小化飞书交互能力，用于诊断和恢复问题状态。

**功能**：
- `create_backup`: 创建配置快照
- `restore_backup`: 从快照恢复
- `list_backups`: 列出可用备份
- `handle_feishu_message`: 最小化飞书消息处理（心跳+日志）
- `restart_daemon`: 安全重启 daemon

**使用场景**：
- 排查 daemon 启动问题
- 恢复误配置
- 隔离问题排查

### 9.6 桌面控制（键鼠 + 截图）

Lamix 可以操作鼠标键盘和截屏，实现 GUI 自动化。

**前提条件**：
- macOS：系统设置 → 隐私与安全 → 辅助功能 → 授予终端/Python 权限
- Windows：以管理员身份运行，或授予对应权限
- 依赖 `pyautogui` 和 `Pillow`（默认安装）

**能力**：截图、点击、输入文字、按键、组合键、滚动、拖拽、UI 元素查询

### 9.7 视觉分析

通过截图 + 视觉模型分析屏幕内容，配合桌面控制实现"看到就能操作"。

**前提条件**：在 `config.yaml` 中配置视觉模型：

```yaml
vision:
  model: "glm-4.6v"
```

未配置时，视觉分析工具调用会提示用户配置。

### 9.8 双层 Skills 设计

#### 目录结构

```
skills/
  <skill-name>/                 # 每个技能一个目录
    SKILL.md                    # 入口：名称、描述、工作流（必选）
    references/                 # 参考资料（可选）
      *.md                      # 多个参考文档
    templates/                   # 模板片段（可选）
      *.md                      # 多个模板
    scripts/                     # 可执行脚本（可选，动态注册为工具）
      *.py
    assets/                      # 静态资源（可选）
      *
  *.md                          # 向后兼容：旧格式单文件 skill
```

#### SKILL.md 格式

```yaml
---
name: code-writing
description: 编写高质量代码的标准工作流
created_at: "2025-01-01"
invocation_count: 0
---
# Code Writing

## 何时使用
当你需要编写新代码时使用此工作流。

## 工作流
1. 理解需求
2. 设计接口
3. 实现
4. 测试

## 子项
- references/：Python/JS 编码规范、常见错误模式
- templates/：类模板、API 模板
```

#### 加载规则

| 调用方式 | 加载内容 |
|---------|---------|
| `skill(action='view', name='code-writing')` | 加载 `code-writing/SKILL.md` |
| `skill(action='view', name='code-writing/references/python-patterns')` | 加载 `code-writing/references/python-patterns.md` |
| `skill(action='search', query='python')` | 在所有 SKILL.md + references/ 下全文搜索 |

#### 渐进加载策略

- **第 1 层**（索引）：`SKILL.md` 的 name + description + 子项摘要（references/ ×N, templates/ ×N）
- **第 2 层**（按需）：读取 `references/`、`templates/`、`scripts/`、`assets/`
- **scripts/** 下的 `.py` 文件定义了 `TOOL_SCHEMA` + `TOOL_RUNNER` 时，自动注册为工具

#### 向后兼容

- `skills/*.md`（平铺单文件）仍被识别为一个有效的 skill，等价于 `skills/xxx/SKILL.md`
- 迁移脚本自动将旧单文件迁移到新目录结构

### 9.9 Config 热重载

daemon 运行中修改 `~/.lamix/config.yaml`（比如改了飞书配置），无需重启。daemon 每 30 秒检测配置文件变化，自动热重载飞书 adapter。

## 十、部署

```bash
# 安装
cd ~/lamix && pip install -e .

# CLI 交互
lamix cli

# 后台 daemon
lamix gateway

# Windows 开机自启（管理员）
python scripts/install_windows.py
```

### Windows 特殊说明

**跨平台进程检测**：Windows 上 `os.kill(pid, 0)` 不可靠，改用 `tasklist` 命令检测进程是否存在。

**Gateway 管理命令**：
```cmd
lamix gateway start   # 启动
lamix gateway stop   # 停止
lamix gateway restart # 重启
```

---

## 十一、版本历史

### v0.2.x
- 新增 `gateway start/stop/restart` 命令
- CLI 模式 Ctrl+C 中断支持
- Shell 工具通配符滥用检测（`cat *.py` 等）
- Shell 命令长度限制 100KB
- 跨平台 `_process_exists()` 函数（Windows 用 tasklist）
- 首次运行自动问候语
- CLI 工具调用实时进度显示
- Skills 双层目录结构（skills/\<name\>/SKILL.md + references/ + templates/ + scripts/ + assets/）
