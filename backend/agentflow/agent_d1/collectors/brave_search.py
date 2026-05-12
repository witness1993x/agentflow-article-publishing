"""Brave Web Search collector (v1.1.9).

Third recall layer alongside ``twitter.py`` (KOL pulls) and
``twitter_search.py`` (Twitter keyword search). Where Twitter recall is
constrained to the Twitter graph, Brave gives us **independent web
discovery** — vendor blogs, research posts, GitHub READMEs, niche
publications — for the same query universe.

Behaviour matrix (mirrors twitter_search.py exactly so the recall pipeline
stays uniform):

* ``MOCK_LLM=true``                                → deterministic fixtures.
* ``AGENTFLOW_BRAVE_SEARCH_ENABLED`` != ``true``  → empty (default off,
                                                     backward compat).
* ENABLED + no ``BRAVE_API_KEY``                   → empty + warning;
  + ``MOCK_LLM`` not set                             never falls back to mocks.
* ENABLED + key present                            → real Brave Search API
                                                     (https://api.search.brave.com).

Brave's free tier is 1 query/sec; the collector self-paces between calls so
back-to-back queries from a single sources.yaml don't trip 429.

Each emitted ``RawSignal`` carries ``raw_metadata.via="brave_search"`` so
downstream code can tell vendor-blog finds apart from Twitter chatter.
``source="rss"`` is reused — Brave results have the same shape as RSS
entries (title + snippet + URL + age) and the rest of D1 already knows
how to score them.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import RawSignal

_log = get_logger("agent_d1.brave_search")


# Brave Web Search API endpoint. Returned shape (relevant fields):
#   {"web": {"results": [{"title": "...", "url": "...",
#                          "description": "...", "age": "2d"}]}}
_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

# Brave free tier: 1 query/sec, 2k/month. Paid: 20 qps.
# We self-pace at 1.1s between calls regardless of tier so a misconfigured
# yaml with 30 queries doesn't burn the entire monthly quota in 30 seconds.
_MIN_INTERVAL_SECONDS = 1.1

# Per-query result cap. Brave allows up to 20; we default to 10 because
# DBSCAN clustering downstream gets noisy past that without strong dedup.
_DEFAULT_COUNT = 10
_API_MAX_COUNT = 20


def _is_mock() -> bool:
    return os.environ.get("MOCK_LLM", "").lower() == "true"


def _is_enabled() -> bool:
    return (
        os.environ.get("AGENTFLOW_BRAVE_SEARCH_ENABLED", "")
        .strip()
        .lower()
        == "true"
    )


def _query_hash(query: str) -> str:
    return hashlib.blake2b(query.encode("utf-8"), digest_size=4).hexdigest()


def _clamp_count(n: int) -> int:
    return max(1, min(int(n or _DEFAULT_COUNT), _API_MAX_COUNT))


# ---------------------------------------------------------------------------
# Mock fixtures (deterministic per query)
# ---------------------------------------------------------------------------


_MOCK_TEMPLATES = [
    (
        "Brave search recall: {query} surfaced a vendor research post that "
        "no Twitter KOL would have linked. Independent web discovery is the "
        "missing layer."
    ),
    (
        "Top hit on {query}: a long-form engineering blog from a tier-1 "
        "infra team — the kind of signal that never makes it to Twitter "
        "because they don't post there."
    ),
    (
        "Niche aggregator on {query}: catches GitHub READMEs and Substack "
        "drops that don't show up in HN or KOL timelines. Wider net, lower "
        "noise floor than HN broad-keyword search."
    ),
]


def _mock_search(query: str, count: int, weight: str) -> list[RawSignal]:
    qhash = _query_hash(query)
    seed = int(qhash, 16)
    base = datetime.now(timezone.utc) - timedelta(hours=3)

    out: list[RawSignal] = []
    n = min(max(count, 2), len(_MOCK_TEMPLATES))
    for i in range(n):
        text = _MOCK_TEMPLATES[i].format(query=query)
        published = base - timedelta(hours=i * 6)
        out.append(
            RawSignal(
                source="rss",  # see module docstring — Brave hits look like RSS
                source_item_id=f"brave_{qhash}_{i}",
                author=f"brave:{qhash}",
                text=f"{text}\n\nQuery: {query}",
                url=f"https://example.test/brave/{seed + i}",
                published_at=published,
                engagement={"score": 50 + (seed + i) % 50},
                raw_metadata={
                    "mock": True,
                    "via": "brave_search",
                    "query": query,
                    "weight": weight,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Real Brave HTTP call (sync, wrapped in to_thread)
# ---------------------------------------------------------------------------


def _parse_age_to_published_at(age: str | None) -> datetime:
    """Brave returns ages like '2d', '4 hours ago', '2026-04-30T12:00:00Z'.
    Best-effort: fall back to now when we can't parse, so unparseable freshness
    just looks "fresh enough" rather than dropping the signal."""
    now = datetime.now(timezone.utc)
    if not age:
        return now
    raw = str(age).strip().lower()
    try:
        # ISO-ish first
        if "t" in raw and ":" in raw:
            cleaned = raw.replace("z", "+00:00")
            return datetime.fromisoformat(cleaned)
    except ValueError:
        pass
    # "Xd" / "Xh" / "Xm" relative
    for suffix, unit in (("d", "days"), ("h", "hours"), ("m", "minutes")):
        if raw.endswith(suffix):
            try:
                qty = int(raw[: -len(suffix)].strip())
                return now - timedelta(**{unit: qty})
            except ValueError:
                continue
    # "X days ago" / "X hours ago"
    for unit in ("days", "hours", "minutes"):
        token = f" {unit[:-1]}"  # "day"/"hour"/"minute"
        if token in raw:
            try:
                qty = int(raw.split(token)[0].strip().split()[-1])
                return now - timedelta(**{unit: qty})
            except (ValueError, IndexError):
                continue
    return now


def _fetch_real_sync(queries: list[dict[str, Any]]) -> list[RawSignal]:
    try:
        import requests  # type: ignore
    except ImportError:
        _log.warning(
            "requests not installed; returning empty Brave search signals"
        )
        return []

    api_key = os.getenv("BRAVE_API_KEY")
    if not api_key:
        return []

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
        "User-Agent": "agentflow/1.1.9 (+brave_search)",
    }

    signals: list[RawSignal] = []
    last_call_at = 0.0

    for entry in queries:
        query = (entry.get("query") or "").strip()
        if not query:
            continue
        count = _clamp_count(int(entry.get("count") or _DEFAULT_COUNT))
        weight = str(entry.get("weight") or "").strip().lower()
        qhash = _query_hash(query)

        # Self-pace to stay under the free tier rate cap.
        elapsed = time.monotonic() - last_call_at
        if elapsed < _MIN_INTERVAL_SECONDS:
            time.sleep(_MIN_INTERVAL_SECONDS - elapsed)

        try:
            resp = requests.get(
                _BRAVE_ENDPOINT,
                params={"q": query, "count": count},
                headers=headers,
                timeout=15,
            )
            last_call_at = time.monotonic()
            if resp.status_code == 429:
                _log.warning(
                    "brave_search: rate-limited on %r — backing off + skipping rest of batch",
                    query,
                )
                # Don't raise — preserve whatever signals we already have.
                break
            if not resp.ok:
                _log.warning(
                    "brave_search: HTTP %s on %r — %s",
                    resp.status_code, query, resp.text[:200],
                )
                continue
            body = resp.json() or {}
            results = ((body.get("web") or {}).get("results") or [])
            for i, item in enumerate(results):
                title = str(item.get("title") or "").strip()
                description = str(item.get("description") or "").strip()
                url = str(item.get("url") or "").strip()
                if not (title and url):
                    continue
                age = item.get("age") or item.get("page_age")
                published = _parse_age_to_published_at(age)
                # Title + snippet are the actual textual signal — D2's
                # clustering tokenizer reads RawSignal.text.
                text = f"{title}\n\n{description}" if description else title
                signals.append(
                    RawSignal(
                        source="rss",
                        source_item_id=f"brave_{qhash}_{i}_{hashlib.md5(url.encode()).hexdigest()[:8]}",
                        author=f"brave:{qhash}",
                        text=text,
                        url=url,
                        published_at=published,
                        engagement={"score": int(item.get("score") or 0)},
                        raw_metadata={
                            "via": "brave_search",
                            "query": query,
                            "weight": weight,
                            "title": title,
                        },
                    )
                )
        except Exception as err:  # pragma: no cover — network
            _log.warning(
                "brave_search.collect failed for query %r: %s", query, err,
            )
            continue

    return signals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def collect(
    queries: list[dict[str, Any]],
    *,
    default_count: int = _DEFAULT_COUNT,
) -> list[RawSignal]:
    """Run Brave Web Search for each query.

    ``queries``: list of ``{"query": "MEV OR rollup", "count": 10,
    "weight": "medium"}``. Per-query ``count`` falls back to ``default_count``
    when missing.

    Returns an empty list (without raising) on every error path:
      * no queries
      * collector disabled
      * real-mode without ``BRAVE_API_KEY``
      * requests missing or per-query API failures
    """
    if not queries:
        return []

    if not _is_enabled():
        return []

    normalized: list[dict[str, Any]] = []
    for q in queries:
        if not isinstance(q, dict):
            continue
        query = (q.get("query") or "").strip()
        if not query:
            continue
        count = q.get("count") or default_count
        weight = q.get("weight") or ""
        normalized.append({
            "query": query,
            "count": int(count),
            "weight": str(weight),
        })
    if not normalized:
        return []

    if _is_mock():
        _log.info("brave_search.collect: using mock data (MOCK_LLM=true)")
        out: list[RawSignal] = []
        for entry in normalized:
            out.extend(
                _mock_search(entry["query"], entry["count"], entry["weight"])
            )
        return out

    if not os.getenv("BRAVE_API_KEY"):
        # Mirrors twitter_search.py v1.0.8 contract: enabled + missing key +
        # not in mock mode = silent skip with a warning, never fabricate.
        _log.warning(
            "brave_search.collect: skipping (no BRAVE_API_KEY, "
            "MOCK_LLM not set; refusing to fabricate signals)"
        )
        return []

    try:
        return await asyncio.to_thread(_fetch_real_sync, normalized)
    except Exception as err:  # pragma: no cover — defensive
        _log.warning("brave_search.collect failed, returning empty: %s", err)
        return []
