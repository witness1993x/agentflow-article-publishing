"""Thin orchestrator that reads a list of sources (files + URLs) into dicts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from agentflow.shared.file_readers import read_article
from agentflow.shared.logger import get_logger

_log = get_logger("agent_d0.extractor")

_SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".docx"}


def _iter_dir(path: Path) -> list[Path]:
    """Return supported article files inside ``path`` (non-recursive)."""
    if not path.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(path.iterdir()):
        if child.is_file() and child.suffix.lower() in _SUPPORTED_SUFFIXES:
            found.append(child)
    return found


def extract_sources(sources: Iterable[str]) -> list[dict[str, Any]]:
    """Read each source (path or URL), return list of article dicts.

    Each dict carries ``text``, ``title``, ``source_type``, ``source_id``.
    Unreadable sources are logged as warnings and skipped.
    """
    articles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for src in sources:
        try:
            article = read_article(src)
        except Exception as err:
            _log.warning("Skipping unreadable source %r: %s", src, err)
            continue

        if not article.get("text"):
            _log.warning("Skipping empty article from source %r", src)
            continue

        sid = article.get("source_id")
        if sid and sid in seen_ids:
            _log.info("Skipping duplicate source_id=%s for %r", sid, src)
            continue
        if sid:
            seen_ids.add(sid)

        # Carry the original source string for traceability.
        article.setdefault("source_ref", str(src))
        articles.append(article)

    return articles


def expand_inputs(
    *,
    dir_: str | None = None,
    files: Iterable[str] | None = None,
    urls: Iterable[str] | None = None,
) -> list[str]:
    """Expand CLI-style inputs into a flat list of source strings."""
    sources: list[str] = []

    if dir_:
        for p in _iter_dir(Path(dir_).expanduser()):
            sources.append(str(p))

    if files:
        for f in files:
            sources.append(str(f))

    if urls:
        for u in urls:
            sources.append(str(u))

    return sources
