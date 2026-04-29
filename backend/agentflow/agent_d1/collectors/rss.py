"""RSS collector.

Uses ``feedparser`` under the hood. In mock mode returns deterministic entries
so DBSCAN still has multi-signal input. Per-feed failures are logged and
skipped — one broken feed never breaks the rest.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from time import mktime

from agentflow.shared.logger import get_logger
from agentflow.shared.models import RawSignal

_log = get_logger("agent_d1.rss")


def _is_mock() -> bool:
    return os.environ.get("MOCK_LLM", "").lower() == "true"


_MOCK_ENTRIES = [
    (
        "Why Claude subagents changed our CI pipeline",
        "A walk-through of how we replaced a 900-line bash CI script with "
        "three Claude Code subagents running in parallel. Latency down 40%, "
        "context-rot-induced bugs gone.",
    ),
    (
        "State machines are the missing layer in agent frameworks",
        "Most agent frameworks ship as DAGs. We argue the right primitive is "
        "a state machine with explicit resume semantics, especially once you "
        "have >3 tool-calls per turn.",
    ),
    (
        "Vibe Coding: a year-one retrospective from a 3-person team",
        "We rebuilt our onboarding flow entirely with spec-driven LLM "
        "pair-programming. Here's what broke, what didn't, and what we'd do "
        "different on day one.",
    ),
]


def _mock_signals_for_feed(feed: dict) -> list[RawSignal]:
    url = feed.get("url", "")
    name = feed.get("name") or url
    seed = int(hashlib.blake2b(url.encode("utf-8"), digest_size=4).hexdigest(), 16)
    base = datetime.now(timezone.utc) - timedelta(hours=4)

    out: list[RawSignal] = []
    for i, (title, summary) in enumerate(_MOCK_ENTRIES):
        # Only emit 2 mock entries per feed to keep total signal count reasonable.
        if i >= 2:
            break
        entry_id = f"mock_rss_{seed}_{i}"
        published = base - timedelta(hours=i * 6 + 1)
        out.append(
            RawSignal(
                source="rss",
                source_item_id=entry_id,
                author=name,
                text=f"{title}\n\n{summary}",
                url=f"{url}#mock-{i}",
                published_at=published,
                engagement={},
                raw_metadata={"mock": True, "feed_name": name, "feed_url": url},
            )
        )
    return out


# ---------------------------------------------------------------------------
# Real per-feed fetch (sync — wrapped in thread)
# ---------------------------------------------------------------------------


def _fetch_real_sync(feeds: list[dict]) -> list[RawSignal]:
    try:
        import feedparser  # type: ignore
    except ImportError:
        _log.warning("feedparser not installed; returning empty RSS signals")
        return []

    signals: list[RawSignal] = []
    for feed in feeds:
        url = feed.get("url")
        name = feed.get("name") or url
        if not url:
            continue
        try:
            parsed = feedparser.parse(url)
            entries = getattr(parsed, "entries", []) or []
            feed_title = getattr(getattr(parsed, "feed", None), "title", name) or name
            for entry in entries[:15]:
                title = getattr(entry, "title", "") or ""
                summary = getattr(entry, "summary", "") or entry.get("description", "") or ""
                link = getattr(entry, "link", "") or entry.get("id", url)

                published_at = None
                for attr in ("published_parsed", "updated_parsed"):
                    tp = getattr(entry, attr, None) or entry.get(attr)
                    if tp:
                        try:
                            published_at = datetime.fromtimestamp(
                                mktime(tp), tz=timezone.utc
                            )
                            break
                        except Exception:
                            pass
                if published_at is None:
                    published_at = datetime.now(timezone.utc)

                text = f"{title}\n\n{summary}".strip()
                if not text:
                    continue

                signals.append(
                    RawSignal(
                        source="rss",
                        source_item_id=getattr(entry, "id", link) or link,
                        author=feed_title,
                        text=text,
                        url=link,
                        published_at=published_at,
                        engagement={},
                        raw_metadata={"feed_name": feed_title, "feed_url": url},
                    )
                )
        except Exception as err:  # pragma: no cover - network
            _log.warning("RSS fetch failed for %s: %s", url, err)
            continue

    return signals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def collect(feeds: list[dict]) -> list[RawSignal]:
    """Collect entries from a list of RSS feed configs (each a dict with url, name).

    Returns deterministic mocks if ``MOCK_LLM=true``. Per-feed failures never
    abort the whole call.
    """
    if not feeds:
        return []

    if _is_mock():
        _log.info("rss.collect: using mock data (MOCK_LLM=true)")
        out: list[RawSignal] = []
        for feed in feeds:
            out.extend(_mock_signals_for_feed(feed))
        return out

    try:
        return await asyncio.to_thread(_fetch_real_sync, feeds)
    except Exception as err:  # pragma: no cover - defensive
        _log.warning("rss.collect failed, returning empty: %s", err)
        return []
