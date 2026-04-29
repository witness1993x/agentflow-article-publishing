"""HackerNews collector.

Hits the public Firebase endpoint for top stories, filters by score +
optional keywords. Mock mode returns a small, deterministic set.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from agentflow.shared.logger import get_logger
from agentflow.shared.models import RawSignal

_log = get_logger("agent_d1.hackernews")


_HN_BASE = "https://hacker-news.firebaseio.com/v0"


def _is_mock() -> bool:
    return os.environ.get("MOCK_LLM", "").lower() == "true"


_MOCK_STORIES = [
    {
        "id": 98000001,
        "title": "Claude Code's subagent primitive: an underrated unlock",
        "text": "",
        "score": 412,
        "descendants": 203,
    },
    {
        "id": 98000002,
        "title": "Vibe Coding in production: 6 months with spec-driven LLM pairing",
        "text": "",
        "score": 318,
        "descendants": 172,
    },
    {
        "id": 98000003,
        "title": "Why agent frameworks over-promise: the state-machine gap",
        "text": "",
        "score": 221,
        "descendants": 91,
    },
]


def _match_keywords(text: str, keywords: list[str] | None) -> bool:
    if not keywords:
        return True
    low = text.lower()
    return any(kw.lower() in low for kw in keywords)


def _mock_signals(filter_keywords: list[str] | None, min_score: int) -> list[RawSignal]:
    base = datetime.now(timezone.utc) - timedelta(hours=6)
    out: list[RawSignal] = []
    for i, story in enumerate(_MOCK_STORIES):
        if story["score"] < min_score:
            continue
        title = story["title"]
        if not _match_keywords(title, filter_keywords):
            continue
        out.append(
            RawSignal(
                source="hackernews",
                source_item_id=str(story["id"]),
                author="hn",
                text=title,
                url=f"https://news.ycombinator.com/item?id={story['id']}",
                published_at=base - timedelta(hours=i * 2 + 1),
                engagement={
                    "hn_score": int(story["score"]),
                    "comment_count": int(story["descendants"]),
                },
                raw_metadata={"mock": True},
            )
        )
    return out


# ---------------------------------------------------------------------------
# Real async fetch via aiohttp
# ---------------------------------------------------------------------------


async def _fetch_real(
    filter_keywords: list[str] | None, min_score: int
) -> list[RawSignal]:
    try:
        import aiohttp  # type: ignore
    except ImportError:
        _log.warning("aiohttp not installed; returning empty HN signals")
        return []

    signals: list[RawSignal] = []
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{_HN_BASE}/topstories.json") as resp:
                resp.raise_for_status()
                top_ids = await resp.json()
            top_ids = (top_ids or [])[:30]

            async def _fetch_item(item_id: int) -> dict | None:
                try:
                    async with session.get(f"{_HN_BASE}/item/{item_id}.json") as r:
                        r.raise_for_status()
                        return await r.json()
                except Exception as err:  # pragma: no cover - network
                    _log.warning("HN item fetch failed (%s): %s", item_id, err)
                    return None

            items = await asyncio.gather(*[_fetch_item(i) for i in top_ids])

            for item in items:
                if not item:
                    continue
                score = int(item.get("score", 0) or 0)
                if score < min_score:
                    continue
                title = item.get("title", "") or ""
                body = item.get("text", "") or ""
                combined = f"{title}\n\n{body}".strip() if body else title
                if not _match_keywords(combined, filter_keywords):
                    continue
                ts = item.get("time")
                published = (
                    datetime.fromtimestamp(int(ts), tz=timezone.utc)
                    if ts
                    else datetime.now(timezone.utc)
                )
                item_id = item.get("id")
                signals.append(
                    RawSignal(
                        source="hackernews",
                        source_item_id=str(item_id),
                        author="hn",
                        text=combined,
                        url=f"https://news.ycombinator.com/item?id={item_id}",
                        published_at=published,
                        engagement={
                            "hn_score": score,
                            "comment_count": int(item.get("descendants", 0) or 0),
                        },
                        raw_metadata={"type": item.get("type")},
                    )
                )
    except Exception as err:  # pragma: no cover - network
        _log.warning("HackerNews fetch failed: %s", err)
        return []

    return signals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def collect(
    filter_keywords: list[str] | None = None, min_score: int = 50
) -> list[RawSignal]:
    """Fetch the top 30 HN stories, filter by score + optional keywords."""
    if _is_mock():
        _log.info("hackernews.collect: using mock data (MOCK_LLM=true)")
        return _mock_signals(filter_keywords, min_score)
    try:
        return await _fetch_real(filter_keywords, min_score)
    except Exception as err:  # pragma: no cover - defensive
        _log.warning("hackernews.collect failed, returning empty: %s", err)
        return []
