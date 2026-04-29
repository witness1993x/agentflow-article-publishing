"""Generate suggestion-only learning artifacts for topic profiles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.topic_profile_lifecycle import (
    make_learning_suggestion,
    user_profile_bootstrap_state,
)
from agentflow.shared.topic_profiles import _dedupe_keep_order, _flatten_terms


def _current_profile(profile_id: str) -> dict[str, Any]:
    state = user_profile_bootstrap_state(profile_id)
    return (state.get("current_profile") or state.get("seed_profile") or {})


def _append_unique_patch(profile: dict[str, Any], *, key: str, candidates: list[str]) -> list[str]:
    existing = _dedupe_keep_order(_flatten_terms(profile.get(key) or []))
    merged = _dedupe_keep_order(existing + [c for c in candidates if str(c).strip()])
    return merged if merged != existing else []


def _append_unique_nested_patch(
    profile: dict[str, Any],
    *,
    parent_key: str,
    child_key: str,
    candidates: list[str],
) -> list[str]:
    parent = profile.get(parent_key) or {}
    if not isinstance(parent, dict):
        parent = {}
    existing = _dedupe_keep_order(_flatten_terms(parent.get(child_key) or []))
    merged = _dedupe_keep_order(existing + [c for c in candidates if str(c).strip()])
    return merged if merged != existing else []


def suggest_from_search(
    *,
    profile_id: str,
    queries: list[str],
    hotspot_count: int,
) -> dict[str, Any] | None:
    profile = _current_profile(profile_id)
    merged_queries = _append_unique_patch(profile, key="search_queries", candidates=queries)
    if not merged_queries or hotspot_count <= 0:
        return None
    return make_learning_suggestion(
        profile_id=profile_id,
        stage="search",
        title="Search query expansion suggestion",
        summary="Successful search queries can be promoted into reusable profile search queries.",
        proposed_patch={
            "search_queries": merged_queries,
            "default_search_query": merged_queries[0],
        },
        evidence=[{"queries": queries, "hotspot_count": hotspot_count}],
        risk_level="low",
    )


def suggest_from_hotspots(
    *,
    profile_id: str,
    filter_meta: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not filter_meta:
        return None
    boundary = (filter_meta.get("boundary") or {}).get("level")
    if boundary not in {"too_narrow", "narrow", "balanced"}:
        return None
    preview = filter_meta.get("filtered_out_preview") or []
    candidate_queries = [
        str(item.get("topic_one_liner") or "").strip()
        for item in preview[:3]
        if isinstance(item, dict)
    ]
    candidate_queries = [q for q in candidate_queries if q]
    if not candidate_queries:
        return None
    profile = _current_profile(profile_id)
    merged_queries = _append_unique_patch(
        profile,
        key="search_queries",
        candidates=candidate_queries,
    )
    if not merged_queries:
        return None
    return make_learning_suggestion(
        profile_id=profile_id,
        stage="hotspots",
        title="Hotspots recall widening suggestion",
        summary="Filtered-but-relevant topics can be saved as future search queries for broader recall.",
        proposed_patch={"search_queries": merged_queries},
        evidence=[{"filter": filter_meta}],
        risk_level="low",
    )


def suggest_from_write(
    *,
    profile_id: str,
    hotspot: dict[str, Any] | None,
    article_id: str,
    draft_title: str | None = None,
) -> dict[str, Any] | None:
    candidates = []
    if hotspot:
        title = str(hotspot.get("topic_one_liner") or "").strip()
        if title:
            candidates.append(title)
    if draft_title:
        candidates.append(str(draft_title).strip())
    candidates = [c for c in candidates if c]
    if not candidates:
        return None
    profile = _current_profile(profile_id)
    merged_queries = _append_unique_patch(profile, key="search_queries", candidates=candidates)
    if not merged_queries:
        return None
    return make_learning_suggestion(
        profile_id=profile_id,
        stage="write",
        title="Accepted writing topic suggestion",
        summary="Topics that turned into a draft can be remembered as future search seeds.",
        proposed_patch={"search_queries": merged_queries},
        evidence=[{"article_id": article_id, "candidates": candidates}],
        risk_level="low",
    )


def suggest_from_publish(
    *,
    article_id: str,
    platform: str,
    published_url: str | None,
) -> dict[str, Any] | None:
    meta_path = agentflow_home() / "drafts" / article_id / "metadata.json"
    if not meta_path.exists():
        return None
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    profile_id = str(((metadata.get("intent_profile") or metadata.get("profile") or {}) if isinstance(metadata.get("intent_profile") or metadata.get("profile"), dict) else {}).get("id") or "")
    if not profile_id:
        return None
    profile = _current_profile(profile_id)
    title = str(metadata.get("title") or "").strip()
    tags = _dedupe_keep_order(_flatten_terms(((metadata.get("metadata_overrides") or {}).get("medium") or {}).get("tags") or []))
    patch: dict[str, Any] = {}
    merged_queries = _append_unique_patch(profile, key="search_queries", candidates=[title] if title else [])
    if merged_queries:
        patch["search_queries"] = merged_queries
    merged_tags = _append_unique_nested_patch(
        profile,
        parent_key="publisher_account",
        child_key="default_tags",
        candidates=tags,
    )
    if merged_tags:
        patch["publisher_account"] = {"default_tags": merged_tags}
    if not patch:
        return None
    return make_learning_suggestion(
        profile_id=profile_id,
        stage="publish",
        title="Published article learning suggestion",
        summary="Published titles and operator tags can be folded back into reusable topic/profile hints.",
        proposed_patch=patch,
        evidence=[
            {
                "article_id": article_id,
                "platform": platform,
                "published_url": published_url,
                "title": title,
                "tags": tags,
            }
        ],
        risk_level="low",
    )
