"""Auto-resolve draft image placeholders against a local image library."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from agentflow.agent_d2.main import load_draft, save_draft
from agentflow.shared.models import ImagePlaceholder

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_WORD_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def default_image_library() -> Path:
    raw = os.environ.get("AGENTFLOW_IMAGE_LIBRARY") or "~/Pictures/agentflow"
    return Path(raw).expanduser()


def _tokenize(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens: set[str] = set()
    for match in _WORD_RE.findall(lowered):
        cleaned = match.strip("_- ")
        if len(cleaned) >= 2:
            tokens.add(cleaned)
        if any("一" <= ch <= "鿿" for ch in cleaned):
            for idx in range(len(cleaned) - 1):
                bigram = cleaned[idx : idx + 2]
                if len(bigram) == 2:
                    tokens.add(bigram)
    return tokens


def _candidate_text(path: Path) -> str:
    parent = " ".join(part for part in path.parent.parts[-3:] if part not in {"/", ""})
    return f"{path.stem} {parent}".strip()


def _score_candidate(placeholder: ImagePlaceholder, candidate: Path) -> float:
    desc_tokens = _tokenize(placeholder.description)
    heading_tokens = _tokenize(placeholder.section_heading)
    candidate_text = _candidate_text(candidate).lower()
    candidate_tokens = _tokenize(candidate_text)
    if not desc_tokens or not candidate_tokens:
        return 0.0

    desc_overlap = len(desc_tokens & candidate_tokens) / max(len(desc_tokens), 1)
    heading_overlap = (
        len(heading_tokens & candidate_tokens) / max(len(heading_tokens), 1)
        if heading_tokens
        else 0.0
    )

    desc_phrase = re.sub(r"\s+", " ", placeholder.description.strip().lower())
    compact_phrase = re.sub(r"[\s_\-]+", "", desc_phrase)
    compact_candidate = re.sub(r"[\s_\-]+", "", candidate_text)
    bonus = 0.0
    if compact_phrase and compact_phrase in compact_candidate:
        bonus += 0.35
    elif any(tok in compact_candidate for tok in desc_tokens if len(tok) >= 4):
        bonus += 0.15

    return round(min(1.0, desc_overlap * 0.8 + heading_overlap * 0.2 + bonus), 4)


def _scan_library(root: Path) -> list[Path]:
    if not root.exists():
        raise FileNotFoundError(f"image library does not exist: {root}")
    if not root.is_dir():
        raise FileNotFoundError(f"image library is not a directory: {root}")
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _IMAGE_EXTS]
    return sorted(files)


def auto_resolve_images(
    article_id: str,
    *,
    library: Path | None = None,
    min_score: float = 0.55,
) -> dict[str, Any]:
    draft = load_draft(article_id)
    target_library = (library or default_image_library()).expanduser().resolve()
    candidates = _scan_library(target_library)

    already_resolved = 0
    auto_resolved = 0
    matches: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for placeholder in draft.image_placeholders:
        if placeholder.resolved_path:
            already_resolved += 1
            continue

        best_path: Path | None = None
        best_score = 0.0
        for candidate in candidates:
            score = _score_candidate(placeholder, candidate)
            if score > best_score:
                best_score = score
                best_path = candidate

        if best_path is None or best_score < min_score:
            unresolved.append(
                {
                    "placeholder_id": placeholder.id,
                    "description": placeholder.description,
                    "section_heading": placeholder.section_heading,
                    "best_score": round(best_score, 4),
                }
            )
            continue

        resolved = str(best_path.resolve())
        placeholder.resolved_path = resolved
        auto_resolved += 1
        matches.append(
            {
                "placeholder_id": placeholder.id,
                "description": placeholder.description,
                "section_heading": placeholder.section_heading,
                "matched_path": resolved,
                "score": best_score,
            }
        )

    if auto_resolved:
        save_draft(draft)

    remaining_unresolved = sum(
        1 for placeholder in draft.image_placeholders if not placeholder.resolved_path
    )
    return {
        "article_id": article_id,
        "library": str(target_library),
        "min_score": min_score,
        "scanned_files": len(candidates),
        "total_placeholders": len(draft.image_placeholders),
        "already_resolved_count": already_resolved,
        "auto_resolved_count": auto_resolved,
        "remaining_unresolved_count": remaining_unresolved,
        "matches": matches,
        "unresolved": unresolved,
    }
