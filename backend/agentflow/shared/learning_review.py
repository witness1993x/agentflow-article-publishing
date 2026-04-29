"""Build weekly learning review reports from local AgentFlow state."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.memory import read_memory_events
from agentflow.shared.topic_profile_lifecycle import list_suggestions

SCHEMA_VERSION = 1

_MEMORY_EVENT_TYPES = (
    "topic_profile_suggestion_created",
    "topic_profile_suggestion_applied",
    "topic_intent_used",
    "article_created",
    "fill_choices",
)


def parse_since(value: str) -> datetime | None:
    """Return a UTC floor for ``Nd`` windows, or None for ``all``."""
    raw = (value or "").strip().lower()
    if raw == "all":
        return None
    match = re.fullmatch(r"(\d+)d", raw)
    if not match:
        raise ValueError(f"invalid since value {value!r}; use Nd (e.g. 7d) or 'all'")
    return datetime.now(timezone.utc) - timedelta(days=int(match.group(1)))


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _in_window(value: Any, floor: datetime | None) -> bool:
    if floor is None:
        return True
    parsed = _parse_ts(value)
    return bool(parsed and parsed >= floor)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records.append(data)
    return records


def _suggestion_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    status_rank = 0 if item.get("status") == "pending" else 1
    ts = str(item.get("updated_at") or item.get("applied_at") or item.get("created_at") or "")
    return (status_rank, ts)


def _suggestion_summary() -> dict[str, Any]:
    suggestions = list_suggestions()
    counts = Counter(str(item.get("status") or "unknown") for item in suggestions)
    normalized_counts = {
        "pending": counts.get("pending", 0),
        "applied": counts.get("applied", 0),
        "dismissed": counts.get("dismissed", 0),
        "other": sum(
            count
            for status, count in counts.items()
            if status not in {"pending", "applied", "dismissed"}
        ),
        "total": len(suggestions),
    }
    top_items = []
    pending_first = sorted(suggestions, key=lambda item: _suggestion_sort_key(item)[1], reverse=True)
    pending_first = sorted(pending_first, key=lambda item: _suggestion_sort_key(item)[0])
    for item in pending_first[:10]:
        top_items.append(
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "profile_id": item.get("profile_id"),
                "stage": item.get("stage"),
                "title": item.get("title"),
                "summary": item.get("summary"),
                "risk_level": item.get("risk_level"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at") or item.get("applied_at"),
                "path": item.get("path"),
            }
        )
    return {"counts": normalized_counts, "top_items": top_items}


def _publish_history_summary(home: Path, floor: datetime | None) -> dict[str, Any]:
    records = [
        record
        for record in _read_jsonl(home / "publish_history.jsonl")
        if _in_window(record.get("published_at"), floor)
    ]
    status_counts = Counter(str(record.get("status") or "unknown") for record in records)
    counts = {
        "success": status_counts.get("success", 0),
        "manual": status_counts.get("manual", 0),
        "failed": status_counts.get("failed", 0),
        "rolled_back": status_counts.get("rolled_back", 0),
        "rollback_failed": status_counts.get("rollback_failed", 0),
        "skipped": status_counts.get("skipped", 0),
        "other": sum(
            count
            for status, count in status_counts.items()
            if status
            not in {
                "success",
                "manual",
                "failed",
                "rolled_back",
                "rollback_failed",
                "skipped",
            }
        ),
        "total": len(records),
    }

    per_platform: dict[str, dict[str, int]] = {}
    for record in records:
        platform = str(record.get("platform") or "unknown")
        status = str(record.get("status") or "unknown")
        bucket = per_platform.setdefault(
            platform,
            {
                "success": 0,
                "manual": 0,
                "failed": 0,
                "rolled_back": 0,
                "rollback_failed": 0,
                "skipped": 0,
                "other": 0,
                "total": 0,
            },
        )
        key = status if status in bucket and status != "total" else "other"
        bucket[key] += 1
        bucket["total"] += 1

    recent_articles = []
    for record in sorted(records, key=lambda item: str(item.get("published_at") or ""), reverse=True):
        recent_articles.append(
            {
                "article_id": record.get("article_id"),
                "platform": record.get("platform"),
                "status": record.get("status"),
                "published_url": record.get("published_url"),
                "failure_reason": record.get("failure_reason"),
                "published_at": record.get("published_at"),
            }
        )
        if len(recent_articles) >= 10:
            break

    return {
        "counts": counts,
        "per_platform": dict(sorted(per_platform.items())),
        "recent_articles": recent_articles,
    }


def _memory_summary(floor: datetime | None) -> dict[str, Any]:
    events = [event for event in read_memory_events() if _in_window(event.get("ts"), floor)]
    counts = Counter(str(event.get("event_type") or "unknown") for event in events)
    selected_counts = {event_type: counts.get(event_type, 0) for event_type in _MEMORY_EVENT_TYPES}
    return {
        "counts": selected_counts,
        "total": len(events),
    }


def _style_learning_summary(home: Path, publish_counts: dict[str, int]) -> dict[str, Any]:
    corpus_root = home / "style_corpus"
    corpus_files = []
    if corpus_root.exists():
        corpus_files = [
            path
            for path in corpus_root.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        ]
    style_profile_path = home / "style_profile.yaml"
    style_profile_exists = style_profile_path.exists()
    has_published = publish_counts.get("success", 0) > 0
    should_learn = has_published and (not style_profile_exists or len(corpus_files) == 0)
    reason = None
    if should_learn:
        reason = "published articles exist but style learning has not been refreshed locally"
    elif not has_published:
        reason = "no successful publishes in the selected window"
    else:
        reason = "style corpus/profile already exists"
    return {
        "style_corpus_count": len(corpus_files),
        "style_profile_exists": style_profile_exists,
        "style_profile_path": str(style_profile_path),
        "recommend_learn_style_from_published": should_learn,
        "recommendation_reason": reason,
    }


def _recommendations(
    *,
    suggestions: dict[str, Any],
    publish_history: dict[str, Any],
    memory_events: dict[str, Any],
    style_learning: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    suggestion_counts = suggestions["counts"]
    publish_counts = publish_history["counts"]
    memory_counts = memory_events["counts"]
    if suggestion_counts["pending"]:
        out.append("Review pending constraint suggestions and apply or dismiss the high-confidence items.")
    if publish_counts["failed"] or publish_counts["rollback_failed"]:
        out.append("Inspect failed publish records before the next publish run.")
    if publish_counts["rolled_back"]:
        out.append("Review rolled back platforms and keep risky channels in draft mode until confirmed.")
    if style_learning["recommend_learn_style_from_published"]:
        out.append("Run `af learn-style --from-published` to refresh the style profile from shipped work.")
    if memory_counts.get("fill_choices", 0) >= 3:
        out.append("Run `af prefs-rebuild` so repeated fill choices become reusable defaults.")
    if memory_counts.get("topic_intent_used", 0) == 0:
        out.append("Use `af intent-set` or `--profile` before scans to keep learning tied to a topic intent.")
    if not out:
        out.append("No urgent action; continue publishing and review learning signals next week.")
    return out


def build_learning_review(since: str = "7d") -> dict[str, Any]:
    """Build a stable JSON-serializable weekly learning review."""
    ensure_user_dirs()
    home = agentflow_home()
    floor = parse_since(since)
    suggestions = _suggestion_summary()
    publish_history = _publish_history_summary(home, floor)
    memory_events = _memory_summary(floor)
    style_learning = _style_learning_summary(home, publish_history["counts"])
    recommendations = _recommendations(
        suggestions=suggestions,
        publish_history=publish_history,
        memory_events=memory_events,
        style_learning=style_learning,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since,
        "since_floor": floor.isoformat() if floor else None,
        "suggestions": suggestions,
        "publish_history": publish_history,
        "memory_events": memory_events,
        "style_learning": style_learning,
        "recommendations": recommendations,
    }
