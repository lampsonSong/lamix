"""记忆库重复检测 scanner（2026-10-27 治理实战沉淀）。

覆盖三类审计此前查不出的重复：
1. 跨层/同层文件名归一化重复（foo.md + foos.md、foo-pitfall + foo-pitfalls）
2. 内容近似重复（token Jaccard 相似度判定，非 md5 精确匹配）
3. skills 同名 .md 与同名目录并存（导致系统提示双列）

设计：长度分桶限流，避免 O(n²) 全量比对拖慢每日审计。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.core.self_audit.models import AuditFinding

# 文件名归一化后缀（复数/常见变体）
_NAME_SUFFIX = re.compile(r"[-_]?(pitfalls?|traps?|lessons?|notes?|tips?|记录|踩坑|坑)$")


def _normalize_name(stem: str) -> str:
    n = stem.lower()
    n = _NAME_SUFFIX.sub("", n)
    n = n.rstrip("s")
    return n


def _tokens(text: str) -> set[str]:
    """内容 token 集：英文词 + 中文 2-gram。"""
    out: set[str] = set()
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower()):
        out.add(w)
    cn = re.sub(r"[^一-鿿]", " ", text)
    for seg in cn.split():
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    return out


def _collect_files(dirs: list[Path]) -> list[tuple[Path, str, str]]:
    """返回 (path, layer, text)，跳过 .archived 与隐藏文件。"""
    items: list[tuple[Path, str, str]] = []
    for layer, d in dirs:
        if not d or not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            if p.name.startswith("."):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            items.append((p, layer, text))
    return items


def scan_duplicate_knowledge(
    projects_dir: Path | None = None,
    skills_dir: Path | None = None,
    info_dir: Path | None = None,
) -> list[AuditFinding]:
    """检测记忆库中的重复：同名归一化 / 内容近似 / skill 同名并存。"""
    findings: list[AuditFinding] = []
    dirs = [
        ("project", projects_dir),
        ("skill", skills_dir),
        ("info", info_dir),
    ]
    items = _collect_files([(l, d) for l, d in dirs if d])

    # ── 1. 文件名归一化重复 ──
    by_name: dict[str, list[tuple[str, str]]] = {}
    for p, layer, _ in items:
        by_name.setdefault(_normalize_name(p.stem), []).append((layer, p.stem))
    for norm, group in by_name.items():
        if len(group) < 2:
            continue
        names = sorted({f"{layer}/{stem}" for layer, stem in group})
        findings.append(AuditFinding(
            severity="warning",
            category="memory",
            target=norm,
            message=f"文件名归一化后重复（{len(names)} 份）：{', '.join(names)}",
            suggestion="核对内容：重复的合并保留一份，其余移入对应层 .archived/",
        ))

    # ── 2. skill 同名 .md 与同名目录并存 ──
    if skills_dir and skills_dir.exists():
        md_stems = {p.stem for p in skills_dir.glob("*.md") if p.is_file()}
        for sub in sorted(skills_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            if sub.name in md_stems:
                findings.append(AuditFinding(
                    severity="warning",
                    category="skill",
                    target=sub.name,
                    message=f"skill 同名并存：{sub.name}.md 与 {sub.name}/ 目录同时存在（系统提示会双列）",
                    suggestion="合并两份内容保留一份形态（平铺 .md 或目录 SKILL.md），另一份移入 .archived/",
                ))

    # ── 3. 内容近似重复（长度分桶限流）──
    buckets: dict[int, list[tuple[str, str, set[str]]]] = {}
    for p, layer, text in items:
        if len(text) < 150:
            continue
        length = len(text)
        for b in (length // 512, length // 1024, length // 2048):
            buckets.setdefault(b, []).append((layer, p.stem, _tokens(text)))
    seen_pairs: set[tuple[str, str]] = set()
    for bucket in buckets.values():
        if len(bucket) < 2 or len(bucket) > 120:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                la, na, ta = bucket[i]
                lb, nb, tb = bucket[j]
                pair = tuple(sorted((f"{la}/{na}", f"{lb}/{nb}")))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                inter = len(ta & tb)
                if inter < 25:
                    continue
                union = len(ta | tb)
                if not union:
                    continue
                if inter / union >= 0.62:
                    findings.append(AuditFinding(
                        severity="warning",
                        category="memory",
                        target=na,
                        message=f"内容疑似重复（相似度 {inter / union:.2f}）：{pair[0]} ↔ {pair[1]}",
                        suggestion="两文件主题重叠：合并保留信息更全的一份，另一份移入 .archived/",
                    ))
    return findings
