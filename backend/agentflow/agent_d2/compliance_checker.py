"""Pure-Python compliance checker for D2 generated content.

Scans a paragraph/section/draft against a style profile for:

- **Taboo vocabulary** — plain substring match against ``taboos.vocabulary``.
- **Taboo sentence patterns** — regex (or plain substring) match against
  ``taboos.sentence_patterns``. Patterns that look like prose with ellipses
  (e.g. ``"首先...其次...最后"``) are converted to regex (``.``→``.``, ``...``→
  ``.*?``).
- **Paragraph length** — any paragraph whose word count exceeds
  ``paragraph_preferences.max_length_words``.

Scoring is a fixed per-violation penalty so retry progress shows up as a
score improvement (dividing by paragraph count made small sections with any
violations collapse straight to 0, hiding whether retry actually helped):

    score = max(0.0, 1.0 - VIOLATION_PENALTY * len(violations))

where ``VIOLATION_PENALTY = 0.15`` — 1 violation → 0.85, 3 → 0.55, 7+ → 0.
"""

from __future__ import annotations

import re
from typing import Any

from agentflow.shared.markdown_utils import count_words, split_paragraphs
from agentflow.shared.models import DraftOutput

VIOLATION_PENALTY = 0.15


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def check(text: str, style_profile: dict[str, Any]) -> tuple[float, list[str]]:
    """Check one blob of markdown/text against a style profile.

    Returns ``(score, violations)`` where ``score`` is in ``[0, 1]`` and
    ``violations`` is a list of human-readable strings (empty on full pass).
    """
    violations: list[str] = []

    taboos = style_profile.get("taboos") or {}
    vocab = [w for w in (taboos.get("vocabulary") or []) if w]
    patterns = [p for p in (taboos.get("sentence_patterns") or []) if p]

    para_prefs = style_profile.get("paragraph_preferences") or {}
    max_para_words = int(para_prefs.get("max_length_words") or 0)

    # 1. Taboo vocabulary (plain substring).
    for word in vocab:
        if word and word in text:
            violations.append(f"taboo_vocab: {word!r}")

    # 2. Taboo sentence patterns (regex-compiled).
    for pattern in patterns:
        if _pattern_hits(pattern, text):
            violations.append(f"taboo_pattern: {pattern!r}")

    # 3. Paragraph length.
    paragraphs = split_paragraphs(text) or [text]
    if max_para_words > 0:
        for i, para in enumerate(paragraphs, start=1):
            if para.startswith("#"):
                continue  # headings don't count toward paragraph length
            w = count_words(para)
            if w > max_para_words:
                violations.append(
                    f"paragraph_too_long: para {i} has {w} words > {max_para_words}"
                )

    score = max(0.0, 1.0 - VIOLATION_PENALTY * len(violations))
    return score, violations


def scan_draft(
    draft: DraftOutput, style_profile: dict[str, Any]
) -> dict[str, Any]:
    """Aggregate compliance scan across every section in a draft."""
    section_reports: list[dict[str, Any]] = []
    total_violations: list[str] = []
    scores: list[float] = []

    for section in draft.sections:
        score, violations = check(section.content_markdown, style_profile)
        scores.append(score)
        section_reports.append(
            {
                "heading": section.heading,
                "score": score,
                "violations": violations,
                "word_count": section.word_count,
            }
        )
        total_violations.extend(
            f"[{section.heading}] {v}" for v in violations
        )

    avg_score = sum(scores) / len(scores) if scores else 1.0
    return {
        "article_id": draft.article_id,
        "title": draft.title,
        "average_score": avg_score,
        "total_violations": total_violations,
        "sections": section_reports,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _pattern_hits(pattern: str, text: str) -> bool:
    """Detect a taboo sentence pattern in text.

    Literals like ``"综上所述"`` → substring match.
    Prose templates with ``...`` (ellipsis) → interpreted as wildcard, e.g.
    ``"首先...其次...最后"`` → ``首先.*?其次.*?最后``.
    """
    if "..." in pattern or "…" in pattern:
        normalized = pattern.replace("…", "...")
        parts = [re.escape(chunk) for chunk in normalized.split("...") if chunk]
        if not parts:
            return False
        regex = ".*?".join(parts)
        return re.search(regex, text) is not None
    return pattern in text
