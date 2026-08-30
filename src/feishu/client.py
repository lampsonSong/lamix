"""飞书客户端：直接调用飞书开放平台 REST API，支持发送和读取消息。

认证方式：app_id + app_secret 获取 tenant_access_token（有效期 2 小时，自动刷新）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx


FEISHU_BASE = "https://open.feishu.cn/open-apis"
from src.core.constants import FEISHU_TOKEN_TTL as TOKEN_TTL

_logger = logging.getLogger(__name__)


class FeishuClient:
    """封装飞书 API 调用，自动管理 access token。"""

    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        # trust_env=False: 不读取 HTTP_PROXY/HTTPS_PROXY 环境变量。
        # Windows 系统代理设置（127.0.0.1:7897）若未启动服务会让 httpx 卡死，
        # 飞书直连即可，禁用代理读取避免阻塞。
        self._http = httpx.Client(timeout=15.0, trust_env=False)

    def _get_token(self) -> str:
        """获取 tenant_access_token，过期自动刷新。"""
        if self._token and time.time() < self._token_expires_at:
            return self._token

        resp = self._http.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书 token 获取失败：{data.get('msg')}")

        self._token = data["tenant_access_token"]
        self._token_expires_at = time.time() + TOKEN_TTL
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def send_message(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "user_id",
    ) -> dict[str, Any]:
        """发送文本消息到指定用户或群。

        Args:
            receive_id: 接收者 ID（user_id / open_id / chat_id 等）
            text: 消息文本内容
            receive_id_type: ID 类型，默认 user_id
        """
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        resp = self._http.post(
            f"{FEISHU_BASE}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers=self._headers(),
            json=payload,
        )
        # 无论成功失败都打日志，方便排查 400 等错误
        _logger.info(f"飞书文本发送 status={resp.status_code} receive_id={receive_id} type={receive_id_type}")
        if resp.status_code >= 400:
            _logger.error(f"飞书文本发送失败 status={resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书发送消息失败：{data.get('msg')} (code={data.get('code')})")
        return data

    def send_card(
        self,
        receive_id: str,
        card: dict[str, Any],
        receive_id_type: str = "user_id",
    ) -> dict[str, Any]:
        """发送卡片消息到指定用户或群。

        Args:
            receive_id: 接收者 ID
            card: 卡片内容字典，参考飞书卡片格式
            receive_id_type: ID 类型，默认 user_id
        """
        card_json = json.dumps(card, ensure_ascii=False)
        # 飞书 interactive 消息 content 上限约 30KB，超出会 400
        MAX_CONTENT_LEN = 28000
        if len(card_json) > MAX_CONTENT_LEN:
            _logger.warning(f"卡片内容过长 ({len(card_json)} chars)，截断到 {MAX_CONTENT_LEN}")
            card_json = card_json[:MAX_CONTENT_LEN]
        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": card_json,
        }
        resp = self._http.post(
            f"{FEISHU_BASE}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers=self._headers(),
            json=payload,
        )
        _logger.info(f"飞书卡片发送 status={resp.status_code} receive_id={receive_id} type={receive_id_type}")
        if resp.status_code >= 400:
            _logger.error(f"飞书卡片发送失败 status={resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书发送卡片失败：{data.get('msg')} (code={data.get('code')})")
        return data

    def update_message(
        self,
        message_id: str,
        card: dict[str, Any],
    ) -> dict[str, Any]:
        """更新已发送的卡片消息内容（飞书 PATCH API）。"""
        resp = self._http.patch(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}",
            headers=self._headers(),
            json={"content": json.dumps(card, ensure_ascii=False)},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书更新消息失败：{data.get('msg')} (code={data.get('code')})")
        return data

    def build_card(
        self,
        title: str,
        header: Optional[dict[str, str]] = None,
        elements: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """构建一个简单的卡片消息。"""
        card: dict[str, Any] = {
            "schema": "2.0",
            "body": {"elements": elements or []},
        }
        if header:
            card["header"] = {
                "title": {"tag": "plain_text", "content": header.get("title", "")},
                "subtitle": {"tag": "plain_text", "content": header.get("subtitle", "")},
                "template": header.get("template", "blue"),
            }
        return card

    def build_table_card(
        self,
        title: str,
        columns: list[dict[str, Any]],
        rows: list[list[str]],
        header_template: str = "blue",
    ) -> dict[str, Any]:
        """构建带表格的卡片消息。"""
        elements: list[dict[str, Any]] = [
            {
                "tag": "table",
                "columns": [{"title": col.get("title", ""), "width": col.get("width", "auto")} for col in columns],
                "fields": [
                    {"value": {"tag": "plain_text", "content": cell}, "is_short": True}
                    for row in rows
                    for cell in row
                ],
            }
        ]
        card = self.build_card(title=title, elements=elements)
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": header_template,
        }
        return card

    def build_form_card(
        self,
        title: str,
        fields: list[dict[str, str]],
        header_template: str = "blue",
    ) -> dict[str, Any]:
        """构建带表单布局的卡片消息（每行两个字段，适合展示键值对）。"""
        elements: list[dict[str, Any]] = [
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": i % 2 == 0,
                        "value": {
                            "tag": "lark_md",
                            "content": f"**{f.get('label', '')}**\n{f.get('value', '')}",
                        },
                    }
                    for i, f in enumerate(fields)
                ],
            }
        ]
        card = self.build_card(title=title, elements=elements)
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": header_template,
        }
        return card

    def build_md_card(
        self,
        title: str,
        content: str,
        header_template: str = "blue",
    ) -> dict[str, Any]:
        """构建纯 Markdown 内容的卡片消息。"""
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": content}
        ]
        card = self.build_card(title=title, elements=elements)
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": header_template,
        }
        return card

    def get_messages(
        self,
        container_id: str,
        container_id_type: str = "chat",
        page_size: int = 10,
    ) -> list[dict[str, Any]]:
        """从指定会话拉取最近消息列表（轮询方式）。"""
        resp = self._http.get(
            f"{FEISHU_BASE}/im/v1/messages",
            params={
                "container_id_type": container_id_type,
                "container_id": container_id,
                "page_size": page_size,
                "sort_type": "ByCreateTimeDesc",
            },
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书读取消息失败：{data.get('msg')} (code={data.get('code')})")
        items = data.get("data", {}).get("items", [])
        return items

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        """获取单条消息详情。

        Returns:
            消息 dict（含 body/content 等）或 None（消息不存在或无权限）。
        """
        resp = self._http.get(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.warning(f"[feishu] 获取消息 {message_id} 失败: {data.get('msg')} (code={data.get('code')})")
            return None
        return data.get("data", {})

    def get_bot_info(self) -> dict[str, Any]:
        """获取机器人自身信息，用于测试连接是否正常。"""
        resp = self._http.get(
            f"{FEISHU_BASE}/bot/v3/info",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._http.close()


# ─── 全局单例（由 CLI 初始化后注入） ─────────────────────────────────────────

_client: Optional[FeishuClient] = None


def init_client(app_id: str, app_secret: str) -> None:
    """初始化全局飞书客户端。"""
    global _client
    _client = FeishuClient(app_id=app_id, app_secret=app_secret)


def get_client() -> FeishuClient:
    if _client is None:
        raise RuntimeError("飞书客户端未初始化，请先在配置中填写 feishu.app_id 和 feishu.app_secret。")
    return _client



# ─── 工具函数（供 tools.py 注册） ────────────────────────────────────────────

def _detect_id_type(receive_id: str, explicit_type: str | None = None) -> str:
    """根据 ID 前缀自动判断类型，显式指定时优先用显式值。"""
    if explicit_type and explicit_type != "user_id":
        return explicit_type
    rid = receive_id.strip()
    if rid.startswith("ou_"):
        return "open_id"
    if rid.startswith("oc_"):
        return "chat_id"
    if "@" in rid:
        return "email"
    if rid.startswith("on_"):
        return "union_id"
    return explicit_type or "open_id"


def tool_feishu_send(params: dict[str, Any]) -> str:
    """统一飞书发送工具：msg_type='text' 发文本，msg_type='card' 发卡片。"""
    receive_id = params.get("receive_id", "").strip()
    msg_type = params.get("msg_type", "text")
    receive_id_type = _detect_id_type(receive_id, params.get("receive_id_type"))

    if not receive_id:
        return "[错误] 缺少 receive_id 参数。"

    try:
        client = get_client()

        if msg_type == "text":
            text = params.get("text", "").strip()
            if not text:
                return "[错误] 消息内容不能为空。"
            _logger.info(f"[tool] feishu_send(text): receive_id={receive_id}")
            client.send_message(receive_id=receive_id, text=text, receive_id_type=receive_id_type)
            return f"消息已发送给 {receive_id}：{text}"

        elif msg_type == "card":
            card_type = params.get("card_type", "form")
            title = params.get("title", "信息")
            header_template = params.get("header_template", "blue")
            fields = params.get("fields", [])
            content = params.get("content", "")
            _logger.info(f"[tool] feishu_send(card): receive_id={receive_id}, card_type={card_type}")

            if card_type == "form":
                card = client.build_form_card(title=title, fields=fields, header_template=header_template)
            elif card_type == "md":
                card = client.build_md_card(title=title, content=content, header_template=header_template)
            else:
                return f"[错误] 不支持的 card_type: {card_type}，支持: form, md"

            client.send_card(receive_id=receive_id, card=card, receive_id_type=receive_id_type)
            return f"卡片已发送给 {receive_id}：{title}"

        else:
            return f"[错误] 不支持的 msg_type: {msg_type}，支持: text, card"

    except RuntimeError as e:
        return f"[飞书错误] {e}"
    except httpx.HTTPError as e:
        return f"[网络错误] {e}"


def tool_feishu_read(params: dict[str, Any]) -> str:
    """工具实现：读取飞书消息。"""
    container_id = params.get("container_id", "").strip()
    container_id_type = params.get("container_id_type", "chat")
    page_size = int(params.get("page_size", 5))

    if not container_id:
        return "[错误] 缺少 container_id 参数（会话/群 ID）。"

    try:
        client = get_client()
        items = client.get_messages(
            container_id=container_id,
            container_id_type=container_id_type,
            page_size=page_size,
        )
        if not items:
            return "没有找到消息。"

        lines = [f"最近 {len(items)} 条消息："]
        for item in items:
            sender = item.get("sender", {}).get("sender_id", {}).get("user_id", "unknown")
            create_time = item.get("create_time", "")
            body = item.get("body", {})
            content = body.get("content", "")
            try:
                parsed = json.loads(content)
                text = parsed.get("text", content)
            except Exception:
                text = content
            lines.append(f"  [{create_time}] {sender}: {text}")

        return chr(10).join(lines)
    except RuntimeError as e:
        return f"[飞书错误] {e}"
    except httpx.HTTPError as e:
        return f"[网络错误] {e}"


# ─── Tool Schema ──────────────────────────────────────────────────────────────

FEISHU_SEND_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "feishu_send",
        "description": (
            "发送飞书消息给指定用户或群。msg_type='text' 发文本消息，msg_type='card' 发卡片消息。"),
        "parameters": {
            "type": "object",
            "properties": {
                "receive_id": {
                    "type": "string",
                    "description": "接收者的 user_id、open_id 或 chat_id",
                },
                "msg_type": {
                    "type": "string",
                    "enum": ["text", "card"],
                    "description": "消息类型：text 文本消息，card 卡片消息",
                },
                "text": {
                    "type": "string",
                    "description": "文本消息内容（msg_type='text' 时使用）",
                },
                "receive_id_type": {
                    "type": "string",
                    "description": "ID 类型：user_id / open_id / chat_id，默认 user_id",
                    "enum": ["user_id", "open_id", "union_id", "email", "chat_id"],
                },
                "card_type": {
                    "type": "string",
                    "description": "卡片类型（msg_type='card' 时使用）：form(表单) / md(Markdown)",
                    "enum": ["form", "md"],
                },
                "title": {
                    "type": "string",
                    "description": "卡片标题（msg_type='card' 时使用）",
                },
                "header_template": {
                    "type": "string",
                    "description": "卡片表头颜色（msg_type='card' 时使用）：blue / red / yellow / green / purple / orange / grey",
                },
                "fields": {
                    "type": "array",
                    "description": "表单字段列表（card_type='form' 时使用），每个字段包含 label 和 value",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                    },
                },
                "content": {
                    "type": "string",
                    "description": "Markdown 内容（card_type='md' 时使用）",
                },
            },
            "required": ["receive_id", "msg_type"],
        },
    },
}

FEISHU_READ_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "feishu_read",
        "description": "读取指定飞书会话的最近消息",
        "parameters": {
            "type": "object",
            "properties": {
                "container_id": {
                    "type": "string",
                    "description": "会话 ID（chat_id）",
                },
                "container_id_type": {
                    "type": "string",
                    "description": "会话 ID 类型，默认 chat",
                    "enum": ["chat"],
                },
                "page_size": {
                    "type": "integer",
                    "description": "拉取条数，默认 5，最大 50",
                },
            },
            "required": ["container_id"],
        },
    },
}
