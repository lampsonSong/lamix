"""Agent 主循环：接收用户输入，调用 LLM，处理 tool calling，返回最终回复。

需要技能/项目时由语义检索在规划或 Fast Path 中注入匹配全文，不预载 skill 目录。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from src.core.adapters import BaseModelAdapter
from src.core.adapters.base import (
    LLMError, LLMRetryableError, LLMRateLimitError, LLMFatalError, LLMContextTooLongError,
)
from src.core.interrupt import AgentInterrupted
from src.core.llm import LLMClient
from src.core import tools as tool_registry
from src.core.compaction import CompactionConfig, CompactionResult, apply_compaction
from src.planning.steps import Plan, PlanStatus
from src.memory import session_store as _session_store
from src.core.error_log import log_error as _log_error, SOURCE_LLM as _SRC_LLM, SOURCE_TOOL as _SRC_TOOL

if TYPE_CHECKING:
    from src.skills.manager import Skill

logger = logging.getLogger(__name__)


_DEFAULT_MAX_TOOL_ROUNDS = 30


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        adapter: BaseModelAdapter,
        compaction_config: CompactionConfig | None = None,
        max_tool_rounds: int | None = None,
        fallback_models: list[tuple[LLMClient, BaseModelAdapter]] | None = None,
    ) -> None:
        self.llm = llm
        self.adapter = adapter
        self._tools = tool_registry.get_all_schemas()
        self.skills: dict[str, "Skill"] = {}
        self.skill_index: Any = None
        self.project_index: Any = None
        self.retrieval_config: dict[str, Any] = {}
        self.last_total_tokens: int = 0
        self.last_prompt_tokens: int = 0
        self.last_stop_reason: str | None = None
        self._fast_path_tool_count: int = 0

        self._compaction_config: CompactionConfig | None = compaction_config
        self.max_tool_rounds: int = max_tool_rounds or _DEFAULT_MAX_TOOL_ROUNDS
        self.current_plan: Plan | None = None
        self.fallback_models: list[tuple[LLMClient, BaseModelAdapter]] = fallback_models or []
        # 压缩操作锁：防止飞书等多线程场景下并发执行压缩
        self._compaction_lock = threading.Lock()

        # ── 中断机制 ───────────────────────────────────────────────────
        # volatile 标志：true = 被新消息抢占，需停止
        self._interrupted: bool = False
        # 中断锁：防止 check_interrupt 并发写 progress_summary
        self._interrupt_lock = threading.Lock()
        # 中断时的进度摘要（供 Session 保存）
        self._interrupted_summary: str = ""

        # 中间过程回调：由 listener 注入，用于实时发送工具调用状态
        self.progress_callback: Callable[[str], None] | None = None
        # 中间文本回调：由 listener 注入，用于向用户发送阶段性总结等中间内容
        self.interim_sender: Callable[[str], None] | None = None
        # 工具调用计数器（跨多次 tool_loop 累计，用于判断是否应继续循环）
        self._total_tool_calls: int = 0
        # 连续 LLM 调用失败计数：连续3次失败后触发熔断
        self._consecutive_llm_failures: int = 0

        # ── fallback 缓存 ───────────────────────────────────────────────
        # 主模型失败后 fallback 成功时，缓存该模型 10 分钟，避免每次重试主模型
        self._fallback_cache_model: str = ""        # 缓存的 fallback 模型名
        self._fallback_cache_adapter = None         # 缓存的 adapter
        self._fallback_cache_llm = None             # 缓存的 LLMClient
        self._fallback_cache_until: float = 0.0     # 缓存过期时间戳

        # ── trace / 完整复现 ───────────────────────────────────────────
        # session_id：由 Session 注入，用于写 trace 行到 JSONL
        self.session_id: str = ""

        # ── 反思水位线 ────────────────────────────────────────────────
        # 记录上次反思时 messages 的长度，下次只反思增量部分
        self._reflect_watermark: int = 0
        self.reflect_notify_callback: Callable[[str], None] | None = None
    def refresh_tools(self) -> None:
        """重新加载工具列表（外部注册新工具后调用）。"""
        self._tools = tool_registry.get_all_schemas()

    def _load_or_refresh_skills(self) -> None:
        """刷新 skill index，确保本次 session 能加载新建的 skill。反思完成后调用。"""
        if self.skill_index is not None:
            self.skill_index.load_or_build()

    def set_context(self) -> None:
        """设置 system prompt 上下文（启动时调用一次）。"""
        self.llm.set_system_context()

    def switch_llm(
        self,
        new_llm: LLMClient,
        new_adapter: BaseModelAdapter,
        compaction_config: CompactionConfig | None = None,
    ) -> None:
        """切换底层 LLM 与适配器，并迁移对话历史。

        Args:
            new_llm: 新的 LLM 客户端。
            new_adapter: 新的模型适配器。
            compaction_config: 新模型的压缩配置（不同模型 context_window 不同）。
        """
        old_llm = self.llm
        new_llm.set_system_context()
        new_llm.migrate_from(old_llm)
        self.llm = new_llm
        self.adapter = new_adapter
        if compaction_config is not None:
            self._compaction_config = compaction_config

    # ── 中断检查 ───────────────────────────────────────────────────────

    def request_interrupt(self) -> None:
        """由 Session 调用，请求中断当前任务（设置标志位）。"""
        with self._interrupt_lock:
            self._interrupted = True

    def check_interrupt(self) -> None:
        """在 tool_loop 的检查点调用；若被抢占则抛出 AgentInterrupted。"""
        if not self._interrupted:
            return
        with self._interrupt_lock:
            if not self._interrupted:
                return
            # 标记已处理，防止重复抛出
            self._interrupted = False

        # 构建中断摘要
        summary = self._build_interrupted_summary()
        with self._interrupt_lock:
            self._interrupted_summary = summary
        raise AgentInterrupted(progress_summary=summary)

    def _build_interrupted_summary(self) -> str:
        """从当前 messages 构建中断进度摘要。"""
        try:
            lines = ["[任务被中断，以下是已完成的进度]\n"]
            tool_calls_found: list[str] = []
            last_user_query = ""

            # 找最后一条 user 消息作为原始任务
            for msg in self.llm.messages:
                role = msg.get("role", "")
                if role == "user":
                    content = msg.get("content", "")
                    # 跳过飞书 context meta
                    if "[feishu_context" in content:
                        content = content.split("]", 1)[-1].strip()
                    if content and not content.startswith("[任务被中断"):
                        last_user_query = content[:300]

            if last_user_query:
                lines.append(f"**原任务**：{last_user_query}\n")

            # 收集 tool 调用
            for msg in self.llm.messages:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        fname = tc.get("function", {}).get("name", "")
                        args_str = tc.get("function", {}).get("arguments", "")
                        try:
                            args = json.loads(args_str)
                            # 过滤敏感/大字段
                            safe_args = {k: v for k, v in args.items()
                                         if k not in ("password", "token", "secret", "key")}
                            args_display = json.dumps(safe_args, ensure_ascii=False)[:150]
                        except Exception:
                            args_display = args_str[:150]
                        tool_calls_found.append(f"  - `{fname}`({args_display})")

            if tool_calls_found:
                lines.append(f"**已调用 {len(tool_calls_found)} 个工具**：")
                lines.extend(tool_calls_found[:10])

            plan = self.current_plan
            if plan is not None:
                lines.append(f"\n**当前计划**：{plan.description[:200]}")

            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"构建中断摘要失败: {e}")
            return "[任务被中断，进度摘要生成失败]"

    def clear_interrupt_state(self) -> None:
        """由 Session 在处理完一条消息后调用，重置中断标志。"""
        with self._interrupt_lock:
            self._interrupted = False
            self._interrupted_summary = ""

    # ── LLM 调用 ───────────────────────────────────────────────────────

    def _set_fallback_cache(self, llm: LLMClient, adapter: BaseModelAdapter, ttl_seconds: int = 600) -> None:
        """缓存成功的 fallback 模型，ttl_seconds 内优先使用。"""
        import time as _time
        self._fallback_cache_model = llm.model
        self._fallback_cache_llm = llm
        self._fallback_cache_adapter = adapter
        self._fallback_cache_until = _time.time() + ttl_seconds
        logger.info(f"已缓存 fallback 模型 {llm.model}，{ttl_seconds}s 内优先使用")

    def _clear_fallback_cache(self) -> None:
        """清除 fallback 缓存。"""
        self._fallback_cache_model = ""
        self._fallback_cache_llm = None
        self._fallback_cache_adapter = None
        self._fallback_cache_until = 0.0

    def _order_fallbacks(self, primary_base: str) -> list[tuple[LLMClient, BaseModelAdapter]]:
        """重排 fallback 模型：优先不同供应商，再排同供应商。

        当主模型因限流/超时失败时，同供应商的模型大概率也有问题，
        优先尝试不同 base_url 的 fallback 可以更快找到可用模型。
        """
        diff_vendor: list[tuple[LLMClient, BaseModelAdapter]] = []
        same_vendor: list[tuple[LLMClient, BaseModelAdapter]] = []
        for fb in self.fallback_models:
            if fb[0].base_url != primary_base:
                diff_vendor.append(fb)
            else:
                same_vendor.append(fb)
        return diff_vendor + same_vendor

    def _sanitize_tool_messages(self) -> None:
        """清理 messages 中所有不完整的 tool 调用序列。

        遍历整个 messages 列表，找出所有 assistant 消息的 tool_calls，
        验证每个 tool_call 都有对应 ID 的 tool_result。
        如果某个 tool_call 没有对应 result，补上错误占位 result。
        这确保发给任何 LLM 的 messages 都是完整干净的。
        """
        msgs = self.llm.messages
        if not msgs:
            return

        # 建立所有 tool_call_id 的集合
        tool_call_ids: set[str] = set()
        # 建立所有已有 tool_result 的 tool_call_id 集合
        result_ids: set[str] = set()

        for msg in msgs:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg.get("tool_calls", []):
                    tc_id = tc.get("id") or ""
                    if tc_id:
                        tool_call_ids.add(tc_id)
            elif msg.get("role") == "tool":
                result_ids.add(msg.get("tool_call_id") or "")

        # 找出缺失的 tool_call_id（有 call 无 result）
        missing_ids = tool_call_ids - result_ids
        # 找出孤立的 tool 消息（有 result 无对应的 assistant(tool_calls)）
        orphan_ids = result_ids - tool_call_ids

        if not missing_ids and not orphan_ids:
            logger.info(f"[_sanitize_tool_messages] 无需清理（tool_call_ids={tool_call_ids}, result_ids={result_ids}）")
            return

        # 为每个缺失的 ID 追加错误占位 result
        for tc_id in missing_ids:
            msgs.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": "[错误] 工具执行失败或被中断，结果未获取",
            })

        logger.info(f"[_sanitize_tool_messages] 补全了 {len(missing_ids)} 个缺失的 tool_result，IDs: {missing_ids}")

        # 删除孤立的 tool 消息（没有对应 assistant(tool_calls) 的）
        if orphan_ids:
            before = len(msgs)
            self.llm.messages = [m for m in msgs if not (m.get("role") == "tool" and (m.get("tool_call_id") or "") in orphan_ids)]
            removed = before - len(self.llm.messages)
            logger.info(f"[_sanitize_tool_messages] 删除了 {removed} 条孤立 tool 消息（无对应 assistant(tool_calls)），IDs: {orphan_ids}")

        logger.info(f"[_sanitize_tool_messages] 消息列表状态：")
        for i, m in enumerate(msgs):
            if m.get("role") in ("assistant", "tool", "user"):
                tc_ids = [tc.get("id") for tc in m.get("tool_calls", [])]
                logger.info(f"  [{i}] role={m.get('role')}, tool_calls={tc_ids}, tool_call_id={m.get('tool_call_id')}")

    def _chat_with_fallback(self, tools=None):
        """从主模型开始，失败时 fallback；fallback 成功后缓存 10 分钟。

        - 缓存有效期内直接用缓存的 fallback 模型，跳过主模型。
        - 缓存过期后恢复正常流程，先试主模型。
        - 不会永久切换模型——响应仍写回主模型的 messages。
        """
        # 防御性清理 messages：保证 tool_call 和 tool_result 完整匹配
        self._sanitize_tool_messages()

        # 检查中断（LLM 调用前）
        self.check_interrupt()

        # 0. 检查 fallback 缓存：有效期内直接用缓存的模型
        import time as _time
        if (
            self._fallback_cache_llm
            and _time.time() < self._fallback_cache_until
        ):
            cached_name = self._fallback_cache_model
            cached_llm = self._fallback_cache_llm
            cached_adapter = self._fallback_cache_adapter
            logger.info(f"fallback 缓存命中，直接使用 {cached_name}（剩余 {int(self._fallback_cache_until - _time.time())}s）")
            try:
                cached_llm.messages = list(self.llm.messages)
                _fb_start = _session_store._now_ms()
                result = cached_adapter.chat(cached_llm.messages, tools=tools, timeout=90)
                _fb_end = _session_store._now_ms()
                parsed = self.adapter.parse_response(result)
                input_tokens = getattr(result.usage, 'prompt_tokens', 0) if result.usage else 0
                output_tokens = getattr(result.usage, 'completion_tokens', 0) if result.usage else 0
                _session_store.write_llm_call_trace(
                    self.session_id,
                    model=cached_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=_fb_end - _fb_start,
                    stop_reason=parsed.finish_reason or "stop",
                )
                return result
            except LLMContextTooLongError:
                raise
            except (LLMFatalError, LLMRateLimitError, LLMRetryableError) as e:
                logger.warning(f"缓存模型 {cached_name} 也失败了，清除缓存，回退到正常流程: {e}")
                self._clear_fallback_cache()
                if self.session_id:
                    _session_store.write_llm_error_trace(
                        self.session_id,
                        model=cached_name,
                        error_type=type(e).__name__,
                        detail=str(e)[:500],
                        duration_ms=_session_store._now_ms() - _fb_start,
                    )
                # 继续走下面的正常流程

        # 1. 先试主模型（默认60s）
        _no_fallback = False
        _fatal_error: Exception | None = None
        # 写 system_prompt trace（hash 去重）
        if self.session_id and self.llm.messages and self.llm.messages[0].get("role") == "system":
            system_content = self.llm.messages[0].get("content", "")
            if system_content:
                _session_store.write_system_prompt_trace(self.session_id, system_content)

        # 记录 LLM 调用开始时间（用于计算 duration_ms）
        _call_start = _session_store._now_ms()
        _call_model = self.llm.model

        try:
            result = self.adapter.chat(self.llm.messages, tools=tools)
            # 写 llm_call trace（成功）
            _call_end = _session_store._now_ms()
            parsed = self.adapter.parse_response(result)
            input_tokens = getattr(result.usage, 'prompt_tokens', 0) if result.usage else 0
            output_tokens = getattr(result.usage, 'completion_tokens', 0) if result.usage else 0
            _session_store.write_llm_call_trace(
                self.session_id,
                model=_call_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=_call_end - _call_start,
                stop_reason=parsed.finish_reason or "stop",
            )
            return result
        except LLMContextTooLongError:
            raise
        except (LLMFatalError, LLMRateLimitError, LLMRetryableError) as e:
            # 写 llm_error trace + 错误日志
            if self.session_id:
                _session_store.write_llm_error_trace(
                    self.session_id,
                    model=self.llm.model,
                    error_type=type(e).__name__,
                    detail=str(e)[:500],
                    duration_ms=_session_store._now_ms() - _call_start,
                )
                _log_error(
                    type(e).__name__, str(e)[:500], _SRC_LLM,
                    session_id=self.session_id,
                    detail={'model': self.llm.model, 'duration_ms': _session_store._now_ms() - _call_start},
                    messages_snapshot=self.llm.messages,
                    exception=e,
                )
            if not self.fallback_models:
                raise
            # 如果是参数错误（400），说明 messages 有问题，不 fallback
            if isinstance(e, LLMFatalError) and "400" in str(e) and "tool_call" in str(e):
                logger.warning(f"主模型 {self.llm.model} 返回参数错误（疑似 messages 不完整），不 fallback: {e}")
                _no_fallback = True
                _fatal_error = e
            else:
                logger.warning(f"主模型 {self.llm.model} 调用失败（{type(e).__name__}），尝试 fallback: {e}")
                self._on_model_switch(f"主模型 {self.llm.model} 失败，切换 fallback...")

        # 如果是 400+tool_call 错误，直接上抛，不走 fallback
        if _no_fallback:
            raise _fatal_error or LLMFatalError("主模型参数错误")

        # 2. 按供应商分组重排 fallback：优先尝试不同供应商的模型
        primary_base = self.llm.base_url
        ordered = self._order_fallbacks(primary_base)

        # 3. 依次试 fallback，统一 90s 超时（复杂 prompt 需要更长推理时间）
        FALLBACK_TIMEOUT = 90
        for fb_llm, fb_adapter in ordered:
            logger.warning(f"尝试 fallback: {fb_llm.model} (timeout={FALLBACK_TIMEOUT}s)")
            self._on_model_switch(f"尝试 {fb_llm.model}...")
            try:
                fb_llm.messages = list(self.llm.messages)
                _fb_start = _session_store._now_ms()
                result = fb_adapter.chat(fb_llm.messages, tools=tools, timeout=FALLBACK_TIMEOUT)
                _fb_end = _session_store._now_ms()
                self._on_model_switch(f"已切换到 {fb_llm.model}")
                # 写 llm_call trace（fallback 成功）
                parsed = self.adapter.parse_response(result)
                input_tokens = getattr(result.usage, 'prompt_tokens', 0) if result.usage else 0
                output_tokens = getattr(result.usage, 'completion_tokens', 0) if result.usage else 0
                _session_store.write_llm_call_trace(
                    self.session_id,
                    model=fb_llm.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=_fb_end - _fb_start,
                    stop_reason=parsed.finish_reason or "stop",
                )
                # 缓存成功的 fallback 模型，10 分钟内直接用
                self._set_fallback_cache(fb_llm, fb_adapter, ttl_seconds=600)
                return result
            except LLMContextTooLongError:
                # prompt 超长换模型也没用，直接上抛
                raise
            except (LLMFatalError, LLMRateLimitError, LLMRetryableError) as e:
                logger.warning(f"fallback {fb_llm.model} 也失败（{type(e).__name__}）: {e}")
                # 写 llm_error trace + 错误日志
                if self.session_id:
                    _session_store.write_llm_error_trace(
                        self.session_id,
                        model=fb_llm.model,
                        error_type=type(e).__name__,
                        detail=str(e)[:500],
                        duration_ms=_session_store._now_ms() - _fb_start,
                    )
                    _log_error(
                        type(e).__name__, str(e)[:500], _SRC_LLM,
                        session_id=self.session_id,
                        detail={'model': fb_llm.model, 'duration_ms': _session_store._now_ms() - _fb_start, 'is_fallback': True},
                        messages_snapshot=self.llm.messages,
                        exception=e,
                    )
                continue

        raise LLMFatalError("所有模型（含 fallback）均调用失败")

    def _run_tool_loop(self) -> str:
        while True:
            for round_num in range(self.max_tool_rounds):
                try:
                    # 运行时兜底：过滤掉 schema 格式错误的工具，防止坏 schema 导致 LLM 400
                    valid_tools = [s for s in self._tools if not tool_registry.validate_tool_schema(s)]
                    response = self._chat_with_fallback(tools=valid_tools)
                    self._consecutive_llm_failures = 0  # 新增：成功则重置
                except AgentInterrupted:
                    raise  # 直接上抛，不吞掉
                except LLMContextTooLongError as e:
                    logger.warning(f"Prompt 超长: {e}")
                    # 自动压缩最多重试 3 次，每次压完直接试 LLM call，靠实际错误判断是否成功
                    # 不依赖 bytes/4 估算（context overflow 后 last_prompt_tokens=0，估算不准确）
                    compacted = False
                    msg_count_before = len(self.llm.messages)
                    for attempt in range(3):
                        if attempt > 0:
                            logger.info(f"自动压缩第 {attempt} 次重试...")
                        cr = self.force_compact(
                            session_store=_session_store,
                            session_id=self.session_id or "",
                            progress_callback=self.progress_callback,
                        )
                        if cr is None or not cr.success:
                            break
                        # 检查压缩是否有效：消息数必须减少 20% 以上
                        msg_count_after = len(self.llm.messages)
                        reduction = 1 - msg_count_after / max(msg_count_before, 1)
                        if reduction < 0.2 and attempt > 0:
                            logger.warning(
                                f"压缩无效（消息数 {msg_count_before} → {msg_count_after}，"
                                f"仅减少 {reduction:.0%}），停止重试"
                            )
                            compacted = True  # 让它试一次 LLM call
                            break
                        msg_count_before = msg_count_after
                        # 压完直接 continue，让外层循环重新 try LLM call
                        # 如果还超，LLMContextTooLongError 会再次被捕获，进入下一轮压缩
                        compacted = True
                        logger.info(f"自动压缩成功（第 {attempt + 1} 次），继续重试 LLM")
                        self._on_model_switch("已自动压缩上下文，继续执行")
                        break
                    if compacted:
                        continue  # 重新进入 for round_num，重新 try _chat_with_fallback
                    return "[上下文过长，自动压缩已达上限，请使用 /compaction 手动压缩后重试]"
                except LLMError as e:
                    self._consecutive_llm_failures += 1
                    if self._consecutive_llm_failures >= 3:
                        logger.error(f"连续 {self._consecutive_llm_failures} 次 LLM 调用失败，熔断退出")
                        return f"[LLM 错误] 连续多次调用失败，LLM 服务暂时不可用。请稍后再试。\n最后一次错误: {e}"
                    return f"[LLM 错误] {e}"

                # 检查中断（收到 LLM 响应后、解析 tool_calls 前）
                self.check_interrupt()

                if response.usage:
                    self.last_total_tokens = response.usage.total_tokens
                    self.last_prompt_tokens = getattr(response.usage, 'prompt_tokens', 0) or self.last_prompt_tokens

                self.llm.messages.append(
                    response.choices[0].message.model_dump(exclude_none=True)
                )

                parsed = self.adapter.parse_response(response)
                self.last_stop_reason = parsed.finish_reason

                # 中间旁白：LLM 在调用工具前说的文字（如让我先看看配置），发给用户
                if parsed.tool_calls and parsed.content and parsed.content.strip():
                    if self.interim_sender:
                        try:
                            self.interim_sender(parsed.content.strip())
                        except Exception:
                            pass

                if not parsed.tool_calls:
                    logger.info(f"tool_loop round {round_num+1}: finish (no tool_calls), content_len={len(parsed.content or '')}")
                    return parsed.content or ""

                for tc in parsed.tool_calls:
                    # 检查中断（每个工具调用执行前）
                    self.check_interrupt()

                    logger.info(f"tool_loop round {round_num+1}: dispatch {tc.name}({tc.raw_arguments[:200]})")

                    # 写 tool_call trace
                    if self.session_id:
                        try:
                            args_dict = json.loads(tc.raw_arguments) if isinstance(tc.raw_arguments, str) else tc.raw_arguments
                        except Exception:
                            args_dict = {"raw": tc.raw_arguments}
                        _session_store.write_tool_call_trace(
                            self.session_id,
                            tool_call_id=tc.id,
                            name=tc.name,
                            arguments=args_dict,
                        )

                    result = tool_registry.dispatch(tc.name, tc.raw_arguments)
                    self._fast_path_tool_count += 1

                    # 如果激活了新工具组，刷新工具列表使新工具可用
                    if tc.name == "activate_tool_group":
                        self._tools = tool_registry.get_all_schemas()

                    # 记录 tool call 供 skill 审计使用
                    try:
                        from src.core.skill_audit import record_tool_call
                        record_tool_call(tc.name, tc.raw_arguments[:200])
                    except Exception:
                        pass

                    # 写 tool_result trace + 错误日志
                    if self.session_id:
                        error_info = None
                        if result.startswith("[错误]") or result.startswith("[Exception"):
                            error_info = {"type": "ToolError", "message": result[:200]}
                            _log_error(
                                "ToolExecutionError", result[:500], _SRC_TOOL,
                                session_id=self.session_id,
                                tool_name=tc.name,
                                tool_arguments=args_dict,
                                tool_result=result,
                                messages_snapshot=self.llm.messages,
                            )
                        _session_store.write_tool_result_trace(
                            self.session_id,
                            tool_call_id=tc.id,
                            result=result,
                            error=error_info,
                        )

                    # 实时通知 listener：一个工具调用完成
                    self._on_tool_progress(round_num + 1, tc.name, tc.raw_arguments, result)

                    # 截断超长 tool result，防止塞进 messages 后超出 context window
                    _MAX_TOOL_RESULT_CHARS = 8000
                    result_for_llm = result
                    if len(result) > _MAX_TOOL_RESULT_CHARS:
                        result_for_llm = (
                            result[:_MAX_TOOL_RESULT_CHARS]
                            + f"\n...[截断：原始结果 {len(result)} 字符，已省略 {len(result) - _MAX_TOOL_RESULT_CHARS} 字符]"
                        )

                    tool_msg = self.adapter.format_tool_result(tc.id, result_for_llm)
                    self.llm.messages.append(tool_msg)

                    # 检查中断（每个工具调用完成后）
                    self.check_interrupt()

            # ── 达到最大轮数：总结现状，清空计数器，继续解决 ──
            # 先检查中断
            self.check_interrupt()

            logger.info(f"tool_loop: reached max_tool_rounds ({self.max_tool_rounds}), summarizing and continuing")
            summary_prompt = (
                "你已达到本轮工具调用上限（"
                + str(self.max_tool_rounds)
                + " 轮）。请简洁总结当前进展："
                "1) 已经完成了什么；2) 还在尝试什么；3) 下一步计划。\n\n"
                "总结的最后一行必须是以下两行之一（只写这一行，不要加其他内容）：\n"
                "[继续] — 如果任务还没完成，需要继续调用工具\n"
                "[完成] — 如果任务已经全部完成，不再需要调用工具\n\n"
                "直接回复内容，不要调用任何工具。"
            )
            self.llm.messages.append({"role": "user", "content": summary_prompt})
            try:
                response = self._chat_with_fallback(tools=None)
                self.llm.messages.append(
                    response.choices[0].message.model_dump(exclude_none=True)
                )
                content = (response.choices[0].message.content or "").strip()
                # 检查最后一行是否标记为完成
                last_line = content.strip().split("\n")[-1].strip()
                # 把总结发给用户（去掉标记行）
                display_lines = content.strip().split("\n")
                if display_lines[-1].strip() in ("[继续]", "[完成]"):
                    display_content = "\n".join(display_lines[:-1]).strip()
                else:
                    display_content = content
                if display_content and self.interim_sender:
                    try:
                        self.interim_sender(display_content)
                    except Exception:
                        pass
                if last_line == "[完成]":
                    logger.info("tool_loop: LLM indicated [完成] after summary")
                    # 去掉标记行，返回总结内容
                    summary_content = "\n".join(content.strip().split("\n")[:-1]).strip()
                    return summary_content or content
                logger.info("tool_loop: LLM indicated [继续], resuming tool loop")
                # 通知进度系统重置：结束旧卡片，后续工具调用会新开卡片
                if self.progress_callback:
                    try:
                        self.progress_callback({"type": "progress_reset"})
                    except Exception:
                        pass
            except AgentInterrupted:
                raise
            except Exception as e:
                logger.warning(f"summary call failed after max iterations: {e}")

            # 追加继续提示，让 LLM 继续解决
            self.llm.messages.append({
                "role": "user",
                "content": "请继续解决上述问题。如果已解决请直接回复结论，不需要再调用工具。",
            })

    def _on_tool_progress(self, round_num: int, tool_name: str, args: str, result: str) -> None:
        """每个工具调用完成后实时通知 listener 更新进度卡片。

        优先使用 progress_callback（卡片模式），fallback 到 interim_sender（文本模式）。
        fallback 场景：新消息中断后 progress_callback 被清空，但 _process_with_interrupt
        循环仍在跑，此时通过 interim_sender 保证进度不丢失。
        """
        if self.progress_callback:
            try:
                args_preview = args[:80] + ("..." if len(args) > 80 else "")
                result_preview = result[:120] + ("..." if len(result) > 120 else "")
                self.progress_callback({
                    "type": "tool_progress",
                    "round": round_num,
                    "tool": tool_name,
                    "args_preview": args_preview,
                    "result_preview": result_preview,
                })
                return
            except Exception:
                pass
        # Fallback：progress_callback 不可用时，通过 interim_sender 发文本进度
        if self.interim_sender:
            try:
                args_preview = args[:60] + ("..." if len(args) > 60 else "")
                result_preview = result[:80] + ("..." if len(result) > 80 else "")
                self.interim_sender(f">{tool_name}({args_preview})\n  {result_preview}")
            except Exception:
                pass

    def _on_model_switch(self, message: str) -> None:
        """模型切换时通知 listener 实时展示状态。"""
        if not self.progress_callback:
            return
        try:
            self.progress_callback({
                "type": "model_switch",
                "message": message,
            })
        except Exception:
            pass

    def run(self, user_input: str) -> str:
        """处理一轮用户输入，返回最终回复文本。"""
        self._consecutive_llm_failures = 0  # 新增：每次新任务开始时重置
        self._fast_path_tool_count = 0  # 跨轮次重置，确保反思计数涵盖所有 tool loop

        self.llm.add_user_message(user_input)
        try:
            result = self._run_tool_loop()
        except AgentInterrupted:
            from src.core.skill_audit import clear_audit
            clear_audit()
            raise

        # Skill 执行审计：检查是否有遗漏步骤
        audit_reminder = None
        try:
            from src.core.skill_audit import end_audit, record_llm_output
            if result:
                record_llm_output(result[:500])
            audit_reminder = end_audit()
        except Exception:
            pass

        # 如果审计发现遗漏步骤，将提醒注入下一轮让 LLM 补上
        if audit_reminder:
            self.llm.add_user_message(audit_reminder)
            try:
                result = self._run_tool_loop()
            except AgentInterrupted:
                from src.core.skill_audit import clear_audit
                clear_audit()
                raise
        # context 占比提醒：>= 80% 时在回复末尾追加占比信息
        if self._compaction_config and result:
            estimated = self._estimate_context_tokens()
            cw = self._compaction_config.context_window
            end_threshold_percent = self._compaction_config.end_threshold_percent
            usage_pct = estimated / cw * 100 if cw else 0
            if usage_pct >= end_threshold_percent:
                result += f"\n---\n📊 Context: {estimated:,} / {cw:,} tokens ({usage_pct:.0f}%)"

        # ── 后台反思：end turn 后判断是否需要沉淀 skill/info/project ──
        self._maybe_spawn_background_reflection(user_input)

        return result

    def _estimate_context_tokens(self) -> int:
        """估算当前 messages 的 token 总数。

        优先使用 LLM 返回的 prompt_tokens（精确值），
        仅在从未调用过 LLM 时 fallback 到 bytes/4 估算。
        """
        if self.last_prompt_tokens > 0:
            return self.last_prompt_tokens
        try:
            serialized = json.dumps(self.llm.messages, ensure_ascii=False)
            return len(serialized.encode("utf-8")) // 4
        except Exception:
            return 0

    # ── 后台反思 ─────────────────────────────────────────────────────────────

    def _maybe_spawn_background_reflection(self, user_input: str) -> None:
        """end turn 后判断是否需要后台反思沉淀。

        用 should_reflect() 做短路判断，通过后起后台线程执行
        run_reflection_loop，不阻塞用户回复。
        """
        from src.core.reflection import should_reflect

        tool_count = self._fast_path_tool_count

        if not should_reflect(
            plan=self.current_plan,
            tool_call_count=tool_count,
            skill_activated=list(self.skills.keys())[0] if self.skills else None,
            user_input=user_input,
        ):
            return

        # 需要快照增量对话：从上次反思水位线到现在的所有 messages
        messages_snapshot = list(self.llm.messages)
        watermark = self._reflect_watermark
        incremental_messages = messages_snapshot[watermark:]
        # 更新水位线
        self._reflect_watermark = len(messages_snapshot)

        # 构建执行摘要：收集本轮所有 tool call 名称
        tool_names: list[str] = []
        for msg in messages_snapshot:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fname = tc.get("function", {}).get("name", "")
                    if fname:
                        tool_names.append(fname)

        execution_summary = f"本轮调用了 {len(tool_names)} 次工具：{', '.join(tool_names)}" if tool_names else "本轮无工具调用"

        def _run_reflection():
            """后台线程：调用 run_reflection_loop 执行沉淀。"""
            _notify = self.reflect_notify_callback
            try:
                from src.core.reflection import mark_reflection_done, run_reflection_loop

                active_project = self._infer_active_project()
                recent_ctx = self._format_messages_for_context(incremental_messages)

                hints = run_reflection_loop(
                    goal=user_input[:200],
                    execution_summary=execution_summary,
                    llm_client=self.llm,
                    adapter=self.adapter,
                    skill_activated=list(self.skills.keys())[0] if self.skills else None,
                    recent_context=recent_ctx,
                    fallback_models=self.fallback_models,
                    active_project=active_project,
                )
                mark_reflection_done()
                if hints:
                    logger.info(f"[后台反思] 沉淀完成: {'; '.join(hints)}")
                    if _notify:
                        _notify(f"📝 反思沉淀完成:\n" + "\n".join(f"  • {h}" for h in hints))
                    # 刷新 skill/index，确保本次 session 能加载新建的 skill
                    self._load_or_refresh_skills()
                elif _notify:
                    _notify("📝 反思完成，暂无新内容需要沉淀")
            except Exception as e:
                try:
                    from src.core.reflection import mark_reflection_done
                    mark_reflection_done()
                except Exception:
                    pass
                logger.warning(f"[后台反思] 失败: {e}")
                if _notify:
                    _notify(f"反思失败: {e}")

        t = threading.Thread(target=_run_reflection, name="bg-reflection", daemon=True)
        t.start()

    @staticmethod
    def _format_messages_for_context(messages: list[dict]) -> str:
        """将消息列表格式化为反思上下文文本。"""
        lines = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                preview = content[:300]
                lines.append(f"[{role}] {preview}")
        return "\n".join(lines) if lines else "（无对话记录）"

    def maybe_compact(
        self,
        session_store: Any = None,
        session_id: str = "",
        progress_callback: Callable[[str], None] | None = None,
    ) -> CompactionResult | None:
        """检查并执行上下文压缩。

        Args:
            session_store: session_store 模块，用于写入 segment_boundary。
            session_id: 当前会话 id，需与 JSONL 一致；空字符串则仍执行压缩逻辑但不落 segment 边界。
            progress_callback: 可选，Compaction 各阶段进度文案（发往 UI）。
        """
        if self._compaction_config is None:
            return None

        if (
            self.current_plan is not None
            and self.current_plan.status == PlanStatus.executing
        ):
            return None

        estimated_tokens = self._estimate_context_tokens()

        try:
            result = apply_compaction(
                agent_llm=self.llm,
                config=self._compaction_config,
                estimated_tokens=estimated_tokens,
                stop_reason=self.last_stop_reason,
                session_id=session_id,
                session_store=session_store,
                progress_callback=progress_callback,
                fallback_llms=self.fallback_models,
            )
        except Exception as e:
            logger.warning(f"压缩异常: {e}")
            return None

        # 压缩成功后重置 prompt token 计数，避免下次 _estimate_context_tokens 返回旧值
        if result is not None and result.success:
            self.last_prompt_tokens = 0

        return result

    def force_compact(
        self,
        session_store: Any = None,
        session_id: str = "",
        progress_callback: Callable[[str], None] | None = None,
    ) -> CompactionResult | None:
        """手动触发上下文压缩（/compaction 命令调用，无视 token 阈值）。

        使用 threading.Lock 保证多线程安全（飞书 WebSocket 回调可能并发触发）。
        如果另一条压缩正在执行，返回 None。

        Args:
            progress_callback: 可选，Compaction 各阶段进度文案（发往 UI）。
        """
        if not self._compaction_lock.acquire(blocking=False):
            logger.info("force_compact: 另一次压缩正在执行，跳过")
            return None

        try:
            if self._compaction_config is None:
                return None

            if (
                self.current_plan is not None
                and self.current_plan.status == PlanStatus.executing
            ):
                return None

            try:
                result = apply_compaction(
                    agent_llm=self.llm,
                    config=self._compaction_config,
                    estimated_tokens=self._estimate_context_tokens(),
                    stop_reason="end_turn",
                    session_id=session_id,
                    session_store=session_store,
                    force=True,
                    progress_callback=progress_callback,
                    fallback_llms=self.fallback_models,
                )
            except Exception as e:
                logger.warning(f"手动压缩异常: {e}")
                return None

            # 压缩成功后重置 prompt token 计数
            if result is not None and result.success:
                self.last_prompt_tokens = 0

            return result
        finally:
            self._compaction_lock.release()

    def _infer_active_project(self) -> str:
        """从对话历史中推断当前操作的项目名。"""
        import re
        # 从 messages 中的 tool call 参数提取文件路径
        path_pattern = re.compile(r"/Users/\S+?/([a-zA-Z0-9_-]+)/(?:src|lib|app|pkg)/")
        project_counts: dict[str, int] = {}
        for msg in self.llm.messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    args_str = tc.get("function", {}).get("arguments", "")
                    for m in path_pattern.finditer(args_str):
                        name = m.group(1)
                        project_counts[name] = project_counts.get(name, 0) + 1
            elif msg.get("role") == "tool":
                tool_content = msg.get("content", "")
                for m in path_pattern.finditer(tool_content):
                    name = m.group(1)
                    project_counts[name] = project_counts.get(name, 0) + 1
        if not project_counts:
            return ""
        # 返回出现次数最多的项目名
        return max(project_counts, key=project_counts.get)

    def _get_recent_context(self, max_turns: int = 5) -> str:
        """提取最近几轮对话的文本摘要，供反思模块分析。"""
        messages = self.llm.messages
        # 只取最近 max_turns*2 条（user+assistant 为一轮）
        recent = messages[-(max_turns * 2):] if len(messages) > max_turns * 2 else messages
        lines = []
        for msg in recent:
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            c = msg.get("content", "")
            if not c or c.startswith("[技能激活:"):
                continue
            # 截断过长内容
            preview = c[:300] + ("..." if len(c) > 300 else "")
            lines.append(f"{role}: {preview}")
        return "\n".join(lines) if lines else "（无对话记录）"

