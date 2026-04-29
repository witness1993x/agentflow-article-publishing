"""Topic ↔ publisher fit scoring.

Given a hotspot dict (Hotspot.to_dict() shape) and a publisher_account dict
(see ``topic_profile_publisher_account``), return a 0..1 Jaccard-overlap score
over a tokenized representation of both sides.

The scorer mirrors the tokenizer used elsewhere in agentflow (see
``agentflow.cli.intent_commands._tokenize``):

  - Lowercase Latin words of length ≥ 3
  - CJK runs are split into overlapping 2-grams so single Chinese terms
    survive even when they appear inside a longer phrase
  - Very common stopwords are dropped (English glue + Chinese function chars)

Used by ``agent_review.triggers.post_gate_a`` to rerank `af hotspots`
candidates so that an active publisher_account (any brand) downranks
topics that share no product surface with the publisher's domain.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


_WORD_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)

# Latin glue. Kept short — we only need to suppress noise, not be clever.
_STOPWORDS_EN: frozenset[str] = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "out",
    "are", "was", "were", "you", "your", "our", "use", "using", "via",
    "but", "not", "all", "any", "can", "has", "have", "had", "its",
    "their", "them", "they", "what", "when", "how", "why", "who",
    "about", "over", "under", "than", "then", "more", "most", "some",
    "新", "里", "上", "下", "中",
})

# CJK function chars / single-char glue that show up in 2-gram noise.
_STOPWORDS_CJK: frozenset[str] = frozenset({
    "的", "了", "和", "或", "是", "在", "有", "也", "就", "都", "而",
    "与", "及", "对", "把", "被", "从", "向", "为", "于", "等", "做",
    "这", "那", "你", "我", "他", "她", "它", "们", "个", "之", "以",
})


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _tokenize(text: str) -> list[str]:
    """Lowercase Latin tokens (≥3 chars) + CJK overlapping 2-grams."""
    if not text:
        return []
    out: list[str] = []
    for match in _WORD_RE.findall(str(text).lower()):
        if any(_is_cjk(ch) for ch in match):
            for i in range(len(match) - 1):
                bigram = match[i : i + 2]
                if len(bigram) == 2 and bigram not in _STOPWORDS_CJK:
                    out.append(bigram)
        else:
            if len(match) >= 3 and match not in _STOPWORDS_EN:
                out.append(match)
    return out


def _tokens_from(values: Iterable[Any]) -> set[str]:
    bag: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            bag.update(_tokens_from(value))
        elif isinstance(value, dict):
            bag.update(_tokens_from(value.values()))
        else:
            bag.update(_tokenize(str(value)))
    return bag


def _hotspot_tokens(hotspot: dict[str, Any]) -> set[str]:
    parts: list[Any] = [hotspot.get("topic_one_liner")]
    for angle in hotspot.get("suggested_angles") or []:
        if isinstance(angle, dict):
            parts.extend([angle.get("title"), angle.get("angle"), angle.get("hook")])
    for ref in hotspot.get("source_references") or []:
        if isinstance(ref, dict):
            parts.extend([ref.get("title"), ref.get("text_snippet")])
    return _tokens_from(parts)


def _publisher_tokens(publisher_account: dict[str, Any]) -> set[str]:
    parts: list[Any] = [
        publisher_account.get("brand"),
        publisher_account.get("summary"),
        publisher_account.get("intent"),
        publisher_account.get("pronoun"),
        publisher_account.get("product_facts") or [],
        publisher_account.get("default_tags") or [],
    ]
    keywords = publisher_account.get("keywords_payload")
    if isinstance(keywords, dict):
        parts.extend([
            keywords.get("primary") or [],
            keywords.get("expanded") or [],
        ])
    return _tokens_from(parts)


def score_fit(hotspot: dict, publisher_account: dict) -> float:
    """Return 0..1 fit score (1=perfect overlap, 0=disjoint).

    Score: |tokens_h ∩ tokens_p| / |tokens_h ∪ tokens_p| (Jaccard).
    Returns 0.0 when either side has no tokens (no signal to work with).
    """
    if not publisher_account:
        return 0.0
    tokens_h = _hotspot_tokens(hotspot or {})
    if not tokens_h:
        return 0.0
    tokens_p = _publisher_tokens(publisher_account)
    if not tokens_p:
        return 0.0
    intersection = tokens_h & tokens_p
    union = tokens_h | tokens_p
    if not union:
        return 0.0
    return min(1.0, len(intersection) / len(union))


def score_profile_fit(
    hotspot: dict[str, Any],
    profile: dict[str, Any],
    *,
    profile_id: str = "",
) -> float:
    """Return topic-profile fit using the full profile keyword surface."""
    if not profile:
        return 0.0
    from agentflow.shared.topic_profiles import (
        topic_profile_keywords_payload,
        topic_profile_label,
        topic_profile_publisher_account,
    )

    publisher = topic_profile_publisher_account(profile)
    fit_context = {
        **publisher,
        "brand": publisher.get("brand") or topic_profile_label(profile, profile_id),
        "summary": profile.get("summary"),
        "intent": profile.get("intent"),
        "keywords_payload": topic_profile_keywords_payload(profile),
    }
    return score_fit(hotspot, fit_context)
