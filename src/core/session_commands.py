"""Session 的 `/` 命令处理 mixin。

抽出的原因：Session 内的 `_handle_*` 命令处理器加起来约 600 行，是最大的可独立
维护的一块。这里定义 mixin，Session 通过多重继承混入。命令行相关的常量
（HELP_TEXT、HandleResult）也一并归此，避免 session.py 与本文件双向依赖。
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.core.config import LAMIX_DIR, SKILLS_DIR, PROJECTS_DIR
from src.core.adapters import BaseModelAdapter, create_adapter
from src.core.llm import LLMClient
from src.core.metrics import format_summary
from src.memory import manager as memory_mgr
from src.memory import session_store
from src.memory.session_search import search_sessions
from src.skills import manager as skills_mgr

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


HELP_TEXT = """\
可用命令：
  /help                          显示此帮助
  /config                        查看当前配置
  /model                         显示当前模型和可用模型列表
  /model <name>                  切换到指定模型
  /model all <question>          同时向所有可用模型提问，对比回答
  /memory show                   查看长期记忆
  /memory add <text>             添加记忆条目
  /memory search <keyword>       搜索记忆
  /memory forget <keyword>       删除含关键词的记忆条目
  /search <keyword>              搜索历史对话记录
  /resume                        列出最近 5 个 session
  /resume <id>                   加载指定 session 到当前对话
  /background <prompt>           后台运行任务，完成后推送结果
  /tasks                         查看运行中的后台任务
  /cancel <task_id>              取消后台任务
  /skills list                   列出所有技能
  /skills show <name>            查看技能详情
  /skills create <name>          创建新技能
  /skills consolidate            分析并合并重复/耦合的技能
  /feishu send <id> <msg>        发送飞书消息（需配置 app_id/secret）
  /feishu read <chat_id>         读取飞书消息
  /update <需求描述>              触发自更新
  /update rollback               回滚自更新
  /update list                   列出自更新分支
  /metrics                       查看最近任务指标统计
  /compact                    手动触发上下文压缩
  /context-size                   查看当前上下文长度和占比
  /self-audit                    立即触发自我审计
  /audit-report                  列出历史审计报告
  /audit-report <path>           查看指定审计报告详情
  /new                           开始新 session（清空当前对话上下文）
  /exit                          退出

直接输入自然语言即可与 Lamix 对话。"""


@dataclass
class HandleResult:
    """handle_input 的返回值，让 gateway 知道发生了什么。"""

    reply: str = ""              # 要展示/发送的回复文本
    is_exit: bool = False        # 用户要求退出
    is_command: bool = False     # 这是一条 / 命令（不需要再格式化）
    is_new: bool = False        # 用户要求开始新 session
    is_safe_mode: bool = False  # 用户要求进入 safe_mode
    compaction_msg: str = ""     # 压缩通知（空字符串表示没压缩）


class SessionCommandsMixin:
    """Session 的 `/` 命令处理器集合。

    所有方法通过 `self.<attr>` 访问 Session 状态（agent / config / llm_clients /
    partial_sender / skills / channel / session_id / _current_model_name 等），
    因此必须与真实的 Session 混入使用。
    """

    def _handle_command(self, cmd: str) -> HandleResult:
        """处理 / 开头的命令。"""
        parts = cmd.strip().split()
        if not parts:
            return HandleResult(is_command=True)

        command = parts[0].lower()

        if command == "/exit":
            return HandleResult(is_exit=True, is_command=True)

        if command == "/new":
            return HandleResult(is_new=True, is_command=True)

        if command == "/metrics":
            return HandleResult(reply=format_summary(), is_command=True)

        if command == "/compact":
            return self._handle_compaction()

        if command == "/context-size":
            return HandleResult(reply=self._handle_contextsize(), is_command=True)

        if command == "/self-audit":
            return self._handle_self_audit()

        if command == "/audit-report":
            return HandleResult(reply=self._handle_audit_report(parts), is_command=True)

        if command == "/help":
            return HandleResult(reply=HELP_TEXT, is_command=True)

        if command == "/config":
            return HandleResult(reply=self._format_config(), is_command=True)

        if command == "/memory":
            return HandleResult(reply=self._handle_memory(parts), is_command=True)

        if command == "/skills":
            return HandleResult(reply=self._handle_skills(parts), is_command=True)

        if command == "/feishu":
            return HandleResult(reply=self._handle_feishu(parts), is_command=True)

        if command == "/update":
            return HandleResult(reply=self._handle_update(parts), is_command=True)

        if command == "/model":
            return HandleResult(reply=self._handle_model(parts), is_command=True)

        if command == "/search":
            return HandleResult(reply=self._handle_search(parts), is_command=True)

        if command == "/background":
            prompt = " ".join(parts[1:]) if len(parts) > 1 else ""
            if not prompt:
                return HandleResult(reply="用法: /background <prompt>", is_command=True)
            return self._handle_background(prompt)

        if command == "/tasks":
            return self._handle_tasks()

        if command == "/cancel":
            return self._handle_cancel(parts)

        if command == "/safemode":
            return HandleResult(reply="正在切换到安全模式...", is_safe_mode=True, is_command=True)
        if command == "/resume":
            return HandleResult(reply=self._handle_resume(parts), is_command=True)

        logger.info("[session] 未识别的 / 命令 %r，当普通文本处理", command)
        return None

    def _handle_compaction(self) -> HandleResult:
        """手动触发上下文压缩。"""
        try:
            cr = self.agent.force_compact(
                session_store=session_store,
                session_id=self.session_id or "",
                progress_callback=self.partial_sender,
            )
            if cr is None:
                return HandleResult(reply="压缩不可用（未配置 compaction 或有计划正在执行）", is_command=True)
            if cr.success:
                self._current_segment += 1
                return HandleResult(
                    reply=f"[上下文压缩] 已完成，归档 {cr.archived_count} 条内容，{cr.tokens_before} → {cr.tokens_after} token。",
                    is_command=True,
                    compaction_msg="",
                )
            else:
                return HandleResult(
                    reply="[上下文压缩] 失败: " + (cr.error or "未知错误"),
                    is_command=True,
                )
        except Exception as e:
            return HandleResult(reply=f"[上下文压缩] 异常: {e}", is_command=True)

    def _handle_contextsize(self) -> str:
        """返回当前上下文长度信息。"""
        messages = self.agent.llm.messages
        total_msgs = len(messages)
        user_msgs = sum(1 for m in messages if m.get("role") == "user")
        assistant_msgs = sum(1 for m in messages if m.get("role") == "assistant")
        tool_msgs = sum(1 for m in messages if m.get("role") == "tool")

        estimated = self.agent._estimate_context_tokens()
        source = "LLM返回值" if self.agent.last_prompt_tokens > 0 else "bytes/4估算"

        cw = 0
        if self.agent._compaction_config:
            cw = self.agent._compaction_config.context_window

        lines = [
            f"当前上下文: {estimated:,} tokens（{source}）",
        ]
        if cw > 0:
            pct = estimated / cw * 100
            lines.append(f"Context Window: {cw:,} tokens")
            lines.append(f"使用率: {pct:.1f}%")
            threshold = int(cw * self.agent._compaction_config.trigger_threshold)
            lines.append(f"压缩触发阈值: {threshold:,} tokens（{self.agent._compaction_config.trigger_threshold:.0%}）")
        lines.append(f"消息数: {total_msgs} 条（user: {user_msgs}, assistant: {assistant_msgs}, tool: {tool_msgs}）")

        return "\n".join(lines)

    def _handle_self_audit(self) -> HandleResult:
        """手动触发自我审计。"""
        try:
            from src.core.self_audit import run_audit, format_report_detail, save_report
            report = run_audit()
            save_report(report)
            detail = format_report_detail(report)
            return HandleResult(reply=detail, is_command=True)
        except Exception as e:
            return HandleResult(reply=f"[自我审计] 失败: {e}", is_command=True)

    def _handle_audit_report(self, parts: list[str]) -> str:
        """处理 /audit-report 命令：列出历史报告或查看指定报告详情。"""
        from src.core.self_audit import list_reports, load_report, format_report_detail

        if len(parts) >= 2:
            report_path = parts[1]
            report = load_report(report_path)
            if report is None:
                return f"[错误] 无法加载报告: {report_path}"
            return format_report_detail(report)

        reports = list_reports(limit=10)
        if not reports:
            return "[audit-report] 暂无历史审计报告。"

        lines = ["[audit-report] 历史审计报告：\n"]
        for i, r in enumerate(reports, 1):
            ts = r["timestamp"]
            findings = r["findings_count"]
            skills = r["skills_scanned"]
            projects = r["projects_scanned"]
            lines.append(
                f"  {i}. [{ts}] 扫描 {skills} skills / {projects} projects，"
                f"发现 {findings} 条问题"
            )
            lines.append(f"     → {r['path']}")
        lines.append("\n使用 /audit-report <path> 查看详情。")
        return "\n".join(lines)

    def _format_config(self) -> str:
        """脱敏后格式化配置。"""
        import yaml

        safe_config = dict(self.config)
        llm_cfg = safe_config.get("llm", {})
        if llm_cfg.get("api_key"):
            safe_config["llm"] = dict(llm_cfg)
            key = safe_config["llm"]["api_key"]
            safe_config["llm"]["api_key"] = key[:6] + "..." + key[-4:] if len(key) > 10 else "***"
        feishu_cfg = safe_config.get("feishu", {})
        if feishu_cfg.get("app_secret"):
            safe_config["feishu"] = dict(feishu_cfg)
            safe_config["feishu"]["app_secret"] = "***"
        return yaml.dump(safe_config, allow_unicode=True, default_flow_style=False)

    def _handle_memory(self, parts: list[str]) -> str:
        sub = parts[1] if len(parts) > 1 else "show"

        if sub == "show":
            return memory_mgr.show_memory()

        if sub == "add":
            if len(parts) < 3:
                return "用法: /memory add <text>"
            text = " ".join(parts[2:])
            return memory_mgr.add_memory(text)

        if sub == "search":
            if len(parts) < 3:
                return "用法: /memory search <keyword>"
            keyword = " ".join(parts[2:])
            return memory_mgr.search_memory(keyword)

        if sub == "forget":
            if len(parts) < 3:
                return "用法: /memory forget <keyword>"
            keyword = " ".join(parts[2:])
            return memory_mgr.forget_memory(keyword)

        return "用法: /memory [show|add <text>|search <keyword>|forget <keyword>]"

    def _handle_skills(self, parts: list[str]) -> str:
        sub = parts[1] if len(parts) > 1 else "list"

        if sub == "list":
            return skills_mgr.list_skills(self.skills)

        if sub == "show":
            if len(parts) < 3:
                return "用法: /skills show <name>"
            return skills_mgr.show_skill(parts[2], self.skills)

        if sub == "create":
            if len(parts) < 3:
                return "用法: /skills create <name>"
            name = parts[2]
            desc = " ".join(parts[3:]) if len(parts) > 3 else ""
            result = skills_mgr.create_skill(name, description=desc)
            self.skills.clear()
            self.skills.update(skills_mgr.load_all_skills())
            self._refresh_system_prompt()
            return result

        if sub == "consolidate":
            bundle = self.llm_clients.get(self._current_model_name)
            if not bundle:
                return "[错误] 当前模型无可用的 LLM Client"
            llm_client = bundle.get("llm")
            if not llm_client:
                return "[错误] 无法获取 LLM Client"
            actions, analysis = skills_mgr.consolidate_skills(self.skills, llm_client)
            if not actions:
                if analysis.startswith("[错误]"):
                    return analysis
                return f"分析结果：\n{analysis}\n\n无需合并。"

            result = skills_mgr.execute_consolidation(actions)
            self.skills.clear()
            self.skills.update(skills_mgr.load_all_skills())
            self._reload_skill_index()
            self._refresh_system_prompt()
            return f"分析：{analysis}\n\n{result}"

        return "用法: /skills [list|show <name>|create <name>|consolidate]"

    def _handle_feishu(self, parts: list[str]) -> str:
        if len(parts) < 2:
            return "用法: /feishu [send <id> <msg>|read <chat_id>]"

        try:
            from src.feishu import client as feishu_client
            feishu_client.get_client()
        except RuntimeError as e:
            return f"[飞书] {e}"

        sub = parts[1]
        if sub == "send":
            if len(parts) < 4:
                return "用法: /feishu send <receive_id> <消息内容>"
            receive_id = parts[2]
            text = " ".join(parts[3:])
            return feishu_client.tool_feishu_send({
                "receive_id": receive_id,
                "text": text,
            })

        if sub == "read":
            if len(parts) < 3:
                return "用法: /feishu read <chat_id>"
            return feishu_client.tool_feishu_read({
                "container_id": parts[2],
                "page_size": 10,
            })

        return "用法: /feishu [send <id> <msg>|read <chat_id>]"

    def _handle_model(self, parts: list[str]) -> str:
        """处理 /model 命令。"""
        if len(parts) == 1:
            lines = [f"当前模型：{self._current_model_name}", "可用模型："]
            for name in self.llm_clients:
                marker = " ← 当前" if name == self._current_model_name else ""
                lines.append(f"  - {name}{marker}")
            return "\n".join(lines)

        if parts[1].lower() == "all":
            question = " ".join(parts[2:])
            if not question.strip():
                return "用法: /model all <问题内容>"

            tools_schemas = self.agent._tools

            def query_model(name: str, client_bundle: Any) -> tuple[str, str]:
                """完整工具调用循环（经 Model Adapter），每轮实时反馈。"""
                from src.core import tools as _tool_reg

                max_rounds = self.agent.max_tool_rounds
                sender = self.partial_sender

                def _send(text: str) -> None:
                    if sender:
                        try:
                            sender("[{}] {}".format(name, text))
                        except Exception:
                            pass

                try:
                    base_llm: LLMClient = client_bundle["llm"]
                    tmp = LLMClient(
                        api_key=base_llm.client.api_key,
                        base_url=str(base_llm.client.base_url),
                        model=base_llm.model,
                    )
                    tmp_adapter = create_adapter(tmp)
                    tmp.add_user_message(question)

                    for round_num in range(max_rounds):
                        try:
                            resp = tmp_adapter.chat(tmp.messages, tools=tools_schemas)
                        except RuntimeError as e:
                            _send("=== end turn ===\n[请求失败: {}]".format(e))
                            return name, ""

                        tmp.messages.append(
                            resp.choices[0].message.model_dump(exclude_none=True)
                        )
                        parsed = tmp_adapter.parse_response(resp)

                        if not parsed.tool_calls:
                            final_text = parsed.content or ""
                            _send("=== end turn ===\n" + final_text)
                            return name, ""

                        for tc in parsed.tool_calls:
                            preview = tc.raw_arguments[:200]
                            _send("Round {}: 调用 {}({})".format(
                                round_num + 1, tc.name, preview,
                            ))
                            result = _tool_reg.dispatch(tc.name, tc.raw_arguments)
                            result_preview = result[:500] + (
                                "..." if len(result) > 500 else ""
                            )
                            _send("  结果: " + result_preview)
                            tmp.messages.append(
                                tmp_adapter.format_tool_result(tc.id, result)
                            )

                    _send("=== end turn ===\n[超过 {} 轮限制]".format(max_rounds))
                    return name, ""

                except Exception as e:
                    _send("=== end turn ===\n[请求失败: {}]".format(e))
                    return name, ""

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(self.llm_clients)
            ) as executor:
                futures = {
                    executor.submit(query_model, name, bundle): name
                    for name, bundle in self.llm_clients.items()
                }
                try:
                    for future in concurrent.futures.as_completed(futures, timeout=180):
                        name, _ = future.result()
                except concurrent.futures.TimeoutError:
                    for name in self.llm_clients:
                        sender = self.partial_sender
                        if sender:
                            try:
                                sender("[{}] === end turn ===\n[请求超时]".format(name))
                            except Exception:
                                pass

            return ""

        target_name = parts[1]
        if target_name not in self.llm_clients:
            available = ", ".join(sorted(self.llm_clients.keys()))
            return f"未知模型：{target_name}，可用模型：{available}"

        bundle = self.llm_clients[target_name]
        new_llm = bundle["llm"]
        new_adapter: BaseModelAdapter = bundle["adapter"]
        model_cw = bundle.get("context_window")
        from src.core.session import _build_compaction_config
        new_compaction = _build_compaction_config(self.config, model_context_window=model_cw)
        self._current_model_name = target_name
        self.agent.switch_llm(new_llm, new_adapter, compaction_config=new_compaction)
        return f"已切换到模型：{target_name}"

    def _handle_update(self, parts: list[str]) -> str:
        from src.selfupdate import updater

        if len(parts) < 2:
            return "用法: /update <需求描述> 或 /update rollback 或 /update list"

        sub = parts[1]
        if sub == "rollback":
            return updater.run_rollback()
        if sub == "list":
            return updater.list_update_branches()

        description = " ".join(parts[1:])
        return updater.run_update(description, self.agent.llm)

    def _handle_search(self, parts: list[str]) -> str:
        """处理 /search 命令：搜索历史对话。"""
        if len(parts) < 2:
            return "用法: /search <关键词>"

        query = " ".join(parts[1:])
        try:
            results = search_sessions(query=query, limit=5)
        except Exception as e:
            return f"[错误] 搜索失败: {e}"

        if not results:
            return f"没有找到与 \"{query}\" 相关的历史对话。"

        from datetime import datetime

        lines = [f"找到 {len(results)} 条与 \"{query}\" 相关的记录：\n"]
        for i, r in enumerate(results, 1):
            role_label = "用户" if r.role == "user" else "Lamix"
            try:
                dt = datetime.fromtimestamp(r.ts / 1000).strftime("%Y-%m-%d %H:%M")
            except Exception:
                dt = str(r.ts)
            lines.append(f"--- 结果 {i} ---\n[{dt}] {role_label}（session: {r.session_id}）\n{r.snippet}\n")

        return "\n".join(lines)

    def _handle_resume(self, parts: list[str]) -> str:
        """处理 /resume 命令：列出或加载历史 session。"""
        if len(parts) >= 2:
            session_id = parts[1]
            return self.load_session(session_id=session_id, inject=True)

        try:
            sessions = session_store.list_recent_sessions(limit=5)
        except Exception as e:
            return f"[错误] 获取历史 session 失败: {e}"

        if not sessions:
            return "没有找到历史 session。"

        lines = ["最近的 session：\n"]
        for i, s in enumerate(sessions, 1):
            sid = s["session_id"]
            from datetime import datetime
            try:
                dt = datetime.fromtimestamp(s["started_at"] / 1000).strftime("%Y-%m-%d %H:%M")
            except Exception:
                dt = str(s["started_at"])
            msg_count = s.get("message_count", "?")
            lines.append(f"  {i}. [{dt}] {sid} ({msg_count} 条消息)")
        lines.append("\n使用 /resume <id> 加载指定 session。")
        return "\n".join(lines)

    def _maybe_update_memory_md(self) -> None:
        """检查是否需要更新 MEMORY.md（退出时调用）。

        触发条件：累计 archive 次数 > 5 或距上次更新超过 24 小时。
        """
        from src.core.compaction import COMPACTION_LOG

        archive_count = 0
        if COMPACTION_LOG.exists():
            try:
                with open(COMPACTION_LOG, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        entry = json.loads(line)
                        targets = entry.get("archive_targets", [])
                        if targets:
                            archive_count += len(targets)
            except Exception:
                pass

        memory_path = LAMIX_DIR / "MEMORY.md"
        hours_since_update = float("inf")
        if memory_path.exists():
            mtime = memory_path.stat().st_mtime
            hours_since_update = (time.time() - mtime) / 3600

        if archive_count <= 5 and hours_since_update < 24:
            return

        skill_summaries: list[str] = []
        for p in SKILLS_DIR.glob("*.md"):
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    skill_summaries.append(f"### {p.stem}\n{content[:500]}")
            except OSError:
                pass

        project_summaries: list[str] = []
        for p in PROJECTS_DIR.glob("*.md"):
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    project_summaries.append(f"### {p.stem}\n{content[:500]}")
            except OSError:
                pass

        if not skill_summaries and not project_summaries:
            return

        all_content = "\n\n".join(skill_summaries + project_summaries)
        prompt = (
            "以下是 Lamix 的归档知识（skill 和 project），"
            "请抽取对长期记忆最有价值的精华，生成简洁的 MEMORY.md 内容。\n"
            "只保留：用户偏好、关键决策、重要约束、常用工具技巧。\n"
            "每条一行，用简洁的中文描述。不要超过 50 行。\n\n"
            f"## Skills\n{all_content}\n"
        )
        try:
            self.agent.channel = self.channel
            self.agent.partial_sender = self.partial_sender
            result = self.agent.run(prompt)
            if result and result.strip():
                memory_path.parent.mkdir(parents=True, exist_ok=True)
                if memory_path.exists():
                    import shutil
                    shutil.copy2(memory_path, memory_path.with_suffix(".md.bak"))
                memory_path.write_text(result.strip(), encoding="utf-8")
                logger.info(f"[MEMORY.md] 已更新（archive={archive_count}, 距上次 {hours_since_update:.0f}h）")
        except Exception as e:
            logger.warning(f"core.md 更新失败: {e}")
