"""Skill / Project 语义索引：JSONL 存储、增量更新、远程 Embedding API + 关键词降级。"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import openai
import yaml

logger = logging.getLogger(__name__)

from src.core.skills_tools import _parse_skill

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_WORD_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)


@dataclass
class _EmbeddingClient:
    """远程 Embedding API 客户端（OpenAI 兼容接口）。"""

    provider: str
    model: str
    api_key: str
    base_url: str
    _client: openai.OpenAI | None = field(default=None, repr=False)

    def _get_client(self) -> openai.OpenAI:
        if self._client is None:
            self._client = openai.OpenAI(
                api_key=self.api_key, base_url=self.base_url
            )
        return self._client

    def embed(self, text: str) -> list[float]:
        """单条文本 embedding，失败返回空列表。"""
        if not text.strip() or not self.api_key or not self.base_url:
            return []
        try:
            resp = self._get_client().embeddings.create(
                model=self.model, input=[text]
            )
            return [float(x) for x in resp.data[0].embedding]
        except Exception as ex:
            logger.warning("Embedding API failed for provider=%s: %s", self.provider, ex)
            return []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding，失败逐条降级重试，最终仍失败返回空列表。"""
        if not texts or not self.api_key or not self.base_url:
            return [[] for _ in texts]
        # 过滤空文本
        results: list[list[float]] = []
        non_empty_indices: list[int] = []
        non_empty_texts: list[str] = []
        for i, t in enumerate(texts):
            if t.strip():
                non_empty_indices.append(i)
                non_empty_texts.append(t)
            results.append([])
        if not non_empty_texts:
            return results
        try:
            resp = self._get_client().embeddings.create(
                model=self.model, input=non_empty_texts
            )
            for idx, data in zip(non_empty_indices, resp.data):
                results[idx] = [float(x) for x in data.embedding]
            return results
        except Exception as ex:
            logger.warning(
                "Batch embedding failed for provider=%s, falling back to single: %s",
                self.provider, ex,
            )
            # 逐条降级
            for idx, text in zip(non_empty_indices, non_empty_texts):
                results[idx] = self.embed(text)
            return results


def _parse_project_body(content: str) -> str:
    """去掉 YAML frontmatter 后取正文，用于项目摘要与 embedding 文本。"""
    match = _FRONTMATTER_RE.match(content)
    if match:
        return content[match.end() :].strip()
    return content.strip()


def _project_preview_text(name: str, content: str) -> str:
    body = _parse_project_body(content)
    snippet = body[:200] if body else ""
    return f"{name} {snippet}".strip()


def _extract_description(content: str) -> str:
    """从项目 md 中提取一句话描述。
    1. 找第一个 **key**: value 行（如 "名称: xxx"）
    2. 取第一个 # 标题文本
    3. fallback 取第一个非空非表格非分隔线行
    """
    body = _parse_project_body(content)
    # 第一轮：找 **key**: value 行
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r"^-\s+\*\*(.+?)\*\*:\s*(.+?)$", line)
        if m:
            val = m.group(2).strip()
            if len(val) > 120:
                val = val[:120] + '...'
            return val
    # 第二轮：取第一个 # 标题文本（去掉 # 前缀）
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            title = stripped.lstrip('#').strip()
            if title:
                return title
    # 第三轮：任意非空非表格非分隔线行
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith('|') or line == '---':
            continue
        # 去掉列表前缀
        line = re.sub(r"^[-*]\s+", "", line)
        # 去掉粗体标记
        line = re.sub(r"\*\*(.+?)\*\*", r"", line)
        if len(line) > 120:
            line = line[:120] + '...'
        return line
    return ""



def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _keyword_match_score(query: str, text: str) -> float:
    if not query.strip() or not text:
        return 0.0
    q = query.lower().strip()
    t = text.lower()
    if q in t:
        return 1.0
    words = _WORD_RE.findall(q)
    if not words:
        return 0.0
    return sum(1.0 for w in words if w in t) / len(words)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
        except OSError:
            pass


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _iter_skill_paths(skills_dir: Path) -> list[Path]:
    """扫描 skills 目录，返回所有 SKILL.md 文件路径。
    
    双层优先：skills/<name>/SKILL.md
    向后兼容：skills/<name>.md（平铺单文件）
    """
    if not skills_dir.exists():
        return []
    
    paths: list[Path] = []
    seen: set[str] = set()  # 避免重复
    
    # 优先：双层目录结构 skills/<name>/SKILL.md
    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        if skill_dir.name.startswith("."):
            continue
        entry_file = skill_dir / "SKILL.md"
        if entry_file.is_file():
            key = str(entry_file.resolve())
            if key not in seen:
                paths.append(entry_file)
                seen.add(key)
    
    # 向后兼容：平铺单文件 skills/<name>.md
    for md_file in skills_dir.rglob("*.md"):
        if md_file.name.startswith("."):
            continue
        # 如果同名目录已存在，跳过（双层优先）
        dir_path = skills_dir / md_file.stem
        if dir_path.is_dir():
            continue
        key = str(md_file.resolve())
        if key not in seen:
            paths.append(md_file)
            seen.add(key)
    
    return sorted(paths, key=lambda p: p.name)


def _skill_search_text(parsed: dict[str, Any]) -> str:
    name = str(parsed.get("name", ""))
    desc = str(parsed.get("description", ""))
    tr = parsed.get("triggers", [])
    if isinstance(tr, str):
        parts = [tr] if tr.strip() else []
    else:
        parts = [str(x) for x in tr] if isinstance(tr, list) else []
    return f"{name} {desc} {' '.join(parts)}".strip()


def _skill_created_and_invocation(skill_file: Path) -> tuple[str | None, int]:
    raw = _read_text_file(skill_file)
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return None, 0
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None, 0
    ca = meta.get("created_at")
    ca_str = str(ca)[:10] if ca is not None and str(ca).strip() else None
    ic_raw = meta.get("invocation_count", 0)
    try:
        ic = int(ic_raw)
    except (TypeError, ValueError):
        ic = 0
    return ca_str, ic


def _category_for_skill(skills_dir: Path, skill_file: Path) -> str:
    try:
        rel = skill_file.relative_to(skills_dir)
        prts = rel.parts
        if len(prts) >= 2:
            return prts[-2]
    except ValueError:
        pass
    return "general"


class SkillIndex:
    """Skill 索引管理器（仅元数据 + 关键词检索，不存 embedding）。"""

    def __init__(
        self,
        skills_dir: Path,
        index_dir: Path,
        embedding_config: dict[str, str] | None = None,
        skills_management: dict[str, Any] | None = None,
    ) -> None:
        _ = embedding_config  # Skill 索引不使用 embedding
        self.skills_dir = skills_dir
        self.index_dir = index_dir
        self._skills_management = skills_management
        self._entries: list[dict[str, Any]] = []
        self._by_path: dict[str, dict[str, Any]] = {}

    def _cleanup_config(self) -> dict[str, int]:
        if self._skills_management is not None:
            from src.core.config import get_skills_management_config

            return get_skills_management_config(
                {"skills_management": self._skills_management}
            )
        from src.core.config import get_skills_management_config, load_config

        return get_skills_management_config(load_config())

    def _maybe_cleanup(self) -> None:
        """归档逻辑已移到 self_audit.cleanup_stale_knowledge，这里不再处理。"""
        return
        # 以下代码保留但不会执行
        cfg = self._cleanup_config()
        max_skills = cfg["cleanup_max_skills"]
        if len(self._entries) < max_skills:
            return
        age_days = cfg["cleanup_age_days"]
        min_inv = cfg["cleanup_min_invocations"]
        cutoff = (datetime.now() - timedelta(days=age_days)).date().isoformat()
        to_archive: list[dict[str, Any]] = []
        for e in self._entries:
            ca = e.get("created_at")
            if not ca:
                continue
            ca_str = str(ca)[:10]
            if ca_str > cutoff:
                continue
            try:
                inv_int = int(e.get("invocation_count", 0))
            except (TypeError, ValueError):
                inv_int = 0
            if inv_int <= min_inv:
                to_archive.append(e)
        if not to_archive:
            return
        archive_dir = self.skills_dir / ".archived"
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            logger.warning("Could not create archive dir %s: %s", archive_dir, ex)
            return
        archived_keys: set[str] = set()
        archived_names: list[str] = []
        for e in to_archive:
            pkey = str(e.get("path", ""))
            skill_md = Path(pkey)
            if not skill_md.is_file():
                continue
            dest = archive_dir / skill_md.name
            try:
                if dest.exists():
                    logger.warning("Skip archive, destination exists: %s", dest)
                    continue
                shutil.move(str(skill_md), str(dest))
                archived_keys.add(pkey)
                archived_names.append(str(e.get("name", skill_md.stem)))
            except OSError as ex:
                logger.warning("Archive failed for %s: %s", skill_dir, ex)
        if not archived_keys:
            return
        self._entries = [x for x in self._entries if str(x.get("path", "")) not in archived_keys]
        self._by_path = {r["path"]: r for r in self._entries}
        out_path = self.index_dir / "skills.jsonl"
        _write_jsonl(out_path, self._entries)
        logger.info("Archived %d cold skill(s): %s", len(archived_names), archived_names)

    def load_or_build(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        path = self.index_dir / "skills.jsonl"
        old_rows = {str(r.get("path", "")): r for r in _read_jsonl(path) if r.get("path")}

        skill_files = _iter_skill_paths(self.skills_dir)
        current_paths: set[str] = set()
        new_rows: list[dict[str, Any]] = []

        for sf in skill_files:
            if ".archived" in sf.parts:
                continue
            key = str(sf.resolve())
            current_paths.add(key)
            try:
                mtime = sf.stat().st_mtime
            except OSError:
                continue
            prev = old_rows.get(key)
            if prev and abs(float(prev.get("mtime", 0)) - mtime) < 1e-6:
                row = {k: v for k, v in prev.items() if k != "embedding"}
                if not row.get("search_text"):
                    parsed0 = _parse_skill(sf)
                    if parsed0:
                        row = {**row, "search_text": _skill_search_text(parsed0)}
                if "created_at" not in row or "invocation_count" not in row:
                    ca, inv = _skill_created_and_invocation(sf)
                    row = {**row, "created_at": ca, "invocation_count": inv}
                new_rows.append(row)
                continue
            parsed = _parse_skill(sf)
            if not parsed:
                continue
            stext = _skill_search_text(parsed)
            ca, inv = _skill_created_and_invocation(sf)
            new_rows.append(
                {
                    "name": parsed["name"],
                    "category": _category_for_skill(self.skills_dir, sf),
                    "description": parsed.get("description", ""),
                    "triggers": list(parsed.get("triggers", [])),
                    "path": key,
                    "mtime": mtime,
                    "search_text": stext,
                    "created_at": ca,
                    "invocation_count": inv,
                }
            )

        new_rows = [r for r in new_rows if r.get("path") in current_paths]
        self._by_path = {r["path"]: r for r in new_rows}
        self._entries = list(self._by_path.values())
        _write_jsonl(path, new_rows)
        self._maybe_cleanup()

    def search(
        self, query: str, top_k: int = 3, similarity_threshold: float = 0.3
    ) -> list[str]:
        """关键词检索，返回匹配 skill 的全文列表。"""
        if not self._entries or not query.strip():
            return []
        q = query.strip()
        scored: list[tuple[float, str]] = []
        for e in self._entries:
            path = e.get("path", "")
            if not path:
                continue
            s = _keyword_match_score(q, e.get("search_text", ""))
            if s >= similarity_threshold:
                scored.append((s, path))
        scored.sort(key=lambda x: -x[0])
        out: list[str] = []
        for _, p in scored[:top_k]:
            content = _read_text_file(Path(p))
            if content.strip():
                out.append(content)
        return out

    def list_summaries(self) -> list[dict[str, str]]:
        """供 memory 等场景展示的 skill 概要（无需全文）。"""
        items: list[dict[str, str]] = []
        for e in self._entries:
            desc = str(e.get("description", ""))
            trs = e.get("triggers", [])
            if isinstance(trs, list):
                ts = [str(t) for t in trs]
            else:
                ts = []
            items.append(
                {
                    "name": str(e.get("name", "")),
                    "description": desc,
                    "triggers": ", ".join(ts),
                }
            )
        return items


def _iter_project_files(projects_dir: Path) -> list[Path]:
    if not projects_dir.exists():
        return []
    return [p for p in projects_dir.rglob("*.md") if p.is_file()]


class ProjectIndex:
    """Project 语义索引管理器。"""

    def __init__(
        self,
        projects_dir: Path,
        index_dir: Path,
        embedding_config: dict[str, str] | None = None,
    ) -> None:
        self.projects_dir = projects_dir
        self.index_dir = index_dir
        if embedding_config and embedding_config.get("api_key") and embedding_config.get("base_url"):
            self._embed_client = _EmbeddingClient(
                provider=embedding_config["provider"],
                model=embedding_config["model"],
                api_key=embedding_config["api_key"],
                base_url=embedding_config["base_url"],
            )
        else:
            self._embed_client = None
        self._entries: list[dict[str, Any]] = []
        self._by_path: dict[str, dict[str, Any]] = {}

    @property
    def _use_embedding(self) -> bool:
        return self._embed_client is not None

    def _embed(self, text: str) -> list[float]:
        if not text.strip() or not self._embed_client:
            return []
        return self._embed_client.embed(text)

    def load_or_build(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        path = self.index_dir / "projects.jsonl"
        old_rows = {str(r.get("path", "")): r for r in _read_jsonl(path) if r.get("path")}

        project_files = _iter_project_files(self.projects_dir)
        current_paths: set[str] = set()
        new_rows: list[dict[str, Any]] = []

        for pf in project_files:
            key = str(pf.resolve())
            current_paths.add(key)
            try:
                mtime = pf.stat().st_mtime
            except OSError:
                continue
            prev = old_rows.get(key)
            if prev and abs(float(prev.get("mtime", 0)) - mtime) < 1e-6 and (
                (not self._use_embedding)
                or (prev.get("embedding") and len(prev.get("embedding", [])) > 0)
            ):
                if not prev.get("search_text"):
                    raw0 = _read_text_file(pf)
                    st0 = _project_preview_text(pf.stem, raw0)
                    prev = {**prev, "search_text": st0}
                if not prev.get("description"):
                    raw_desc = _read_text_file(pf)
                    prev = {**prev, "description": _extract_description(raw_desc)}
                new_rows.append(prev)
                continue
            raw = _read_text_file(pf)
            name = pf.stem
            stext = _project_preview_text(name, raw)
            emb = self._embed(stext) if self._use_embedding else []
            if self._use_embedding and not emb and prev and prev.get("embedding"):
                emb = [float(x) for x in prev["embedding"]]
            new_rows.append(
                {
                    "name": name,
                    "path": key,
                    "mtime": mtime,
                    "embedding": emb,
                    "search_text": stext,
                    "description": _extract_description(raw),
                }
            )

        new_rows = [r for r in new_rows if r.get("path") in current_paths]
        self._by_path = {r["path"]: r for r in new_rows}
        self._entries = list(self._by_path.values())
        _write_jsonl(path, new_rows)

    def search(
        self, query: str, top_k: int = 2, similarity_threshold: float = 0.3
    ) -> list[str]:
        """语义检索，返回匹配 project 的 .md 全文列表。"""
        if not self._entries or not query.strip():
            return []
        q = query.strip()
        qvec = self._embed(q) if self._use_embedding else []
        scored: list[tuple[float, str]] = []
        for e in self._entries:
            path = e.get("path", "")
            if not path:
                continue
            if qvec and e.get("embedding") and len(e["embedding"]) == len(qvec):
                s = _cosine_sim(qvec, [float(x) for x in e["embedding"]])
            else:
                s = _keyword_match_score(q, e.get("search_text", ""))
            if s >= similarity_threshold:
                scored.append((s, path))
        scored.sort(key=lambda x: -x[0])
        out: list[str] = []
        for _, p in scored[: top_k]:
            content = _read_text_file(Path(p))
            if content.strip():
                out.append(f"# {Path(p).stem}\n\n{content}")
        return out

    def list_summaries(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for e in self._entries:
            name = str(e.get("name", ""))
            items.append(
                {
                    "name": name,
                    "preview": e.get("search_text", name)[:300],
                }
            )
        return items


# ── EmbeddingIndexer: 异步批量队列 ─────────────────────────────────────
# 设计文档 §4.7

import queue
import threading
import time


class EmbeddingIndexer:
    """后台线程批量计算 embedding 并写入 messages_embedding 表。

    攒 batch（默认 32 条或每 5 秒）后调远程 API 批量算向量，
    写入 SQLite messages_embedding 表。
    """

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        batch_size: int = 32,
    ) -> None:
        self._provider = provider
        self._model = model
        self._client = _EmbeddingClient(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
        self._batch_size = batch_size
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        logger.info(
            "EmbeddingIndexer started: provider=%s, model=%s, batch_size=%d",
            provider, model, batch_size,
        )

    def enqueue(self, msg_id: str, session_id: str, ts: int, content: str) -> None:
        """将一条消息加入待计算队列。"""
        self._queue.put({
            "msg_id": msg_id,
            "session_id": session_id,
            "ts": ts,
            "content": content,
        })

    def stop(self) -> None:
        """停止后台线程（会 flush 剩余）。"""
        self._stop.set()
        self._worker.join(timeout=10)

    def _run(self) -> None:
        batch: list[dict[str, Any]] = []
        while not self._stop.wait(5.0):  # 每 5 秒醒来 flush 一次
            while len(batch) < self._batch_size and not self._queue.empty():
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if batch:
                self._flush(batch)
                batch = []
        # 退出前 flush 剩余
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._flush(batch)
        logger.info("EmbeddingIndexer stopped")

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        """批量调 embedding API 并写入 SQLite。"""
        texts = [b["content"] for b in batch]
        vectors = self._client.embed_batch(texts)
        if not vectors or len(vectors) != len(batch):
            logger.warning("Embedding batch size mismatch: %d items, %d vectors",
                           len(batch), len(vectors) if vectors else 0)
            return

        import sqlite3
        import struct as _struct
        from src.memory.session_store import _get_db

        conn = _get_db()
        try:
            now = int(time.time() * 1000)
            for item, vec in zip(batch, vectors):
                if not vec:
                    continue
                blob = _struct.pack(f"{len(vec)}f", *vec)
                conn.execute(
                    "INSERT OR REPLACE INTO messages_embedding(msg_id, session_id, ts, embedding, provider, indexed_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (item["msg_id"], item["session_id"], item["ts"], blob, self._provider, now),
                )
            conn.commit()
            logger.debug("EmbeddingIndexer flushed %d vectors", len(batch))
        except Exception as ex:
            logger.error("EmbeddingIndexer flush failed: %s", ex)
        finally:
            conn.close()
