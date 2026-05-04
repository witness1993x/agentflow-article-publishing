"""Twitter/X keyword search collector (v1.0.26).

Second recall layer alongside ``twitter.py``'s curated-KOL pulls. Where the
KOL collector pulls timelines for handles in ``sources.yaml::twitter_kols``,
this collector runs ``search_recent_tweets`` against the v2 API for each
query in ``sources.yaml::twitter_search``.

Behaviour matrix (mirrors twitter.py v1.0.8 design — refuse to silently
fabricate signals when the operator hasn't opted into mocks):

* ``MOCK_LLM=true``                                → deterministic fixtures.
* ``AGENTFLOW_TWITTER_SEARCH_ENABLED`` != ``true`` → empty (default off,
                                                     backward compat).
* ENABLED + no ``TWITTER_BEARER_TOKEN``            → empty + warning;
  + ``MOCK_LLM`` not set                             never falls back to mocks.
* ENABLED + bearer present                         → real Twitter v2 API.

Each emitted ``RawSignal`` carries ``raw_metadata.via="search"`` so
downstream code can discriminate KOL-pull vs search provenance even though
both share ``source="twitter"`` (so ``_provenance_summary`` aggregates them).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import RawSignal

_log = get_logger("agent_d1.twitter_search")

# v2 search_recent_tweets caps max_results at 100 per call (essential tier
# guarantees 10..100). Anything higher is rejected by the API; clamp here so
# operator-supplied configs never trip the upstream 400.
_API_MAX_RESULTS_CAP = 100


def _is_mock() -> bool:
    return os.environ.get("MOCK_LLM", "").lower() == "true"


def _is_enabled() -> bool:
    return (
        os.environ.get("AGENTFLOW_TWITTER_SEARCH_ENABLED", "")
        .strip()
        .lower()
        == "true"
    )


def _query_hash(query: str) -> str:
    return hashlib.blake2b(query.encode("utf-8"), digest_size=4).hexdigest()


def _clamp_max_results(n: int) -> int:
    # v2 search_recent_tweets requires 10..100. Clamp aggressively so the
    # collector never causes a Twitter 400 because of a misconfigured yaml.
    return max(10, min(int(n or 10), _API_MAX_RESULTS_CAP))


# ---------------------------------------------------------------------------
# Mock fixtures (deterministic per query)
# ---------------------------------------------------------------------------

_MOCK_TEMPLATES = [
    (
        "Search recall: spotted a thread on {query} that nobody curated KOL "
        "list would surface. Real-time keyword recall is the missing half "
        "of a hotspots scan."
    ),
    (
        "Hot take on {query}: the noise floor on this term is high but the "
        "signal is concentrated in the first 30 tweets. Reranking >> better "
        "queries."
    ),
    (
        "If your scan only watches a fixed KOL list you'll miss the next "
        "breakout author every time. Expanding {query} to firehose search "
        "fixed our recall gap overnight."
    ),
]


def _mock_search(query: str, max_results: int, weight: str) -> list[RawSignal]:
    """Return a deterministic 2-3 tweet fixture for a query.

    Each signal is tagged with ``raw_metadata.mock=True`` so the v1.0.10
    mock-tag drop in agent_d1.main catches them in real-mode regressions.
    """
    qhash = _query_hash(query)
    seed = int(qhash, 16)
    base = datetime.now(timezone.utc) - timedelta(hours=2)

    out: list[RawSignal] = []
    count = min(max(max_results, 2), len(_MOCK_TEMPLATES))
    for i in range(count):
        tweet_id = f"mock_search_{qhash}_{i}"
        text = _MOCK_TEMPLATES[i].format(query=query)
        published = base - timedelta(hours=i * 4)
        out.append(
            RawSignal(
                source="twitter",
                source_item_id=f"search_{tweet_id}",
                author=f"@search_{qhash}",
                text=text,
                url=f"https://twitter.com/i/web/status/{seed + i}",
                published_at=published,
                engagement={
                    "reply_count": 5 + (seed + i) % 20,
                    "retweet_count": 8 + (seed + i) % 30,
                    "like_count": 40 + (seed + i) % 200,
                    "quote_count": 1 + (seed + i) % 6,
                },
                raw_metadata={
                    "mock": True,
                    "via": "search",
                    "query": query,
                    "weight": weight,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Real tweepy search (sync, wrapped in to_thread)
# ---------------------------------------------------------------------------


def _fetch_real_sync(queries: list[dict[str, Any]]) -> list[RawSignal]:
    try:
        import tweepy  # type: ignore
    except ImportError:
        _log.warning("tweepy not installed; returning empty Twitter search signals")
        return []

    bearer = os.getenv("TWITTER_BEARER_TOKEN")
    if not bearer:
        return []

    client = tweepy.Client(bearer_token=bearer)
    signals: list[RawSignal] = []

    for entry in queries:
        query = (entry.get("query") or "").strip()
        if not query:
            continue
        max_results = _clamp_max_results(int(entry.get("max_results") or 20))
        weight = str(entry.get("weight") or "").strip().lower()
        qhash = _query_hash(query)

        try:
            resp = client.search_recent_tweets(
                query=query,
                max_results=max_results,
                tweet_fields=["created_at", "public_metrics", "author_id"],
            )
            tweets = getattr(resp, "data", None) or []

            # Best-effort author lookup: ``includes.users`` is only populated
            # when the caller passes ``expansions=["author_id"]`` and
            # ``user_fields``. tweepy may or may not surface it depending on
            # the call signature — we tolerate both shapes.
            includes = getattr(resp, "includes", None) or {}
            users_by_id: dict[str, str] = {}
            users = includes.get("users") if isinstance(includes, dict) else None
            for u in users or []:
                uid = getattr(u, "id", None)
                uname = getattr(u, "username", None)
                if uid is not None and uname:
                    users_by_id[str(uid)] = uname

            for tw in tweets:
                metrics = getattr(tw, "public_metrics", {}) or {}
                created = getattr(tw, "created_at", None)
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)

                author_id = getattr(tw, "author_id", None)
                username = users_by_id.get(str(author_id)) if author_id else None
                author = (
                    f"@{username}" if username else f"@search_{qhash}"
                )

                signals.append(
                    RawSignal(
                        source="twitter",
                        source_item_id=f"search_{tw.id}",
                        author=author,
                        text=getattr(tw, "text", "") or "",
                        url=f"https://twitter.com/i/web/status/{tw.id}",
                        published_at=created or datetime.now(timezone.utc),
                        engagement={
                            "reply_count": int(metrics.get("reply_count", 0)),
                            "retweet_count": int(metrics.get("retweet_count", 0)),
                            "like_count": int(metrics.get("like_count", 0)),
                            "quote_count": int(metrics.get("quote_count", 0)),
                        },
                        raw_metadata={
                            "via": "search",
                            "query": query,
                            "weight": weight,
                        },
                    )
                )
        except Exception as err:  # pragma: no cover - network
            _log.warning(
                "twitter_search.collect failed for query %r: %s", query, err,
            )
            continue

    return signals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def collect(
    queries: list[dict[str, Any]],
    *,
    default_max_results: int = 20,
) -> list[RawSignal]:
    """Run Twitter v2 ``search_recent_tweets`` for each query.

    ``queries``: list of ``{"query": "MEV OR rollup", "max_results": 30,
    "weight": "medium"}``. Per-query ``max_results`` falls back to
    ``default_max_results`` when missing.

    Returns an empty list (without raising) on every error path:
      * no queries
      * collector disabled
      * real-mode without bearer token
      * tweepy missing or per-query API failures
    """
    if not queries:
        return []

    if not _is_enabled():
        # Default-off: until the operator opts into search recall, the
        # collector emits nothing even if ``twitter_search`` is populated
        # in sources.yaml. Backward-compat for v<=1.0.25 deployments.
        return []

    # Normalize per-query payloads (fill in default_max_results, keep weight).
    normalized: list[dict[str, Any]] = []
    for q in queries:
        if not isinstance(q, dict):
            continue
        query = (q.get("query") or "").strip()
        if not query:
            continue
        max_results = q.get("max_results") or default_max_results
        weight = q.get("weight") or ""
        normalized.append(
            {
                "query": query,
                "max_results": int(max_results),
                "weight": str(weight),
            }
        )
    if not normalized:
        return []

    if _is_mock():
        # Explicit opt-in to mocks — used by CI / smoke tests / dev runs.
        _log.info("twitter_search.collect: using mock data (MOCK_LLM=true)")
        out: list[RawSignal] = []
        for entry in normalized:
            out.extend(
                _mock_search(
                    entry["query"], entry["max_results"], entry["weight"],
                )
            )
        return out

    bearer_present = bool(os.getenv("TWITTER_BEARER_TOKEN"))
    if not bearer_present:
        # Operator enabled the search recall path but didn't supply a token
        # AND didn't opt into mocks. Silently mocking would inject synthetic
        # tweets into a production hotspots scan; refuse instead. Mirrors
        # twitter.py v1.0.8 behaviour exactly.
        _log.warning(
            "twitter_search.collect: skipping (no TWITTER_BEARER_TOKEN, "
            "MOCK_LLM not set; refusing to fabricate signals)"
        )
        return []

    try:
        return await asyncio.to_thread(_fetch_real_sync, normalized)
    except Exception as err:  # pragma: no cover - defensive
        _log.warning("twitter_search.collect failed, returning empty: %s", err)
        return []
