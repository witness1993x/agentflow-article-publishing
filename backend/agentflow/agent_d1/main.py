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
    kols = sources.get("twitter_kols") or []
    return [k.get("handle") for k in kols if k.get("handle")]


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

    tasks = [
        twitter_collector.collect(handles, max_results_per_kol=20),
        rss_collector.collect(feeds),
    ]
    if hn_enabled:
        tasks.append(hn_collector.collect(hn_keywords, min_score=hn_min))

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

    _log.info(
        "collectors produced %d signals (provenance=%s, mock_mode=%s)",
        len(all_signals),
        _provenance_summary(all_signals),
        explicit_mock,
    )
    return all_signals


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

    hotspots: list[Hotspot] = await asyncio.gather(
        *[_gated_mine(c) for c in selected]
    )

    output = D1Output(generated_at=now, hotspots=list(hotspots))
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
