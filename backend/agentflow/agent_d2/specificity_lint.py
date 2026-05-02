"""Specificity / anchoring lint for D2 drafts.

Catches the "draft sounds specific but it's not OUR specific" failure
mode flagged in the autopost feedback ("没有很贴合具体的产品相关的文章
出来,都是泛谈"). Different from `language_lint.py` which catches
mixed-language. This catches mixed-grounding: a draft that has dates,
numbers, and product names — but the names are generic AI/Web3 lingo,
not the publisher's own product / perspectives / brand.

The detector counts how often the body references tokens drawn from
``publisher_account``: ``brand``, ``product_facts`` content,
``perspectives`` content, ``default_description`` content. Section
density below ``_DENSITY_THRESHOLD`` per section is flagged.

Used by ``agent_review.triggers.post_gate_b`` alongside ``language_lint``.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping


# How many publisher anchors must appear, on average, per section.
# 0.5 is conservative — half the sections must mention something from
# the publisher's own facts / perspectives.
_DENSITY_THRESHOLD = 0.5

# Tokens shorter than this are ignored when extracting anchor tokens
# (avoids matching "AI" / "ML" / "API" which co-occur in any tech
# article and wouldn't prove specificity).
_MIN_ANCHOR_TOKEN_LEN = 4


_PUNCT_RE = re.compile(r"[\s　、,，.。;；:：!！?？\(\)\[\]\{\}<>《》\"'`]+")


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    raw = _PUNCT_RE.split(text)
    return {tok.strip().lower() for tok in raw if len(tok.strip()) >= _MIN_ANCHOR_TOKEN_LEN}


def _extract_anchor_tokens(publisher: Mapping[str, object] | None) -> set[str]:
    """Build the anchor vocabulary from a publisher_account dict. Each
    string from product_facts / perspectives / default_description /
    brand contributes its long-enough words to the set.
    """
    if not publisher:
        return set()
    tokens: set[str] = set()

    def _harvest(value: object) -> None:
        if isinstance(value, str):
            tokens.update(_tokenize(value))
        elif isinstance(value, (list, tuple)):
            for item in value:
                _harvest(item)
        elif isinstance(value, Mapping):
            for v in value.values():
                _harvest(v)

    for key in ("brand", "default_description", "product_facts", "perspectives"):
        _harvest(publisher.get(key))
    return tokens


def detect_specificity_drift(
    sections: Iterable[Mapping[str, object]],
    publisher: Mapping[str, object] | None,
    *,
    density_threshold: float = _DENSITY_THRESHOLD,
) -> str | None:
    """Return a warning string when the body's anchor density falls
    below ``density_threshold`` (anchors per section), else None.

    Inputs are lenient: missing publisher / no anchor vocabulary / no
    sections all return None (we can't lint what we don't have).
    """
    sections_list = list(sections or [])
    if not sections_list:
        return None
    anchor_tokens = _extract_anchor_tokens(publisher)
    if len(anchor_tokens) < 5:
        # Profile too thin to lint against; a separate doctor probe
        # surfaces the weak-profile case so we don't double-warn here.
        return None

    total_hits = 0
    sections_with_hits = 0
    for section in sections_list:
        body = str(section.get("content_markdown") or "")
        body_tokens = _tokenize(body)
        hit_set = body_tokens & anchor_tokens
        if hit_set:
            sections_with_hits += 1
            total_hits += len(hit_set)

    section_count = len(sections_list)
    density = total_hits / max(1, section_count)

    if density >= density_threshold:
        return None

    return (
        f"⚠ specificity drift: 平均每节仅 {density:.2f} 处 publisher 锚点 "
        f"(阈值 {density_threshold}); {sections_with_hits}/{section_count} "
        f"节有 product_facts/perspectives 引用 — 多数段落像泛谈,不是 publisher "
        f"自家视角. 建议 af edit 重写或扩充 product_facts/perspectives."
    )
