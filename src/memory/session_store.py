"""
Session Store: JSONL 写入 + SQLite 索引同步。

JSONL 是 source of truth（~/.lamix/memory/sessions/YYYY-MM-DD/{session_id}.jsonl）；
SQLite search.db 是加速层（FTS5 + sessions/segments 表）。

设计文档：docs/memory-design.md
"""

from __future__ import annotations

import json
import re
import sqlite3
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from src.core.config import SKILLS_DIR, PROJECTS_DIR

import jieba

# ── 路径配置 ────────────────────────────────────────────────────────────

LAMIX_DIR = Path.home() / ".lamix"
SESSIONS_DIR = LAMIX_DIR / "memory" / "sessions"
SEARCH_DB = LAMIX_DIR / "memory" / "search.db"
TOOL_BODIES_DIR = LAMIX_DIR / "memory" / "tool_bodies"

# ── session_id → source 内存缓存（进程级别）──────────────────────────────

_sid_source_cache: dict[str, str] = {}
_sid_path_cache: dict[str, Path] = {}

# ── Schema ──────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at INTEGER NOT NULL,   -- 毫秒时间戳
    ended_at INTEGER,              -- 毫秒时间戳，session_end 时写入
    source TEXT NOT NULL DEFAULT 'cli',
    summary TEXT                   -- (deprecated, no longer used)
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    segment INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    UNIQUE(session_id, segment)
);

CREATE TABLE IF NOT EXISTS messages_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    role TEXT,
    content TEXT,                      -- jieba 预分词后的文本（空格分隔），供 FTS5 索引
    raw_json TEXT NOT NULL DEFAULT ''  -- 完整 JSONL 原始行，用于双向重建
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert
AFTER INSERT ON messages_index BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete
AFTER DELETE ON messages_index BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TABLE IF NOT EXISTS messages_embedding (
    msg_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    indexed_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages_index(session_id);
CREATE INDEX IF NOT EXISTS idx_embedding_session ON messages_embedding(session_id);
CREATE INDEX IF NOT EXISTS idx_segments_session ON segments(session_id);
"""

# ── jieba 预分词 ──────────────────────────────────────────────────────

def _jieba_cut(text: str) -> str:
    """jieba 分词后空格 join，供 FTS5 unicode61 按空格切分。"""
    if not text or not text.strip():
        return ""
    return " ".join(jieba.lcut(text))


# ── 初始化 ──────────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    LAMIX_DIR.mkdir(parents=True, exist_ok=True)


def close_orphan_sessions() -> int:
    """关闭所有未正常结束的孤儿 session（ended_at IS NULL）。

    在进程启动时调用，清理上次异常退出留下的 session。
    
    防御策略：
    1. 先执行 UPDATE ... WHERE ended_at IS NULL
    2. 验证 + WAL checkpoint 重试（防止 SQLite WAL 竞态）
    3. 扫描 JSONL 兜底（防止 SQLite 完全没记录的 session）
    
    Returns: 关闭的 session 数量。
    """
    now_ms = _now_ms()
    conn = _get_db()
    total_closed = 0
    try:
        # 第一轮：标准 UPDATE
        cursor = conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL",
            (now_ms,),
        )
        total_closed = cursor.rowcount
        conn.commit()

        # 第二轮：验证 — WAL checkpoint 后重试
        remaining = conn.execute(
            "SELECT session_id FROM sessions WHERE ended_at IS NULL"
        ).fetchall()
        if remaining:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            conn.commit()
            cursor = conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL",
                (now_ms,),
            )
            total_closed += cursor.rowcount
            conn.commit()

        # 第三轮：JSONL 兜底 — 扫描有 start 无 end 的孤儿
        jsonl_orphans = _find_jsonl_orphan_sessions()
        for sid in jsonl_orphans:
            end_row = {"ts": now_ms, "session_id": sid, "type": "session_end"}
            _jsonl_append(sid, end_row)
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
                (now_ms, sid),
            )
            total_closed += 1
        if jsonl_orphans:
            conn.commit()

        if total_closed > 0:
            import logging
            logging.getLogger(__name__).info(
                f"已关闭 {total_closed} 个孤儿 session"
                + (f"（含 {len(jsonl_orphans)} 个 JSONL 兜底）" if jsonl_orphans else "")
            )
        return total_closed
    finally:
        conn.close()


def _find_jsonl_orphan_sessions() -> list[str]:
    """扫描 JSONL 文件，找出有 session_start 但无 session_end 的孤儿 session。

    只扫描最近 2 天的文件，避免扫描全量历史。
    Returns: 孤儿 session_id 列表。
    """
    orphans: list[str] = []
    from datetime import timedelta

    check_dates = [
        date.today().isoformat(),
        (date.today() - timedelta(days=1)).isoformat(),
    ]
    for d in check_dates:
        day_dir = SESSIONS_DIR / d
        if not day_dir.exists():
            continue
        for jsonl_path in day_dir.rglob("*.jsonl"):
            try:
                has_start = False
                has_end = False
                sid = ""
                with open(jsonl_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        t = row.get("type", "")
                        if t == "session_start":
                            has_start = True
                            sid = row.get("session_id", "")
                        elif t == "session_end":
                            has_end = True
                if has_start and not has_end and sid:
                    orphans.append(sid)
            except OSError:
                continue
    return orphans




def purge_empty_sessions() -> int:
    """删除所有 0 消息的空 session（SQLite + JSONL 全清理）。

    判断标准：messages_index 中该 session 无 role='user' 或 role='assistant' 的行。
    同时删除 sessions 表记录、messages_index 中的特殊行、JSONL 文件。
    Returns: 删除的 session 数量。
    """
    conn = _get_db()
    try:
        # 找出没有实际消息的 session
        empty_ids = [
            row["session_id"]
            for row in conn.execute(
                """
                SELECT s.session_id
                FROM sessions s
                WHERE NOT EXISTS (
                    SELECT 1 FROM messages_index m
                    WHERE m.session_id = s.session_id AND m.role IN ('user', 'assistant')
                )
                """
            ).fetchall()
        ]
        if not empty_ids:
            return 0

        # 删 JSONL 文件 + 清路径缓存
        for sid in empty_ids:
            jsonl = _find_jsonl(sid)
            if jsonl and jsonl.exists():
                jsonl.unlink()
            _sid_path_cache.pop(sid, None)
            _sid_source_cache.pop(sid, None)

        placeholders = ",".join("?" for _ in empty_ids)

        # FTS5 delete trigger 在批量删时会报 SQL logic error，
        # 需要 drop trigger → 删数据 → 重建 trigger
        conn.execute("DROP TRIGGER IF EXISTS messages_fts_delete")

        # 删 FTS5 行
        conn.execute(
            f"DELETE FROM messages_fts WHERE rowid IN ("
            f"SELECT id FROM messages_index WHERE session_id IN ({placeholders}))",
            empty_ids,
        )
        # 删 messages_index（含特殊行）
        conn.execute(
            f"DELETE FROM messages_index WHERE session_id IN ({placeholders})",
            empty_ids,
        )
        # 重建 trigger
        conn.execute(
            "CREATE TRIGGER messages_fts_delete "
            "AFTER DELETE ON messages_index BEGIN "
            "INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content); "
            "END"
        )

        # 删 segments
        conn.execute(
            f"DELETE FROM segments WHERE session_id IN ({placeholders})",
            empty_ids,
        )
        # 删 sessions
        conn.execute(
            f"DELETE FROM sessions WHERE session_id IN ({placeholders})",
            empty_ids,
        )
        conn.commit()

        import logging
        logging.getLogger(__name__).info(f"已清理 {len(empty_ids)} 个空 session")
        return len(empty_ids)
    finally:
        conn.close()


def is_session_empty(session_id: str) -> bool:
    """检查 session 是否没有 user/assistant 消息（即空 session）。"""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages_index "
            "WHERE session_id = ? AND role IN ('user', 'assistant')",
            (session_id,),
        ).fetchone()
        return row["cnt"] == 0
    finally:
        conn.close()


def purge_session(session_id: str) -> bool:
    """删除单个 session（SQLite + JSONL 全清理），不判断是否为空。

    Returns: 是否成功删除。
    """
    conn = _get_db()
    try:
        # 检查 session 是否存在
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return False

        # 删 JSONL 文件 + 清路径缓存
        jsonl = _find_jsonl(session_id)
        if jsonl and jsonl.exists():
            jsonl.unlink()
        _sid_path_cache.pop(session_id, None)
        _sid_source_cache.pop(session_id, None)

        # Drop FTS5 trigger（批量操作需要）
        conn.execute("DROP TRIGGER IF EXISTS messages_fts_delete")

        # 删 FTS5 行
        conn.execute(
            "DELETE FROM messages_fts WHERE rowid IN ("
            "SELECT id FROM messages_index WHERE session_id = ?)",
            (session_id,),
        )
        # 删 messages_index（含特殊行）
        conn.execute(
            "DELETE FROM messages_index WHERE session_id = ?",
            (session_id,),
        )
        # 重建 trigger
        conn.execute(
            "CREATE TRIGGER messages_fts_delete "
            "AFTER DELETE ON messages_index BEGIN "
            "INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content); "
            "END"
        )

        # 删 segments
        conn.execute(
            "DELETE FROM segments WHERE session_id = ?",
            (session_id,),
        )
        # 删 sessions
        conn.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def _get_db() -> sqlite3.Connection:
    """获取 SQLite 连接（自动建表）。"""
    _ensure_dirs()
    db_path = SEARCH_DB
    conn = sqlite3.connect(db_path, timeout=10)
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


# ── Session 管理 ────────────────────────────────────────────────────────

@dataclass
class SessionInfo:
    session_id: str
    started_at: int          # ms timestamp
    ended_at: int | None
    source: str = "cli"


def create_session(source: str = "cli") -> SessionInfo:
    """创建新 session，写入 sessions 表和 JSONL。"""
    _ensure_dirs()
    sid = _gen_session_id()
    now_ms = _now_ms()

    info = SessionInfo(session_id=sid, started_at=now_ms, ended_at=None, source=source)

    # 缓存 source
    _sid_source_cache[sid] = source

    # 写 JSONL（指定 source 以写入正确子目录）
    start_row = {
        "ts": now_ms,
        "type": "session_start",
        "session_id": sid,
        "source": source,
    }
    _jsonl_append(sid, start_row, source=source)

    # 写 SQLite
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO sessions(session_id, started_at, source) VALUES(?, ?, ?)",
            (sid, now_ms, source),
        )
        # 特殊行入库（role=NULL, content=''，不进 FTS5）
        conn.execute(
            "INSERT INTO messages_index(session_id, ts, role, content, raw_json) VALUES(?, ?, NULL, '', ?)",
            (sid, now_ms, json.dumps(start_row, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()

    return info


def _gen_session_id() -> str:
    """生成简短可读的 session ID：HHMM-随机4位hex。

    例：2145-a3f2，按文件名排序即时间顺序。
    """
    now = datetime.now()
    time_prefix = now.strftime("%H%M")
    return f"{time_prefix}-{uuid.uuid4().hex[:4]}"


def end_session(session_id: str) -> None:
    """标记 session 结束，写入 session_end 行。"""
    now_ms = _now_ms()

    # 写 JSONL
    end_row: dict[str, Any] = {
        "ts": now_ms,
        "session_id": session_id,
        "type": "session_end",
    }
    _jsonl_append(session_id, end_row)

    # 写 SQLite
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
            (now_ms, session_id),
        )
        # 同时更新最后一个 segment 的 ended_at
        row = conn.execute(
            "SELECT id FROM segments WHERE session_id = ? ORDER BY segment DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE segments SET ended_at = ? WHERE id = ?",
                (now_ms, row["id"]),
            )
        # 特殊行入库
        conn.execute(
            "INSERT INTO messages_index(session_id, ts, role, content, raw_json) VALUES(?, ?, NULL, '', ?)",
            (session_id, now_ms, json.dumps(end_row, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def get_session(session_id: str) -> SessionInfo | None:
    """从 sessions 表读取 session 元数据。"""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT session_id, started_at, ended_at, source FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return SessionInfo(
            session_id=row["session_id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            source=row["source"],
        )
    finally:
        conn.close()


def list_recent_sessions(
    limit: int = 5,
    source: str | None = None,
    ended_only: bool = False,
) -> list[dict]:
    """列出最近的 session（包含未正常关闭的）。

    Args:
        limit: 最多返回几个 session。
        source: 按 source 过滤，为 None 则不过滤。
        ended_only: 仅返回已结束的 session（ended_at IS NOT NULL）。

    Returns:
        列表，每项包含 session_id、started_at、ended_at、message_count。
    """
    conn = _get_db()
    try:
        where_clauses = []
        params: list[Any] = []
        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if ended_only:
            where_clauses.append("s.ended_at IS NOT NULL")
        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        rows = conn.execute(
            f"""
            SELECT s.session_id, s.started_at, s.ended_at,
                   (SELECT COUNT(*) FROM messages_index m WHERE m.session_id = s.session_id AND m.role IS NOT NULL) AS msg_count
            FROM sessions s
            {where_sql}
            ORDER BY s.started_at DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()

        result = []
        for row in rows:
            result.append({
                "session_id": row["session_id"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "message_count": row["msg_count"],
            })
        return result
    finally:
        conn.close()


# ── JSONL 写入 ──────────────────────────────────────────────────────────

def append_message(
    session_id: str,
    role: Literal["user", "assistant"],
    content: str,
    tool_calls: list[dict] | None = None,
    tool_result: str | None = None,
    referenced_tool_results: list[str] | None = None,
    segment: int = 0,
    # ── trace 扩展字段（仅 assistant 行有效） ──
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    stop_reason: str | None = None,
) -> None:
    """追加一条消息到 JSONL 文件，同时更新 SQLite 索引。

    assistant 行可额外传入 model、input_tokens、output_tokens、stop_reason，
    用于完整复现（写入 JSONL 行内）。
    """
    now_ms = _now_ms()

    row: dict[str, Any] = {
        "ts": now_ms,
        "session_id": session_id,
        "segment": segment,
        "role": role,
        "content": content,
        "type": "assistant" if role == "assistant" else "user",
    }
    if tool_calls:
        row["tool_calls"] = tool_calls
    if tool_result:
        row["tool_result"] = tool_result
    if referenced_tool_results:
        row["referenced_tool_results"] = referenced_tool_results

    # trace 扩展字段（assistant 行）
    if role == "assistant":
        if model:
            row["model"] = model
        if input_tokens is not None:
            row["input_tokens"] = input_tokens
        if output_tokens is not None:
            row["output_tokens"] = output_tokens
        if stop_reason:
            row["stop_reason"] = stop_reason

    _jsonl_append(session_id, row)

    # raw_json：存完整 JSONL 行（用于双向重建）
    raw_json = json.dumps(row, ensure_ascii=False)
    # content 用 jieba 预分词（空格分隔，供 FTS5）
    segmented_content = _jieba_cut(content)

    # 更新 SQLite 索引
    conn = _get_db()
    try:
        # 防御：确保 sessions 表有这条记录（重启/异常可能丢失）
        conn.execute(
            "INSERT OR IGNORE INTO sessions(session_id, started_at, source) VALUES(?, ?, ?)",
            (session_id, now_ms, "unknown"),
        )

        # 查当前 session 的 latest segment
        latest_seg = conn.execute(
            "SELECT MAX(segment) FROM segments WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if latest_seg is None:
            # 第一次有消息，建立 segment 0
            conn.execute(
                "INSERT INTO segments(session_id, segment, started_at) VALUES(?, ?, ?)",
                (session_id, 0, now_ms),
            )

        # 写入消息索引（jieba 分词后 content + raw_json）
        conn.execute(
            "INSERT INTO messages_index(session_id, ts, role, content, raw_json) VALUES(?, ?, ?, ?, ?)",
            (session_id, now_ms, role, segmented_content, raw_json),
        )
        conn.commit()
    finally:
        conn.close()


def write_segment_boundary(
    session_id: str,
    segment: int,
    next_segment_started_at: int,
    archive: list[dict] | None = None,
) -> None:
    """
    写入 segment_boundary 行到 JSONL，并更新 segments 表的 ended_at。
    archive: [{"target": "skill:xxx", "entry_count": 3}, ...]
    """
    now_ms = _now_ms()

    row = {
        "ts": now_ms,
        "session_id": session_id,
        "segment": segment,
        "type": "segment_boundary",
        "next_segment_started_at": next_segment_started_at,
    }
    if archive:
        row["archive"] = archive

    _jsonl_append(session_id, row)

    # 更新 segments 表的 ended_at
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE segments SET ended_at = ? WHERE session_id = ? AND segment = ?",
            (now_ms, session_id, segment),
        )
        # 建立下一个 segment 的 started_at
        conn.execute(
            "INSERT OR IGNORE INTO segments(session_id, segment, started_at) VALUES(?, ?, ?)",
            (session_id, segment + 1, next_segment_started_at),
        )
        # 特殊行入库
        conn.execute(
            "INSERT INTO messages_index(session_id, ts, role, content, raw_json) VALUES(?, ?, NULL, '', ?)",
            (session_id, now_ms, json.dumps(row, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()



def get_session_messages(
    session_id: str,
    from_segment: int | None = None,
    before_ts: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """
    从 JSONL 读取 session 的消息。
    可指定 from_segment（返回指定 segment 及之后的消息）和 before_ts。
    limit: 最多返回 N 条消息（返回最后的 N 条）。
    """
    path = _find_jsonl(session_id)
    if not path:
        return []

    messages = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("type") in ("session_start", "session_end"):
                continue
            if from_segment is not None and msg.get("segment", 0) < from_segment:
                continue
            if before_ts is not None and msg.get("ts", 0) >= before_ts:
                continue
            messages.append(msg)

    if limit is not None:
        messages = messages[-limit:]

    return messages


def get_latest_segment_boundary(session_id: str) -> dict | None:
    """从 JSONL 读取最后一个 segment_boundary 行。"""
    path = _find_jsonl(session_id)
    if not path:
        return None

    boundary = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("type") == "segment_boundary":
                boundary = msg
    return boundary


# ── 内部工具 ───────────────────────────────────────────────────────────

def _get_source(session_id: str) -> str:
    """获取 session 的 source（从缓存或 SQLite）。"""
    source = _sid_source_cache.get(session_id)
    if source:
        return source
    # 从 SQLite 查
    try:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT source FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                source = row["source"]
                _sid_source_cache[session_id] = source
                return source
        finally:
            conn.close()
    except Exception:
        pass
    return "cli"


def _jsonl_path(session_id: str, source: str | None = None) -> Path:
    """根据 session_id 找 JSONL 路径，新文件按 source 分子目录。

    目录结构：sessions/YYYY-MM-DD/{source}/{session_id}.jsonl
    """
    # 路径缓存：避免每次 append 都 rglob
    cached = _sid_path_cache.get(session_id)
    if cached and cached.exists():
        return cached

    # 查找已有文件
    existing = _find_jsonl(session_id)
    if existing:
        _sid_path_cache[session_id] = existing
        return existing

    # 新文件：按 source 分目录，文件名加日期前缀
    if source is None:
        source = _get_source(session_id)
    today = date.today()
    source_dir = SESSIONS_DIR / today.isoformat() / source
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{today.isoformat()}_{session_id}.jsonl"
    _sid_path_cache[session_id] = path
    return path


def _find_jsonl(session_id: str) -> Path | None:
    """根据 session_id 找 JSONL 路径（rglob 搜所有子目录）。
    兼容新旧命名：{session_id}.jsonl 和 {date}_{session_id}.jsonl。
    """
    # 新命名：{date}_{session_id}.jsonl
    for path in SESSIONS_DIR.rglob(f"*_{session_id}.jsonl"):
        return path
    # 旧命名：{session_id}.jsonl
    for path in SESSIONS_DIR.rglob(f"{session_id}.jsonl"):
        return path
    return None


def _jsonl_append(session_id: str, row: dict, source: str | None = None) -> None:
    """追加一行到 JSONL 文件。"""
    path = _jsonl_path(session_id, source=source)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


# ── 通用追加 & Resume ──────────────────────────────────────────────────


def append(session_id: str, row: dict) -> None:
    """通用追加：写一行 JSON 到 JSONL 文件（不更新 SQLite 索引）。

    供 compaction 写 segment_boundary 等特殊行使用。
    需要同步 SQLite 的场景请用 append_message / write_segment_boundary。
    """
    _jsonl_append(session_id, row)


def load_resume_context(session_id: str) -> str | None:
    """读取最后一个 segment_boundary 的 archive 字段，加载 skill/project 内容。

    返回格式化的上下文字符串，或 None（没有可恢复的上下文）。
    """
    boundary = get_latest_segment_boundary(session_id)
    if not boundary:
        return None

    archive = boundary.get("archive")
    if not archive:
        return None

    parts: list[str] = []
    for item in archive:
        target = item.get("target", "")
        if not target:
            continue
        path = _resolve_archive_target(target)
        if path and path.exists():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"## 归档内容: {target}\n\n{content}")
            except OSError:
                pass

    if not parts:
        return None

    segment = boundary.get("segment", "?")
    header = f"# Context Resume（从 segment {segment} 恢复）\n\n以下内容来自 compaction 归档，供参考：\n"
    return header + "\n\n---\n\n".join(parts)


def _resolve_archive_target(target: str) -> Path | None:
    """将 archive target 解析为文件路径。"""
    if target.startswith("skill:"):
        return SKILLS_DIR / f"{target[6:]}.md"
    elif target.startswith("project:"):
        return PROJECTS_DIR / f"{target[8:]}.md"
    return None


# ── rebuild_index ──────────────────────────────────────────────────────

def rebuild_index(sessions_dir: Path | None = None, db_path: Path | None = None) -> None:
    """
    从 JSONL 重建 SQLite 索引。

    步骤：
    1. 加写锁
    2. 清空 sessions/segments/messages_index 表
    3. 流式解析 JSONL（逐行读取）
    4. 批量 INSERT（每 1000 条 commit 一次）
    5. 释放写锁
    """
    sessions_dir = sessions_dir or SESSIONS_DIR
    db_path = db_path or SEARCH_DB

    lock_path = db_path.with_suffix(".lock")
    if lock_path.exists():
        raise RuntimeError("索引正在重建中，请稍后再试")

    try:
        lock_path.write_text("")
        _rebuild_unsafe(sessions_dir, db_path)
    finally:
        lock_path.unlink(missing_ok=True)


def _rebuild_unsafe(sessions_dir: Path, db_path: Path) -> None:
    """无锁重建，供内部调用。"""
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)  # 确保表存在
    conn.row_factory = sqlite3.Row

    # 清空表（FTS 用 DELETE 而非 TRUNCATE）
    conn.execute("DELETE FROM messages_fts")
    conn.execute("DELETE FROM messages_index")
    conn.execute("DELETE FROM segments")
    conn.execute("DELETE FROM sessions")
    conn.commit()

    BATCH_SIZE = 1000
    batch: list[dict] = []

    for jsonl_path in _iter_jsonl_files(sessions_dir):
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                batch.append(msg)
                if len(batch) >= BATCH_SIZE:
                    _flush_batch(conn, batch)
                    batch = []

    if batch:
        _flush_batch(conn, batch)

    conn.close()


def rebuild_jsonl(db_path: Path | None = None, sessions_dir: Path | None = None) -> None:
    """从 SQLite 重建 JSONL 文件（反向重建）。

    依赖 raw_json 字段（完整 JSONL 行），按 session_id 分组，
    sessions.started_at 推算日期目录，按 ts 排序写入。
    """
    from datetime import timezone

    db_path = db_path or SEARCH_DB
    sessions_dir = sessions_dir or SESSIONS_DIR
    sessions_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 按 session_id 分组，获取 raw_json
    sessions = conn.execute(
        "SELECT session_id, started_at, source FROM sessions ORDER BY started_at"
    ).fetchall()

    for sess in sessions:
        sid = sess["session_id"]
        started_at = sess["started_at"]
        source = sess["source"] or "cli"

        # 从 started_at 推算日期目录
        dt = datetime.fromtimestamp(started_at / 1000, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
        source_dir = sessions_dir / date_str / source
        source_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = source_dir / f"{sid}.jsonl"
        rows = conn.execute(
            "SELECT raw_json FROM messages_index WHERE session_id = ? ORDER BY ts",
            (sid,),
        ).fetchall()

        with open(jsonl_path, "w", encoding="utf-8") as f:
            for row in rows:
                if row["raw_json"]:
                    f.write(row["raw_json"] + "\n")

    conn.close()


def _iter_jsonl_files(sessions_dir: Path):
    """按时间顺序遍历所有 JSONL 文件（含 source 子目录）。"""
    if not sessions_dir.exists():
        return
    for date_dir in sorted(sessions_dir.iterdir()):
        if date_dir.is_dir():
            # 兼容扁平结构和 source 子目录
            for jsonl_file in sorted(date_dir.rglob("*.jsonl")):
                yield jsonl_file


def _flush_batch(conn: sqlite3.Connection, batch: list[dict]) -> None:
    """批量写入，事务提交。"""
    pending_segment: dict | None = None  # (session_id, segment) -> started_at

    for msg in batch:
        t = msg.get("type")
        sid = msg.get("session_id", "")
        seg = msg.get("segment", 0)
        raw = json.dumps(msg, ensure_ascii=False)

        if t == "session_start":
            conn.execute(
                "INSERT OR IGNORE INTO sessions(session_id, started_at, source) VALUES(?, ?, ?)",
                (sid, msg["ts"], msg.get("source", "cli")),
            )
            # 建立 segment 0
            pending_segment = {"session_id": sid, "segment": 0, "started_at": msg["ts"]}
            # 特殊行入库
            conn.execute(
                "INSERT INTO messages_index(session_id, ts, role, content, raw_json) VALUES(?, ?, NULL, '', ?)",
                (sid, msg["ts"], raw),
            )

        elif t == "session_end":
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
                (msg["ts"], sid),
            )
            # 更新最后一个 segment
            row = conn.execute(
                "SELECT id FROM segments WHERE session_id = ? ORDER BY segment DESC LIMIT 1",
                (sid,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE segments SET ended_at = ? WHERE id = ?",
                    (msg["ts"], row["id"]),
                )
            # 特殊行入库
            conn.execute(
                "INSERT INTO messages_index(session_id, ts, role, content, raw_json) VALUES(?, ?, NULL, '', ?)",
                (sid, msg["ts"], raw),
            )

        elif t == "segment_boundary":
            # 上一 segment 结束
            conn.execute(
                "UPDATE segments SET ended_at = ? WHERE session_id = ? AND segment = ?",
                (msg["ts"], sid, seg),
            )
            # 建立下一 segment
            next_seg = seg + 1
            next_started = msg.get("next_segment_started_at", msg["ts"])
            conn.execute(
                "INSERT OR IGNORE INTO segments(session_id, segment, started_at) VALUES(?, ?, ?)",
                (sid, next_seg, next_started),
            )
            # 特殊行入库
            conn.execute(
                "INSERT INTO messages_index(session_id, ts, role, content, raw_json) VALUES(?, ?, NULL, '', ?)",
                (sid, msg["ts"], raw),
            )

        elif msg.get("role") in ("user", "assistant"):
            # 确保 segment 存在
            if pending_segment is None or pending_segment["session_id"] != sid:
                row = conn.execute(
                    "SELECT segment FROM segments WHERE session_id = ? ORDER BY segment DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                curr_seg = row["segment"] if row else 0
                if seg <= curr_seg:
                    pending_segment = {"session_id": sid, "segment": seg, "started_at": msg["ts"]}
            if pending_segment:
                try:
                    conn.execute(
                        "INSERT INTO segments(session_id, segment, started_at) VALUES(?, ?, ?)",
                        (pending_segment["session_id"], pending_segment["segment"], pending_segment["started_at"]),
                    )
                except sqlite3.IntegrityError:
                    pass  # 已存在
                pending_segment = None

            # jieba 预分词 + raw_json
            content = msg.get("content", "")
            segmented = _jieba_cut(content)
            conn.execute(
                "INSERT INTO messages_index(session_id, ts, role, content, raw_json) VALUES(?, ?, ?, ?, ?)",
                (sid, msg["ts"], msg["role"], segmented, raw),
            )

    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Trace Log（完整复现支持）
# 设计文档：docs/trace-design.md
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_tool_bodies_dir() -> Path:
    """确保 tool_bodies 目录存在并返回路径。"""
    TOOL_BODIES_DIR.mkdir(parents=True, exist_ok=True)
    return TOOL_BODIES_DIR


def _sha256_hash(content: str) -> str:
    """计算内容的 SHA256 hash，返回格式化的 hash 字符串（sha256:{前16位}）。"""
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{h[:16]}"


def append_trace(session_id: str, row: dict) -> None:
    """追加一条 trace 行到 JSONL（不更新 SQLite 索引）。

    供 system_prompt / llm_call / llm_error / tool_call / tool_result 使用。
    写入位置与该 session 的 sessions/ JSONL 相同。
    """
    _jsonl_append(session_id, row)


def write_system_prompt_trace(
    session_id: str,
    content: str,
    ts: int | None = None,
) -> dict:
    """写入 system_prompt 行，处理 hash 去重。

    如果 prompt_hash 相同则 content=null（行仍写入，省的是磁盘而非 I/O）。
    Returns: 写入的 row dict。
    """
    now_ms = ts or _now_ms()
    prompt_hash = _sha256_hash(content)

    row = {
        "ts": now_ms,
        "type": "system_prompt",
        "session_id": session_id,
        "prompt_hash": prompt_hash,
        "content": content,
    }
    append_trace(session_id, row)
    return row


def write_llm_call_trace(
    session_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
    stop_reason: str,
    ts: int | None = None,
) -> dict:
    """写入 llm_call 行（调试/计费用）。"""
    now_ms = ts or _now_ms()

    row = {
        "ts": now_ms,
        "type": "llm_call",
        "session_id": session_id,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "stop_reason": stop_reason,
    }
    append_trace(session_id, row)
    return row


def write_llm_error_trace(
    session_id: str,
    model: str,
    error_type: str,
    detail: str,
    duration_ms: int,
    ts: int | None = None,
) -> dict:
    """写入 llm_error 行。"""
    now_ms = ts or _now_ms()

    row = {
        "ts": now_ms,
        "type": "llm_error",
        "session_id": session_id,
        "model": model,
        "error_type": error_type,
        "detail": detail[:500],  # 截断到前 500 字
        "duration_ms": duration_ms,
    }
    append_trace(session_id, row)
    return row


def write_tool_call_trace(
    session_id: str,
    tool_call_id: str,
    name: str,
    arguments: dict,
    ts: int | None = None,
) -> dict:
    """写入 tool_call 行。"""
    now_ms = ts or _now_ms()

    row = {
        "ts": now_ms,
        "type": "tool_call",
        "session_id": session_id,
        "id": tool_call_id,
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),  # 序列化为字符串
    }
    append_trace(session_id, row)
    return row


def write_tool_result_trace(
    session_id: str,
    tool_call_id: str,
    result: str,
    ts: int | None = None,
    error: dict | None = None,
) -> dict:
    """写入 tool_result 行，处理去重和 inline 逻辑。

    采用「只写不检查」策略（方案 B）：
    - 直接计算 hash 并写入 tool_bodies/{hash}.json
    - 不检查文件是否已存在，避免额外 I/O
    - 返回的 row dict 含 result_ref（>2KB）或 result_inline（≤2KB）

    Args:
        session_id: session ID
        tool_call_id: 对应的 tool_call id
        result: 工具执行结果内容
        ts: 时间戳（毫秒），默认当前时间
        error: 错误信息（结构化对象 {type, message}），无错误则 None

    Returns: 写入的 row dict（含 result_ref 或 result_inline）。
    """
    now_ms = ts or _now_ms()
    size = len(result.encode("utf-8"))

    if size <= 2048:
        # 小型结果内联
        row: dict[str, Any] = {
            "ts": now_ms,
            "type": "tool_result",
            "session_id": session_id,
            "id": tool_call_id,
            "result_size": size,
            "result_inline": result if not error else None,
            "error": error,
        }
        append_trace(session_id, row)
        return row

    # 大型结果写 hash 文件
    hash_key = _sha256_hash(result)
    h = hash_key.split(":")[1]  # 提取 hash 值（去掉 sha256: 前缀）
    path = _ensure_tool_bodies_dir() / f"{h}.json"

    path.write_text(
        json.dumps({
            "hash": hash_key,
            "size": size,
            "content": result,
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "ts": now_ms,
        "type": "tool_result",
        "session_id": session_id,
        "id": tool_call_id,
        "result_size": size,
        "result_ref": hash_key,
        "error": error,
    }


# ── GC / 清理策略 ──────────────────────────────────────────────────────

def gc_tool_bodies(ttl_days: int = 60) -> dict:
    """GC tool_bodies：只用时间窗口清理，不做引用计数。

    Args:
        ttl_days: 时间窗口天数，默认 60 天

    Returns: {"deleted": int, "total_freed_bytes": int}
    """
    import time

    deleted = 0
    total_freed_bytes = 0
    cutoff_ts = time.time() - (ttl_days * 86400)  # ttl_days 天前的时间戳

    try:
        tool_bodies_dir = _ensure_tool_bodies_dir()
    except Exception:
        tool_bodies_dir = TOOL_BODIES_DIR

    if tool_bodies_dir.exists():
        for f in tool_bodies_dir.iterdir():
            if not f.is_file() or not f.name.endswith(".json"):
                continue

            try:
                # 检查是否过期
                if f.stat().st_mtime < cutoff_ts:
                    file_size = f.stat().st_size
                    f.unlink()
                    total_freed_bytes += file_size
                    deleted += 1
            except Exception:
                continue

    return {
        "deleted": deleted,
        "total_freed_bytes": total_freed_bytes,
    }
