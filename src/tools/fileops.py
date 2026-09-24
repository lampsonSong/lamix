"""文件读写工具：file_read 和 file_write，含大小限制保护。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


MAX_READ_SIZE = 100 * 1024  # 100KB

# 顶层 ~/.lamix/{info,projects,skills}/ 是历史遗留位置，凡是落到那里的写入都自动
# 改写到 ~/.lamix/memory/<name>/，兜底 LLM 猜错路径。
_LAMIX_DIR = Path.home() / ".lamix"
_LEGACY_TOPLEVEL = {"info", "projects", "skills"}


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def _redirect_legacy_lamix_path(p: Path) -> Path:
    """若目标位于 ~/.lamix/{info,projects,skills}/... 顶层，改写到 memory/ 子目录。

    memory/ 前缀已经在了则原样返回。返回的路径可能仍指向别处（如非 lamix 目录）。
    """
    try:
        rel = p.relative_to(_LAMIX_DIR)
    except ValueError:
        return p
    parts = rel.parts
    if not parts or parts[0] not in _LEGACY_TOPLEVEL:
        return p
    redirected = _LAMIX_DIR / "memory" / rel
    logger.info("[fileops] path corrected: %s -> %s", p, redirected)
    return redirected


def file_read(path: str, offset: int = 0, limit: int | None = None) -> str:
    """读取文件内容，支持行偏移和行数限制。"""
    p = _expand(path)
    if not p.exists():
        return f"[错误] 文件不存在：{path}"
    if not p.is_file():
        return f"[错误] 路径不是文件：{path}"

    size = p.stat().st_size
    if size > MAX_READ_SIZE:
        return (
            f"[拒绝] 文件过大（{size // 1024}KB > 100KB），请缩小范围后重试。"
            f"\n提示：可使用 offset 和 limit 参数读取部分内容。"
        )

    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"[错误] 读取文件失败：{e}"

    if offset:
        lines = lines[offset:]
    if limit is not None:
        lines = lines[:limit]

    return "\n".join(lines)


def file_write(path: str, content: str) -> str:
    """写入文件，自动创建父目录。

    ~/.lamix/{info,projects,skills}/ 顶层写入会自动改写到 memory/ 子目录。
    """
    p = _redirect_legacy_lamix_path(_expand(path))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[成功] 已写入 {p}（{len(content.encode())} 字节）"
    except Exception as e:
        return f"[错误] 写入文件失败：{e}"


FILE_READ_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "file_read",
        "description": "读取本地文件内容，支持通过 offset 和 limit 读取部分内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径，支持 ~ 展开",
                },
                "offset": {
                    "type": "integer",
                    "description": "从第几行开始读取（0 表示从头），默认 0",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取多少行，不填则读取全部",
                },
            },
            "required": ["path"],
        },
    },
}

FILE_WRITE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "file_write",
        "description": "将内容写入本地文件，文件不存在则创建，已存在则覆盖。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目标文件路径，支持 ~ 展开",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文本内容",
                },
            },
            "required": ["path", "content"],
        },
    },
}


def run_file_read(params: dict[str, Any]) -> str:
    path = params.get("path", "")
    if not path:
        return "[错误] path 参数不能为空"
    offset = int(params.get("offset", 0))
    limit = params.get("limit")
    limit = int(limit) if limit is not None else None
    return file_read(path, offset=offset, limit=limit)


def run_file_write(params: dict[str, Any]) -> str:
    path = params.get("path", "")
    content = params.get("content", "")
    if not path:
        return "[错误] path 参数不能为空"
    return file_write(path, content)
