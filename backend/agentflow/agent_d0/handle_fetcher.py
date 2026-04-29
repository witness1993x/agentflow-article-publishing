"""Resolve an author handle / blog URL to recent article URLs.

Used by ``af learn-from-handle`` (and the onboarding wizard) so users can
seed their style corpus with their own past writing in one shot:

    af learn-from-handle medium.com/@username
    af learn-from-handle alice.substack.com
    af learn-from-handle https://example.com/feed.xml
    af learn-from-handle 0xabc.mirror.xyz

The resolver normalizes the input to an RSS/Atom feed URL where possible,
fetches it, and returns the top-N article URLs. The caller then dispatches
each URL through the existing D0 ingestion path
(``agent_d0.main.run(url=[...])``).

No platform-specific auth required — RSS is the universal contract.
Twitter / X is intentionally NOT supported here: anonymous timeline scraping
is blocked, and OAuth-flavored ingestion belongs in a separate path that
reuses ``TWITTER_BEARER_TOKEN``. Use ``--file`` / ``--url`` for tweets.
"""

from __future__ import annotations

import re
from typing import NamedTuple


_FEED_SUFFIX_RE = re.compile(r"/(feed|rss|atom|index\.xml)/?$|\.(xml|atom|rss)$", re.I)
_MEDIUM_USER_RE = re.compile(r"medium\.com/@([\w.\-]+)", re.I)
_SUBSTACK_RE = re.compile(r"^([a-z0-9][a-z0-9\-]*)\.substack\.com", re.I)
_MIRROR_RE = re.compile(r"^([0-9a-fA-Fx]+)\.mirror\.xyz", re.I)


class ResolvedSource(NamedTuple):
    feed_url: str | None     # None means "this IS already a single article URL"
    direct_url: str | None   # set when input is just one article we should ingest directly
    label: str               # human-readable description for logs


def resolve(handle_or_url: str) -> ResolvedSource:
    """Normalize a handle/URL to either a feed URL or a direct article URL.

    Recognized patterns:
      ``medium.com/@user``                    → https://medium.com/feed/@user
      ``@user`` (with --platform medium)      → https://medium.com/feed/@user
      ``alice.substack.com``                  → https://alice.substack.com/feed
      ``0xabc.mirror.xyz``                    → https://0xabc.mirror.xyz/feed.atom
      ``https://blog.example.com/feed``       → passes through (looks like feed)
      ``https://blog.example.com/2024/post``  → treated as a single article URL
    """
    raw = (handle_or_url or "").strip()
    if not raw:
        raise ValueError("empty handle / URL")

    # Strip leading scheme for pattern matching, keep canonical form for return.
    bare = re.sub(r"^https?://", "", raw, flags=re.I).rstrip("/")

    # Medium @username
    m = _MEDIUM_USER_RE.search(bare)
    if m:
        return ResolvedSource(
            feed_url=f"https://medium.com/feed/@{m.group(1)}",
            direct_url=None,
            label=f"medium @{m.group(1)}",
        )

    # Substack subdomain
    m = _SUBSTACK_RE.match(bare)
    if m:
        return ResolvedSource(
            feed_url=f"https://{m.group(1)}.substack.com/feed",
            direct_url=None,
            label=f"substack {m.group(1)}",
        )

    # Mirror.xyz address
    m = _MIRROR_RE.match(bare)
    if m:
        return ResolvedSource(
            feed_url=f"https://{m.group(1)}.mirror.xyz/feed.atom",
            direct_url=None,
            label=f"mirror {m.group(1)}",
        )

    # Anything that looks like a feed already (path ends in /feed, /rss, .xml etc.)
    if _FEED_SUFFIX_RE.search(bare):
        return ResolvedSource(
            feed_url=raw if raw.lower().startswith("http") else f"https://{bare}",
            direct_url=None,
            label=f"feed {bare}",
        )

    # Otherwise: treat as a single article URL. The caller should still
    # accept it — feeding one article is a valid sample.
    if not raw.lower().startswith("http"):
        raw = f"https://{bare}"
    return ResolvedSource(feed_url=None, direct_url=raw, label=f"single {bare}")


def fetch_top_urls(feed_url: str, *, max_samples: int = 5) -> list[str]:
    """Parse ``feed_url`` and return the top-``max_samples`` entry URLs.

    Robust to slightly-malformed feeds (feedparser is forgiving). Empty
    list when the feed has no entries or fails to parse.
    """
    if max_samples < 1:
        return []
    try:
        import feedparser
    except ImportError:
        raise RuntimeError(
            "feedparser is not installed; check requirements.txt"
        )

    parsed = feedparser.parse(feed_url)
    entries = list(getattr(parsed, "entries", []) or [])[:max_samples]
    out: list[str] = []
    for entry in entries:
        link = getattr(entry, "link", "") or ""
        if isinstance(entry, dict):
            link = link or entry.get("link") or ""
        if isinstance(link, str) and link.startswith(("http://", "https://")):
            out.append(link)
    return out


def resolve_handle_to_urls(
    handle_or_url: str, *, max_samples: int = 5
) -> tuple[list[str], str]:
    """One-shot: handle/URL → list of article URLs (≤ max_samples) + label.

    Returns ``(urls, label)``. ``urls`` is empty if the feed had no entries
    AND the input wasn't recognized as a single article URL.
    """
    src = resolve(handle_or_url)
    if src.direct_url:
        return [src.direct_url], src.label
    if src.feed_url:
        urls = fetch_top_urls(src.feed_url, max_samples=max_samples)
        return urls, src.label
    return [], src.label
