"""Context Compaction：对话轮次压缩系统。

设计原则：
- 以"轮"（turn）为单位，不逐条分类
- 策略 A：tail 占比 > 50% 时，尾部逐轮摘要（按 query/assistant 谁长压谁）
- 策略 B：tail 占比 ≤ 50% 时，前段合并成一条 summary，后段原封不动
- 消息序列完整性：轮内结构不变，tool_calls/tool_results 原封保留

触发条件：Token 估算 >= context_window * trigger_threshold，且 stopReason 为 end_turn/aborted。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from math import ceil
from pathlib import Path
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _notify_progress(cb: Callable[[str], None] | None, msg: str) -> None:
    if cb is None:
        return
    try:
        cb(msg)
    except Exception as e:
        logger.debug("Compaction progress_callback 失败: %s", e)


# ── 目录常量 ──────────────────────────────────────────────────────────────────

LAMIX_DIR = Path.home() / ".lamix"
COMPACTION_LOG = LAMIX_DIR / ".compaction_log.jsonl"

# ── 配置 ──────────────────────────────────────────────────────────────────────

STOP_REASONS = {"end_turn", "aborted", "stop", "stop_sequence"}
END_THRESHOLD_PERCENT = 80.0
DEFAULT_CONTEXT_WINDOW = 131_072
DEFAULT_TRIGGER_THRESHOLD = 0.90

# ── 模型上下文长度注册表（前缀匹配，从短到长排列以避免误匹配） ────────────────
# 数据来源：各模型官方文档 / OpenRouter
MODEL_CONTEXT_PREFIXES: list[tuple[str, int]] = [
    # GLM series (Zhipu / BigModel)
    ("glm-5",         1_000_000),  # GLM-5 / 5.1 / 5.2: 1M context
    ("glm-4.7",       200_000),
    ("glm-4.6",       200_000),
    ("glm-4.5",       128_000),
    ("glm-4",         128_000),   # GLM-4 / 4-Plus / 4-Air 等
    # MiniMax series
    ("MiniMax-M3",    204_800),
    ("MiniMax-M2",    204_800),   # M2 / M2.1 / M2.5 / M2.7 及 highspeed 变体
    # DeepSeek series
    ("deepseek-v4",   1_000_000),  # DeepSeek V4: 1M context
    ("deepseek-v3",   128_000),
    ("deepseek-r1",   128_000),
    # Anthropic Claude series (via proxy)
    ("claude-4",      200_000),
    ("claude-3.7",    200_000),
    ("claude-3.5",    200_000),
    # OpenAI series
    ("gpt-5",         400_000),
    ("gpt-4.1",       1_000_000),
    ("gpt-4o",        128_000),
]


def resolve_context_window(model_name: str, explicit: int | None = None) -> int:
    """根据模型名自动解析上下文窗口大小。

    优先级：explicit（config 中手动设置的）> 注册表前缀匹配 > DEFAULT_CONTEXT_WINDOW。
    """
    if explicit:
        return int(explicit)
    model_lower = model_name.lower()
    for prefix, cw in MODEL_CONTEXT_PREFIXES:
        if model_lower.startswith(prefix.lower()):
            return cw
    logger.info(
        "[compaction] 模型 '%s' 未在注册表中，使用默认 context_window=%d",
        model_name, DEFAULT_CONTEXT_WINDOW,
    )
    return DEFAULT_CONTEXT_WINDOW


@dataclass
class CompactionConfig:
    """压缩配置。"""

    context_window: int = DEFAULT_CONTEXT_WINDOW
    trigger_threshold: float = DEFAULT_TRIGGER_THRESHOLD
    end_threshold_percent: float = END_THRESHOLD_PERCENT
    compaction_log_max_bytes: int = 10 * 1024 * 1024  # 10MB 轮转
    tail_ratio: float = 0.2  # 最后 20% 轮
    tail_threshold: float = 0.5  # tail 占比 > 50% 走策略 A

    def should_trigger(self, estimated_tokens: int, stop_reason: str | None) -> bool:
        """判断是否应该触发压缩。"""
        if stop_reason not in STOP_REASONS:
            return False
        threshold_tokens = int(self.context_window * self.trigger_threshold)
        return estimated_tokens >= threshold_tokens


# ── 数据类 ─────────────────────────────────────────────────────────────────────

@dataclass
class CompactionResult:
    """压缩结果。"""

    success: bool
    summary: str = ""
    messages_kept: list[dict[str, Any]] = field(default_factory=list)
    archived_count: int = 0
    archive_details: str = ""
    archive_targets: list[dict[str, Any]] = field(default_factory=list)
    tokens_before: int = 0  # 压缩前 token 估算数
    tokens_after: int = 0   # 压缩后 token 估算数
    error: str | None = None


@dataclass
class Turn:
    """一轮对话。"""
    index: int
    messages: list[dict[str, Any]]
    user_query: str
    user_query_len: int
    assistant_texts: list[str]
    assistant_texts_len: int
    byte_length: int  # 含 tool 层


# ── Prompt 模板 ───────────────────────────────────────────────────────────────

_SUMMARY_TURN_PROMPT = """以下是一轮对话，请精简总结。

要求：
1. 保留核心信息（做了什么、发现了什么、结论是什么）
2. 删除冗余细节
3. 总长度不超过原始的 40%

## 用户问题
{user_query}

## 助手回复
{assistant_texts}

## 总结"""


_SUMMARY_HEAD_PROMPT = """以下是一段对话历史，请按主题分组总结。

要求：
1. 按讨论的主题分组，每个主题一段
2. 每段包含：讨论了什么、做出了什么决定/结论、是否有待办事项
3. 保留所有关键信息（文件路径、命令、决策、偏好），不要遗漏
4. 总长度不超过原始对话的 30%

## 对话历史
{messages_text}

## 总结"""


_TASK_CONTEXT_PROMPT = """你是一个任务进度摘要助手。以下是一段对话历史，请生成简洁的任务上下文说明。

要求：
1. 说明当前任务是什么（用户的原始请求）
2. 已完成哪些步骤，有什么结论
3. 当前卡在哪一步或下一步要做什么
4. 最多 200 字，要简洁

## 对话历史

{messages_text}"""


# ── 核心算法 ────────────────────────────────────────────────────────────────

def split_into_turns(messages: list[dict[str, Any]]) -> list[Turn]:
    """以 user query 为锚点，将消息列表切分为若干轮。

    一轮 = 一条 user 消息 + 后续所有非 user 消息（assistant、tool）。
    """
    turns: list[Turn] = []
    current_turn: list[dict[str, Any]] = []
    user_query = ""
    user_query_len = 0
    assistant_texts: list[str] = []
    assistant_texts_len = 0
    byte_length = 0

    for msg in messages:
        role = msg.get("role", "")
        # 遇到新 user 消息：先保存当前轮，再开新轮
        if role == "user":
            if current_turn:
                turns.append(Turn(
                    index=len(turns),
                    messages=list(current_turn),
                    user_query=user_query,
                    user_query_len=user_query_len,
                    assistant_texts=list(assistant_texts),
                    assistant_texts_len=assistant_texts_len,
                    byte_length=byte_length,
                ))
                current_turn = []
                user_query = ""
                user_query_len = 0
                assistant_texts = []
                assistant_texts_len = 0
                byte_length = 0
            # 记录 user query
            content = _extract_content(msg.get("content", ""))
            user_query = content
            user_query_len = len(content.encode("utf-8"))
            current_turn.append(msg)
            byte_length += len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        else:
            # assistant 或 tool 消息
            current_turn.append(msg)
            byte_length += len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            if role == "assistant":
                # 只提取文字回复，不含 tool_calls
                content = _extract_content(msg.get("content", ""))
                if content:
                    assistant_texts.append(content)
                    assistant_texts_len += len(content.encode("utf-8"))

    # 最后一批
    if current_turn:
        turns.append(Turn(
            index=len(turns),
            messages=list(current_turn),
            user_query=user_query,
            user_query_len=user_query_len,
            assistant_texts=list(assistant_texts),
            assistant_texts_len=assistant_texts_len,
            byte_length=byte_length,
        ))

    return turns


def _llm_summarize_turn(
    turn: Turn,
    llm: Any,
    fallback_llms: list[tuple[Any, Any]] | None = None,
) -> str:
    """对单轮对话做摘要，根据 user_query/assistant 谁长决定压谁。"""
    if turn.assistant_texts_len > turn.user_query_len:
        # assistant 是大头 → 摘要 assistant，输入含 user_query 上下文
        prompt = _SUMMARY_TURN_PROMPT.format(
            user_query=turn.user_query[:500],
            assistant_texts="\n".join(turn.assistant_texts),
        )
    else:
        # user_query 是大头 → 摘要 user_query
        prompt = (
            "以下是一段用户输入，请精简保留关键信息（文件路径、核心需求等），删除冗余内容：\n\n"
            + turn.user_query
        )

    return _llm_call(prompt, llm, fallback_llms)


def _llm_summarize_head_turns(
    head_turns: list[Turn],
    llm: Any,
    fallback_llms: list[tuple[Any, Any]] | None = None,
) -> str:
    """对前段多轮对话生成一条整体 summary。"""
    lines: list[str] = []
    for i, turn in enumerate(head_turns):
        user_part = turn.user_query[:200]
        assistant_part = "\n".join(t[:200] for t in turn.assistant_texts) if turn.assistant_texts else "(无文字回复)"
        lines.append(f"[Round {i + 1}]\nUser: {user_part}\nAssistant: {assistant_part}")

    messages_text = "\n\n".join(lines)
    prompt = _SUMMARY_HEAD_PROMPT.format(messages_text=messages_text)
    return _llm_call(prompt, llm, fallback_llms)


def _llm_call(
    prompt: str,
    llm: Any,
    fallback_llms: list[tuple[Any, Any]] | None = None,
) -> str:
    """调用 LLM，带 fallback 降级。"""
    def _do_call(target_llm: Any) -> str:
        tc = _make_temp_client(target_llm)
        tc.messages = [{"role": "system", "content": "你是一个简洁准确的对话摘要助手。"}]
        tc.add_user_message(prompt)
        response = tc.chat()
        return (response.choices[0].message.content or "").strip()

    try:
        return _do_call(llm)
    except Exception as e:
        logger.warning(f"LLM 摘要调用失败: {e}")

    if fallback_llms:
        for fb_llm, _ in fallback_llms:
            try:
                logger.info(f"摘要 fallback 使用: {getattr(fb_llm, 'model', '?')}")
                return _do_call(fb_llm)
            except Exception as e2:
                logger.warning(f"摘要 fallback 失败: {e2}")

    return ""  # 所有 LLM 都失败，返回空字符串


def _make_temp_client(llm: Any) -> Any:
    """复用 llm 的连接参数创建临时客户端。"""
    from src.core.llm import LLMClient

    client = llm.client
    return LLMClient(
        api_key=client.api_key if hasattr(client, "api_key") else getattr(llm, "_api_key", ""),
        base_url=str(client.base_url) if hasattr(client, "base_url") else getattr(llm, "_base_url", ""),
        model=llm.model,
        timeout=600.0,
    )


# ── Compactor ─────────────────────────────────────────────────────────────────

class Compactor:
    """轮次压缩器：split → strategy A/B → assemble。"""

    def __init__(
        self,
        llm: Any,
        config: CompactionConfig | None = None,
        fallback_llms: list[tuple[Any, Any]] | None = None,
    ) -> None:
        self.llm = llm
        self.config = config or CompactionConfig()
        self.fallback_llms: list[tuple[Any, Any]] = fallback_llms or []

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_tool_fields(msg: dict[str, Any]) -> dict[str, Any]:
        """摘要 assistant 消息时移除 tool 相关字段，防止残留 tool_calls 导致
        tool_result 孤立、DeepSeek/MiniMax 等严格模型报 400 错误。"""
        cleaned = dict(msg)
        cleaned.pop("tool_calls", None)
        cleaned.pop("tool_call_id", None)
        cleaned.pop("name", None)
        return cleaned

    def compact(
        self,
        messages: list[dict[str, Any]],
        session_store: Any = None,
        session_id: str = "",
        progress_callback: Callable[[str], None] | None = None,
    ) -> CompactionResult:
        """执行轮次压缩流水线。

        步骤：
        1. 分轮（split_into_turns）
        2. 计算 tail 占比，决定策略 A / B
        3. 执行压缩（摘要或合并）
        4. 写 segment_boundary + compaction_log
        5. 注入任务上下文摘要
        """
        if not messages:
            return CompactionResult(success=False, error="空消息列表")

        # Step 1: 分轮
        _notify_progress(progress_callback, "[1/4] 正在切分对话轮次...")
        turns = split_into_turns(messages)

        # 边界：轮数太少时，只对最大轮做摘要压缩
        if len(turns) <= 3:
            logger.info(f"轮数 {len(turns)} <= 3，尝试对最大轮做摘要压缩")
            _notify_progress(progress_callback, "[跳过] 对话轮数太少，尝试压缩最大轮次...")
            max_turn = max(turns, key=lambda t: t.byte_length)
            if not max_turn.assistant_texts:
                logger.info("最大轮次无 assistant 文字，跳过压缩")
                return CompactionResult(
                    success=True,
                    messages_kept=list(messages),
                    archived_count=0,
                    archive_details="轮数太少且无 assistant 文字，未压缩",
                )
            summary = _llm_summarize_turn(max_turn, self.llm, self.fallback_llms)
            if not summary:
                logger.info("摘要生成失败，跳过压缩")
                return CompactionResult(
                    success=True,
                    messages_kept=list(messages),
                    archived_count=0,
                    archive_details="轮数太少且摘要失败，未压缩",
                )
            summary_bytes = len(summary.encode("utf-8"))
            if summary_bytes >= max_turn.assistant_texts_len * 0.7:
                logger.info("摘要未达到 30% 压缩率，跳过压缩")
                return CompactionResult(
                    success=True,
                    messages_kept=list(messages),
                    archived_count=0,
                    archive_details="轮数太少且压缩率不足，未压缩",
                )
            result_messages = list(messages)
            for i, msg in enumerate(result_messages):
                if msg in max_turn.messages and msg.get("role") == "assistant":
                    msg_copy = self._strip_tool_fields(msg)
                    msg_copy["content"] = summary
                    result_messages[i] = msg_copy
                    break
            return CompactionResult(
                success=True,
                messages_kept=result_messages,
                archived_count=0,
                archive_details=f"轮数太少，仅压缩最大轮次 assistant（{max_turn.assistant_texts_len}→{summary_bytes} bytes）",
            )

        # Step 2: 计算 tail 占比
        tail_count = max(1, ceil(len(turns) * self.config.tail_ratio))
        tail_turns = turns[-tail_count:]
        tail_len = sum(t.byte_length for t in tail_turns)
        total_len = sum(t.byte_length for t in turns)
        ratio = tail_len / total_len if total_len > 0 else 0

        logger.info(
            f"Compaction V2: 总轮数 {len(turns)}，tail {tail_count} 轮，"
            f"占比 {ratio:.0%}（{'策略A' if ratio > self.config.tail_threshold else '策略B'}）"
        )

        # Step 3: 执行压缩
        _notify_progress(
            progress_callback,
            f"[2/4] 正在压缩上下文（{'策略A' if ratio > self.config.tail_threshold else '策略B'}）..."
        )
        result_messages, summary_text = self._apply_strategy(turns, tail_turns, ratio)

        # Step 4: 写 segment_boundary + compaction_log
        _notify_progress(progress_callback, "[3/4] 正在写入日志...")
        if session_store is not None and session_id:
            _write_segment_boundary(messages, session_id, session_store)

        try:
            _log_compaction(
                original_turns=len(turns),
                tail_ratio=ratio,
                strategy="A" if ratio > self.config.tail_threshold else "B",
                config=self.config,
            )
        except Exception as e:
            logger.warning(f"Compaction 日志写入失败: {e}")

        # Step 5: 注入任务上下文摘要
        _notify_progress(progress_callback, "[4/4] 正在生成任务上下文...")
        task_context = self._generate_task_context(messages)
        if task_context:
            context_msg = {
                "role": "user",
                "content": f"【任务上下文】{task_context}\n\n请继续完成上述任务。",
                "is_task_context": True,
            }
            result_messages = [context_msg] + result_messages
            logger.info(f"Compaction 注入任务上下文: {task_context[:80]}...")

        _notify_progress(
            progress_callback,
            f"[完成] 压缩完成，保留 {len(turns)} 轮 → {len(result_messages)} 条消息",
        )

        return CompactionResult(
            success=True,
            summary=task_context or "",
            messages_kept=result_messages,
            archived_count=0,
            archive_details=f"V2: 策略{'A' if ratio > self.config.tail_threshold else 'B'}，tail占比{ratio:.0%}",
        )

    def _apply_strategy(
        self,
        turns: list[Turn],
        tail_turns: list[Turn],
        ratio: float,
    ) -> tuple[list[dict[str, Any]], str]:
        """执行策略 A 或 B，返回扁平化的消息列表。"""
        if ratio > self.config.tail_threshold:
            # 策略 A：尾部逐轮摘要
            return self._strategy_a(turns, tail_turns)
        else:
            # 策略 B：前段合并 + 后段保留
            return self._strategy_b(turns, tail_turns)

    def _strategy_a(
        self,
        turns: list[Turn],
        tail_turns: list[Turn],
    ) -> tuple[list[dict[str, Any]], str]:
        """策略 A：前 80% 轮原封不动，后 20% 轮逐轮摘要。"""
        head_turns = turns[:-len(tail_turns)] if len(tail_turns) < len(turns) else []

        result: list[dict[str, Any]] = []

        # 前段：原封不动
        for turn in head_turns:
            result.extend(turn.messages)

        # 后段：逐轮摘要
        for turn in tail_turns:
            # user query 原封保留（除非为空）
            has_user = False
            for msg in turn.messages:
                if msg.get("role") == "user":
                    result.append(msg)
                    has_user = True
                    break

            # assistant 摘要
            if turn.assistant_texts:
                summary = _llm_summarize_turn(turn, self.llm, self.fallback_llms)
                if summary:
                    # 替换 assistant 文字消息为摘要，同时移除 tool_calls 等字段
                    # （摘要后不再是 tool_call 模式，tool_result 已不在 result 中）
                    for msg in turn.messages:
                        if msg.get("role") == "assistant":
                            msg_copy = self._strip_tool_fields(msg)
                            msg_copy["content"] = summary
                            result.append(msg_copy)
                            break
                else:
                    # LLM 失败，保留原始
                    result.extend(turn.messages)
            else:
                # 没有 assistant 文字（只有 tool_calls），原封保留
                result.extend(turn.messages)

        return result, ""

    def _strategy_b(
        self,
        turns: list[Turn],
        tail_turns: list[Turn],
    ) -> tuple[list[dict[str, Any]], str]:
        """策略 B：前 80% 轮合并成一条 summary，后 20% 轮原封不动。"""
        head_turns = turns[:-len(tail_turns)] if len(tail_turns) < len(turns) else []

        result: list[dict[str, Any]] = []

        # 前段：合并成一条 summary
        if head_turns:
            summary_text = _llm_summarize_head_turns(
                head_turns, self.llm, self.fallback_llms
            )
            if summary_text:
                result.append({
                    "role": "assistant",
                    "content": f"## 对话摘要\n\n{summary_text}",
                    "is_compaction_summary": True,
                })
            else:
                # LLM 失败，保留原始前段
                for turn in head_turns:
                    result.extend(turn.messages)

        # 后段：原封不动
        for turn in tail_turns:
            result.extend(turn.messages)

        return result, summary_text if head_turns else ""

    def _generate_task_context(self, messages: list[dict[str, Any]]) -> str:
        """生成任务进度摘要，防止多次压缩后任务上下文丢失。"""
        recent_context = messages[-min(len(messages), 30):]

        lines: list[str] = []
        for msg in recent_context:
            role = msg.get("role", "unknown")
            content = _extract_content(msg.get("content", ""))
            if content and role in ("user", "assistant"):
                truncated = content[:300] + ("..." if len(content) > 300 else "")
                lines.append(f"[{role}] {truncated}")

        if not lines:
            return ""

        messages_text = "\n".join(lines)
        prompt = _TASK_CONTEXT_PROMPT.format(messages_text=messages_text)

        temp_client = _make_temp_client(self.llm)
        temp_client.messages = [{"role": "system", "content": "你是任务进度摘要助手。"}]
        temp_client.add_user_message(prompt)

        try:
            response = temp_client.chat()
            result = (response.choices[0].message.content or "").strip()
            if len(result) > 300:
                result = result[:297] + "..."
            return result
        except Exception as e:
            logger.warning(f"任务上下文摘要生成失败: {e}")
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = _extract_content(msg.get("content", ""))
                    if content:
                        return content[:200]
            return ""


# ── 日志 ──────────────────────────────────────────────────────────────────────

def _log_compaction(
    original_turns: int,
    tail_ratio: float,
    strategy: str,
    config: CompactionConfig,
) -> None:
    """写压缩操作日志，超过 max_bytes 自动轮转。"""
    COMPACTION_LOG.parent.mkdir(parents=True, exist_ok=True)

    if COMPACTION_LOG.exists() and COMPACTION_LOG.stat().st_size > config.compaction_log_max_bytes:
        _rotate_compaction_log()

    with open(COMPACTION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "original_turns": original_turns,
            "tail_ratio": round(tail_ratio, 3),
            "strategy": strategy,
        }, ensure_ascii=False) + "\n")


def _rotate_compaction_log() -> None:
    """轮转压缩日志：.compaction_log.jsonl → .1 → .2 → ... → .5。"""
    import shutil

    for i in range(4, 0, -1):
        src = COMPACTION_LOG.with_suffix(f".jsonl.{i}")
        dst = COMPACTION_LOG.with_suffix(f".jsonl.{i + 1}")
        if src.exists():
            shutil.move(str(src), str(dst))
    shutil.move(str(COMPACTION_LOG), str(COMPACTION_LOG.with_suffix(".jsonl.1")))


# ── Segment Boundary ─────────────────────────────────────────────────────────

def _write_segment_boundary(
    messages: list[dict[str, Any]],
    session_id: str,
    session_store: Any,
) -> None:
    """写入 segment_boundary 到 session JSONL。"""
    if not session_id:
        return

    current_segment = messages[-1].get("segment", 0) if messages else 0
    ts = int(datetime.now().timestamp() * 1000)

    try:
        session_store.write_segment_boundary(
            session_id=session_id,
            segment=current_segment,
            next_segment_started_at=ts,
            archive=None,
        )
    except Exception as e:
        logger.warning(f"segment_boundary 写入失败: {e}")


# ── 工具方法 ─────────────────────────────────────────────────────────────────

def _extract_content(content: str | list[Any] | None) -> str:
    """从消息的 content 字段提取可读文本（支持 list block 格式）。"""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "toolResult":
                parts.append(f"[tool result]: {block.get('content', '')}")
            elif btype == "thinking":
                parts.append(f"[thinking]: {block.get('thinking', '')}")
        return "\n".join(parts)
    return str(content)


def _parse_json(text: str | None) -> dict[str, Any] | None:
    """从 LLM 回复中提取并解析 JSON。"""
    import re

    if not text:
        return None
    text = text.strip()

    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start: end + 1])
        except json.JSONDecodeError:
            pass

    return None


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算消息列表的 token 总数（粗略：UTF-8 字节数 / 4）。"""
    try:
        serialized = json.dumps(messages, ensure_ascii=False)
        return len(serialized.encode("utf-8")) // 4
    except Exception:
        return 0


def _estimate_text_tokens(text: str) -> int:
    """估算纯文本的 token 数（粗略：UTF-8 字节数 / 4）。"""
    try:
        return len(text.encode("utf-8")) // 4
    except Exception:
        return 0


# ── Agent 集成 ───────────────────────────────────────────────────────────────

def apply_compaction(
    agent_llm: Any,
    config: CompactionConfig,
    estimated_tokens: int,
    stop_reason: str | None = None,
    session_id: str = "",
    session_store: Any = None,
    *,
    force: bool = False,
    progress_callback: Callable[[str], None] | None = None,
    fallback_llms: list[tuple[Any, Any]] | None = None,
) -> CompactionResult | None:
    """检查并执行压缩。

    在 Agent.run() 返回后调用。
    如果超过阈值且 stopReason 允许，执行压缩并返回结果。
    """
    if not force and not config.should_trigger(estimated_tokens, stop_reason):
        return None

    messages = [m for m in agent_llm.messages if m.get("role") != "system"]
    if not messages:
        return None

    threshold_tokens = int(config.context_window * config.trigger_threshold)
    system_msg = agent_llm.messages[0] if agent_llm.messages else {}

    compactor = Compactor(llm=agent_llm, config=config, fallback_llms=fallback_llms)
    result = compactor.compact(
        messages,
        session_store=session_store,
        session_id=session_id,
        progress_callback=progress_callback,
    )

    if not result.success or not result.messages_kept:
        return result

    # 紧急截断：压缩后仍超阈值，强制只保留最近 2 轮
    new_messages = [system_msg] + result.messages_kept
    new_estimated = _estimate_messages_tokens(new_messages)
    if new_estimated >= threshold_tokens and len(result.messages_kept) > 4:
        logger.warning(
            f"Compaction 后仍超阈值（{new_estimated} >= {threshold_tokens}），"
            f"启动紧急截断：只保留最近 2 轮"
        )
        _notify_progress(progress_callback, "[紧急] 压缩不够，紧急截断到最近 2 轮")
        # 取最后 2 轮
        turns = split_into_turns(new_messages)
        kept_turns = turns[-2:] if len(turns) >= 2 else turns
        emergency_msgs = []
        for turn in kept_turns:
            emergency_msgs.extend(turn.messages)
        # 加上 task_context（如果有）
        task_msgs = [m for m in result.messages_kept if m.get("is_task_context")]
        result = CompactionResult(
            success=True,
            summary=result.summary,
            messages_kept=task_msgs + emergency_msgs,
            archived_count=result.archived_count,
            archive_details=result.archive_details + "\n  [紧急截断] 只保留最近 2 轮",
            archive_targets=result.archive_targets,
        )
        new_messages = [system_msg] + result.messages_kept

    # 更新 agent messages
    agent_llm.messages = new_messages

    # 安全网：system prompt 本身过长的情况
    final_estimated = _estimate_messages_tokens(agent_llm.messages)
    if final_estimated >= threshold_tokens and len(result.messages_kept) <= 3:
        logger.warning(
            f"Compaction 后消息已很少（{len(result.messages_kept)} 条）"
            f"但 token 估算仍超（{final_estimated} >= {threshold_tokens}），"
            f"system prompt 可能过长"
        )


    # 记录压缩前后 token 数
    result.tokens_before = estimated_tokens
    result.tokens_after = final_estimated
    return result
