"""Embedding-based topic clustering via DBSCAN.

Embeddings come from :class:`LLMClient` (OpenAI in real mode, deterministic
hash-seeded fake vectors in mock mode). Clusters are built from DBSCAN labels;
noise points (``label == -1``) are dropped here — the caller decides whether
to fall back to single-item clusters.
"""

from __future__ import annotations

from typing import Iterable

from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger
from agentflow.shared.models import RawSignal, TopicCluster

_log = get_logger("agent_d1.clustering")


def _mean(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    out = [0.0] * dim
    for v in vectors:
        for i, val in enumerate(v):
            out[i] += val
    n = float(len(vectors))
    return [x / n for x in out]


async def cluster(
    signals: list[RawSignal],
    *,
    eps: float | None = None,
    min_samples: int | None = None,
) -> list[TopicCluster]:
    """Embed signals and run DBSCAN. Returns topic clusters (no noise).

    ``eps`` and ``min_samples`` default to env vars ``D1_DBSCAN_EPS`` and
    ``D1_DBSCAN_MIN_SAMPLES`` (falling back to ``0.35`` / ``2`` — the spec
    hardcoded values). Real Jina-v3 embeddings on mixed Twitter/RSS content
    tend to cluster too sparsely at ``eps=0.35``; try ``0.45`` – ``0.55``
    for broader topic buckets.
    """
    if not signals:
        return []

    import os as _os

    if eps is None:
        eps = float(_os.environ.get("D1_DBSCAN_EPS", "0.35"))
    if min_samples is None:
        min_samples = int(_os.environ.get("D1_DBSCAN_MIN_SAMPLES", "2"))

    client = LLMClient()
    texts = [(s.text or "")[:500] for s in signals]
    embeddings = await client.embed(texts)

    try:
        import numpy as np  # type: ignore
        from sklearn.cluster import DBSCAN  # type: ignore
    except ImportError:  # pragma: no cover - defensive
        _log.warning("sklearn/numpy not available; returning empty clusters")
        return []

    X = np.array(embeddings, dtype=float)
    if X.size == 0:
        return []

    labels = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine").fit_predict(X)

    clusters: list[TopicCluster] = []
    unique_labels = sorted({int(l) for l in labels if int(l) != -1})
    for cid in unique_labels:
        idxs = [i for i, l in enumerate(labels) if int(l) == cid]
        cluster_signals = [signals[i] for i in idxs]
        cluster_embeds = [embeddings[i] for i in idxs]
        centroid = _mean(cluster_embeds)
        clusters.append(
            TopicCluster(
                cluster_id=f"c_{cid:03d}",
                signals=cluster_signals,
                centroid_embedding=centroid,
                summary_one_liner="",  # filled by viewpoint miner
            )
        )

    _log.info(
        "clustering: %d signals -> %d clusters (noise=%d)",
        len(signals),
        len(clusters),
        sum(1 for l in labels if int(l) == -1),
    )
    return clusters


def singletons_from_signals(
    signals: Iterable[RawSignal], embeddings: list[list[float]] | None = None
) -> list[TopicCluster]:
    """Fallback: wrap each signal as its own single-item cluster.

    Useful when DBSCAN returns 0 clusters (all noise) — which happens with
    small / diverse mock inputs.
    """
    out: list[TopicCluster] = []
    for i, sig in enumerate(signals):
        centroid = embeddings[i] if embeddings and i < len(embeddings) else []
        out.append(
            TopicCluster(
                cluster_id=f"s_{i:03d}",
                signals=[sig],
                centroid_embedding=centroid,
                summary_one_liner="",
            )
        )
    return out
