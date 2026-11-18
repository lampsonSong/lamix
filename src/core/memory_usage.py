"""记忆层「真实使用信号」sidecar 存储（2026-11-18 新增）。

背景
----
projects / info 的归档判据是「30 天未使用」。但承载这个判据的 last_used_at
会被**写入事件**污染：审计自动补 frontmatter 时填当天、记忆库治理战役整篇重写
时也带当天日期，于是每轮战役都把计时器清零，30 天线从未真正触发——2026-11-18
实测 326 个 project 的 last_used_at 无一超过 23 天，0 个被自动归档。

对策
----
把「被读取过」这件事收到 sidecar（<memory_dir>/usage.json）：只有真正读到内容
的路径（load_project_context / load_info）才写入，而写入路径（file_write 整篇
重写）碰不到它 → 治理战役不再重置计时器。

存量文件首次被审计看到时按「历史基线」入库一次（取 max(last_used_at,
created_at, mtime)），保持原有时间线：既不提前归档，也不延后。

信号语义（projects / info 两层一致）
----
- last_read_at：内容被真实读取的日期（sidecar 唯一权威来源）
- 归档判据：last_read_at 距锚点日超过 30 天 → 归档
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path

# 同进程内的写锁——daemon 单进程，但审计任务与对话线程可能并发
_LOCK = threading.Lock()

USAGE_FILENAME = "usage.json"

# 参与使用信号的层（skills 的调用计数是另一套语义，不在此列）
LAYERS = ("projects", "info")


def store_path(memory_dir: Path) -> Path:
    """sidecar 路径：<memory_dir>/usage.json（生产为 ~/.lamix/memory/usage.json）。"""
    return Path(memory_dir) / USAGE_FILENAME


def _read_store(path: Path) -> dict[str, dict[str, str]]:
    """读取 sidecar；损坏/缺失一律按空库处理（不阻断主流程）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for layer, entries in data.items():
        if isinstance(entries, dict):
            cleaned = {str(k): str(v)[:10] for k, v in entries.items() if str(v or "").strip()}
            if cleaned:
                out[str(layer)] = cleaned
    return out


def _write_store(path: Path, store: dict[str, dict[str, str]]) -> None:
    """原子写 sidecar（先写临时文件再 replace，避免半截文件被并发读成坏 JSON）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(store, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass


def mark_read(memory_dir: Path, layer: str, name: str, date_str: str | None = None) -> None:
    """记录一次真实读取（只有读取路径调用，写入路径不得调用）。"""
    if layer not in LAYERS or not name:
        return
    stamp = (date_str or date.today().isoformat())[:10]
    path = store_path(memory_dir)
    with _LOCK:
        store = _read_store(path)
        store.setdefault(layer, {})[name] = stamp
        _write_store(path, store)


def get_read_at(memory_dir: Path, layer: str, name: str) -> date | None:
    """取某个条目的末次真实读取日期；没有记录返回 None。"""
    if not name:
        return None
    store = _read_store(store_path(memory_dir))
    raw = store.get(layer, {}).get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def seed_baseline(memory_dir: Path, layer: str, name: str, date_str: str) -> bool:
    """首次见到某条目时按历史基线入库（已有记录不覆盖）。

    Returns:
        True = 本次写入；False = 已有记录或参数无效，未改动。
    """
    if layer not in LAYERS or not name or not date_str:
        return False
    stamp = str(date_str)[:10]
    path = store_path(memory_dir)
    with _LOCK:
        store = _read_store(path)
        entries = store.setdefault(layer, {})
        if name in entries:
            return False
        entries[name] = stamp
        _write_store(path, store)
        return True


def forget(memory_dir: Path, layer: str, name: str) -> None:
    """条目被归档后清掉 sidecar 记录（保持 sidecar 只反映在册条目）。"""
    if layer not in LAYERS or not name:
        return
    path = store_path(memory_dir)
    with _LOCK:
        store = _read_store(path)
        entries = store.get(layer)
        if not entries or name not in entries:
            return
        entries.pop(name, None)
        if not entries:
            store.pop(layer, None)
        _write_store(path, store)