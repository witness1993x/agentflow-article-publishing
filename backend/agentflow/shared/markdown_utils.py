"""Markdown helpers: image placeholders, paragraph splitting, word counting."""

from __future__ import annotations

import re
from typing import Any

_IMAGE_PLACEHOLDER_RE = re.compile(r"\[IMAGE:\s*(.+?)\]")
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")


def extract_image_placeholders(md: str) -> list[dict[str, Any]]:
    """Return list of {"id", "description", "line_number"} for each [IMAGE: desc].

    Line numbers are 1-indexed (matching most editors).
    """
    placeholders: list[dict[str, Any]] = []
    counter = 0
    for lineno, line in enumerate(md.splitlines(), start=1):
        for match in _IMAGE_PLACEHOLDER_RE.finditer(line):
            counter += 1
            placeholders.append(
                {
                    "id": f"img_{counter}",
                    "description": match.group(1).strip(),
                    "line_number": lineno,
                }
            )
    return placeholders


def replace_image_placeholder(
    md: str, placeholder_id: str, resolved_path: str
) -> str:
    """Replace a specific [IMAGE: desc] with ![desc](path).

    Finds the Nth placeholder (1-indexed by id like "img_3") and swaps it.
    If the id doesn't resolve, returns md unchanged.
    """
    try:
        target_index = int(placeholder_id.split("_")[-1])
    except (ValueError, IndexError):
        return md

    if target_index < 1:
        return md

    counter = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        if counter == target_index:
            desc = match.group(1).strip()
            return f"![{desc}]({resolved_path})"
        return match.group(0)

    return _IMAGE_PLACEHOLDER_RE.sub(_sub, md)


def strip_image_placeholders(md: str) -> str:
    """Remove entire lines that contain unresolved [IMAGE: …] placeholders."""
    kept: list[str] = []
    for line in md.splitlines():
        if _IMAGE_PLACEHOLDER_RE.search(line):
            # Drop the line completely — publisher force-path.
            continue
        kept.append(line)
    return "\n".join(kept)


def split_paragraphs(md: str) -> list[str]:
    """Split markdown by blank lines.

    Headings ("# ...") are emitted as their own paragraphs; code fences are
    preserved as single blocks.
    """
    paragraphs: list[str] = []
    buffer: list[str] = []
    in_fence = False

    def _flush() -> None:
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                paragraphs.append(text)
            buffer.clear()

    for line in md.splitlines():
        stripped = line.strip()

        if stripped.startswith("```"):
            in_fence = not in_fence
            buffer.append(line)
            if not in_fence:
                _flush()
            continue

        if in_fence:
            buffer.append(line)
            continue

        if stripped == "":
            _flush()
            continue

        if stripped.startswith("#"):
            _flush()
            paragraphs.append(stripped)
            continue

        buffer.append(line)

    _flush()
    return paragraphs


def count_words(text: str) -> int:
    """Count words. Each CJK character = 1, English words split on whitespace."""
    cjk = len(_CJK_RE.findall(text))
    english_fragment = _CJK_RE.sub(" ", text)
    english_tokens = [t for t in english_fragment.split() if t.strip()]
    return cjk + len(english_tokens)
