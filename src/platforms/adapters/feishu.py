"""飞书平台适配器：继承 BasePlatformAdapter，通过 WebSocket 长连接接收消息。

完整迁移自 src/feishu/listener.py，保留并统一所有功能：
- WebSocket 长连接
- 消息去重（MessageDeduplicator）
- 进度卡片（实时工具调用进度）
- emoji reaction（ack）
- post 富文本消息解析
- /exit、/recovery、/new 命令处理

新接口遵循 BasePlatformAdapter：start/shutdown/send/send_card 均为异步或非阻塞。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import re

from src.feishu.client import FeishuClient
import threading
import time
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)
from lark_oapi.api.im.v1.model import (
    CreateMessageReactionRequestBuilder,
    CreateMessageReactionRequestBodyBuilder,
    EmojiBuilder,
    DeleteMessageReactionRequestBuilder,
)

from src.platforms.base import BasePlatformAdapter, PlatformMessage
from src.core.adapters.base import LLMFatalError
import logging
logger = logging.getLogger(__name__)


def _suppress_lark_stdout() -> None:
    """抑制 Lark SDK 的 stdout 日志，避免污染终端输出。

    lark_oapi 在 core/log.py 中自动为「Lark」logger 添加了 StreamHandler(sys.stdout)，
    且不设置 handler level（默认 NOTSET，所有级别通放）。
    WS 客户端创建时又调用 logger.setLevel(INFO)，导致 INFO 级日志直出 stdout。

    本函数移除 Lark 的全部现有 handler，替换为一个 WARNING 级别的 stderr handler。
    日志仍会通过 propagate 流向 root logger（daemon 的 stderr 日志不受影响）。
    """
    import logging as _logging
    _lark = _logging.getLogger("Lark")
    _lark.setLevel(_logging.WARNING)
    for _h in _lark.handlers[:]:
        _lark.removeHandler(_h)
    _sh = _logging.StreamHandler()
    _sh.setLevel(_logging.WARNING)
    _lark.addHandler(_sh)


class MessageDeduplicator:
    """消息去重器：基于 message_id 防止重复处理。"""

    def __init__(self, ttl_seconds: int = 600, max_size: int = 10000) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_size = max_size

    def is_duplicate(self, message_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            expired = [mid for mid, ts in self._seen.items() if now - ts > self._ttl]
            for mid in expired:
                del self._seen[mid]
            return message_id in self._seen

    def mark_processed(self, message_id: str) -> None:
        with self._lock:
            if len(self._seen) >= self._max_size:
                oldest = min(self._seen, key=self._seen.get)
                del self._seen[oldest]
            self._seen[message_id] = time.monotonic()


class FeishuAdapter(BasePlatformAdapter):
    """飞书平台适配器：通过 WebSocket 长连接接收消息。"""

    platform = "feishu"

    def __init__(self, config: dict) -> None:
        self.app_id: str = config["app_id"]
        self.app_secret: str = config["app_secret"]
        self._dedup = MessageDeduplicator()
        self._ws_client: Any | None = None
        self._ws_thread: threading.Thread | None = None
        self._stopped: bool = False  # 热重载时标记为 True，不再处理新消息
        self._lark_client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .build()
        )
        self._feishu_client = FeishuClient(self.app_id, self.app_secret)
        self._shutdown_callback: callable | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="feishu-handler"
        )

    # ─── BasePlatformAdapter 接口 ─────────────────────────────────────────

    def start(self) -> None:
        """启动 WebSocket 长连接（非阻塞，线程中运行）。"""
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(
                lambda data: None
            )
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        # 抑制 Lark SDK 的 stdout 日志（lark_oapi 导入时自动添加了 stdout handler）
        _suppress_lark_stdout()
        t = threading.Thread(target=self._ws_client.start, daemon=True, name="feishu-ws")
        self._ws_thread = t
        t.start()

    async def shutdown(self, timeout: float = 30.0) -> None:
        """优雅关闭 WebSocket 连接。"""
        ws = self._ws_client
        if ws is not None:
            for name in ("stop", "close", "interrupt"):
                fn = getattr(ws, name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception as e:
                        logger.info(f"[feishu] shutdown {name}: {e}")
                    break
        th = self._ws_thread
        if th is not None and th.is_alive():
            th.join(timeout=timeout)
        self._executor.shutdown(wait=False)

    async def send(self, chat_id: str, text: str, thread_id: str | None = None) -> None:
        """发送文本消息到指定会话。"""
        await asyncio.to_thread(self._send_text_sync, chat_id, text)

    async def send_card(self, chat_id: str, card: dict, thread_id: str | None = None) -> None:
        """发送卡片消息到指定会话。"""
        await asyncio.to_thread(self._send_card_sync, chat_id, card)

    # ─── 内部发送方法（同步，供 to_thread 调用）────────────────────────────

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """移除模型输出的 think 标签及内容，不发给用户。"""
        import re
        # 移除 <think>...</think> 标签及内容
        text = re.sub(r'<think>[\s\S]*?</think>', '', text)
        # 移除 <think>...</think> 格式
        text = re.sub(r'<think>[\s\S]*?</think>', '', text)
        return text.strip()

    def _send_reply(self, chat_id: str, text: str) -> str | None:
        """发送最终回复，自动判断用卡片还是文本。返回回复消息的 message_id。"""
        text = self._strip_think_tags(text)
        if not text:
            return None
        if self._should_use_card(text):
            return self._send_reply_as_card(chat_id, text)
        else:
            return self._send_reply_as_text(chat_id, text)

    @staticmethod
    def _should_use_card(text: str) -> bool:
        """判断回复是否含结构化数据，适合用卡片展示。"""
        if "|---" in text or "| ---" in text:
            return True
        return False

    def _send_reply_as_card(self, chat_id: str, text: str) -> None:
        """用 Markdown 卡片发送回复（表格/指标等结构化数据）。"""
        try:
            client = self._feishu_client
            title = "Lamix 回复"
            m = re.search(r"##\s*(.+)", text) or re.search(r"\*\*(.+?)\*\*", text)
            if m:
                title = m.group(1).strip()
            card = client.build_md_card(title=title, content=text, header_template="green")
            data = client.send_card(receive_id=chat_id, card=card, receive_id_type="chat_id")
            logger.info("[feishu] 卡片消息发送成功")
            return data.get("data", {}).get("message_id") if isinstance(data, dict) else None
        except Exception as e:
            logger.error(f"[feishu] 卡片发送失败({e})，降级为文本")
            return self._send_reply_as_text(chat_id, text)

    def _send_reply_as_text(self, chat_id: str, text: str) -> None:
        """发送纯文本消息。"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self._lark_client.im.v1.message.create(request)
        if not resp.success():
            logger.error(f"[feishu] 发送消息失败: code={resp.code} msg={resp.msg}")
            return None
        else:
            logger.info(f"[feishu] 消息发送成功 to={chat_id}")
            return getattr(resp.data, "message_id", None)

    def _send_text_sync(self, chat_id: str, text: str) -> None:
        """同步发送文本（在线程池中执行）。"""
        self._send_reply_as_text(chat_id, text)

    def _send_card_sync(self, chat_id: str, card: dict) -> None:
        """同步发送卡片（在线程池中执行）。"""
        try:
            client = self._feishu_client
            client.send_card(receive_id=chat_id, card=card, receive_id_type="chat_id")
            logger.info(f"[feishu] 卡片发送成功 to={chat_id}")
        except Exception as e:
            logger.error(f"[feishu] 卡片发送失败: {e}，降级为文本")
            self._send_text_sync(
                chat_id,
                card.get("body", {}).get("elements", [{}])[0].get("content", str(card)),
            )

    # ─── 进度卡片 ─────────────────────────────────────────────────────────

    def _make_progress_card(self, lines: list[str], finished: bool = False) -> dict:
        """构建工具调用进度卡片。"""
        status = "已完成" if finished else "处理中..."
        elements = [
            {"tag": "markdown", "content": f"**{status}** ({len(lines)} 个工具调用)"},
        ]
        shown = lines[-15:]
        if len(lines) > 15:
            elements.append({"tag": "markdown", "content": f"_...前 {len(lines) - 15} 条省略_"})
        for line in shown:
            safe_line = line[:200] + ("..." if len(line) > 200 else "")
            elements.append({"tag": "markdown", "content": safe_line})
        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "Lamix 工作进度"},
                "template": "green" if finished else "blue",
            },
            "body": {"elements": elements},
        }

    def _send_progress_card(self, chat_id: str, lines: list[str], finished: bool = False) -> str | None:
        """发送进度卡片，返回 message_id。失败时 fallback 到文本消息。"""
        card = self._make_progress_card(lines, finished=finished)
        logger.info(f"[feishu] _send_progress_card: lines={len(lines)}, finished={finished}")
        try:
            client = self._feishu_client
            data = client.send_card(receive_id=chat_id, card=card, receive_id_type="chat_id")
            logger.info(f"[feishu] progress card sent ok")
            return data.get("data", {}).get("message_id")
        except Exception as e:
            resp_body = getattr(getattr(e, "response", None), "text", "N/A")
            logger.error(f"[feishu] 发送进度卡片失败: {e}\n  response: {resp_body[:500]}")
            try:
                client = self._feishu_client
                status = "已完成" if finished else "处理中"
                text_lines = [f"[Lamix 工作进度 - {status} ({len(lines)} 个工具调用)]"]
                for line in lines[-10:]:
                    text_lines.append(line[:150])
                client.send_text(receive_id=chat_id, text="\n".join(text_lines), receive_id_type="chat_id")
            except Exception as e2:
                logger.error(f"[feishu] 进度文本 fallback 也失败: {e2}")
            return None

    def _update_progress_card(self, message_id: str, lines: list[str], finished: bool = False) -> None:
        """更新已有的进度卡片；失败时 re-raise 供熔断计数。"""
        card = self._make_progress_card(lines, finished=finished)
        client = self._feishu_client
        try:
            client.update_message(message_id=message_id, card=card)
        except Exception as e:
            resp_body = getattr(getattr(e, "response", None), "text", "N/A")
            logger.error(f"[feishu] 更新进度卡片失败: {e}\n  response: {resp_body[:500]}")
            raise

    # ─── Reaction ─────────────────────────────────────────────────────────

    def _add_reaction(self, message_id: str, emoji_type: str = "THINKING") -> str | None:
        try:
            req = (
                CreateMessageReactionRequestBuilder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBodyBuilder()
                    .reaction_type(EmojiBuilder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )
            resp = self._lark_client.im.v1.message_reaction.create(req)
            if resp.success():
                return resp.data.reaction_id
            return None
        except Exception:
            return None

    def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        if not reaction_id:
            return
        try:
            req = (
                DeleteMessageReactionRequestBuilder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            self._lark_client.im.v1.message_reaction.delete(req)
        except Exception:
            pass

    def _maybe_save_owner_identity(self, open_id: str, chat_id: str) -> None:
        """首次收到飞书消息时，自动保存 owner_open_id 和 owner_chat_id 到 config。

        这样后续上线通知、boot_tasks、审计报告都能自动发到正确的地方。
        """
        try:
            from src.core.config import load_config, save_config
            config = load_config()
            feishu_cfg = config.setdefault("feishu", {})
            changed = False
            if not feishu_cfg.get("owner_chat_id"):
                feishu_cfg["owner_chat_id"] = chat_id
                changed = True
            if not feishu_cfg.get("user_open_id"):
                feishu_cfg["user_open_id"] = open_id
                changed = True
            if changed:
                save_config(config)
                logger.info(f"[feishu] 已自动保存 owner 身份: chat_id={chat_id}, open_id={open_id}")
        except Exception as e:
            logger.error(f"[feishu] 保存 owner 身份失败: {e}")

    # ─── 消息处理 ───────────────────────────────────────────────────────

    def _handle_message(self, data) -> None:
        """WebSocket 收到消息，转换为 PlatformMessage 后推给 PlatformManager。"""
        # 热重载后旧 adapter 不再处理新消息
        if self._stopped:
            return
        try:
            sender = data.event.sender
            message = data.event.message

            sender_type = getattr(sender, "sender_type", None)
            if sender_type == "app":
                logger.warning("[feishu] 收到机器人自己的消息，跳过")
                return

            open_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id

            # 自动保存 owner 身份到 config（首次收到消息时）
            self._maybe_save_owner_identity(open_id, chat_id)

            # 过期消息丢弃（>5min）
            create_time_str = getattr(message, "create_time", None)
            if create_time_str:
                try:
                    create_ts = int(create_time_str) / 1000
                    if time.time() - create_ts > 300:
                        return
                except (ValueError, TypeError):
                    pass

            message_id = getattr(message, "message_id", None) or str(id(data))
            if self._dedup.is_duplicate(message_id):
                return

            text = self._extract_text(message.content)
            if not text:
                return

            # 处理引用消息：提取 parent_id，拉取被引用消息内容拼接到文本前面
            parent_id = getattr(message, "parent_id", None)
            quoted_text = self._fetch_quoted_text(parent_id) if parent_id else None

            if quoted_text:
                text = f"[引用消息]\n{quoted_text}\n---\n{text}"

            reaction_id = self._add_reaction(message_id)

            msg = PlatformMessage(
                platform="feishu",
                sender_id=open_id,
                chat_id=chat_id,
                thread_id=None,
                message_id=message_id,
                text=text,
                timestamp=time.time(),
                reaction_id=reaction_id,
            )

            # 提交到线程池，避免阻塞 WebSocket 事件循环
            self._executor.submit(self._deliver_to_platform, msg, reaction_id)

        except Exception as e:
            logger.error(f"[feishu] _handle_message 错误: {e}")

    def _deliver_to_platform(self, msg: PlatformMessage, reaction_id: str | None) -> None:
        """在线程池中执行：调用 on_message + 处理结果发送。"""
        try:
            self.on_message(msg)
            self._dedup.mark_processed(msg.message_id)
        except Exception as e:
            logger.error(f"[feishu] on_message 错误: {e}")

    def _extract_text(self, raw_content: str) -> str:
        """从消息 content 中提取纯文本。"""
        if not raw_content:
            return ""
        try:
            obj = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError):
            return raw_content

        # text 类型
        if isinstance(obj, dict) and obj.get("text"):
            return obj["text"].strip()

        # post 富文本类型
        if isinstance(obj, dict):
            post = obj.get("content")
            if isinstance(post, list):
                parts = []
                for row in post:
                    if isinstance(row, list):
                        for elem in row:
                            if isinstance(elem, dict):
                                parts.append(elem.get("text", ""))
                return " ".join(parts).strip()

        return ""

    def _fetch_quoted_text(self, parent_id: str) -> str | None:
        """根据 parent_id 获取被引用消息的文本内容。"""
        try:
            from src.feishu.client import get_client
            client = get_client()
            msg_data = client.get_message(parent_id)
            if not msg_data:
                return None

            # 飞书单条消息 API 返回 data.items 数组
            items = msg_data.get("items", [])
            item = items[0] if items and isinstance(items, list) else msg_data

            # 尝试多种路径提取 content：items[0].body.content / body.content / content
            content = ""
            body = item.get("body", {})
            if isinstance(body, dict):
                content = body.get("content", "")
            if not content:
                content = item.get("content", "")
            if content:
                return self._extract_text(content)
        except Exception as e:
            logger.warning(f"[feishu] 获取引用消息失败 (parent_id={parent_id}): {e}")
        return None

    # ─── 调度入口（供 PlatformManager.dispatch 调用）────────────────────────

    def _handle_dispatch(
        self,
        open_id: str,
        chat_id: str,
        message_id: str,
        text: str,
        reaction_id: str | None,
    ) -> None:
        """在线程池中执行：session.handle_input + 进度卡片 + 发送回复。"""
        if self.session_manager is None:
            logger.info("[feishu] session_manager 未设置，无法处理消息")
            return

        try:
            session = self.session_manager.get_or_create("feishu", open_id)
            session.partial_sender = lambda t: self._send_reply(chat_id, t)
            session._reply_callback = lambda t: self._send_reply(chat_id, t)
            session.set_reply_channel("feishu", chat_id)

            # ─── 进度卡片 worker ───────────────────────────────────────
            _progress_queue: queue.Queue[dict[str, Any]] = queue.Queue()
            _progress_done = threading.Event()
            progress_msg_id: str | None = None

            def _progress_worker() -> None:
                nonlocal progress_msg_id
                progress_lines: list[str] = []
                last_update_ts = 0.0
                update_interval = 1.5
                _card_fail_count = 0
                _MAX_CARD_FAILS = 3

                while True:
                    try:
                        event = _progress_queue.get(timeout=0.5)
                        logger.info(f"[feishu] progress_worker received: type={event.get('type') if isinstance(event, dict) else 'non-dict'}")
                    except queue.Empty:
                        if _progress_done.is_set():
                            if progress_lines and _card_fail_count < _MAX_CARD_FAILS:
                                if progress_msg_id is None:
                                    self._send_progress_card(chat_id, progress_lines, finished=True)
                                else:
                                    self._update_progress_card(progress_msg_id, progress_lines, finished=True)
                            return
                        continue

                    if not isinstance(event, dict):
                        continue

                    if event.get("type") == "progress_reset":
                        if progress_lines and _card_fail_count < _MAX_CARD_FAILS:
                            if progress_msg_id is None:
                                self._send_progress_card(chat_id, progress_lines, finished=True)
                            else:
                                self._update_progress_card(progress_msg_id, progress_lines, finished=True)
                        progress_lines = []
                        progress_msg_id = None
                        last_update_ts = 0.0
                        continue

                    if event.get("type") == "model_switch":
                        progress_lines.append(f"**[模型切换]** {event.get('message', '')}")
                        now = time.monotonic()
                        if now - last_update_ts >= update_interval and _card_fail_count < _MAX_CARD_FAILS:
                            if progress_msg_id is None:
                                progress_msg_id = self._send_progress_card(chat_id, progress_lines)
                                if progress_msg_id is None:
                                    _card_fail_count += 1
                            else:
                                try:
                                    self._update_progress_card(progress_msg_id, progress_lines)
                                except Exception:
                                    _card_fail_count += 1
                        continue

                    if event.get("type") != "tool_progress":
                        continue

                    round_n = event["round"]
                    tool = event["tool"]
                    args_p = event["args_preview"]
                    result_p = event["result_preview"]
                    is_error = (
                        result_p.startswith("[错误]")
                        or result_p.startswith("[飞书错误]")
                        or result_p.startswith("[网络错误]")
                    )
                    icon = "x" if is_error else ">"
                    progress_lines.append(f"**{round_n}.** `{tool}`({args_p})\n  {icon} {result_p}")

                    now = time.monotonic()
                    if now - last_update_ts < update_interval:
                        continue

                    if _card_fail_count < _MAX_CARD_FAILS:
                        if progress_msg_id is None:
                            progress_msg_id = self._send_progress_card(chat_id, progress_lines)
                            if progress_msg_id is None:
                                _card_fail_count += 1
                        else:
                            try:
                                self._update_progress_card(progress_msg_id, progress_lines)
                            except Exception:
                                _card_fail_count += 1
                    last_update_ts = time.monotonic()

            _worker = threading.Thread(target=_progress_worker, daemon=True, name="feishu-progress")
            _worker.start()

            def _progress_cb(event: dict) -> None:
                logger.info(f"[feishu] _progress_cb: event_type={event.get('type')}")
                _progress_queue.put(event)

            session.set_message_context(message_id=message_id, chat_id=chat_id)
            session.agent.progress_callback = _progress_cb
            session.agent.interim_sender = lambda t: self._send_reply(chat_id, t)
            try:
                result = session.handle_input(text)
            finally:
                _progress_done.set()
                _worker.join(timeout=3.0)
                session.agent.progress_callback = None
                session.agent.interim_sender = None

            # 入队场景：消息已入队，不回复
            if (
                not result.reply
                and not result.is_new
                and not result.is_exit
                and not result.is_command
                and not result.is_safe_mode
            ):
                self._dedup.mark_processed(message_id)
                logger.info(f"[feishu] 消息已入队，等待当前任务中断后处理")
                return

            # /exit 命令
            if result.is_exit:
                self._dedup.mark_processed(message_id)
                self._send_reply(chat_id, result.reply or "正在退出...")
                return

            # /recovery 命令
            if result.is_safe_mode and self.safe_mode_callback:
                self._dedup.mark_processed(message_id)
                self._send_reply(chat_id, result.reply or "正在切换到安全模式...")
                self.safe_mode_callback()
                return

            # /new 命令
            if result.is_new:
                session = self.session_manager.reset_session("feishu", open_id)
                session.partial_sender = lambda t: self._send_reply(chat_id, t)
                self._dedup.mark_processed(message_id)
                self._send_reply(chat_id, "[新 session 已开始]")
                return

            reply = result.reply
            if result.compaction_msg:
                logger.info(f"[feishu] {result.compaction_msg}")

            logger.info(f"[feishu] 回复: {reply}")
            self._remove_reaction(message_id, reaction_id or "")
            self._dedup.mark_processed(message_id)
            if reply:
                reply_msg_id = self._send_reply(chat_id, reply)
                # 多轮结束时（有 tool_calls）打一次 MUSCLE emoji
                if reply_msg_id:
                    tool_calls = getattr(result, "tool_calls", None)
                    tool_call_count = len(tool_calls) if tool_calls else 0
                    if tool_call_count > 0:
                        self._add_reaction(reply_msg_id, "MUSCLE")

            # 处理完毕后检查：如果 session 仍为空（无 user/assistant 消息），立即清理
            # 典型场景：daemon 重启后积压的 /resume /compaction 等命令，
            # 走 _handle_command 不写 JSONL，留下空 session
            # 但如果 llm.messages 中已有加载的消息（如 /resume），跳过清理
            if session.session_id:
                try:
                    from src.memory import session_store as ss
                    _has_memory_msgs = hasattr(session, "agent") and hasattr(session.agent, "llm") and len(session.agent.llm.messages) > 1
                    if ss.is_session_empty(session.session_id) and not _has_memory_msgs:
                        self.session_manager.remove_session("feishu", open_id)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[feishu] _handle_dispatch 错误: {e}")
            # LLM 全部失败时回复用户，而非静默吞掉
            try:
                if isinstance(e, LLMFatalError):
                    self._send_reply(
                        chat_id,
                        f"抱歉，所有模型（含 fallback）均调用失败。\n\n错误信息: {str(e)[:200]}",
                    )
            except Exception:
                pass
