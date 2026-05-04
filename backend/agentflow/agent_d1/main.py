"""Agent D1 orchestrator.

Pipeline:

1. Load style_profile + sources + content_matrix.
2. Run twitter / rss / hackernews collectors concurrently.
3. Cluster via embeddings + DBSCAN; fall back to singletons if all noise.
4. Score + select top N clusters.
5. Mine viewpoints via Claude (or mock) in parallel -> Hotspots.
6. Serialize to ``~/.agentflow/hotspots/<YYYY-MM-DD>.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agentflow.agent_d1 import clustering, scoring, viewpoint_miner
from agentflow.agent_d1.collectors import hackernews as hn_collector
from agentflow.agent_d1.collectors import rss as rss_collector
from agentflow.agent_d1.collectors import twitter as twitter_collector
from agentflow.agent_d1.collectors import twitter_search as twitter_search_collector
from agentflow.config.sources_loader import load_sources
from agentflow.config.style_loader import load_style_profile
from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.logger import get_logger
from agentflow.shared.models import D1Output, Hotspot, RawSignal, TopicCluster

_log = get_logger("agent_d1.main")


# ---------------------------------------------------------------------------
# Content matrix loader (not in config/, so we inline it)
# ---------------------------------------------------------------------------


def _load_content_matrix(style_profile: dict[str, Any]) -> dict[str, Any]:
    """Prefer ~/.agentflow/content_matrix.yaml; fall back to style_profile.content_matrix."""
    user_path = agentflow_home() / "content_matrix.yaml"
    if user_path.exists():
        try:
            with user_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            if isinstance(data, dict):
                return data
        except Exception as err:  # pragma: no cover - defensive
            _log.warning("content_matrix.yaml parse failed: %s", err)

    example_path = (
        Path(__file__).resolve().parents[3]
        / "config-examples"
        / "content_matrix.example.yaml"
    )
    if example_path.exists():
        try:
            with example_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    # Last resort: whatever lives under style_profile.content_matrix.
    return dict(style_profile.get("content_matrix") or {})


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _twitter_handles(sources: dict[str, Any]) -> list[str]:
    """Extract twitter handles from sources.yaml ``twitter_kols``.

    v1.0.22: respects per-handle ``weight`` field:
      - ``weight: blocked`` — skipped entirely (operator can keep the row
        for posterity without the signal flooding the recall pool).
      - ``AGENTFLOW_TWITTER_KOL_ONLY_HIGH=true`` env restricts to entries
        with ``weight: high``. Recommended for tightly-scoped publishers
        where general AI/tech KOLs (sama / paulg / karpathy) drown out
        the vertical signal.
    """
    kols = sources.get("twitter_kols") or []
    only_high = (
        os.environ.get("AGENTFLOW_TWITTER_KOL_ONLY_HIGH", "")
        .strip()
        .lower()
        == "true"
    )
    out: list[str] = []
    for k in kols:
        handle = k.get("handle")
        if not handle:
            continue
        weight = str(k.get("weight") or "").strip().lower()
        if weight == "blocked":
            continue
        if only_high and weight != "high":
            continue
        out.append(handle)
    return out


def _twitter_search_enabled() -> bool:
    """v1.0.26 — read AGENTFLOW_TWITTER_SEARCH_ENABLED.

    Default off. Operators opt into the second recall layer alongside the
    curated KOL pulls when they've populated ``sources.yaml::twitter_search``
    AND configured ``TWITTER_BEARER_TOKEN``.
    """
    return (
        os.environ.get("AGENTFLOW_TWITTER_SEARCH_ENABLED", "")
        .strip()
        .lower()
        == "true"
    )


def _twitter_search_queries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    """v1.0.26 — extract twitter search queries from sources.yaml.

    Mirrors ``_twitter_handles`` semantics:
      * ``weight: blocked`` — skipped entirely (operator can keep the row
        for posterity without the signal flooding the recall pool).
      * ``AGENTFLOW_TWITTER_KOL_ONLY_HIGH=true`` env restricts to entries
        with ``weight: high`` (same flag as KOL list — one knob covers both
        Twitter recall paths).

    Returns an empty list when search recall is disabled, regardless of
    what's in sources.yaml.
    """
    if not _twitter_search_enabled():
        return []
    queries = sources.get("twitter_search") or []
    only_high = (
        os.environ.get("AGENTFLOW_TWITTER_KOL_ONLY_HIGH", "")
        .strip()
        .lower()
        == "true"
    )
    out: list[dict[str, Any]] = []
    for q in queries:
        if not isinstance(q, dict):
            continue
        query = (q.get("query") or "").strip()
        if not query:
            continue
        weight = str(q.get("weight") or "").strip().lower()
        if weight == "blocked":
            continue
        if only_high and weight != "high":
            continue
        out.append(q)
    return out


def _rss_feeds(sources: dict[str, Any]) -> list[dict[str, Any]]:
    feeds = sources.get("rss_feeds") or []
    return [f for f in feeds if f.get("url")]


def _hn_config(sources: dict[str, Any]) -> tuple[bool, list[str] | None, int]:
    cfg = sources.get("hackernews") or {}
    enabled = bool(cfg.get("enabled", True))
    keywords = cfg.get("filter_keywords") or None
    min_score = int(cfg.get("min_score", 50))
    return enabled, keywords, min_score


def _is_mock_signal(sig: RawSignal) -> bool:
    meta = getattr(sig, "raw_metadata", None)
    return isinstance(meta, dict) and meta.get("mock") is True


def _provenance_summary(signals: list[RawSignal]) -> dict[str, dict[str, int]]:
    """Per-source real/mock counts. Used for audit logging at end of collection."""
    out: dict[str, dict[str, int]] = {}
    for sig in signals:
        bucket = out.setdefault(
            sig.source or "unknown", {"real": 0, "mock": 0},
        )
        bucket["mock" if _is_mock_signal(sig) else "real"] += 1
    return out


async def _collect_all(sources: dict[str, Any]) -> list[RawSignal]:
    handles = _twitter_handles(sources)
    feeds = _rss_feeds(sources)
    hn_enabled, hn_keywords, hn_min = _hn_config(sources)
    search_queries = _twitter_search_queries(sources)

    # v1.0.26: per-query default for the search collector — operators can
    # override via AGENTFLOW_TWITTER_SEARCH_MAX_RESULTS without editing every
    # entry in sources.yaml.
    try:
        search_default_max = int(
            os.environ.get("AGENTFLOW_TWITTER_SEARCH_MAX_RESULTS", "20") or "20"
        )
    except (TypeError, ValueError):
        search_default_max = 20

    tasks = [
        twitter_collector.collect(handles, max_results_per_kol=20),
        rss_collector.collect(feeds),
    ]
    if hn_enabled:
        tasks.append(hn_collector.collect(hn_keywords, min_score=hn_min))
    if search_queries:
        # Second Twitter recall path. Same source="twitter" as the KOL
        # collector so _provenance_summary buckets them together; the
        # raw_metadata.via field discriminates if anyone wants to look.
        tasks.append(
            twitter_search_collector.collect(
                search_queries, default_max_results=search_default_max,
            )
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_signals: list[RawSignal] = []
    for idx, res in enumerate(results):
        if isinstance(res, Exception):
            _log.warning("collector[%d] raised, skipped: %s", idx, res)
            continue
        all_signals.extend(res)

    # Structural guard: when MOCK_LLM is not explicitly opted into, refuse to
    # let any signal tagged ``raw_metadata.mock=True`` reach clustering /
    # ranking / persistence. v1.0.8 fixed twitter's silent-mock fallback;
    # this is the belt-and-suspenders catch for any future collector that
    # regresses, env that's misconfigured at runtime, or test fixture that
    # leaks through a partial seed. Visible audit so the operator notices.
    explicit_mock = os.environ.get("MOCK_LLM", "").strip().lower() == "true"
    if not explicit_mock:
        before = len(all_signals)
        all_signals = [s for s in all_signals if not _is_mock_signal(s)]
        dropped = before - len(all_signals)
        if dropped:
            _log.error(
                "real-mode hotspots scan: dropped %d mock-tagged signals "
                "(MOCK_LLM is not 'true' but a collector emitted "
                "raw_metadata.mock=True). This indicates a collector bug "
                "or accidental fixture seed; investigate.",
                dropped,
            )

    # v1.0.25: blocklist filter — drop signals whose text mentions any
    # term in the active profile's ``avoid_terms`` or the
    # ``AGENTFLOW_SIGNAL_BLOCKLIST_TOKENS`` env. Cheap pre-filter for
    # cross-domain ambiguity that token coverage can't resolve (e.g.
    # "agent" overlaps both crypto-infra and AI dev tooling — but a
    # signal mentioning "OpenAI"/"ChatGPT" is unambiguously off-domain
    # for a crypto publisher even if it also says "agent").
    all_signals = _apply_signal_blocklist(all_signals)

    # v1.0.22: signal-level domain filter. The composite-rank fit gate
    # (v1.0.21 AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD) operates on already-
    # clustered hotspots; by then a flood of off-domain signals (e.g.
    # @sama / @paulg / @karpathy generic AI tweets for a crypto-infra
    # publisher) has already shaped the clusters. This pass tokenizes
    # each raw signal and drops the ones whose Jaccard overlap with the
    # active publisher's domain tokens is below
    # ``AGENTFLOW_SIGNAL_DOMAIN_THRESHOLD`` (env, default 0 = disabled).
    # Recommended for tightly-scoped publishers: 0.03.
    all_signals = _apply_signal_domain_filter(all_signals)

    _log.info(
        "collectors produced %d signals (provenance=%s, mock_mode=%s)",
        len(all_signals),
        _provenance_summary(all_signals),
        explicit_mock,
    )
    return all_signals


def _signal_text_tokens(sig: RawSignal) -> set[str]:
    """Tokenize the user-visible content of a signal for the v1.0.22
    domain filter. Uses topic_spine_lint's tokenizer to keep signals on
    the same scale as the Gate B spine_lint."""
    from agentflow.agent_d2.topic_spine_lint import _tokenize  # type: ignore
    parts: list[str] = []
    parts.append(getattr(sig, "text", "") or "")
    parts.append(getattr(sig, "author", "") or "")
    raw_meta = getattr(sig, "raw_metadata", None) or {}
    if isinstance(raw_meta, dict):
        for v in raw_meta.values():
            if isinstance(v, str):
                parts.append(v)
    bag: set[str] = set()
    for p in parts:
        bag.update(_tokenize(p))
    return bag


def _resolve_active_publisher_tokens() -> set[str] | None:
    """Best-effort resolution of the active publisher's domain tokens.
    Returns None when no profile / intent / tokens (caller treats that
    as "skip the filter"). Lazy-imports to avoid agent_d1 → agent_review
    coupling on import.

    Resolution order (first non-empty wins):
      1. ``load_current_intent()`` → publisher_account
      2. ``_read_active_profile_id()`` → profile.publisher_account
      3. ``AGENTFLOW_DEFAULT_TOPIC_PROFILE`` env → profile.publisher_account
      4. If ``topic_profiles.yaml`` has exactly one profile, use it.
    """
    try:
        from agentflow.shared.memory import load_current_intent
        from agentflow.shared.topic_profiles import (
            resolve_publisher_account_from_intent,
        )
        from agentflow.agent_d2.topic_spine_lint import _publisher_domain_tokens
    except Exception:
        return None

    pub: dict | None = None
    profile: dict | None = None

    try:
        intent = load_current_intent() or {}
        pub = resolve_publisher_account_from_intent(intent)
    except Exception:
        pub = None

    if not pub:
        try:
            from agentflow.shared.topic_profile_lifecycle import load_user_topic_profiles
            data = load_user_topic_profiles() or {}
            profiles = (
                data.get("profiles") or {}
                if isinstance(data, dict) else {}
            )
        except Exception:
            profiles = {}

        candidate_pid: str | None = None
        # 2. active profile id (if state file says so)
        try:
            from agentflow.cli.topic_profile_commands import _read_active_profile_id  # type: ignore
            candidate_pid = _read_active_profile_id() or None
        except Exception:
            candidate_pid = None
        # 3. env-pinned default
        if not candidate_pid:
            env_pid = (os.environ.get("AGENTFLOW_DEFAULT_TOPIC_PROFILE") or "").strip()
            if env_pid and env_pid in profiles:
                candidate_pid = env_pid
        # 4. single-profile inference
        if not candidate_pid and isinstance(profiles, dict) and len(profiles) == 1:
            candidate_pid = next(iter(profiles.keys()))

        if candidate_pid and candidate_pid in profiles:
            profile = profiles.get(candidate_pid) or {}
            pub = profile.get("publisher_account") or {}

    if not pub:
        return None
    # Augment publisher with profile-level keyword_groups so the
    # tokenizer sees domain keywords too (when fallback path resolved).
    if profile is not None and isinstance(profile.get("keyword_groups"), (dict, list)):
        pub = dict(pub)
        pub["keyword_groups"] = profile["keyword_groups"]
    tokens = _publisher_domain_tokens(pub)
    return tokens if tokens else None


def _resolve_signal_blocklist() -> set[str]:
    """Build the blocklist token set from env + active profile avoid_terms.

    Both sources are merged; env terms take effect even when no profile
    is resolvable. Terms are stripped + lowercased for substring match.
    """
    out: set[str] = set()
    raw = os.environ.get("AGENTFLOW_SIGNAL_BLOCKLIST_TOKENS", "") or ""
    for term in raw.split(","):
        t = term.strip().lower()
        if t:
            out.add(t)
    # Merge active profile's avoid_terms (works without intent / pinned
    # active id thanks to the v1.0.23 single-profile fallback chain).
    try:
        from agentflow.shared.topic_profile_lifecycle import load_user_topic_profiles
        data = load_user_topic_profiles() or {}
        profiles = (
            data.get("profiles") or {}
            if isinstance(data, dict) else {}
        )
        candidate_pid: str | None = None
        try:
            from agentflow.cli.topic_profile_commands import _read_active_profile_id  # type: ignore
            candidate_pid = _read_active_profile_id() or None
        except Exception:
            candidate_pid = None
        if not candidate_pid:
            env_pid = (os.environ.get("AGENTFLOW_DEFAULT_TOPIC_PROFILE") or "").strip()
            if env_pid and env_pid in profiles:
                candidate_pid = env_pid
        if not candidate_pid and isinstance(profiles, dict) and len(profiles) == 1:
            candidate_pid = next(iter(profiles.keys()))
        if candidate_pid and candidate_pid in profiles:
            avoid = (profiles[candidate_pid] or {}).get("avoid_terms") or []
            if isinstance(avoid, list):
                for t in avoid:
                    if isinstance(t, str) and t.strip():
                        out.add(t.strip().lower())
    except Exception:
        pass
    return out


def _signal_haystack(sig: RawSignal) -> str:
    """Lowercased text + author for blocklist substring match."""
    return (
        (getattr(sig, "text", "") or "") + " " +
        (getattr(sig, "author", "") or "")
    ).lower()


def _apply_signal_blocklist(signals: list[RawSignal]) -> list[RawSignal]:
    """Drop signals whose text/author contains any blocklist term.
    Case-insensitive substring match. No-op when blocklist is empty."""
    blocklist = _resolve_signal_blocklist()
    if not blocklist or not signals:
        return signals
    kept: list[RawSignal] = []
    dropped_examples: list[str] = []
    for sig in signals:
        haystack = _signal_haystack(sig)
        match: str | None = None
        for term in blocklist:
            if term in haystack:
                match = term
                break
        if match is None:
            kept.append(sig)
            continue
        if len(dropped_examples) < 3:
            preview = (getattr(sig, "text", "") or "")[:60].replace("\n", " ")
            dropped_examples.append(
                f"{getattr(sig, 'source', '?')}/{getattr(sig, 'author', '?')} "
                f"[hit={match!r}]: {preview!r}"
            )
    dropped = len(signals) - len(kept)
    if dropped:
        _log.warning(
            "signal blocklist dropped %d/%d signals (%d terms). examples: %s",
            dropped, len(signals), len(blocklist),
            "; ".join(dropped_examples),
        )
    return kept


def _apply_signal_domain_filter(signals: list[RawSignal]) -> list[RawSignal]:
    raw = os.environ.get("AGENTFLOW_SIGNAL_DOMAIN_THRESHOLD", "0").strip()
    try:
        threshold = max(0.0, float(raw or "0"))
    except (TypeError, ValueError):
        threshold = 0.0
    if threshold <= 0 or not signals:
        return signals
    pub_tokens = _resolve_active_publisher_tokens()
    if not pub_tokens or len(pub_tokens) < 5:
        return signals
    kept: list[RawSignal] = []
    dropped_examples: list[str] = []
    for sig in signals:
        sig_tokens = _signal_text_tokens(sig)
        if not sig_tokens:
            continue
        intersect = sig_tokens & pub_tokens
        # v1.0.22.1: use signal-anchored coverage (fraction of the
        # signal's own tokens that map to publisher domain), NOT
        # Jaccard. Jaccard's denominator is dominated by the publisher
        # token set (often 100+ entries), so even on-topic signals
        # score < 0.05 and the threshold rejects everything. Coverage
        # is publisher-set-size-invariant: "1 out of every N signal
        # tokens is on-domain" is a stable signal at any pub_tokens size.
        denom = len(sig_tokens)
        coverage = len(intersect) / denom if denom else 0.0
        if coverage >= threshold:
            kept.append(sig)
        else:
            if len(dropped_examples) < 3:
                preview = (getattr(sig, "text", "") or "")[:60].replace("\n", " ")
                dropped_examples.append(
                    f"{getattr(sig, 'source', '?')}/{getattr(sig, 'author', '?')}: {preview!r}"
                )
    dropped = len(signals) - len(kept)
    if dropped:
        _log.warning(
            "signal-domain filter dropped %d/%d signals below threshold %.3f. "
            "examples: %s",
            dropped, len(signals), threshold,
            "; ".join(dropped_examples),
        )
    return kept


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _output_path(generated_at: datetime) -> Path:
    ensure_user_dirs()
    return agentflow_home() / "hotspots" / f"{generated_at.strftime('%Y-%m-%d')}.json"


def _save_output(output: D1Output) -> Path:
    path = _output_path(output.generated_at)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(output.to_dict(), fh, ensure_ascii=False, indent=2)
    _log.info("wrote %d hotspots to %s", len(output.hotspots), path)
    return path


# ---------------------------------------------------------------------------
# Filtering by scan_window_hours
# ---------------------------------------------------------------------------


def _within_window(signals: list[RawSignal], hours: int) -> list[RawSignal]:
    if hours <= 0:
        return signals
    now = datetime.now(timezone.utc)
    out = []
    for sig in signals:
        ts = sig.published_at
        if ts is None:
            out.append(sig)
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_hours = (now - ts).total_seconds() / 3600.0
        if age_hours <= hours:
            out.append(sig)
    return out


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


async def run_d1_scan(
    scan_window_hours: int = 24,
    target_candidates: int = 20,
) -> D1Output:
    """End-to-end D1 pass. Returns the populated :class:`D1Output`."""
    ensure_user_dirs()
    viewpoint_miner.reset_id_counter()

    style_profile = load_style_profile()
    sources = load_sources()
    content_matrix = _load_content_matrix(style_profile)

    signals = await _collect_all(sources)
    signals = _within_window(signals, scan_window_hours)

    if not signals:
        _log.warning("no signals collected; emitting empty D1Output")
        output = D1Output(generated_at=datetime.now(timezone.utc), hotspots=[])
        _save_output(output)
        return output

    clusters: list[TopicCluster] = await clustering.cluster(signals)

    if not clusters:
        _log.warning(
            "DBSCAN produced 0 clusters; falling back to single-item clusters"
        )
        # Singleton fallback — no embeddings attached (not needed downstream).
        clusters = clustering.singletons_from_signals(signals)

    now = datetime.now(timezone.utc)
    selected = scoring.select_top(
        clusters, top_n=target_candidates, threshold=0.3, now=now
    )

    _log.info("selected %d clusters for viewpoint mining", len(selected))

    # Bounded parallelism: some providers (notably Moonshot/Kimi on long prompts)
    # return timeouts/connection errors when we fire 3+ parallel requests. Cap
    # concurrency and let the others wait.
    semaphore = asyncio.Semaphore(int(os.environ.get("D1_VIEWPOINT_CONCURRENCY", "2")))

    async def _gated_mine(cluster: TopicCluster) -> Hotspot:
        async with semaphore:
            return await viewpoint_miner.mine(cluster, style_profile, content_matrix)

    mining_results = await asyncio.gather(
        *[_gated_mine(c) for c in selected],
        return_exceptions=True,
    )
    hotspots: list[Hotspot] = []
    for cluster, result in zip(selected, mining_results):
        if isinstance(result, Exception):
            # v1.0.12: real-mode LLM failure on a single cluster drops the
            # cluster instead of emitting a stub Hotspot with empty angles.
            # Mock-mode failures stay silent (handled inside mine()).
            _log.error(
                "viewpoint mining dropped cluster %s: %s",
                cluster.cluster_id, result,
            )
            continue
        hotspots.append(result)

    output = D1Output(generated_at=now, hotspots=hotspots)
    _save_output(output)
    return output


# ---------------------------------------------------------------------------
# Sync wrapper for CLI
# ---------------------------------------------------------------------------


def run(scan_window_hours: int = 24, target_candidates: int = 20) -> D1Output:
    """Sync convenience wrapper used by the ``af hotspots`` CLI."""
    return asyncio.run(
        run_d1_scan(
            scan_window_hours=scan_window_hours,
            target_candidates=target_candidates,
        )
    )
