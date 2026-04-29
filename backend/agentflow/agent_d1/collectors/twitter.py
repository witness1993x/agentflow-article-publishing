"""Twitter/X collector.

Real mode uses tweepy + Bearer Token. Missing token OR ``MOCK_LLM=true`` yields
deterministic mock tweets so the pipeline is runnable without API keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone

from agentflow.shared.logger import get_logger
from agentflow.shared.models import RawSignal

_log = get_logger("agent_d1.twitter")


def _is_mock() -> bool:
    return os.environ.get("MOCK_LLM", "").lower() == "true"


def _normalize_handle(handle: str) -> str:
    return handle.lstrip("@")


# ---------------------------------------------------------------------------
# Mock data (deterministic per handle)
# ---------------------------------------------------------------------------

_MOCK_TEMPLATES = [
    (
        "Spent the day wiring Claude Code subagents into our QA loop. "
        "The real unlock is not speed — it's how much context rot we avoid. "
        "Parallel subagents > bigger single-shot prompts, every time."
    ),
    (
        "Hot take: Vibe Coding is not replacing engineers, it's replacing "
        "the busywork PMs used to route through engineers. Specs-as-code is "
        "where the next 10x lives."
    ),
    (
        "Agent frameworks are massively overfit to demos. If your workflow "
        "can't survive 3 turns without a state machine, you don't have an "
        "agent, you have a prompt with ambition."
    ),
]


def _mock_tweets(handle: str, max_results: int) -> list[RawSignal]:
    clean = _normalize_handle(handle)
    # Deterministic seed from handle.
    seed = int(hashlib.blake2b(clean.encode("utf-8"), digest_size=4).hexdigest(), 16)
    base = datetime.now(timezone.utc) - timedelta(hours=3)

    out: list[RawSignal] = []
    count = min(max_results, len(_MOCK_TEMPLATES))
    for i in range(count):
        tweet_id = f"mock_{clean}_{i}"
        text = _MOCK_TEMPLATES[i]
        published = base - timedelta(hours=i * 5)
        out.append(
            RawSignal(
                source="twitter",
                source_item_id=tweet_id,
                author=f"@{clean}",
                text=text,
                url=f"https://twitter.com/{clean}/status/{seed + i}",
                published_at=published,
                engagement={
                    "reply_count": 12 + (seed + i) % 30,
                    "retweet_count": 20 + (seed + i) % 50,
                    "like_count": 100 + (seed + i) % 400,
                    "quote_count": 4 + (seed + i) % 10,
                },
                raw_metadata={"mock": True, "handle": clean},
            )
        )
    return out


# ---------------------------------------------------------------------------
# Real tweepy collector (sync under the hood, wrapped in to_thread)
# ---------------------------------------------------------------------------


def _fetch_real_sync(
    kol_handles: list[str], max_results_per_kol: int
) -> list[RawSignal]:
    try:
        import tweepy  # type: ignore
    except ImportError:
        _log.warning("tweepy not installed; returning empty Twitter signals")
        return []

    bearer = os.getenv("TWITTER_BEARER_TOKEN")
    if not bearer:
        return []

    client = tweepy.Client(bearer_token=bearer)
    signals: list[RawSignal] = []

    for handle in kol_handles:
        clean = _normalize_handle(handle)
        try:
            user_resp = client.get_user(username=clean)
            user = getattr(user_resp, "data", None)
            if user is None:
                _log.warning("Twitter user not found: %s", clean)
                continue
            user_id = user.id

            tweets_resp = client.get_users_tweets(
                id=user_id,
                max_results=max(5, min(max_results_per_kol, 100)),
                tweet_fields=["created_at", "public_metrics"],
            )
            tweets = getattr(tweets_resp, "data", None) or []
            for tw in tweets:
                metrics = getattr(tw, "public_metrics", {}) or {}
                created = getattr(tw, "created_at", None)
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                signals.append(
                    RawSignal(
                        source="twitter",
                        source_item_id=str(tw.id),
                        author=f"@{clean}",
                        text=tw.text,
                        url=f"https://twitter.com/{clean}/status/{tw.id}",
                        published_at=created or datetime.now(timezone.utc),
                        engagement={
                            "reply_count": int(metrics.get("reply_count", 0)),
                            "retweet_count": int(metrics.get("retweet_count", 0)),
                            "like_count": int(metrics.get("like_count", 0)),
                            "quote_count": int(metrics.get("quote_count", 0)),
                        },
                        raw_metadata={"handle": clean},
                    )
                )
        except Exception as err:  # pragma: no cover - network
            _log.warning("Twitter fetch failed for %s: %s", clean, err)
            continue

    return signals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def collect(
    kol_handles: list[str], max_results_per_kol: int = 20
) -> list[RawSignal]:
    """Collect recent tweets from a set of KOL handles.

    Behaviour matrix:

    * ``MOCK_LLM=true``                    → deterministic fixtures (mock).
    * No ``TWITTER_BEARER_TOKEN`` AND      → SKIP (return empty, log warning).
      ``MOCK_LLM`` not set                   This avoids polluting real hotspots
                                             scans with synthetic tweets when
                                             the operator hasn't opted into
                                             Twitter and forgot to set
                                             ``MOCK_LLM=true``.
    * Bearer token present                 → real Twitter API.

    Never raises — per-handle failures are logged and skipped.
    """
    if not kol_handles:
        return []

    bearer_present = bool(os.getenv("TWITTER_BEARER_TOKEN"))
    if _is_mock():
        # Explicit opt-in to mocks — used by CI / smoke tests / dev runs.
        _log.info("twitter.collect: using mock data (MOCK_LLM=true)")
        out: list[RawSignal] = []
        for handle in kol_handles:
            out.extend(_mock_tweets(handle, max_results_per_kol))
        return out

    if not bearer_present:
        # Operator hasn't supplied a Twitter token AND hasn't opted into
        # mocks. Silently mocking would inject synthetic-looking tweets into a
        # production hotspots scan, corrupting downstream clustering /
        # angle-mining / publish decisions. Skip instead.
        _log.warning(
            "twitter.collect: skipping (no TWITTER_BEARER_TOKEN, "
            "MOCK_LLM not set; refusing to fabricate signals)"
        )
        return []

    try:
        return await asyncio.to_thread(
            _fetch_real_sync, kol_handles, max_results_per_kol
        )
    except Exception as err:  # pragma: no cover - defensive
        _log.warning("twitter.collect failed, returning empty: %s", err)
        return []
