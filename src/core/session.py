"""Session — Agent 生命周期管理、命令路由、上下文压缩。

gateway 层（cli.py / listener.py）只需关心消息收发，
所有业务逻辑通过 Session 这一层统一处理。
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import queue
import threading

from src.core.config import (
    LAMIX_DIR,
    get_retrieval_config,
    get_embedding_config,
    INDEX_DIR,
    SKILLS_DIR,
    PROJECTS_DIR,
)
from src.core.indexer import ProjectIndex, SkillIndex
from src.core.llm import LLMClient
from src.core.adapters import BaseModelAdapter, create_adapter
from src.core.compaction import CompactionConfig, resolve_context_window
from src.core.agent import Agent
from src.memory import manager as memory_mgr
from src.memory import session_store
from src.memory.session_search import search_sessions
from src.core import skills_tools as skills_tools_reg
from src.skills import manager as skills_mgr
from src.core.metrics import TaskCollector, format_summary

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


def _assistant_content_as_text(content: Any) -> str:
    """从 assistant content 提取可检索的纯文本（含多段 text block）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content) if content else ""


def _infer_referenced_tool_call_ids(msgs: list, assistant_msg: dict) -> list[str]:
    """根据本回合 assistant 正文与此前列 tool 结果，推断引用的 tool_call_id。

    与 memory-design 一致：只记录“回复内容实际引用/依据”的 prior tool 结果，而非当条里的 tool_calls。
    """
    content_text = _assistant_content_as_text(assistant_msg.get("content", ""))
    if not content_text.strip():
        return []
    try:
        idx = next(i for i, m in enumerate(msgs) if m is assistant_msg)
    except StopIteration:
        return []

    prior_ids: list[str] = []
    for m in msgs[:idx]:
        if m.get("role") != "tool":
            continue
        tid = m.get("tool_call_id") or m.get("id") or ""
        if tid:
            prior_ids.append(tid)
    if not prior_ids:
        return []

    referenced: list[str] = []
    seen: set[str] = set()
    for tid in prior_ids:
        if tid in content_text:
            if tid not in seen:
                seen.add(tid)
                referenced.append(tid)
            continue
        # 单条 tool 且回复较长时，常见指代词视为引用该结果（启发式）
        if len(prior_ids) == 1 and len(content_text) > 30:
            if any(
                k in content_text
                for k in (
                    "结果",
                    "返回",
                    "输出",
                    "上面",
                    "根据",
                    "如下",
                    "显示",
                )
            ):
                if tid not in seen:
                    seen.add(tid)
                    referenced.append(tid)
    return referenced


@dataclass
class HandleResult:
    """handle_input 的返回值，让 gateway 知道发生了什么。"""

    reply: str = ""              # 要展示/发送的回复文本
    is_exit: bool = False        # 用户要求退出
    is_command: bool = False     # 这是一条 / 命令（不需要再格式化）
    is_new: bool = False        # 用户要求开始新 session
    is_safe_mode: bool = False  # 用户要求进入 safe_mode
    compaction_msg: str = ""     # 压缩通知（空字符串表示没压缩）


class Session:
    """管理 Agent 的完整生命周期。

    gateway 的使用方式：
        session = Session.from_config(config)
        result = session.handle_input(user_input)
        # result.reply → 发给用户
        # result.is_exit → 该退出循环了
    """

    def __init__(
        self,
        agent: Agent,
        config: dict[str, Any],
        skills: dict | None = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.skills: dict = skills or {}
        self.skill_index: SkillIndex | None = None
        self.project_index: ProjectIndex | None = None
        self.retrieval_config: dict[str, Any] = {}
        self._feishu_initialized = False
        # 多个模型：{model_name: {"llm": LLMClient, "adapter": BaseModelAdapter}}
        self.llm_clients: dict[str, Any] = {}
        self._current_model_name: str = ""
        # 待执行的技能合并操作（已废弃，保留字段防兼容问题）
        self._pending_consolidation: list | None = None
        # 实时消息回调：由 listener 注入，用于 /model all 等流式场景
        self.partial_sender: Callable[[str], None] | None = None
        # session 生命周期标识（JSONL 写入用）
        self.session_id: str = ""
        self._current_segment: int = 0
        # SessionManager 引用（用于 start_feishu_listener 传给 FeishuAdapter）
        self._session_manager: Any = None
        self._feishu_adapter: Any = None  # FeishuAdapter（若已启动长连接监听）
        # 上一次活动时间（秒时间戳），用于 idle 超时检测
        self.last_activity_at: float = 0.0
        # 渠道标识（cli/feishu 等）
        self.channel: str = "cli"
        # 每条消息的上下文元数据（由 gateway 层注入）
        self._current_message_id: str = ""
        self._current_chat_id: str = ""

        # ── 中断抢占机制（飞书等并发渠道） ──
        # 消息队列：新消息到来时如果正在处理，入队而非并发执行
        self._input_queue: queue.Queue[str] = queue.Queue()
        # 是否有任务正在处理
        self._processing: bool = False
        # 处理锁：保证同一时刻只有一个线程在处理消息
        self._processing_lock: threading.Lock = threading.Lock()
        # 持久回复回调（飞书渠道由 listener 设置，用于处理循环中发送回复）
        self._reply_callback: Callable[[str], None] | None = None

    # ── 消息上下文 ──

    def set_message_context(self, message_id: str = "", chat_id: str = "") -> None:
        """由 gateway 层（listener）在 handle_input 前调用，注入当条消息的元数据。"""
        self._current_message_id = message_id
        self._current_chat_id = chat_id

    def set_reply_channel(self, platform: str, chat_id: str,
                          thread_id: str | None = None) -> None:
        """注入回复渠道，PlatformManager 在 dispatch 时调用。"""
        from src.platforms.manager import PlatformManager
        mgr = PlatformManager.instance()
        self._reply_adapter = mgr._adapters.get(platform)
        self._reply_chat_id = chat_id
        self._reply_thread_id = thread_id
        # 反思沉淀通知：用同一渠道回传
        if hasattr(self, 'agent') and self.agent:
            self.agent.reflect_notify_callback = self._send_reply_via_channel

    def _send_reply_via_channel(self, text: str) -> None:
        """通过主事件循环安全发送回复（避免 asyncio.run 嵌套）。"""
        if not getattr(self, "_reply_adapter", None):
            return
        from src.platforms.manager import PlatformManager
        mgr = PlatformManager.instance()
        if mgr._loop is None:
            return
        coro = self._reply_adapter.send(
            self._reply_chat_id, text, getattr(self, "_reply_thread_id", None)
        )
        mgr.schedule_async(coro)

    def _snapshot_context(self, max_turns: int = 6) -> "ContextSnapshot":
        """提取当前 session 上下文快照，供后台任务继承。"""
        from src.platforms.background import ContextSnapshot
        messages = self.agent.llm.messages

        recent = []
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role in ("user", "assistant") and len(recent) < max_turns * 2:
                recent.insert(0, {"role": role, "content": msg.get("content", "")[:500]})

        system = ""
        if messages and messages[0].get("role") == "system":
            system = messages[0].get("content", "")

        return ContextSnapshot(
            recent_messages=recent,
            system_prompt=system,
            session_id=self.session_id,
            channel=self.channel,
            chat_id=self._current_chat_id,
        )

    def _handle_background(self, prompt: str) -> HandleResult:
        """处理 /background 命令：启动后台任务。"""
        from src.platforms.manager import PlatformManager
        snapshot = self._snapshot_context()
        mgr = PlatformManager.instance()
        task_id = mgr._background_mgr.start(
            prompt=prompt,
            platform=self.channel,
            chat_id=self._current_chat_id,
            thread_id=None,
            snapshot=snapshot,
        )
        preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
        reply = (
            f"🔄 后台任务已启动\n"
            f"Task ID: {task_id}\n"
            f'"{preview}"\n\n'
            f"完成后会自动通知，继续聊天即可。"
        )
        return HandleResult(reply=reply, is_command=True)

    def _handle_tasks(self) -> HandleResult:
        """处理 /tasks 命令：列出运行中的后台任务。"""
        from src.platforms.manager import PlatformManager
        mgr = PlatformManager.instance()
        tasks = mgr._background_mgr.list()
        if not tasks:
            return HandleResult(reply="当前没有运行中的后台任务。", is_command=True)
        lines = ["运行中的后台任务：\n"]
        for t in tasks:
            lines.append(f"  - [{t['status']}] {t['task_id']}: {t['prompt']}")
        return HandleResult(reply="\n".join(lines), is_command=True)

    def _handle_cancel(self, parts: list[str]) -> HandleResult:
        """处理 /cancel 命令：取消后台任务。"""
        if len(parts) < 2:
            return HandleResult(reply="用法: /cancel <task_id>", is_command=True)
        from src.platforms.manager import PlatformManager
        mgr = PlatformManager.instance()
        ok = mgr._background_mgr.cancel(parts[1])
        if ok:
            return HandleResult(reply=f"已取消任务 {parts[1]}", is_command=True)
        return HandleResult(reply=f"取消失败：任务 {parts[1]} 不存在或已完成", is_command=True)

    # ── 工厂方法 ──

    @classmethod
    def from_config(cls, config: dict[str, Any], channel: str = "cli") -> Session:
        """从配置字典创建完整初始化的 Session。

        包含：安装默认技能 → 加载记忆/技能 → 创建 LLM → 创建 Agent → 初始化飞书。
        """
        _install_default_skills()
        _install_default_memory()

        skills = skills_mgr.load_all_skills()

        # 构建多模型客户端字典
        llm_clients: dict[str, Any] = {}

        # 先加入 config.llm（当前激活模型）
        primary_llm, primary_adapter = _create_llm(config, channel=channel)
        primary_name = config["llm"]["model"]
        primary_cw = resolve_context_window(
            config["llm"]["model"],
            explicit=config["llm"].get("context_window"),
        )
        llm_clients[primary_name] = {
            "llm": primary_llm,
            "adapter": primary_adapter,
            "context_window": primary_cw,
        }

        # 加入 config.models 列表中的模型（跳过已添加的主模型）
        primary_api_key = config["llm"].get("api_key", "")
        primary_base_url = config["llm"].get("base_url", "")
        for model_cfg in config.get("models", []):
            name = model_cfg["name"]
            if name not in llm_clients:
                llm_i, adapter_i = _create_llm_from_model_config(
                    model_cfg,
                    fallback_api_key=primary_api_key,
                    fallback_base_url=primary_base_url,
                    channel=channel,
                )
                llm_clients[name] = {
                    "llm": llm_i,
                    "adapter": adapter_i,
                    "context_window": resolve_context_window(
                        name, explicit=model_cfg.get("context_window")
                    ),
                }

        # 构建 fallback_models（排除主模型，保留 llm_clients 中的顺序）
        fallback_models: list[tuple[LLMClient, BaseModelAdapter]] = []
        for name, cw in llm_clients.items():
            if name != primary_name:
                fallback_models.append((cw["llm"], cw["adapter"]))

        compaction_cfg = _build_compaction_config(config, model_context_window=primary_cw)
        max_tool_rounds = config.get("max_tool_rounds")
        agent = Agent(
            primary_llm,
            adapter=primary_adapter,
            compaction_config=compaction_cfg,
            max_tool_rounds=max_tool_rounds,
            fallback_models=fallback_models,
        )
        agent.set_context()
        agent.skills = skills

        retrieval = get_retrieval_config(config)
        embedding_cfg = get_embedding_config(config)
        skills_path = Path(str(config.get("skills_path", str(SKILLS_DIR)))).expanduser()
        projects_path = Path(
            str(config.get("projects_path", str(PROJECTS_DIR)))
        ).expanduser()
        projects_path.mkdir(parents=True, exist_ok=True)
        sm_raw = config.get("skills_management")
        sidx = SkillIndex(
            skills_path,
            INDEX_DIR,
            embedding_config=embedding_cfg,
            skills_management=sm_raw if isinstance(sm_raw, dict) else None,
        )
        sidx.load_or_build()
        pidx = ProjectIndex(projects_path, INDEX_DIR, embedding_config=embedding_cfg)
        pidx.load_or_build()

        session = cls(agent=agent, config=config, skills=skills)
        session._current_segment = 0
        session.skill_index = sidx
        session.project_index = pidx
        session.retrieval_config = retrieval
        session.llm_clients = llm_clients
        session._current_model_name = primary_name
        agent.skill_index = sidx
        agent.project_index = pidx
        agent.retrieval_config = retrieval
        skills_tools_reg.set_retrieval_indices(sidx, pidx)
        # 注入 LLM Client 和 SkillIndex 给 reflection
        from src.core import reflection
        reflection.set_llm_client(primary_llm)
        reflection.set_fallback_llms(fallback_models)
        reflection.set_skill_index(sidx)
        # 注入 Session 引用给 session_load 工具
        from src.tools import session as session_tool
        session_tool.set_current_session(session)
        session.channel = channel
        session.init_feishu()
        return session

    # ── 核心入口 ──

    def handle_input(self, user_input: str) -> HandleResult:
        """处理一条用户输入，返回 HandleResult。

        飞书等并发渠道：如果当前正在处理，将新消息入队并请求中断当前任务。
        CLI 等单线程渠道：直接处理，和原来一样。
        """
        import time as _time_module

        self.last_activity_at = _time_module.time()
        # 记录用户最后活跃日期，归档逻辑依赖此日期而非 date.today()
        from src.core.self_audit import touch_last_active_date
        touch_last_active_date()
        if user_input.startswith("/"):
            result = self._handle_command(user_input)
            if result is not None:
                return result
            # 未匹配已知命令，当普通文本处理

        # CLI 单线程：直接处理，不需要队列
        if self.channel != "feishu":
            return self._run_single(user_input)

        # 飞书并发渠道：队列 + 中断机制
        if self._processing:
            # 当前正在处理 → 入队 + 请求中断
            self._input_queue.put(user_input)
            self.agent.request_interrupt()
            logger.info("[session] 消息已入队，已请求中断当前任务")
            return HandleResult(reply="", compaction_msg="")

        # 没有在处理 → 获取锁，进入处理循环
        acquired = self._processing_lock.acquire(blocking=False)
        if not acquired:
            # 极端竞态：锁被其他线程拿了，入队
            self._input_queue.put(user_input)
            self.agent.request_interrupt()
            return HandleResult(reply="", compaction_msg="")

        self._processing = True
        try:
            return self._process_with_interrupt(user_input)
        finally:
            self._processing = False
            self._processing_lock.release()

    def _run_single(self, user_input: str) -> HandleResult:
        """CLI 单线程模式：直接处理一条消息（原始逻辑）。"""
        # 写入 user 消息到 JSONL
        if self.session_id:
            session_store.append_message(
                session_id=self.session_id,
                role="user",
                content=user_input,
                segment=self._current_segment,
            )

        user_input_for_llm = user_input

        # 启动指标收集
        collector = TaskCollector()
        collector.start(
            model=self._current_model_name,
            channel=self.channel,
            session_id=self.session_id or "",
            input_preview=user_input,
        )
        self.agent.metrics_collector = collector
        # 设置渠道信息，供后台反思使用
        self.agent.channel = self.channel
        self.agent.partial_sender = self.partial_sender

        try:
            reply = self.agent.run(user_input_for_llm)
        except Exception as e:
            collector.finish(success=False)
            self.agent.metrics_collector = None
            return HandleResult(reply=f"[错误] {e}")

        if self.session_id:
            self._write_assistant_to_jsonl()

        compaction_msg = ""
        try:
            cr = self.agent.maybe_compact(
                session_store=session_store,
                session_id=self.session_id or "",
                progress_callback=self.partial_sender,
            )
            if cr is not None:
                if cr.success:
                    compaction_msg = f"[上下文压缩] 已完成，归档 {cr.archived_count} 条内容，{cr.tokens_before} → {cr.tokens_after} token。"
                    self._current_segment += 1
                    collector.record_compaction()
                else:
                    compaction_msg = f"[上下文压缩] 失败: {cr.error}"
        except Exception:
            pass

        collector.finish(success=True)
        self.agent.metrics_collector = None
        return HandleResult(reply=reply, compaction_msg=compaction_msg)

    def _process_with_interrupt(self, user_input: str) -> HandleResult:
        """飞书并发渠道：处理消息 + 中断合并循环。

        在一个线程中串行处理消息，支持被新消息中断后合并重新规划。
        循环逻辑：
        1. 正常处理当前消息 → 成功 → 检查队列 → 继续
        2. 被中断 → 取新消息 → 合并 A 进度 + B 新消息 → 重新规划
        """
        from src.core.interrupt import AgentInterrupted

        current_input = user_input

        while True:
            # 清除中断状态
            self.agent.clear_interrupt_state()

            # 构造 LLM 输入（飞书渠道注入元数据）
            user_input_for_llm = current_input
            if self.channel == "feishu" and self._current_message_id:
                meta = ("[feishu_context message_id=" + self._current_message_id
                        + " chat_id=" + (self._current_chat_id or "") + "]")
                user_input_for_llm = meta + "\n" + current_input

            # 写入 user 消息到 JSONL
            if self.session_id:
                session_store.append_message(
                    session_id=self.session_id,
                    role="user",
                    content=current_input,
                    segment=self._current_segment,
                )

            # 启动指标收集
            collector = TaskCollector()
            collector.start(
                model=self._current_model_name,
                channel=self.channel,
                session_id=self.session_id or "",
                input_preview=current_input,
            )
            self.agent.metrics_collector = collector

            # 调用 agent
            try:
                reply = self.agent.run(user_input_for_llm)
            except AgentInterrupted as e:
                collector.finish(success=False)
                self.agent.metrics_collector = None

                # 补全未闭合的 tool_call 序列
                self.agent._sanitize_tool_messages()

                # 从队列取新消息
                try:
                    new_input = self._input_queue.get_nowait()
                except Exception:
                    return HandleResult(reply="[任务被中断]", compaction_msg="")

                # 立刻给用户发确认信号：新消息已收到，正在处理
                if self._reply_callback:
                    try:
                        self._reply_callback("收到新消息，正在处理")
                    except Exception:
                        pass

                # 确保进度回调可用：中断后 _process_with_interrupt 循环继续，
                # 但 listener 的 progress_callback 可能已被清空。
                # 用 interim_sender 作为 fallback 进度通道。
                if not self.agent.progress_callback and not self.agent.interim_sender:
                    if self.partial_sender:
                        self.agent.interim_sender = self.partial_sender

                # 检查队列里是否还有更多消息（连续发多条的场景）
                pending = []
                while not self._input_queue.empty():
                    try:
                        pending.append(self._input_queue.get_nowait())
                    except Exception:
                        break

                # 构建合并输入：A进度 + B新消息，一次重新规划
                merged = (
                    "[任务被新消息中断，以下是已完成进度]\n"
                    + e.progress_summary
                    + "\n\n--- 用户发来新消息 ---\n"
                    + new_input
                )
                if pending:
                    merged += "\n\n--- 还有后续消息 ---\n" + "\n".join(pending)

                merged += (
                    "\n\n请根据上述进度和新消息，重新规划并执行。"
                    "如果新消息和原任务相关，合并处理；"
                    "如果新消息是全新的独立请求，优先处理新消息。"
                )

                current_input = merged
                continue  # 带着合并输入进入下一轮循环

            except Exception as e:
                collector.finish(success=False)
                self.agent.metrics_collector = None
                return HandleResult(reply=f"[错误] {e}")

            # 处理成功
            if self.session_id:
                self._write_assistant_to_jsonl()

            # 压缩
            compaction_msg = ""
            try:
                cr = self.agent.maybe_compact(
                    session_store=session_store,
                    session_id=self.session_id or "",
                    progress_callback=self.partial_sender,
                )
                if cr is not None:
                    if cr.success:
                        compaction_msg = f"[上下文压缩] 已完成，归档 {cr.archived_count} 条内容，{cr.tokens_before} → {cr.tokens_after} token。"
                        self._current_segment += 1
                        collector.record_compaction()
                    else:
                        compaction_msg = f"[上下文压缩] 失败: {cr.error}"
            except Exception:
                pass

            collector.finish(success=True)
            self.agent.metrics_collector = None

            # 检查队列中是否有新消息
            if not self._input_queue.empty():
                if reply and self._reply_callback:
                    try:
                        self._reply_callback(reply)
                    except Exception:
                        pass
                try:
                    current_input = self._input_queue.get_nowait()
                    continue
                except Exception:
                    pass

            return HandleResult(reply=reply, compaction_msg=compaction_msg)

    # ── 生命周期 ──

    def init_feishu(self) -> bool:
        """初始化飞书客户端。"""
        feishu_cfg = self.config.get("feishu", {})
        app_id = feishu_cfg.get("app_id", "").strip()
        app_secret = feishu_cfg.get("app_secret", "").strip()
        if not app_id or not app_secret:
            self._feishu_initialized = False
            return False
        try:
            from src.feishu import client as feishu_client
            feishu_client.init_client(app_id=app_id, app_secret=app_secret)
            self._feishu_initialized = True
            return True
        except Exception:
            self._feishu_initialized = False
            return False

    @property
    def feishu_ready(self) -> bool:
        return self._feishu_initialized

    def cleanup(self) -> None:
        """结束 session（退出时调用）。"""
        # 写入 session_end
        if self.session_id:
            try:
                session_store.end_session(self.session_id)
            except Exception:
                pass

        # MEMORY.md 更新检查（累计 archive > 5 或距上次更新 > 24h）（累计 archive > 5 或距上次更新 > 24h）
        try:
            self._maybe_update_memory_md()
        except Exception:
            pass

    def _write_assistant_to_jsonl(self) -> None:
        """将 agent.llm.messages 中的最后一条 assistant 消息写入 JSONL。

        扩展字段（model, input_tokens, output_tokens, stop_reason）写入 trace 行，
        用于完整复现。
        """
        msgs = self.agent.llm.messages
        if not msgs:
            return
        # 找最后一条 role=assistant 的消息
        assistant_msg = None
        for msg in reversed(msgs):
            if msg.get("role") == "assistant":
                assistant_msg = msg
                break
        if not assistant_msg:
            return

        content = assistant_msg.get("content", "")
        tool_calls = assistant_msg.get("tool_calls")
        referenced_ids = _infer_referenced_tool_call_ids(msgs, assistant_msg)

        # 扩展字段（从 agent 获取）
        model = getattr(self.agent.llm, "model", None) or self._current_model_name
        total_tokens = getattr(self.agent, "last_total_tokens", 0)
        # 粗略拆分 input/output（total = input + output，比例约 1:3）
        input_tokens = total_tokens // 4
        output_tokens = total_tokens - input_tokens
        stop_reason = getattr(self.agent, "last_stop_reason", None) or "stop"

        try:
            session_store.append_message(
                session_id=self.session_id,
                role="assistant",
                content=content or "",
                tool_calls=tool_calls,
                referenced_tool_results=referenced_ids if referenced_ids else None,
                segment=self._current_segment,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason=stop_reason,
            )
        except Exception:
            pass

    def _reload_skill_index(self) -> None:
        """重建 skills 语义索引（合并后调用）。"""
        if self.skill_index is not None:
            self.skill_index.load_or_build()
            self.agent.skill_index = self.skill_index
            skills_tools_reg.set_retrieval_indices(self.skill_index, self.project_index)

    def refresh_indices(self) -> None:
        """skills/projects/info 目录文件变更后重建索引并刷新 system prompt。"""
        if self.skill_index is not None:
            try:
                self.skill_index.load_or_build()
                self.agent.skill_index = self.skill_index
            except Exception:
                pass
        if self.project_index is not None:
            try:
                self.project_index.load_or_build()
                self.agent.project_index = self.project_index
            except Exception:
                pass
        skills_tools_reg.set_retrieval_indices(self.skill_index, self.project_index)
        self._refresh_system_prompt()

    def _refresh_system_prompt(self) -> None:
        """skills/projects 变更后刷新 system prompt，让后续轮次感知。"""
        try:
            self.agent.llm.refresh_system_prompt()
        except Exception:
            pass

    def load_session(self, session_id: str = "", limit: int | None = None,
                     inject: bool = False) -> str:
        """加载指定或最近 session 的对话历史。

        默认只返回摘要文本（不注入 llm.messages），避免撑爆 context。
        inject=True 时完整注入（仅 /resume 命令使用）。

        Args:
            session_id: 要加载的 session ID。为空则加载最近的。
            limit: 已废弃，保留参数兼容性，不使用。
            inject: True 时完整注入 llm.messages，False 只返回摘要。

        Returns:
            摘要或完整对话内容。
        """
        if not session_id:
            sessions = session_store.list_recent_sessions(limit=1, ended_only=True)
            if not sessions:
                return "没有找到历史 session。"
            session_id = sessions[0]["session_id"]

        # 找最后一个 compaction 点
        boundary = session_store.get_latest_segment_boundary(session_id)
        from_segment = None
        segment_info = ""
        if boundary is not None:
            from_segment = boundary.get("segment", 0) + 1
            segment_info = f"，从 segment {from_segment} 开始（之前已 compaction 归档）"

        # 加载消息
        messages = session_store.get_session_messages(session_id, from_segment=from_segment)
        if not messages:
            return f"Session {session_id} 没有消息记录。"

        # 预处理——按类型收集有意义的消息行
        ordered: list[dict[str, Any]] = []
        for msg in messages:
            msg_type = msg.get("type", "")
            role = msg.get("role", "")
            if msg_type == "user" or role == "user":
                ordered.append({"kind": "user", "data": msg})
            elif msg_type == "assistant" or role == "assistant":
                ordered.append({"kind": "assistant", "data": msg})
            elif msg_type == "tool_call":
                ordered.append({"kind": "tool_call", "data": msg})
            elif msg_type == "tool_result":
                ordered.append({"kind": "tool_result", "data": msg})

        if not ordered:
            return f"Session {session_id} 没有可加载的对话消息。"

        # ── 摘要模式（默认）：只构建可读摘要，不注入 messages ──
        if not inject:
            return self._build_session_summary(session_id, ordered, segment_info)

        # ── 完整注入模式（仅 /resume 使用）──
        return self._inject_session_full(session_id, ordered, segment_info)

    # ── 摘要限制常量 ──
    _SUMMARY_MAX_CHARS = 4000     # 摘要总字符上限
    _SUMMARY_MSG_PREVIEW = 150    # 每条消息预览字符数
    _SUMMARY_MAX_MSGS = 50        # 最多展示的消息条数

    def _build_session_summary(
        self, session_id: str, ordered: list[dict], segment_info: str,
    ) -> str:
        """构建 session 摘要（不注入 messages，只返回可读文本）。"""
        content_lines: list[str] = []
        msg_count = 0
        total_chars = 0

        for item in ordered:
            if msg_count >= self._SUMMARY_MAX_MSGS:
                content_lines.append(f"...（共 {len(ordered)} 条，已截断展示前 {msg_count} 条）")
                break

            line = self._summarize_item(item)
            if not line:
                continue

            msg_count += 1
            # 检查总字符是否超限
            if total_chars + len(line) > self._SUMMARY_MAX_CHARS:
                content_lines.append(f"...（摘要已达 {self._SUMMARY_MAX_CHARS} 字符上限，剩余内容省略）")
                break

            content_lines.append(line)
            total_chars += len(line)

        summary = "\n".join(content_lines)
        return (
            f"[session {session_id} 摘要{segment_info}，共 {len(ordered)} 条消息]\n\n"
            f"{summary}"
        )

    def _summarize_item(self, item: dict) -> str:
        """将一条消息转为摘要行（截断到 _SUMMARY_MSG_PREVIEW 字符）。"""
        kind = item["kind"]
        preview = self._SUMMARY_MSG_PREVIEW

        if kind == "user":
            content = self._extract_text(item["data"].get("content", ""))
            return f"[用户] {content[:preview]}"

        elif kind == "assistant":
            content = self._extract_text(item["data"].get("content", ""))
            if content:
                return f"[Lamix] {content[:preview]}"
            return ""

        elif kind == "tool_call":
            tc_name = item["data"].get("name", "?")
            tc_args = item["data"].get("arguments", {})
            args_str = tc_args if isinstance(tc_args, str) else json.dumps(tc_args, ensure_ascii=False)
            return f"  [tool: {tc_name}({args_str[:60]})]"

        elif kind == "tool_result":
            tr_content = item["data"].get("result_inline") or ""
            return f"  [result: {str(tr_content)[:preview]}]"

        return ""

    @staticmethod
    def _extract_text(content: str | list) -> str:
        """从 content 字段提取纯文本。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return str(content)

    def _inject_session_full(
        self, session_id: str, ordered: list[dict], segment_info: str,
    ) -> str:
        """完整注入 session 到 llm.messages（仅 /resume 使用）。"""
        llm = self.agent.llm
        inject_msgs: list[dict] = []
        content_lines: list[str] = []

        i = 0
        while i < len(ordered):
            item = ordered[i]

            if item["kind"] == "user":
                content = item["data"].get("content", "")
                inject_msgs.append({"role": "user", "content": content})
                content_lines.append(f"[用户] {self._extract_text(content)}")
                i += 1

            elif item["kind"] == "tool_call":
                tool_calls_group: list[dict[str, Any]] = []
                tc_ids: list[str] = []
                tc_names_list: list[str] = []
                while i < len(ordered) and ordered[i]["kind"] == "tool_call":
                    tc_data = ordered[i]["data"]
                    tc_id = tc_data.get("id", "")
                    tc_name = tc_data.get("name", "")
                    tc_args = tc_data.get("arguments", {})
                    args_str = tc_args if isinstance(tc_args, str) else json.dumps(tc_args, ensure_ascii=False)
                    tool_calls_group.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": tc_name, "arguments": args_str},
                    })
                    tc_ids.append(tc_id)
                    tc_names_list.append(tc_name)
                    i += 1

                inject_msgs.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": tool_calls_group,
                })
                content_lines.append(f"  [Lamix tool_calls: {', '.join(tc_names_list)}]")

                result_ids_seen: set[str] = set()
                while i < len(ordered) and ordered[i]["kind"] == "tool_result":
                    tr_data = ordered[i]["data"]
                    tr_id = tr_data.get("id", "")
                    if tr_id in tc_ids:
                        tr_content = tr_data.get("result_inline") or ""
                        inject_msgs.append({
                            "role": "tool",
                            "tool_call_id": tr_id,
                            "content": tr_content,
                        })
                        result_ids_seen.add(tr_id)
                    i += 1

                for missing_id in tc_ids:
                    if missing_id not in result_ids_seen:
                        inject_msgs.append({
                            "role": "tool",
                            "tool_call_id": missing_id,
                            "content": "[工具结果缺失]",
                        })

            elif item["kind"] == "assistant":
                content = item["data"].get("content", "")
                entry = {"role": "assistant", "content": content}
                if item["data"].get("tool_calls"):
                    entry["tool_calls"] = item["data"]["tool_calls"]
                inject_msgs.append(entry)
                content_lines.append(f"[Lamix] {self._extract_text(content)}")
                i += 1

            elif item["kind"] == "tool_result":
                tr_data = item["data"]
                tr_content = tr_data.get("result_inline") or ""
                inject_msgs.append({
                    "role": "tool",
                    "tool_call_id": tr_data.get("id", ""),
                    "content": tr_content,
                })
                i += 1

            else:
                i += 1

        if not inject_msgs:
            return f"Session {session_id} 没有可加载的对话消息。"

        # 在 system prompt 之后插入
        if llm.messages and llm.messages[0].get("role") == "system":
            llm.messages[1:1] = inject_msgs
        else:
            llm.messages[:0] = inject_msgs
        self.agent.last_prompt_tokens = 0

        content_display = "\n".join(content_lines)
        result_text = (
            f"已加载 session {session_id} 的对话历史"
            f"（共 {len(inject_msgs)} 条{segment_info}）\n\n"
            f"{content_display}"
        )

        # 通知用户
        try:
            display_content = content_display
            if len(content_display) > 140:
                display_content = "..." + content_display[-140:]
            summary = (
                f"已加载 session {session_id} 的对话历史"
                f"（共 {len(inject_msgs)} 条{segment_info}）\n\n"
                f"{display_content}"
            )
            sender = self.partial_sender or getattr(self.agent, "interim_sender", None)
            if sender:
                sender(summary)
        except Exception:
            pass

        return result_text

    def start_feishu_listener(self, safe_mode_callback=None, shutdown_callback=None) -> None:
        """启动飞书长连接监听（daemon thread，不阻塞 REPL）。"""
        feishu_cfg = self.config.get("feishu", {})
        app_id = feishu_cfg.get("app_id", "").strip()
        app_secret = feishu_cfg.get("app_secret", "").strip()

        if not app_id or not app_secret:
            raise RuntimeError("飞书未配置，请在 config.yaml 中填写 feishu.app_id 和 feishu.app_secret")

        # 飞书 skills 懒加载：首次启动时自动安装
        from src.core.config import _install_feishu_skills
        _install_feishu_skills()

        from src.platforms.adapters.feishu import FeishuAdapter

        # 通过 SessionManager 启动，确保 feishu 消息路由到正确的 Session
        mgr = self._session_manager
        if mgr is None:
            raise RuntimeError("Session 未绑定到 SessionManager，无法启动飞书监听")

        adapter = FeishuAdapter(
            app_id=app_id,
            app_secret=app_secret,
            session_manager=mgr,
            safe_mode_callback=safe_mode_callback,
            shutdown_callback=shutdown_callback,
        )
        self._feishu_adapter = adapter
        adapter.start()  # WebSocket 在后台线程运行，详见 FeishuListener.shutdown

    # ── 命令路由 ──

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

        # 未知命令：返回 None，让调用方当普通文本处理
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

        # token 估算
        estimated = self.agent._estimate_context_tokens()
        source = "LLM返回值" if self.agent.last_prompt_tokens > 0 else "bytes/4估算"

        # context_window
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
            # 触发阈值
            threshold = int(cw * self.agent._compaction_config.trigger_threshold)
            lines.append(f"压缩触发阈值: {threshold:,} tokens（{self.agent._compaction_config.trigger_threshold:.0%}）")
        lines.append(f"消息数: {total_msgs} 条（user: {user_msgs}, assistant: {assistant_msgs}, tool: {tool_msgs}）")

        return "\n".join(lines)

    def _handle_self_audit(self) -> HandleResult:
        """手动触发自我审计。"""
        try:
            from src.core.self_audit import run_audit, format_report_detail, save_report
            report = run_audit()
            save_report(report)  # 持久化到磁盘
            detail = format_report_detail(report)
            return HandleResult(reply=detail, is_command=True)
        except Exception as e:
            return HandleResult(reply=f"[自我审计] 失败: {e}", is_command=True)

    def _handle_audit_report(self, parts: list[str]) -> str:
        """处理 /audit-report 命令：列出历史报告或查看指定报告详情。"""
        from src.core.self_audit import list_reports, load_report, format_report_detail

        if len(parts) >= 2:
            # /audit-report <path> → 查看指定报告
            report_path = parts[1]
            report = load_report(report_path)
            if report is None:
                return f"[错误] 无法加载报告: {report_path}"
            return format_report_detail(report)

        # /audit-report → 列出最近报告
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
            # 重新加载技能
            self.skills.clear()
            self.skills.update(skills_mgr.load_all_skills())
            self._refresh_system_prompt()
            return result

        if sub == "consolidate":
            # 获取当前模型对应的 llm client
            bundle = self.llm_clients.get(self._current_model_name)
            if not bundle:
                return "[错误] 当前模型无可用的 LLM Client"
            llm_client = bundle.get("llm")
            if not llm_client:
                return "[错误] 无法获取 LLM Client"
            # 分析并直接执行合并
            actions, analysis = skills_mgr.consolidate_skills(self.skills, llm_client)
            if not actions:
                if analysis.startswith("[错误]"):
                    return analysis
                return f"分析结果：\n{analysis}\n\n无需合并。"

            # 直接执行
            result = skills_mgr.execute_consolidation(actions)
            # 重新加载 skills 和 index
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
            # /model → 显示当前模型和可用模型列表
            lines = [f"当前模型：{self._current_model_name}", "可用模型："]
            for name in self.llm_clients:
                marker = " ← 当前" if name == self._current_model_name else ""
                lines.append(f"  - {name}{marker}")
            return "\n".join(lines)

        if parts[1].lower() == "all":
            # /model all <question> → 并行查询所有模型（带工具调用）
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

            # 所有消息已通过 partial_sender 实时发出，不再返回汇总
            return ""

        # /model <name> → 切换模型（方案B：迁移对话历史到新 client）
        target_name = parts[1]
        if target_name not in self.llm_clients:
            available = ", ".join(sorted(self.llm_clients.keys()))
            return f"未知模型：{target_name}，可用模型：{available}"

        bundle = self.llm_clients[target_name]
        new_llm = bundle["llm"]
        new_adapter: BaseModelAdapter = bundle["adapter"]
        model_cw = bundle.get("context_window")
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
            # /resume <id> → 完整加载指定 session（注入 messages）
            session_id = parts[1]
            return self.load_session(session_id=session_id, inject=True)

        # /resume → 列出最近 5 个 session
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
        import os

        # 读取 compaction_log 统计 archive 次数
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

        # 读取 MEMORY.md 最后修改时间
        memory_path = LAMIX_DIR / "MEMORY.md"
        hours_since_update = float("inf")
        if memory_path.exists():
            mtime = memory_path.stat().st_mtime
            hours_since_update = (time.time() - mtime) / 3600

        if archive_count <= 5 and hours_since_update < 24:
            return

        # 需要更新：收集 skill/project 精华
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

        # LLM 抽取精华
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
                # 写前备份
                if memory_path.exists():
                    import shutil
                    shutil.copy2(memory_path, memory_path.with_suffix(".md.bak"))
                memory_path.write_text(result.strip(), encoding="utf-8")
                logger.info(f"[MEMORY.md] 已更新（archive={archive_count}, 距上次 {hours_since_update:.0f}h）")
        except Exception as e:
            logger.warning(f"core.md 更新失败: {e}")


# ── 模块级辅助函数（Session 内部使用，不暴露给 gateway） ──


def _install_default_skills() -> None:
    """将内置技能复制到用户目录（首次运行）。"""
    default_skills_dir = Path(__file__).resolve().parent.parent.parent / "config" / "default_skills"
    try:
        skills_mgr.install_default_skills(default_skills_dir)
    except Exception:
        pass


def _install_default_memory() -> None:
    """将默认 memory.md 和 user.md 复制到用户目录（首次运行）。"""
    lamix_dir = Path.home() / ".lamix"
    import shutil
    config_dir = Path(__file__).resolve().parent.parent.parent / "config"
    
    # 复制 MEMORY.md
    memory_path = lamix_dir / "MEMORY.md"
    default_memory = config_dir / "default_memory.md"
    if not memory_path.exists() and default_memory.exists():
        try:
            shutil.copy2(str(default_memory), str(memory_path))
        except Exception:
            pass
    
    # 复制 USER.md（如果不存在或内容为空/不完整）
    user_path = lamix_dir / "USER.md"
    default_user = config_dir / "default_user.md"
    needs_copy = (
        not user_path.exists()
        or user_path.stat().st_size < 50  # 不完整
    )
    if needs_copy and default_user.exists():
        try:
            shutil.copy2(str(default_user), str(user_path))
        except Exception:
            pass


def _create_llm(config: dict[str, Any], channel: str = "cli") -> tuple[LLMClient, BaseModelAdapter]:
    """从配置创建 LLMClient 与对应 Adapter（使用 config.llm 部分）。"""
    llm_cfg = config["llm"]
    llm = LLMClient(
        api_key=llm_cfg["api_key"],
        base_url=llm_cfg["base_url"],
        model=llm_cfg["model"],
        channel=channel,
    )
    return llm, create_adapter(llm)


def _create_llm_from_model_config(
    model_cfg: dict[str, Any],
    fallback_api_key: str = "",
    fallback_base_url: str = "",
    channel: str = "cli",
) -> tuple[LLMClient, BaseModelAdapter]:
    """从单个模型的配置字典创建 LLMClient 与 Adapter。"""
    api_key = model_cfg.get("api_key", "") or fallback_api_key
    base_url = model_cfg.get("base_url", "") or fallback_base_url
    if not base_url:
        raise ValueError(
            f"模型 {model_cfg.get('name', '?')} 缺少 base_url 配置"
        )
    llm = LLMClient(
        api_key=api_key,
        base_url=base_url,
        model=model_cfg["name"],
        channel=channel,
    )
    return llm, create_adapter(llm)


def _build_compaction_config(
    config: dict[str, Any],
    model_context_window: int | None = None,
) -> CompactionConfig:
    """从配置字典构建 CompactionConfig。

    Args:
        config: 顶层配置字典（含 compaction 段）。
        model_context_window: 模型自身的 context_window，优先于配置文件中的值。
    """
    c = config.get("compaction", {})
    # 优先使用模型实际的 context_window，而非配置文件中的固定值
    cw = model_context_window or c.get("context_window", 131_072)
    return CompactionConfig(
        context_window=int(cw),
        trigger_threshold=float(c.get("trigger_threshold", 0.9)),
        end_threshold_percent=c.get("end_threshold_percent", 80.0),
        compaction_log_max_bytes=c.get("compaction_log_max_bytes", 10 * 1024 * 1024),
        tail_ratio=float(c.get("tail_ratio", 0.2)),
        tail_threshold=float(c.get("tail_threshold", 0.5)),
    )
