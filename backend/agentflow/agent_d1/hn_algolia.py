"""HackerNews Algolia search collector.

Unlike the subscription-style ``hackernews.py`` collector (which pulls top
stories), this one hits HN's public Algolia search API so the caller can
issue a free-form query. Used by :mod:`agent_d1.search` for the
``af search <query>`` CLI path.

Public endpoint (no auth):
    https://hn.algolia.com/api/v1/search?query=<q>&tags=story&numericFilters=...

Docs: https://hn.algolia.com/api
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import RawSignal

_log = get_logger("agent_d1.hn_algolia")

_ALGOLIA_ENDPOINT = "https://hn.algolia.com/api/v1/search"


def _is_mock() -> bool:
    return os.environ.get("MOCK_LLM", "").lower() == "true"


def _mock_signals(query: str) -> list[RawSignal]:
    """Deterministic fixtures for offline tests."""
    now = datetime.now(timezone.utc)
    base = [
        {
            "id": "algolia_mock_1",
            "title": f"[mock] How we rebuilt our workflow around {query}",
            "text": f"A long-form post about {query} in production, 6 months in.",
            "score": 412,
            "num_comments": 203,
            "url": f"https://example.com/mock-{query.lower().replace(' ', '-')}-1",
            "author": "mock_user_a",
        },
        {
            "id": "algolia_mock_2",
            "title": f"[mock] {query}: the underrated part nobody talks about",
            "text": "",
            "score": 287,
            "num_comments": 154,
            "url": f"https://example.com/mock-{query.lower().replace(' ', '-')}-2",
            "author": "mock_user_b",
        },
        {
            "id": "algolia_mock_3",
            "title": f"[mock] Show HN: a tiny CLI for {query}",
            "text": "Spent a weekend building this, curious what people think.",
            "score": 168,
            "num_comments": 71,
            "url": f"https://example.com/mock-{query.lower().replace(' ', '-')}-3",
            "author": "mock_user_c",
        },
    ]
    signals: list[RawSignal] = []
    for item in base:
        signals.append(
            RawSignal(
                source="hackernews_search",
                source_item_id=str(item["id"]),
                author=item["author"],
                text=f"{item['title']}\n\n{item['text']}".strip(),
                url=item["url"],
                published_at=now,
                engagement={
                    "score": int(item["score"]),
                    "num_comments": int(item["num_comments"]),
                },
                raw_metadata={"algolia_mock": True, "query": query},
            )
        )
    return signals


async def search(
    query: str,
    days: int = 7,
    min_points: int = 10,
    hits_per_page: int = 30,
) -> list[RawSignal]:
    """Search HN Algolia for stories matching ``query``.

    ``days``: only include stories newer than N days (via ``numericFilters``).
    ``min_points``: drop stories below this score.
    ``hits_per_page``: Algolia page size (max 1000, default 20; we use 30).
    """
    if _is_mock():
        _log.info("hn_algolia.search: MOCK_LLM=true, returning fixtures")
        return _mock_signals(query)

    import httpx

    now_ts = int(datetime.now(timezone.utc).timestamp())
    min_ts = now_ts - days * 86400

    params = {
        "query": query,
        "tags": "story",
        "numericFilters": f"created_at_i>{min_ts},points>={min_points}",
        "hitsPerPage": str(hits_per_page),
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.get(_ALGOLIA_ENDPOINT, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _log.warning("hn_algolia.search HTTP error: %s", exc)
            return []

    data = resp.json()
    hits: list[dict[str, Any]] = data.get("hits", []) or []
    _log.info(
        "hn_algolia.search: query=%r days=%d min_points=%d → %d hits",
        query, days, min_points, len(hits),
    )

    signals: list[RawSignal] = []
    for hit in hits:
        title = hit.get("title") or hit.get("story_title") or ""
        body = hit.get("story_text") or hit.get("comment_text") or ""
        text = f"{title}\n\n{body}".strip() if body else title
        if not text:
            continue

        obj_id = str(hit.get("objectID", ""))
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
        author = hit.get("author")

        created_raw = hit.get("created_at")
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except Exception:
            created = datetime.now(timezone.utc)

        signals.append(
            RawSignal(
                source="hackernews_search",
                source_item_id=obj_id,
                author=author,
                text=text,
                url=url,
                published_at=created,
                engagement={
                    "score": int(hit.get("points") or 0),
                    "num_comments": int(hit.get("num_comments") or 0),
                },
                raw_metadata={"algolia_query": query},
            )
        )

    return signals
