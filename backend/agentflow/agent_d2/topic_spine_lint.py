"""Topic-spine alignment lint for D2 drafts (v1.0.21).

Different signal from `specificity_lint`. specificity_lint asks "does
the body MENTION publisher tokens?" — passes for any draft that name-
drops `Kafka Streams` / `MCP` / `<brand>`. topic_spine_lint asks "is
the SOURCE MATERIAL the article was built from actually about the
publisher's domain?" — fails when the hotspot's source_references
talk about TCG customs / cross-border ecommerce while the publisher
is an on-chain data infra, even if the resulting draft drags those
publisher tokens into every paragraph as forced analogies.

Heuristic: Jaccard overlap between
  A = tokens drawn from hotspot.source_references[*].text_snippet +
      hotspot.topic_one_liner
and
  B = tokens drawn from publisher.product_facts + perspectives +
      keyword_groups (full surface, not just core)

Below `_MISALIGNMENT_THRESHOLD` (default 0.02 — almost-no overlap)
the draft is flagged as "topic-spine misalignment" and the operator
gets a loud warning on the Gate B card. The lint does NOT block the
card; the operator decides whether to reject or proceed (B:reject is
the existing path).
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping


_MISALIGNMENT_THRESHOLD = 0.02
_MIN_SIGNAL_TOKENS = 5    # below this, A is too thin to lint reliably

# Re-use topic_fit's tokenizer flavor so the score is on the same scale
# as the v1.0.0 D1 fit score. Keep this file self-contained (no import
# from agent_d1) so D2 doesn't depend on D1 internals.
_WORD_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)
_STOPWORDS_EN: frozenset[str] = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "out",
    "are", "was", "were", "you", "your", "our", "use", "using", "via",
    "but", "not", "all", "any", "can", "has", "have", "had", "its",
    "their", "them", "they", "what", "when", "how", "why", "who",
    "about", "over", "under", "than", "then", "more", "most", "some",
})
_STOPWORDS_CJK: frozenset[str] = frozenset({
    "的", "了", "和", "或", "是", "在", "有", "也", "就", "都", "而",
    "与", "及", "对", "把", "被", "从", "向", "为", "于", "等", "做",
    "这", "那", "你", "我", "他", "她", "它", "们", "个", "之", "以",
    "里", "上", "下", "中",
})


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _tokenize(text: str) -> list[str]:
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


def _harvest(value: object, sink: set[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        sink.update(_tokenize(value))
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _harvest(v, sink)
    elif isinstance(value, Mapping):
        for v in value.values():
            _harvest(v, sink)


def _publisher_domain_tokens(publisher: Mapping[str, object] | None) -> set[str]:
    """Token surface representing 'this is what the publisher writes about'.
    Different from specificity_lint's anchor surface — here we use the
    DOMAIN scope (product_facts + perspectives + keyword_groups), not the
    brand surface, because this lint asks 'is the topic in our domain'."""
    if not publisher:
        return set()
    sink: set[str] = set()
    for key in ("product_facts", "perspectives", "default_description"):
        _harvest(publisher.get(key), sink)
    # Profile keyword_groups payload, when present in the merged profile.
    for key in ("keyword_groups", "keywords_payload"):
        _harvest(publisher.get(key), sink)
    return sink


def _topic_spine_tokens(metadata: Mapping[str, object]) -> set[str]:
    """Tokens describing what the article was BUILT FROM — the upstream
    source material, not the LLM-elaborated draft body. Pulls from the
    hotspot's topic_one_liner + each source_reference's text_snippet
    (when stamped into metadata at write time)."""
    sink: set[str] = set()
    _harvest(metadata.get("topic_one_liner"), sink)
    refs = metadata.get("source_references") or []
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, Mapping):
                _harvest(ref.get("text_snippet"), sink)
                _harvest(ref.get("title"), sink)
    return sink


def detect_topic_spine_misalignment(
    metadata: Mapping[str, object],
    publisher: Mapping[str, object] | None,
    *,
    threshold: float = _MISALIGNMENT_THRESHOLD,
) -> str | None:
    """Return a warning when the article's source spine doesn't intersect
    the publisher's domain tokens. Returns None when:
      - no publisher set
      - publisher domain too thin to lint (< 5 tokens)
      - source spine too thin to lint (< 5 tokens)
      - intersection is non-trivial
    """
    spine = _topic_spine_tokens(metadata)
    if len(spine) < _MIN_SIGNAL_TOKENS:
        return None
    domain = _publisher_domain_tokens(publisher)
    if len(domain) < _MIN_SIGNAL_TOKENS:
        return None
    intersect = spine & domain
    union = spine | domain
    if not union:
        return None
    jaccard = len(intersect) / len(union)
    if jaccard >= threshold:
        return None
    return (
        f"⚠ topic-spine misalignment: hotspot 主题与 publisher 领域脱钩 "
        f"(Jaccard={jaccard:.3f}, 阈值 {threshold:.2f}, 共有 token "
        f"{len(intersect)} / 各方至少 {_MIN_SIGNAL_TOKENS}). "
        f"这是一篇被强行嫁接的稿子 — 即便每段都 anchor 到 publisher_facts, "
        f"骨架仍然不在你领域内. 建议 B:reject + 调高 "
        f"AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD 或调整 D1 filter."
    )
