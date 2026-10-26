"""视觉分析工具：通过 GLM-4.6V 分析截图。

支持两种输入方式：
1. image_path: 传入图片文件路径（推荐，由 Python 层面处理读取和压缩）
2. image_base64: 传入 base64 编码（向后兼容，不推荐大图片使用）
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import httpx
import yaml
from PIL import Image

from src.core.config import get_model_name


def _load_vision_config() -> dict[str, Any]:
    """从 ~/.lamix/config.yaml 加载 vision 配置段。"""
    config_path = Path.home() / ".lamix" / "config.yaml"

    if not config_path.exists():
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        vision = config.get("vision", {})
        if not isinstance(vision, dict):
            return {}
        return vision
    except Exception:
        return {}


def _get_config_value(key: str, default: Any) -> Any:
    """获取 vision 配置项，如果不存在则返回默认值。"""
    vision_config = _load_vision_config()
    return vision_config.get(key, default)


def _get_model_name(default: Any = "") -> Any:
    """获取 vision 模型标识。

    规范键是 ``name``，``model`` 作为兼容别名照收（name 优先，冲突时以 name 为准）。
    """
    vision_config = _load_vision_config()
    name = get_model_name(vision_config, "vision", warn_on_conflict=True)
    return name or default


# 从配置文件读取，提供合理默认值
API_KEY = _get_config_value("api_key", "")
API_URL = _get_config_value("base_url", "")
MODEL = _get_model_name("")
TIMEOUT = _get_config_value("timeout", 60)
MAX_BASE64_LENGTH = _get_config_value("max_base64_length", 4_000_000)


def _load_and_compress_file(image_path: str, max_edge: int = 1920) -> str:
    """从文件路径读取图片，压缩后返回 base64 编码。

    Args:
        image_path: 图片文件路径（支持 PNG、JPEG 等格式）
        max_edge: 长边最大像素数，超过会等比缩放

    Returns:
        压缩后的 JPEG base64 字符串
    """
    path = Path(image_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"图片文件不存在: {path}")

    img = Image.open(path)

    # 缩小到不超过 max_edge 长边
    w, h = img.size
    if max(w, h) > max_edge:
        ratio = max_edge / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

    buf = io.BytesIO()
    img = img.convert("RGB")
    buf.seek(0)
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def _compress_image(image_base64: str, max_length: int = MAX_BASE64_LENGTH) -> str:
    """如果 base64 过大，解码后压缩为 JPEG 再重新编码。"""
    if len(image_base64) <= max_length:
        return image_base64

    raw = base64.b64decode(image_base64)
    img = Image.open(io.BytesIO(raw))

    # 缩小到不超过 1920px 长边
    max_edge = 1920
    w, h = img.size
    if max(w, h) > max_edge:
        ratio = max_edge / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

    buf = io.BytesIO()
    img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def analyze_image(image_base64: str, prompt: str = "描述这张图片的内容") -> str:
    """用视觉模型分析图片。

    Args:
        image_base64: 图片的 base64 编码（不含 data:image/... 前缀）
        prompt: 对图片的提问

    Returns:
        模型的文字回复
    """
    if not API_KEY:
        return (
            "[配置错误] 未配置 vision API key。\n"
            "请在 ~/.lamix/config.yaml 中添加 vision 配置段：\n"
            "vision:\n"
            "  api_key: your_api_key_here\n"
            "  name: your-vision-model\n"
            "  base_url: https://your-vision-provider.com/v1/\n"
            "  timeout: 60\n"
            "  max_base64_length: 4000000"
        )

    try:
        image_base64 = _compress_image(image_base64)

        resp = httpx.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_base64}"
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 1024,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"].get("content", "")
        return f"[错误] API 返回异常：{resp.text[:200]}"
    except httpx.TimeoutException:
        return "[超时] 视觉分析请求超时"
    except Exception as e:
        return f"[错误] 视觉分析失败：{e}"


SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "vision_analyze",
        "description": (
            "用视觉模型分析一张图片。推荐传入 image_path（文件路径），"
            "由 Python 层面处理读取和压缩。也可传入 image_base64（向后兼容，"
            "大图片不推荐）。image_path 和 image_base64 二选一。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "图片文件路径，如 ~/.lamix/screenshots/screenshot_xxx.png（推荐）",
                },
                "image_base64": {
                    "type": "string",
                    "description": "图片的 base64 编码字符串（不含 data:image/... 前缀）。大图片请改用 image_path。",
                },
                "prompt": {
                    "type": "string",
                    "description": "对图片的提问，例如 '屏幕上有哪些按钮？' 或 '描述当前桌面'",
                    "default": "描述这张图片的内容",
                },
            },
        },
    },
}


def run(params: dict[str, Any]) -> str:
    image_path = params.get("image_path", "")
    image_base64 = params.get("image_base64", "")
    prompt = params.get("prompt", "描述这张图片的内容")

    if image_path:
        try:
            image_base64 = _load_and_compress_file(image_path)
        except FileNotFoundError as e:
            return f"[错误] {e}"
        except Exception as e:
            return f"[错误] 读取图片文件失败：{e}"
    elif not image_base64:
        return "[错误] 必须提供 image_path 或 image_base64"

    return analyze_image(image_base64, prompt)
