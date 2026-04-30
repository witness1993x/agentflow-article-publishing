"""Viewpoint miner — turns a topic cluster into a :class:`Hotspot`.

Calls Claude (or the ``d1-viewpoint`` mock) with the ``d1_viewpoint_mining.md``
prompt. Handles ID generation + maps the LLM JSON back onto the
``Hotspot`` / ``SuggestedAngle`` dataclasses.
"""

from __future__ import annotations

import itertools
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agentflow.agent_d1.scoring import freshness_of_cluster
from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger
from agentflow.shared.models import Hotspot, SuggestedAngle, TopicCluster

_log = get_logger("agent_d1.viewpoint_miner")

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d1_viewpoint_mining.md"
)

_ID_COUNTER = itertools.count(1)


def _load_prompt_template() -> str:
    """Load the D1 prompt and strip the doc wrapper, leaving only the body.

    The prompt wraps its template in an outer ```text ... ``` fence. Because the
    body contains inner ``` fences (```json / ```yaml example blocks), we use
    ``rfind`` to match the LAST ``` — the outer closer — rather than the first
    inner fence. Otherwise only the first ~30 chars of the prompt reach the LLM.
    """
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    marker = "```text"
    start = text.find(marker)
    if start == -1:
        return text
    start += len(marker)
    end = text.rfind("```")
    if end == -1 or end <= start:
        return text[start:].strip()
    return text[start:end].strip()


def _next_id(now: datetime) -> str:
    n = next(_ID_COUNTER)
    return f"hs_{now.strftime('%Y%m%d')}_{n:03d}"


def reset_id_counter(start: int = 1) -> None:
    """Reset the module-level ID counter — useful when a run re-enters."""
    global _ID_COUNTER
    _ID_COUNTER = itertools.count(start)


def _truncate(text: str, n: int = 200) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _build_sources_list(cluster: TopicCluster) -> str:
    lines: list[str] = []
    for sig in cluster.signals:
        author = sig.author or sig.source
        lines.append(f"- [{sig.source}] ({author}) {_truncate(sig.text, 200)}")
    return "\n".join(lines)


def _topic_summary(cluster: TopicCluster) -> str:
    """Heuristic one-liner summary — viewpoint miner overrides with topic_one_liner."""
    if cluster.signals:
        first_text = (cluster.signals[0].text or "").strip()
        first_line = first_text.split("\n", 1)[0]
        return _truncate(first_line, 120)
    return ""


def _source_references(cluster: TopicCluster) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for sig in cluster.signals:
        refs.append(
            {
                "source": sig.source,
                "url": sig.url,
                "author": sig.author,
                "text_snippet": _truncate(sig.text, 280),
                "published_at": sig.published_at.isoformat()
                if sig.published_at
                else None,
                "engagement": dict(sig.engagement or {}),
            }
        )
    return refs


def _coerce_mainstream_views(raw: Any) -> list[str]:
    """Tolerant normalizer — accept list[str] or list[{view,...}]."""
    out: list[str] = []
    if not raw:
        return out
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            view = item.get("view") or item.get("text") or ""
            supporters = item.get("supporters") or []
            if supporters and isinstance(supporters, list):
                out.append(f"{view} (supporters: {', '.join(map(str, supporters))})")
            elif view:
                out.append(view)
    return out


def _coerce_overlooked(raw: Any) -> list[str]:
    out: list[str] = []
    if not raw:
        return out
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            angle = item.get("angle") or item.get("text") or ""
            rationale = item.get("rationale") or ""
            if rationale:
                out.append(f"{angle} — {rationale}")
            elif angle:
                out.append(angle)
    return out


def _coerce_suggested_angles(raw: Any) -> list[SuggestedAngle]:
    out: list[SuggestedAngle] = []
    if not raw:
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(SuggestedAngle.from_dict(item))
    if not out:
        # Ensure at least one angle exists — consumers rely on it.
        out.append(
            SuggestedAngle(
                angle="(no angle generated)",
                fit_explanation="",
                depth="medium",
                difficulty="medium",
            )
        )
    return out


def _condense_style_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the viewpoint prompt actually uses.

    Full profile is ~6KB YAML; most of it is irrelevant to D1 and just burns
    tokens + slows down the LLM. D1 needs: taboos (to avoid blacklisted words),
    tone (for risk calibration), voice_principles (to fit suggested_angles),
    and the content_matrix series summaries (for recommended_series).
    """
    keep = ("identity", "content_matrix", "voice_principles", "taboos", "tone")
    return {k: profile[k] for k in keep if k in profile}


async def mine(
    cluster: TopicCluster,
    style_profile: dict[str, Any],
    content_matrix: dict[str, Any] | None = None,
) -> Hotspot:
    """Run the viewpoint-mining prompt and return a populated :class:`Hotspot`."""
    now = datetime.now(timezone.utc)

    client = LLMClient()
    template = _load_prompt_template()
    condensed = _condense_style_profile(style_profile)
    style_yaml = yaml.safe_dump(condensed, allow_unicode=True, sort_keys=False)
    sources_list = _build_sources_list(cluster)
    topic_summary = _topic_summary(cluster)

    prompt = template.format(
        style_profile_yaml=style_yaml,
        topic_summary=topic_summary,
        sources_list=sources_list,
    )

    try:
        parsed = await client.chat_json(
            prompt_family="d1-viewpoint",
            prompt=prompt,
            max_tokens=4000,
        )
    except Exception as err:
        # In MOCK mode (smoke tests / dev) tolerate fixture misses with a
        # stub Hotspot so the pipeline keeps running. In real mode propagate
        # — agent_d1.main.run_d1_scan collects exceptions via
        # ``asyncio.gather(return_exceptions=True)`` and drops failed
        # clusters from the output instead of emitting placeholder hotspots
        # that look real but carry empty angles. This keeps the user's
        # "every search returns real data" invariant honest.
        if os.environ.get("MOCK_LLM", "").strip().lower() == "true":
            _log.warning(
                "viewpoint mining failed for %s in mock mode → stub: %s",
                cluster.cluster_id, err,
            )
            parsed = {}
        else:
            _log.error(
                "viewpoint mining failed for %s in real mode (will drop cluster): %s",
                cluster.cluster_id, err,
            )
            raise

    fresh = freshness_of_cluster(cluster, now)

    hotspot_id = _next_id(now)
    topic_one_liner = (
        parsed.get("topic_one_liner") or topic_summary or "(untitled topic)"
    )
    cluster.summary_one_liner = topic_one_liner

    return Hotspot(
        id=hotspot_id,
        topic_one_liner=topic_one_liner,
        source_references=_source_references(cluster),
        mainstream_views=_coerce_mainstream_views(parsed.get("mainstream_views")),
        overlooked_angles=_coerce_overlooked(parsed.get("overlooked_angles")),
        recommended_series=str(parsed.get("recommended_series") or "B"),
        series_confidence=float(parsed.get("series_confidence", 0.5) or 0.0),
        suggested_angles=_coerce_suggested_angles(parsed.get("suggested_angles")),
        freshness_score=float(fresh),
        depth_potential=str(parsed.get("depth_potential") or "medium"),
        generated_at=now,
    )
