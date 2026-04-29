"""Topic-targeted D1 search.

Companion to :mod:`agent_d1.main`. Where ``run_d1_scan`` does subscription-
style scanning of all configured sources, this module answers a specific
query via HN Algolia (and, if credentialed, future collectors) and runs
the same clustering + viewpoint mining pipeline on the results.

The output is saved to ``~/.agentflow/search_results/search_<slug>_<ts>.json``
so it doesn't overwrite the daily scan file and remains easy to trace later.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from agentflow.agent_d1 import clustering, scoring, viewpoint_miner
from agentflow.agent_d1 import hn_algolia
from agentflow.agent_d1.main import _load_content_matrix, load_style_profile
from agentflow.shared.bootstrap import ensure_user_dirs
from agentflow.shared.hotspot_store import search_results_dir
from agentflow.shared.logger import get_logger
from agentflow.shared.models import D1Output, Hotspot, RawSignal, TopicCluster

_log = get_logger("agent_d1.search")


def _slug(query: str, max_len: int = 32) -> str:
    s = re.sub(r"[^\w\-]+", "_", query.strip().lower(), flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "query"


def _output_path(query: str, generated_at: datetime) -> Path:
    ensure_user_dirs()
    stamp = generated_at.strftime("%Y%m%d%H%M%S")
    name = f"search_{_slug(query)}_{stamp}.json"
    return search_results_dir() / name


def _save(output: D1Output, path: Path, *, search_context: dict[str, object]) -> Path:
    payload = {
        **output.to_dict(),
        "kind": "search_result",
        "search_context": search_context,
    }
    with path.open("w", encoding="utf-8") as fh:
        import json

        json.dump(payload, fh, ensure_ascii=False, indent=2)
    _log.info("wrote %d hotspots to %s", len(output.hotspots), path)
    return path


def _namespace_hotspots(
    hotspots: list[Hotspot],
    *,
    query: str,
    generated_at: datetime,
) -> list[Hotspot]:
    slug = _slug(query, max_len=24)
    stamp = generated_at.strftime("%Y%m%d%H%M%S")
    for index, hotspot in enumerate(hotspots, start=1):
        hotspot.id = f"sr_{slug}_{stamp}_{index:03d}"
    return hotspots


async def run_d1_search(
    query: str,
    days: int = 7,
    min_points: int = 10,
    target_candidates: int = 10,
) -> tuple[D1Output, Path]:
    """End-to-end topic-targeted D1 pass.

    Returns ``(output, saved_path)``. Unlike ``run_d1_scan``, the saved file
    is named ``search_<slug>_<ts>.json`` to avoid clobbering the daily file.
    """
    ensure_user_dirs()
    viewpoint_miner.reset_id_counter()

    style_profile = load_style_profile()
    content_matrix = _load_content_matrix(style_profile)

    signals: list[RawSignal] = await hn_algolia.search(
        query=query, days=days, min_points=min_points
    )

    if not signals:
        _log.warning("no signals found for query=%r", query)
        output = D1Output(generated_at=datetime.now(timezone.utc), hotspots=[])
        path = _save(
            output,
            _output_path(query, output.generated_at),
            search_context={
                "query": query,
                "days": days,
                "min_points": min_points,
                "target_candidates": target_candidates,
            },
        )
        return output, path

    clusters: list[TopicCluster] = await clustering.cluster(signals)
    if not clusters:
        _log.warning("0 clusters; falling back to singletons")
        clusters = clustering.singletons_from_signals(signals)

    now = datetime.now(timezone.utc)
    selected = scoring.select_top(
        clusters, top_n=target_candidates, threshold=0.2, now=now
    )

    semaphore = asyncio.Semaphore(
        int(os.environ.get("D1_VIEWPOINT_CONCURRENCY", "2"))
    )

    async def _gated_mine(cluster: TopicCluster) -> Hotspot:
        async with semaphore:
            return await viewpoint_miner.mine(cluster, style_profile, content_matrix)

    hotspots: list[Hotspot] = await asyncio.gather(
        *[_gated_mine(c) for c in selected]
    )
    hotspots = _namespace_hotspots(hotspots, query=query, generated_at=now)

    output = D1Output(generated_at=now, hotspots=list(hotspots))
    path = _save(
        output,
        _output_path(query, now),
        search_context={
            "query": query,
            "days": days,
            "min_points": min_points,
            "target_candidates": target_candidates,
        },
    )
    return output, path
