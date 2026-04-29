"""Score and rank topic clusters.

Score is a weighted combination of:

* cross-source bonus (distinct source count, capped at +0.6)
* normalized engagement (0-0.4)
* freshness (linear decay over 48h from latest signal)

Scores are clamped to [0, 1] before returning.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agentflow.shared.models import TopicCluster

# Engagement scaler: this many engagement-points maps to the full 0.4 band.
_ENGAGEMENT_SATURATION = 1500.0
_FRESHNESS_WINDOW_HOURS = 48.0


def _weighted_engagement(engagement: dict) -> float:
    reply = float(engagement.get("reply_count", 0) or 0)
    retweet = float(engagement.get("retweet_count", 0) or 0)
    like = float(engagement.get("like_count", 0) or 0)
    hn_score = float(engagement.get("hn_score", 0) or 0)
    return reply * 1.0 + retweet * 2.0 + like * 0.5 + hn_score * 0.3


def score_cluster(cluster: TopicCluster, now: datetime) -> float:
    """Compute a composite 0..1 score for a cluster."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Cross-source bonus (0.2 per distinct source, max 0.6).
    sources = {s.source for s in cluster.signals if s.source}
    cross_source_bonus = min(0.2 * len(sources), 0.6)

    # Engagement (0-0.4).
    raw_engagement = sum(_weighted_engagement(s.engagement) for s in cluster.signals)
    engagement_norm = min(raw_engagement / _ENGAGEMENT_SATURATION, 1.0) * 0.4

    # Freshness (0-1): linear decay over 48h from max published_at.
    max_published = max(
        (s.published_at for s in cluster.signals if s.published_at), default=None
    )
    if max_published is None:
        freshness = 0.0
    else:
        if max_published.tzinfo is None:
            max_published = max_published.replace(tzinfo=timezone.utc)
        hours_ago = max(0.0, (now - max_published).total_seconds() / 3600.0)
        freshness = max(0.0, 1.0 - hours_ago / _FRESHNESS_WINDOW_HOURS)

    # Weighted sum: cross-source (up to 0.6) + engagement (0.4) + 0.3 * freshness.
    total = cross_source_bonus + engagement_norm + 0.3 * freshness
    # Normalize to 0..1 (max possible = 0.6 + 0.4 + 0.3 = 1.3).
    normalized = total / 1.3
    return max(0.0, min(1.0, normalized))


def freshness_of_cluster(cluster: TopicCluster, now: datetime) -> float:
    """Expose the freshness sub-score separately (used to populate Hotspot.freshness_score)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    max_published = max(
        (s.published_at for s in cluster.signals if s.published_at), default=None
    )
    if max_published is None:
        return 0.0
    if max_published.tzinfo is None:
        max_published = max_published.replace(tzinfo=timezone.utc)
    hours_ago = max(0.0, (now - max_published).total_seconds() / 3600.0)
    return max(0.0, min(1.0, 1.0 - hours_ago / _FRESHNESS_WINDOW_HOURS))


def select_top(
    clusters: list[TopicCluster],
    top_n: int = 20,
    threshold: float = 0.3,
    now: datetime | None = None,
) -> list[TopicCluster]:
    """Score, filter by threshold, sort desc, keep top N.

    Returns clusters in score-descending order. Clusters with score <
    ``threshold`` are dropped; if that filters everything, we fall back to
    the top-N raw (so the pipeline never produces zero hotspots just because
    the mock engagement is modest).
    """
    if not clusters:
        return []
    now = now or datetime.now(timezone.utc)

    scored = [(c, score_cluster(c, now)) for c in clusters]
    scored.sort(key=lambda x: -x[1])

    kept = [(c, s) for c, s in scored if s >= threshold]
    if not kept:
        kept = scored  # fallback — preserve the best we have

    return [c for c, _ in kept[:top_n]]
