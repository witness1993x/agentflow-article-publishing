"""Keyword candidate extractor for ``af learn-from-handle --profile <id>``.

Given a list of sample articles (already extracted by the D0 pipeline), call
the LLM once to surface high-frequency proper nouns / domain-specific terms,
grouped into the keyword_groups buckets used by ``topic_profiles.yaml``
(``core`` / ``transport`` / ``products`` / ``adjacent``). Each candidate is
returned as a dict suitable for emitting into the topic-profile *suggestion*
flow (one suggestion per candidate so the user can review individually).

LLM failures degrade gracefully: an empty list is returned, never raised.
"""

from __future__ import annotations

import json
from typing import Any

from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger

_log = get_logger("agent_d0.keyword_extractor")

_VALID_GROUPS = ("core", "transport", "products", "adjacent")
_DEFAULT_GROUP = "core"

# Trim per-article snippets to keep prompt size bounded across N samples.
_PER_ARTICLE_SNIPPET_CHARS = 4000
_MAX_TITLE_CHARS = 200


def _build_prompt(snippets: list[dict[str, str]], max_candidates: int) -> str:
    """Compose a self-contained prompt; no external prompt file dependency."""
    article_blocks: list[str] = []
    for idx, item in enumerate(snippets, start=1):
        title = (item.get("title") or "")[:_MAX_TITLE_CHARS]
        text = (item.get("text") or "")[:_PER_ARTICLE_SNIPPET_CHARS]
        article_blocks.append(
            f"--- ARTICLE {idx} ---\n"
            f"TITLE: {title}\n"
            f"BODY:\n{text}\n"
        )
    articles_joined = "\n".join(article_blocks)
    article_count = len(snippets)

    return f"""You are extracting recurring domain keywords from a small corpus of
{article_count} article(s) by the same author. The keywords will seed the
``keyword_groups`` block of a topic-profile YAML and drive future search
queries, so they must be specific and reusable.

Rules:
- Surface up to {max_candidates} candidates total.
- Prefer multi-word proper nouns, product names, framework names, technical
  jargon, named systems, named protocols, and domain-of-discourse terms.
- Skip generic stop-words, generic verbs, and very common English/Chinese
  filler words. Skip the author's own name.
- Each candidate must appear (or be paraphrased) in at least 2 of the
  articles, OR appear repeatedly in 1 article in a way that signals it's a
  pillar topic for this author.
- Group each candidate into ONE of: core, transport, products, adjacent.
  - core      = the author's primary subject-matter (most articles)
  - transport = delivery mechanisms / protocols / platforms
  - products  = named products or vendor offerings
  - adjacent  = related-but-secondary topics
- Output STRICT JSON, no markdown fences, exactly:
  {{"keyword_candidates": [
      {{"group": "core", "keyword": "...", "evidence": "appears in N/{article_count} articles: ..."}},
      ...
  ]}}

ARTICLES:
{articles_joined}

Return JSON now."""


def _coerce_candidates(raw: Any, max_candidates: int) -> list[dict[str, str]]:
    """Validate + normalize the LLM output. Discards malformed entries silently."""
    if not isinstance(raw, dict):
        return []
    items = raw.get("keyword_candidates")
    if not isinstance(items, list):
        return []

    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()
        if not keyword:
            continue
        group = str(item.get("group") or "").strip().lower()
        if group not in _VALID_GROUPS:
            group = _DEFAULT_GROUP
        evidence = str(item.get("evidence") or "").strip()
        key = (group, keyword.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"group": group, "keyword": keyword, "evidence": evidence})
        if len(out) >= max_candidates:
            break
    return out


async def extract_keyword_candidates(
    articles: list[dict[str, Any]],
    *,
    max_candidates: int = 20,
) -> list[dict[str, str]]:
    """Return a list of ``{"group", "keyword", "evidence"}`` candidate dicts.

    ``articles`` is a list of D0 extractor article dicts (with ``text`` and
    ``title`` keys). LLM failures or malformed output are logged and yield
    an empty list — callers must not block on this.
    """
    if not articles:
        return []
    if max_candidates <= 0:
        return []

    snippets = [
        {"title": a.get("title") or "", "text": a.get("text") or ""}
        for a in articles
        if (a.get("text") or "").strip()
    ]
    if not snippets:
        return []

    prompt = _build_prompt(snippets, max_candidates)

    client = LLMClient()
    try:
        raw = await client.chat_json(
            prompt_family="d0-keyword-candidates",
            prompt=prompt,
            max_tokens=1500,
        )
    except Exception as err:  # pragma: no cover — surfaced as graceful empty
        _log.warning("Keyword candidate LLM call failed: %s", err)
        return []

    if not isinstance(raw, (dict, list)):
        # Some clients return a JSON string; try once more.
        try:
            raw = json.loads(raw)  # type: ignore[arg-type]
        except Exception:
            _log.warning("Keyword candidate output not parseable as JSON")
            return []

    return _coerce_candidates(raw, max_candidates)
