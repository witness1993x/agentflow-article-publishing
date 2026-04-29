"""`af report` — cross-channel daily/weekly status digest.

Spec: ``docs/backlog/ARCHITECTURE_EVAL_SKILL_VS_APP.md`` §9.

The command walks the on-disk state under ``~/.agentflow/`` and
reconstructs a single view of "what's going on":

* IDEAS — hotspot files in window, intent usage
* CONTENT IN FLIGHT — drafts whose ``metadata.json.status`` is not
  ``published``
* SHIPPED — publish_history.jsonl, grouped by platform, counting
  successes in window
* ROLLBACKS — same source, counting rolled_back records in window
* ATTENTION NEEDED — drafts with unresolved ``image_placeholders`` and
  missing env vars per platform

``--json`` prints the structured dict; default is the pretty layout
from the spec.
"""

from __future__ import annotations

import json as _json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click

from agentflow.cli.commands import cli
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.hotspot_store import hotspots_dir, search_results_dir


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


# Platforms we know about + env vars they need to publish. Missing any of
# these flags an ATTENTION NEEDED warning for the next publish run.
# Kept in sync with agentflow/config/accounts_loader.py.
_PLATFORM_ENV = {
    "medium": ["MEDIUM_INTEGRATION_TOKEN"],
    "linkedin_article": ["LINKEDIN_ACCESS_TOKEN", "LINKEDIN_PERSON_URN"],
    "ghost_wordpress": ["GHOST_ADMIN_API_URL", "GHOST_ADMIN_API_KEY"],
    "substack": ["SUBSTACK_EMAIL", "SUBSTACK_PASSWORD"],
    "wechat_mp": ["WECHAT_APP_ID", "WECHAT_APP_SECRET"],
    "x_twitter": ["X_API_KEY", "X_API_SECRET"],
}

_TERMINAL_STATUSES = {"published"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_window(window: str) -> datetime | None:
    """Return the lower-bound timestamp (UTC) or None for 'all'."""
    if window == "all":
        return None
    m = re.fullmatch(r"(\d+)d", window.strip())
    if not m:
        raise click.UsageError(f"invalid --window {window!r}; use Nd or 'all'.")
    days = int(m.group(1))
    return datetime.now(timezone.utc) - timedelta(days=days)


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


def _in_window(ts: datetime | None, floor: datetime | None) -> bool:
    if floor is None:
        return True
    if ts is None:
        return False
    return ts >= floor


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _iter_draft_metadata(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Yield ``(metadata_path, metadata_dict)`` for every draft."""
    if not root.exists():
        return []
    out: list[tuple[Path, dict[str, Any]]] = []
    for sub in sorted(root.iterdir()):
        meta = sub / "metadata.json"
        if not meta.is_file():
            continue
        try:
            data = _json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            out.append((meta, data))
    return out


def _hotspots_in_window(root: Path, floor: datetime | None, *, kind: str) -> list[dict[str, Any]]:
    """Hotspot-like JSON files that fall in the window."""
    if not root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for p in sorted(root.glob("*.json")):
        if not p.is_file():
            continue
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        if not _in_window(mtime, floor):
            continue
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        hs = data.get("hotspots") or data.get("items") or []
        entries.append(
            {
                "file": p.name,
                "path": str(p),
                "hotspot_count": len(hs) if isinstance(hs, list) else 0,
                "modified": mtime.isoformat(),
                "kind": kind,
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _section_ideas(
    home: Path,
    memory_events: list[dict[str, Any]],
    floor: datetime | None,
) -> dict[str, Any]:
    hotspot_files = _hotspots_in_window(hotspots_dir(), floor, kind="scan")
    hotspot_files.extend(
        _hotspots_in_window(search_results_dir(), floor, kind="search")
    )

    intent_events = [
        e for e in memory_events
        if e.get("event_type") == "topic_intent_used"
        and _in_window(_parse_ts(e.get("ts")), floor)
    ]

    # Aggregate intent queries + counts.
    intent_counts: dict[str, int] = {}
    for ev in intent_events:
        q = (ev.get("payload") or {}).get("query")
        if q:
            intent_counts[q] = intent_counts.get(q, 0) + 1

    # Last intent set (independent of window).
    last_intent_set = None
    for ev in reversed(memory_events):
        if ev.get("event_type") == "topic_intent_set":
            payload = ev.get("payload") or {}
            last_intent_set = {
                "ts": ev.get("ts"),
                "query": payload.get("query"),
                "mode": payload.get("mode"),
                "ttl": payload.get("ttl"),
            }
            break

    # Active intent on disk (current.yaml).
    current_intent = None
    current_path = home / "intents" / "current.yaml"
    if current_path.exists():
        try:
            import yaml
            data = yaml.safe_load(current_path.read_text(encoding="utf-8")) or {}
            current_intent = {
                "query": (data.get("query") or {}).get("text"),
                "mode": (data.get("query") or {}).get("mode"),
                "ttl": (data.get("metadata") or {}).get("ttl"),
                "created_at": data.get("created_at"),
            }
        except Exception:
            current_intent = None

    return {
        "hotspot_files": hotspot_files,
        "hotspot_files_count": len(hotspot_files),
        "intents_used": [
            {"query": q, "count": c}
            for q, c in sorted(intent_counts.items(), key=lambda kv: -kv[1])
        ],
        "intents_used_total": sum(intent_counts.values()),
        "current_intent": current_intent,
        "last_intent_set": last_intent_set,
    }


def _section_in_flight(
    home: Path,
    floor: datetime | None,
) -> dict[str, Any]:
    drafts_root = home / "drafts"
    drafts: list[dict[str, Any]] = []
    for _meta_path, meta in _iter_draft_metadata(drafts_root):
        status = meta.get("status")
        if status in _TERMINAL_STATUSES:
            continue
        updated_ts = _parse_ts(meta.get("updated_at") or meta.get("created_at"))
        if not _in_window(updated_ts, floor):
            continue
        drafts.append(
            {
                "article_id": meta.get("article_id"),
                "hotspot_id": meta.get("hotspot_id"),
                "title": meta.get("title") or (
                    (meta.get("skeleton") or {}).get("title_candidates") or [{}]
                )[0].get("text"),
                "status": status,
                "updated_at": meta.get("updated_at") or meta.get("created_at"),
                "target_series": meta.get("target_series"),
            }
        )
    drafts.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
    return {"count": len(drafts), "drafts": drafts}


def _section_shipped(
    home: Path,
    floor: datetime | None,
) -> dict[str, Any]:
    records = _read_jsonl(home / "publish_history.jsonl")
    per_platform: dict[str, int] = {}
    shipped: list[dict[str, Any]] = []
    for r in records:
        if r.get("status") != "success":
            continue
        ts = _parse_ts(r.get("published_at"))
        if not _in_window(ts, floor):
            continue
        platform = str(r.get("platform") or "unknown")
        per_platform[platform] = per_platform.get(platform, 0) + 1
        shipped.append(
            {
                "article_id": r.get("article_id"),
                "platform": platform,
                "published_url": r.get("published_url"),
                "published_at": r.get("published_at"),
            }
        )
    shipped.sort(key=lambda d: d.get("published_at") or "", reverse=True)
    return {
        "total": sum(per_platform.values()),
        "per_platform": dict(sorted(per_platform.items())),
        "records": shipped,
    }


def _section_rollbacks(
    home: Path,
    floor: datetime | None,
) -> dict[str, Any]:
    records = _read_jsonl(home / "publish_history.jsonl")
    per_platform: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    for r in records:
        if r.get("status") != "rolled_back":
            continue
        ts = _parse_ts(r.get("published_at"))
        if not _in_window(ts, floor):
            continue
        platform = str(r.get("platform") or "unknown")
        per_platform[platform] = per_platform.get(platform, 0) + 1
        events.append(
            {
                "article_id": r.get("article_id"),
                "platform": platform,
                "platform_post_id": r.get("platform_post_id"),
                "rolled_back_at": r.get("published_at"),
            }
        )
    events.sort(key=lambda d: d.get("rolled_back_at") or "", reverse=True)
    return {
        "total": sum(per_platform.values()),
        "per_platform": dict(sorted(per_platform.items())),
        "records": events,
    }


def _section_attention(
    home: Path,
) -> dict[str, Any]:
    drafts_root = home / "drafts"
    unresolved_drafts: list[dict[str, Any]] = []
    for _meta_path, meta in _iter_draft_metadata(drafts_root):
        status = meta.get("status")
        if status in _TERMINAL_STATUSES:
            continue
        placeholders = meta.get("image_placeholders") or []
        unresolved = [
            p for p in placeholders
            if isinstance(p, dict) and not p.get("resolved_path")
        ]
        if not unresolved:
            continue
        unresolved_drafts.append(
            {
                "article_id": meta.get("article_id"),
                "status": status,
                "unresolved_count": len(unresolved),
                "placeholders": [
                    {
                        "id": p.get("id"),
                        "description": p.get("description"),
                        "section": p.get("section_heading"),
                    }
                    for p in unresolved
                ],
            }
        )

    # Missing env vars per platform.
    env_status: list[dict[str, Any]] = []
    for platform, env_vars in _PLATFORM_ENV.items():
        missing = [v for v in env_vars if not os.environ.get(v)]
        if missing:
            env_status.append({"platform": platform, "missing_env": missing})

    return {
        "drafts_with_unresolved_images": unresolved_drafts,
        "missing_env_vars": env_status,
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _build_report(window: str) -> dict[str, Any]:
    home = agentflow_home()
    floor = _parse_window(window)

    memory_events = _read_jsonl(home / "memory" / "events.jsonl")

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": window,
        "window_floor": floor.isoformat() if floor else None,
        "ideas": _section_ideas(home, memory_events, floor),
        "in_flight": _section_in_flight(home, floor),
        "shipped": _section_shipped(home, floor),
        "rollbacks": _section_rollbacks(home, floor),
        "attention": _section_attention(home),
    }
    return report


def _pretty_print(report: dict[str, Any]) -> None:
    window = report["window"]
    click.echo(f"AgentFlow Report — window: {window}")
    click.echo("")

    # IDEAS
    click.echo("IDEAS:")
    ideas = report["ideas"]
    scan_files = [f for f in ideas["hotspot_files"] if f.get("kind") == "scan"]
    search_files = [f for f in ideas["hotspot_files"] if f.get("kind") == "search"]
    click.echo(
        f"  - {len(scan_files)} scan file(s), {len(search_files)} search file(s)"
    )
    if ideas["intents_used"]:
        top = ideas["intents_used"][:3]
        for item in top:
            click.echo(f"  - intent used: {item['query']!r}  ×{item['count']}")
    else:
        click.echo("  - no intents used in window")
    if ideas.get("current_intent") and ideas["current_intent"].get("query"):
        ci = ideas["current_intent"]
        click.echo(
            f"  - current intent: {ci.get('query')!r}  "
            f"mode={ci.get('mode')}  ttl={ci.get('ttl')}"
        )
    elif ideas.get("last_intent_set"):
        li = ideas["last_intent_set"]
        click.echo(
            f"  - last intent set: {li.get('query')!r} at {li.get('ts')} "
            "(since cleared)"
        )
    click.echo("")

    # CONTENT IN FLIGHT
    click.echo("CONTENT IN FLIGHT:")
    in_flight = report["in_flight"]
    if in_flight["count"] == 0:
        click.echo("  - (no unpublished drafts in window)")
    else:
        for d in in_flight["drafts"][:10]:
            title = d.get("title") or "(no title)"
            if len(title) > 48:
                title = title[:45] + "..."
            click.echo(
                f"  [{d.get('status','?'):<14}] {d.get('article_id','-')} "
                f"\"{title}\"  updated={d.get('updated_at','-')}"
            )
        if in_flight["count"] > 10:
            click.echo(f"  ...and {in_flight['count'] - 10} more")
    click.echo("")

    # SHIPPED
    click.echo(f"SHIPPED ({window}):")
    shipped = report["shipped"]
    if shipped["total"] == 0:
        click.echo("  - (nothing published in window)")
    else:
        for platform, count in shipped["per_platform"].items():
            click.echo(f"  ok  {platform:<22} {count}")
    click.echo("")

    # ROLLBACKS
    click.echo("ROLLBACKS:")
    rollbacks = report["rollbacks"]
    if rollbacks["total"] == 0:
        click.echo("  - (no rollbacks in window)")
    else:
        for platform, count in rollbacks["per_platform"].items():
            click.echo(f"  <- {platform:<22} {count}")
    click.echo("")

    # ATTENTION NEEDED
    click.echo("ATTENTION NEEDED:")
    attn = report["attention"]
    drafts_iss = attn.get("drafts_with_unresolved_images") or []
    env_iss = attn.get("missing_env_vars") or []
    if not drafts_iss and not env_iss:
        click.echo("  - (nothing urgent)")
    for d in drafts_iss:
        click.echo(
            f"  !! {d['unresolved_count']} unresolved image(s) in "
            f"{d['article_id']}  (status={d['status']})"
        )
    for e in env_iss:
        click.echo(
            f"  !! {e['platform']}: missing env {', '.join(e['missing_env'])}"
            " -- will skip on next publish"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@cli.command(
    "report",
    help="Cross-channel status digest (IDEAS / IN FLIGHT / SHIPPED / ROLLBACKS / ATTENTION).",
)
@click.option(
    "--window",
    "window",
    type=str,
    default="7d",
    show_default=True,
    help="Time window: Nd (e.g. 7d, 30d) or 'all'.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the structured report as JSON on stdout. Human logs go to stderr.",
)
def report(window: str, as_json: bool) -> None:
    report_dict = _build_report(window)

    if as_json:
        # Human summary to stderr so scripts can parse stdout cleanly.
        click.echo(
            f"(af report window={window} in_flight={report_dict['in_flight']['count']} "
            f"shipped={report_dict['shipped']['total']} "
            f"rollbacks={report_dict['rollbacks']['total']})",
            err=True,
        )
        click.echo(_json.dumps(report_dict, ensure_ascii=False, indent=2, default=str))
        return

    _pretty_print(report_dict)
