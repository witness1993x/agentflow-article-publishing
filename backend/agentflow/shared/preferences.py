"""Aggregate ``events.jsonl`` into ``~/.agentflow/preferences.yaml``.

Slice 1 of Memory → Default Strategy. See
``docs/backlog/MEMORY_TO_DEFAULTS.md`` for the full schema. This module
implements the subset that's actionable today:

* ``write.default_title_index``, ``default_opening_index``,
  ``default_closing_index`` from ``fill_choices`` events (majority vote,
  emitted only when N >= 3).
* ``preview.default_platforms`` from the 10 most-recent ``publish`` events
  with ``status=success`` (per-platform intersection).
* ``publish._negative_signals`` tracks recent ``publish_rolled_back``
  events. If 1+ occurred within the last 3 ``publish`` events, set
  ``publish.ghost_status_override = "draft"`` for the next 3 runs.
* ``intent.recent_queries`` from ``intent_used_in_write`` events, plus the
  latest ``topic_intent_set`` with ``ttl=persistent`` for long-term recall.

Design rules (from the MEMO):

* Pure function of ``events.jsonl``. No side effects beyond writing the
  preferences file.
* Exponential decay weight ``exp(-days / 30)``. Events older than 30 days
  get roughly half weight; older events continue to decay.
* Smoothed confidence ``N / (N + 5)`` where ``N`` is the source event
  count backing a field.
* Merge with any existing ``preferences.yaml`` on disk: don't overwrite
  keys that this aggregator doesn't own. That lets a user hand-edit
  unrelated sections without fear of clobbering.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.memory import memory_events_path

# Keys owned by this aggregator. Anything else in preferences.yaml is
# left alone during merge.
#
# User-owned (not aggregated, edited by hand or by `af image-cover-add` /
# `af image-gate`):
#   image_generation:
#     default_mode: cover-only | cover-plus-body | none
#     cover_style:  editorial | diagram | cover         # passed to image-generate
#     cover_size:   "16:9" | ...                        # AtlasCloud aspect
#     cover_resolution: 1k | 2k | 4k
#     brand_overlay:
#       enabled:                   bool
#       logo_path:                 absolute filesystem path to PNG/RGBA
#       anchor:                    bottom_left | bottom_right | bottom_center
#                                  | top_left | top_right | top_center | center
#       width_ratio:               float 0..1 (logo width as ratio of canvas)
#       padding_ratio_x:           float
#       padding_ratio_y:           float
#       recolor_dark_to_light:     bool (recolor near-black logo pixels white)
#       dark_threshold:            int 0..255 (RGB cutoff for "near-black")
_OWNED_TOP_KEYS = {
    "schema_version",
    "last_computed",
    "source_events",
    "notes",
    "intent",
    "write",
    "preview",
    "publish",
}

SCHEMA_VERSION = 1
DEFAULT_PREFS_PATH = agentflow_home() / "preferences.yaml"

# Slice 1 gating thresholds (see MEMO §5.1).
_MIN_FILL_EVENTS = 3
_MIN_PUBLISH_SUCCESS = 3
_PUBLISH_HISTORY_WINDOW = 10  # most-recent N publish(success) events
_NEGATIVE_SIGNAL_LOOKBACK = 3  # recent publishes to scan for rollbacks


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _prefs_path() -> Path:
    ensure_user_dirs()
    return DEFAULT_PREFS_PATH


def load(path: Path | None = None) -> dict[str, Any]:
    """Read ``preferences.yaml``. Returns ``{}`` when the file is absent."""
    path = path or _prefs_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict[str, Any], path: Path | None = None) -> Path:
    """Write ``preferences.yaml`` with stable formatting."""
    path = path or _prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            data,
            fh,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    return path


def _read_events_from(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Weighting
# ---------------------------------------------------------------------------


def _parse_ts(ts: Any) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _decay_weight(event_ts: datetime | None, now: datetime) -> float:
    """Exponential decay. ``exp(-days / 30)`` per MEMO §6.4.

    Events exactly at ``now`` get weight 1.0; events 30 days old get
    roughly 0.37; older events keep decaying. Missing or future
    timestamps are treated as "now" (weight 1.0).
    """
    if event_ts is None:
        return 1.0
    delta_days = (now - event_ts).total_seconds() / 86_400.0
    if delta_days <= 0:
        return 1.0
    return math.exp(-delta_days / 30.0)


def _confidence(n: float) -> float:
    """Smoothed confidence N / (N + 5). N is the weighted source count."""
    if n <= 0:
        return 0.0
    return round(n / (n + 5.0), 4)


def _evidence_from_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_ts": event.get("ts"),
        "event_type": event.get("event_type"),
        "article_id": event.get("article_id"),
        "payload": event.get("payload") or {},
    }


# ---------------------------------------------------------------------------
# Aggregators (Slice 1)
# ---------------------------------------------------------------------------


def _aggregate_write_defaults(
    events: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """Majority-vote title/opening/closing index from fill_choices events."""
    fills = [e for e in events if e.get("event_type") == "fill_choices"]
    if len(fills) < _MIN_FILL_EVENTS:
        return None

    tallies: dict[str, Counter] = {
        "title": Counter(),
        "opening": Counter(),
        "closing": Counter(),
    }
    total_weight = 0.0
    evidence: list[dict[str, Any]] = []
    for ev in fills:
        payload = ev.get("payload") or {}
        w = _decay_weight(_parse_ts(ev.get("ts")), now)
        for key in ("title", "opening", "closing"):
            idx = payload.get(f"chosen_{key}_index")
            if isinstance(idx, int):
                tallies[key][idx] += w
        total_weight += w
        evidence.append(_evidence_from_event(ev))

    def _pick(c: Counter) -> int:
        if not c:
            return 0
        # Majority on weighted counts. Tie-break: lowest index.
        best_weight = max(c.values())
        candidates = sorted([idx for idx, w in c.items() if w == best_weight])
        return candidates[0]

    out = {
        "default_title_index": _pick(tallies["title"]),
        "default_opening_index": _pick(tallies["opening"]),
        "default_closing_index": _pick(tallies["closing"]),
        "_confidence": _confidence(total_weight),
        "_source_events": len(fills),
        "_evidence": evidence[-10:],
    }
    return out


def _aggregate_preview_defaults(
    events: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """Intersection of platforms from the most-recent 10 successful publishes."""
    publishes = [e for e in events if e.get("event_type") == "publish"]
    publishes.sort(key=lambda e: str(e.get("ts") or ""))

    # Successful publishes: at least one result with status=success.
    success_events: list[tuple[dict[str, Any], list[str]]] = []
    for ev in publishes:
        results = ((ev.get("payload") or {}).get("results")) or []
        succ_platforms = sorted({
            r.get("platform")
            for r in results
            if isinstance(r, dict) and r.get("status") == "success" and r.get("platform")
        })
        if succ_platforms:
            success_events.append((ev, succ_platforms))

    if len(success_events) < _MIN_PUBLISH_SUCCESS:
        return None

    window = success_events[-_PUBLISH_HISTORY_WINDOW:]
    platforms_sets: list[set[str]] = [set(ps) for _, ps in window]

    # Intersection: only platforms that succeeded in *every* recent publish.
    if not platforms_sets:
        return None
    intersected = set.intersection(*platforms_sets) if platforms_sets else set()

    # Weighted count drives confidence.
    total_weight = sum(_decay_weight(_parse_ts(e.get("ts")), now) for e, _ in window)
    evidence = [_evidence_from_event(e) for e, _ in window]

    return {
        "default_platforms": sorted(intersected),
        "_confidence": _confidence(total_weight),
        "_source_events": len(window),
        "_evidence": evidence[-10:],
    }


def _aggregate_publish_signals(
    events: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """Track publish_rolled_back in the last 3 publish events; if present,
    set ghost_status_override=draft for next 3 runs."""
    timeline = [
        e for e in events
        if e.get("event_type") in {"publish", "publish_rolled_back"}
    ]
    timeline.sort(key=lambda e: str(e.get("ts") or ""))
    if not timeline:
        return None

    # Walk the last few publish events and check whether any were rolled
    # back.
    recent_publishes = [
        e for e in timeline if e.get("event_type") == "publish"
    ][-_NEGATIVE_SIGNAL_LOOKBACK:]
    recent_rollbacks = [
        e for e in timeline if e.get("event_type") == "publish_rolled_back"
    ]
    # Only count rollbacks that happened since the earliest recent publish.
    if recent_publishes:
        earliest_ts = _parse_ts(recent_publishes[0].get("ts"))
        filtered_rollbacks = [
            r for r in recent_rollbacks
            if (_parse_ts(r.get("ts")) or now) >= (earliest_ts or now)
        ]
    else:
        filtered_rollbacks = recent_rollbacks

    out: dict[str, Any] = {}
    if filtered_rollbacks:
        out["ghost_status_override"] = "draft"
        out["override_remaining_runs"] = _NEGATIVE_SIGNAL_LOOKBACK
        out["_negative_signals"] = [
            {
                "event_ts": r.get("ts"),
                "event_type": r.get("event_type"),
                "article_id": r.get("article_id"),
                "note": (
                    "recent rollback -> downgrade ghost_status to draft "
                    "for next 3 runs"
                ),
                "payload": r.get("payload") or {},
            }
            for r in filtered_rollbacks[-10:]
        ]
        out["_source_events"] = len(filtered_rollbacks)
        out["_confidence"] = _confidence(float(len(filtered_rollbacks)))
    return out or None


def _aggregate_intent_history(
    events: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """Aggregate TopicIntent history for preference introspection.

    ``intent_used_in_write`` captures the queries that actually influenced a
    draft. ``topic_intent_set`` with ``ttl=persistent`` is also remembered so
    a user's explicitly pinned long-term topic shows up even before it has
    many write events behind it.
    """
    grouped: dict[str, dict[str, Any]] = {}
    grouped_profiles: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []
    total_weight = 0.0
    latest_persistent: dict[str, Any] | None = None
    latest_persistent_dt: datetime | None = None

    for ev in events:
        event_type = str(ev.get("event_type") or "")
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        query = ""
        profile_id = str(payload.get("profile_id") or "").strip()
        profile_label = str(payload.get("profile_label") or "").strip()
        ttl = str(payload.get("ttl") or "")
        if event_type == "intent_used_in_write":
            raw_query = payload.get("query") or payload.get("query_text")
            if isinstance(raw_query, str):
                query = raw_query.strip()
        elif event_type == "topic_intent_set":
            raw_query = payload.get("query")
            if isinstance(raw_query, str):
                query = raw_query.strip()
            if ttl != "persistent":
                query = ""
        else:
            continue

        if not query:
            continue

        event_dt = _parse_ts(ev.get("ts"))
        weight = _decay_weight(event_dt, now)
        total_weight += weight
        evidence.append(_evidence_from_event(ev))

        bucket = grouped.setdefault(
            query,
            {
                "query": query,
                "uses": 0,
                "_weighted_uses": 0.0,
                "_last_dt": None,
                "last_used": None,
            },
        )
        bucket["uses"] += 1
        bucket["_weighted_uses"] += weight
        last_dt = bucket.get("_last_dt")
        if event_dt is not None and (
            last_dt is None or (isinstance(last_dt, datetime) and event_dt > last_dt)
        ):
            bucket["_last_dt"] = event_dt
            bucket["last_used"] = event_dt.isoformat()
        elif bucket.get("last_used") is None:
            bucket["last_used"] = ev.get("ts")

        if profile_id:
            profile_bucket = grouped_profiles.setdefault(
                profile_id,
                {
                    "id": profile_id,
                    "label": profile_label or profile_id,
                    "uses": 0,
                    "_weighted_uses": 0.0,
                    "_last_dt": None,
                    "last_used": None,
                },
            )
            profile_bucket["uses"] += 1
            profile_bucket["_weighted_uses"] += weight
            if profile_label:
                profile_bucket["label"] = profile_label
            profile_last_dt = profile_bucket.get("_last_dt")
            if event_dt is not None and (
                profile_last_dt is None
                or (isinstance(profile_last_dt, datetime) and event_dt > profile_last_dt)
            ):
                profile_bucket["_last_dt"] = event_dt
                profile_bucket["last_used"] = event_dt.isoformat()
            elif profile_bucket.get("last_used") is None:
                profile_bucket["last_used"] = ev.get("ts")

        if event_type == "topic_intent_set" and ttl == "persistent":
            if latest_persistent_dt is None or (
                event_dt is not None and event_dt >= latest_persistent_dt
            ):
                latest_persistent = {
                    "query": query,
                    "set_at": ev.get("ts"),
                    "ttl": ttl,
                }
                if profile_id:
                    latest_persistent["profile_id"] = profile_id
                    latest_persistent["profile_label"] = profile_label or profile_id
                latest_persistent_dt = event_dt

    if not grouped:
        return None

    ranked = sorted(
        grouped.values(),
        key=lambda item: (
            -float(item.get("_weighted_uses") or 0.0),
            -int(item.get("uses") or 0),
            str(item.get("query") or ""),
        ),
    )
    recent_queries = [
        {
            "query": str(item.get("query") or ""),
            "uses": int(item.get("uses") or 0),
            "last_used": item.get("last_used"),
        }
        for item in ranked[:10]
    ]

    ranked_profiles = sorted(
        grouped_profiles.values(),
        key=lambda item: (
            -float(item.get("_weighted_uses") or 0.0),
            -int(item.get("uses") or 0),
            str(item.get("id") or ""),
        ),
    )
    recent_profiles = [
        {
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or item.get("id") or ""),
            "uses": int(item.get("uses") or 0),
            "last_used": item.get("last_used"),
        }
        for item in ranked_profiles[:10]
    ]

    out: dict[str, Any] = {
        "recent_queries": recent_queries,
        "recent_profiles": recent_profiles,
        "_confidence": _confidence(total_weight),
        "_source_events": len(evidence),
        "_evidence": evidence[-10:],
    }
    if latest_persistent is not None:
        out["persistent_query"] = latest_persistent
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rebuild_from_events(path: Path | None = None) -> dict[str, Any]:
    """Read events.jsonl -> compute the Slice-1 preferences dict.

    Does NOT write to disk. Callers (e.g. ``af prefs-rebuild``) decide
    whether to persist.
    """
    events_path = path or memory_events_path()
    events = _read_events_from(events_path)
    now = datetime.now(timezone.utc)

    aggregated: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "last_computed": now.isoformat(),
        "source_events": len(events),
        "notes": (
            "Auto-generated by agentflow.shared.preferences. Do not edit "
            "aggregator-owned sections by hand; next rebuild overwrites "
            "them. See docs/backlog/MEMORY_TO_DEFAULTS.md."
        ),
    }

    intent_section = _aggregate_intent_history(events, now)
    if intent_section is not None:
        aggregated["intent"] = intent_section

    write_section = _aggregate_write_defaults(events, now)
    if write_section is not None:
        aggregated["write"] = write_section

    preview_section = _aggregate_preview_defaults(events, now)
    if preview_section is not None:
        aggregated["preview"] = preview_section

    publish_section = _aggregate_publish_signals(events, now)
    if publish_section is not None:
        aggregated["publish"] = publish_section

    return aggregated


def merge_with_existing(
    fresh: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a fresh aggregator output with any existing preferences.yaml.

    Keys owned by the aggregator (``_OWNED_TOP_KEYS``) are replaced with
    the fresh value. Non-owned keys are preserved from ``existing``.
    """
    if not existing:
        return dict(fresh)
    merged = dict(existing)
    # Drop any owned keys from the existing file before copying fresh over;
    # this stops stale aggregator output from sticking around when this
    # run didn't emit them (e.g. rollback cleared).
    for key in _OWNED_TOP_KEYS:
        merged.pop(key, None)
    merged.update(fresh)
    return merged


def explain(key: str, path: Path | None = None) -> list[dict[str, Any]]:
    """Return up to 10 evidence events for a dotted preference key.

    The key must be a dotted path like ``write.default_title_index`` or
    ``publish.ghost_status_override``. The function walks the current
    preferences.yaml to find the nearest ``_evidence`` list for that key
    *or* the section that owns it.
    """
    prefs = load(path=path) if path else load()
    if not prefs:
        return []
    parts = key.split(".")

    # Walk from the full key down to the section root, returning the first
    # _evidence list we hit. This lets callers pass either a leaf
    # (write.default_title_index) or a section (write).
    for depth in range(len(parts), 0, -1):
        node: Any = prefs
        ok = True
        for p in parts[:depth]:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                ok = False
                break
        if not ok:
            continue
        if isinstance(node, dict):
            evidence = node.get("_evidence")
            if isinstance(evidence, list):
                return list(evidence)[:10]
            signals = node.get("_negative_signals")
            if isinstance(signals, list):
                return list(signals)[:10]
    return []


def pick_by_key(key: str, path: Path | None = None) -> Any:
    """Return the value at a dotted key in preferences.yaml, or None."""
    prefs = load(path=path) if path else load()
    node: Any = prefs
    for p in key.split("."):
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return None
    return node


def summarize(prefs: dict[str, Any]) -> dict[str, Any]:
    """Build a short summary dict used by ``af prefs-rebuild`` stdout."""
    out: dict[str, Any] = {
        "schema_version": prefs.get("schema_version"),
        "last_computed": prefs.get("last_computed"),
        "source_events": prefs.get("source_events"),
        "sections": {},
    }
    if "intent" in prefs:
        intent = prefs["intent"]
        recent_queries = intent.get("recent_queries") or []
        top_query = None
        if isinstance(recent_queries, list) and recent_queries:
            first = recent_queries[0]
            if isinstance(first, dict):
                top_query = first.get("query")
        out["sections"]["intent"] = {
            "top_query": top_query,
            "recent_queries_count": len(recent_queries) if isinstance(recent_queries, list) else 0,
            "persistent_query": (
                (intent.get("persistent_query") or {}).get("query")
                if isinstance(intent.get("persistent_query"), dict)
                else None
            ),
            "_confidence": intent.get("_confidence"),
            "_source_events": intent.get("_source_events"),
        }
    if "write" in prefs:
        w = prefs["write"]
        out["sections"]["write"] = {
            "default_title_index": w.get("default_title_index"),
            "default_opening_index": w.get("default_opening_index"),
            "default_closing_index": w.get("default_closing_index"),
            "_confidence": w.get("_confidence"),
            "_source_events": w.get("_source_events"),
        }
    if "preview" in prefs:
        p = prefs["preview"]
        out["sections"]["preview"] = {
            "default_platforms": p.get("default_platforms"),
            "_confidence": p.get("_confidence"),
            "_source_events": p.get("_source_events"),
        }
    if "publish" in prefs:
        pub = prefs["publish"]
        out["sections"]["publish"] = {
            "ghost_status_override": pub.get("ghost_status_override"),
            "override_remaining_runs": pub.get("override_remaining_runs"),
            "_source_events": pub.get("_source_events"),
        }
    return out


def clear_key(key: str, path: Path | None = None) -> bool:
    """Remove a dotted key from preferences.yaml. Returns True if a
    change was written."""
    prefs = load(path=path) if path else load()
    if not prefs:
        return False
    parts = key.split(".")
    node: Any = prefs
    for p in parts[:-1]:
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return False
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    node.pop(parts[-1], None)
    save(prefs, path=path or _prefs_path())
    return True


def clear_all(path: Path | None = None) -> bool:
    """Delete the entire preferences.yaml. Returns True if a file was removed."""
    target = path or _prefs_path()
    if target.exists():
        target.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Consumer helpers (read-side API for CLI commands)
# ---------------------------------------------------------------------------


def get_defaults() -> dict[str, Any]:
    """Thin wrapper around :func:`load` that returns an empty dict if the
    file is missing or the schema is unreadable. Always safe to call."""
    try:
        data = load()
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _section_is_confident(
    section: dict[str, Any] | None,
    min_source_events: int,
    min_confidence: float = 0.5,
) -> bool:
    if not isinstance(section, dict):
        return False
    n = section.get("_source_events")
    c = section.get("_confidence")
    try:
        n_val = int(n) if n is not None else 0
    except (TypeError, ValueError):
        n_val = 0
    try:
        c_val = float(c) if c is not None else 0.0
    except (TypeError, ValueError):
        c_val = 0.0
    return n_val >= min_source_events and c_val >= min_confidence


def pick_write_indices(
    prefs: dict[str, Any] | None = None,
    min_source_events: int = 3,
) -> dict[str, int | None]:
    """Return ``{title_idx, opening_idx, closing_idx}`` from preferences, or
    ``None`` for any field that doesn't meet the confidence/event threshold.

    Reads ``preferences.write.default_*_index`` only when
    ``preferences.write._source_events >= min_source_events`` AND
    ``preferences.write._confidence >= 0.5``.
    """
    result: dict[str, int | None] = {
        "title_idx": None,
        "opening_idx": None,
        "closing_idx": None,
    }
    data = prefs if prefs is not None else get_defaults()
    write = data.get("write") if isinstance(data, dict) else None
    if not _section_is_confident(write, min_source_events):
        return result

    assert isinstance(write, dict)  # narrowed by helper

    def _as_int(v: Any) -> int | None:
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        return None

    result["title_idx"] = _as_int(write.get("default_title_index"))
    result["opening_idx"] = _as_int(write.get("default_opening_index"))
    result["closing_idx"] = _as_int(write.get("default_closing_index"))
    return result


def pick_preview_platforms(
    prefs: dict[str, Any] | None = None,
    min_source_events: int = 3,
) -> list[str] | None:
    """Return ``preferences.preview.default_platforms`` if populated and
    confident, else ``None``."""
    data = prefs if prefs is not None else get_defaults()
    preview = data.get("preview") if isinstance(data, dict) else None
    if not _section_is_confident(preview, min_source_events):
        return None
    assert isinstance(preview, dict)
    platforms = preview.get("default_platforms")
    if not isinstance(platforms, list):
        return None
    cleaned = [str(p) for p in platforms if isinstance(p, str) and p.strip()]
    return cleaned if cleaned else None


def pick_ghost_status_override(
    prefs: dict[str, Any] | None = None,
) -> str | None:
    """Return ``preferences.publish.ghost_status_override`` if
    ``override_remaining_runs > 0``, else ``None``.

    Calling :func:`decrement_override` after a publish is the caller's
    responsibility.
    """
    data = prefs if prefs is not None else get_defaults()
    publish = data.get("publish") if isinstance(data, dict) else None
    if not isinstance(publish, dict):
        return None
    override = publish.get("ghost_status_override")
    if not isinstance(override, str) or not override:
        return None
    remaining = publish.get("override_remaining_runs")
    try:
        remaining_val = int(remaining) if remaining is not None else 0
    except (TypeError, ValueError):
        remaining_val = 0
    if remaining_val <= 0:
        return None
    return override


def decrement_override(path: Path | None = None) -> None:
    """After a publish that honored the override, decrement
    ``override_remaining_runs`` by 1 and save. If it hits 0, clear the
    override fields."""
    prefs = load(path=path) if path else load()
    if not prefs:
        return
    publish = prefs.get("publish")
    if not isinstance(publish, dict):
        return
    remaining = publish.get("override_remaining_runs")
    try:
        remaining_val = int(remaining) if remaining is not None else 0
    except (TypeError, ValueError):
        remaining_val = 0
    if remaining_val <= 0:
        # Already exhausted — clear stale override fields defensively.
        for k in ("ghost_status_override", "override_remaining_runs"):
            publish.pop(k, None)
        save(prefs, path=path or _prefs_path())
        return
    new_remaining = remaining_val - 1
    if new_remaining <= 0:
        publish.pop("ghost_status_override", None)
        publish.pop("override_remaining_runs", None)
    else:
        publish["override_remaining_runs"] = new_remaining
    save(prefs, path=path or _prefs_path())


__all__ = [
    "DEFAULT_PREFS_PATH",
    "SCHEMA_VERSION",
    "clear_all",
    "clear_key",
    "decrement_override",
    "explain",
    "get_defaults",
    "load",
    "merge_with_existing",
    "pick_by_key",
    "pick_ghost_status_override",
    "pick_preview_platforms",
    "pick_write_indices",
    "rebuild_from_events",
    "save",
    "summarize",
]
