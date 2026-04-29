"""Unified article reader: .md / .txt / .docx / URL -> text + metadata."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_YAML_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _source_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _is_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https"}


def _strip_yaml_frontmatter(text: str) -> tuple[str, str | None]:
    """Return (body, title_hint). title_hint is parsed from frontmatter if present."""
    match = _YAML_FRONTMATTER_RE.match(text)
    if not match:
        return text, None

    frontmatter = match.group(1)
    body = text[match.end():]

    title_hint: str | None = None
    for line in frontmatter.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == "title":
            title_hint = value.strip().strip("\"'")
            break
    return body, title_hint


def _read_md(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    body, title_hint = _strip_yaml_frontmatter(text)
    if not title_hint:
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("# "):
                title_hint = s[2:].strip()
                break
    return {
        "text": body.strip(),
        "title": title_hint,
        "source_type": "md",
        "source_id": _source_id(body),
    }


def _read_txt(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return {
        "text": text.strip(),
        "title": None,
        "source_type": "txt",
        "source_id": _source_id(text),
    }


def _read_docx(path: Path) -> dict[str, Any]:
    # Imported lazily — python-docx is not needed for md/txt/url paths.
    from docx import Document  # type: ignore

    doc = Document(str(path))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    text = "\n\n".join(paragraphs).strip()
    title: str | None = None
    if doc.core_properties and doc.core_properties.title:
        title = doc.core_properties.title
    if not title and paragraphs:
        title = paragraphs[0].strip()[:120] or None
    return {
        "text": text,
        "title": title,
        "source_type": "docx",
        "source_id": _source_id(text),
    }


def _read_url(url: str) -> dict[str, Any]:
    # Imported lazily to keep import-time cheap.
    import trafilatura  # type: ignore

    downloaded = trafilatura.fetch_url(url)
    if downloaded is None:
        raise RuntimeError(f"Could not fetch URL: {url}")

    text = trafilatura.extract(downloaded) or ""
    metadata = trafilatura.extract_metadata(downloaded)
    title: str | None = None
    if metadata is not None:
        title = getattr(metadata, "title", None)

    return {
        "text": (text or "").strip(),
        "title": title,
        "source_type": "url",
        "source_id": _source_id(text or url),
    }


def read_article(source: str) -> dict[str, Any]:
    """Read an article from path or URL.

    Returns ``{"text", "title", "source_type", "source_id"}``.

    ``source_type`` is one of ``"md" | "docx" | "txt" | "url"``.
    ``source_id`` is ``sha1(text)[:12]``.
    """
    if _is_url(source):
        return _read_url(source)

    path = Path(source).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No such article source: {source}")

    suffix = path.suffix.lower()
    if suffix == ".md" or suffix == ".markdown":
        return _read_md(path)
    if suffix == ".txt":
        return _read_txt(path)
    if suffix == ".docx":
        return _read_docx(path)

    # Fall back to text read for unknown extensions.
    return _read_txt(path)
