"""`af review-*` — Telegram review CLI subcommands.

Self-registering: imported lazily by ``agentflow.cli.commands`` at package
import time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from agentflow.cli.commands import _emit_json, cli


@cli.command(
    "review-daemon",
    help="Run the Telegram review daemon (long-poll, callbacks, timeout sweeps).",
)
@click.option(
    "--skip-preflight",
    is_flag=True,
    default=False,
    help="Bypass the credential preflight check (af doctor) on startup.",
)
def review_daemon_cmd(skip_preflight: bool) -> None:
    from agentflow.agent_review import daemon

    daemon.run(skip_preflight=skip_preflight)


@cli.command(
    "review-init",
    help="One-shot: print bot info + current chat_id resolution.",
)
def review_init_cmd() -> None:
    from agentflow.agent_review import daemon, tg_client

    me = tg_client.get_me()
    click.echo(f"bot:                @{me.get('username')}")
    click.echo(f"bot id:             {me.get('id')}")
    chat_id = daemon.get_review_chat_id()
    if chat_id is None:
        click.echo("review chat_id:     (not configured)")
        click.echo(
            "next:               send /start to the bot in Telegram, then start "
            "`af review-daemon` to capture chat_id automatically."
        )
    else:
        click.echo(f"review chat_id:     {chat_id}")


@cli.command(
    "review-status",
    help="Show gate_history + current state for an article.",
)
@click.argument("article_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def review_status_cmd(article_id: str, as_json: bool) -> None:
    from agentflow.agent_review import state

    try:
        cur = state.current_state(article_id)
        history = state.gate_history(article_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    if as_json:
        _emit_json({"article_id": article_id, "current_state": cur, "history": history})
        return
    click.echo(f"article_id:    {article_id}")
    click.echo(f"current_state: {cur}")
    click.echo(f"history:       {len(history)} entries")
    for entry in history[-10:]:
        click.echo(
            f"  {entry.get('timestamp', '?')}  {entry.get('gate', '-')}  "
            f"{entry.get('from_state', '?')} -> {entry.get('to_state', '?')}  "
            f"({entry.get('actor', '?')}/{entry.get('decision', '?')})"
        )


@cli.command(
    "review-resume",
    help="Force a state transition (admin escape hatch for stuck articles).",
)
@click.argument("article_id")
@click.option("--to-state", "to_state", required=True)
@click.option("--gate", default="*", show_default=True)
@click.option("--notes", default="")
def review_resume_cmd(
    article_id: str, to_state: str, gate: str, notes: str
) -> None:
    from agentflow.agent_review import state

    entry = state.transition(
        article_id,
        gate=gate,
        to_state=to_state,
        actor="admin",
        decision="resume",
        notes=notes or None,
        force=True,
    )
    click.echo(f"forced transition: {entry.get('from_state')} -> {entry.get('to_state')}")


@cli.command(
    "review-post-b",
    help="Post a Gate B (draft review) card for an article to the configured chat.",
)
@click.argument("article_id")
def review_post_b_cmd(article_id: str) -> None:
    from agentflow.agent_review import triggers

    summary = triggers.post_gate_b(article_id)
    if summary is None:
        raise click.ClickException(
            "TG not configured / no chat_id — run `af review-init`."
        )
    click.echo(
        f"posted Gate B card: short_id={summary['short_id']} "
        f"message_id={summary['tg_message_id']} blockers={summary['blockers']}"
    )


@cli.command(
    "review-post-c",
    help="Post a Gate C (cover review) card for an article.",
)
@click.argument("article_id")
def review_post_c_cmd(article_id: str) -> None:
    from agentflow.agent_review import triggers

    summary = triggers.post_gate_c(article_id)
    if summary is None:
        raise click.ClickException(
            "TG not configured / no chat_id / no cover image — see `af review-init`."
        )
    click.echo(
        f"posted Gate C card: short_id={summary['short_id']} "
        f"message_id={summary['tg_message_id']} blockers={summary['blockers']}"
    )


@cli.command(
    "review-post-d",
    help="Post a Gate D (channel selection) card for an article.",
)
@click.argument("article_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def review_post_d_cmd(article_id: str, as_json: bool) -> None:
    from agentflow.agent_review import triggers

    summary = triggers.post_gate_d(article_id)
    if summary is None:
        raise click.ClickException(
            "TG not configured / no chat_id / no metadata — see `af review-init`."
        )
    if as_json:
        _emit_json(summary)
        return
    click.echo(
        f"posted Gate D card: short_id={summary['short_id']} "
        f"message_id={summary['tg_message_id']} "
        f"selected={summary['selected']} available={summary['available']}"
    )


@cli.command(
    "review-publish-ready",
    help="Manually trigger the final publish-ready card (preview + package + TG).",
)
@click.argument("article_id")
def review_publish_ready_cmd(article_id: str) -> None:
    from agentflow.agent_review import triggers

    summary = triggers.post_publish_ready(article_id)
    if summary is None:
        raise click.ClickException(
            "TG not configured / no chat_id / package missing — see `af review-init`."
        )
    click.echo(
        f"posted publish-ready: message_id={summary['tg_message_id']} "
        f"package={summary['package_path']}"
    )


# ---------------------------------------------------------------------------
# Doctor: credential health + readiness gates
# ---------------------------------------------------------------------------


def _icon(cr: Any) -> str:
    """Pretty marker for a CheckResult."""
    if not cr.present:
        return "✗"
    if cr.valid is True:
        return "✓"
    if cr.valid is False:
        return "✗"
    return "·"  # present but not probed


@cli.command(
    "doctor",
    help="Show credential health for every service (TG, Atlas, Moonshot, "
    "Anthropic, Jina, OpenAI, Twitter, Ghost, LinkedIn). "
    "Use --strict to exit non-zero if anything required is missing.",
)
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Exit non-zero when review-daemon / hotspots / image-gate readiness "
    "would fail. Suitable for cron / CI.",
)
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    help="Skip the 1h probe cache and re-hit each remote API.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def doctor_cmd(strict: bool, fresh: bool, as_json: bool) -> None:
    from agentflow.agent_review import preflight as _pf

    checks = _pf.all_checks(fresh=fresh)
    # v1.0.4: report which file each env var resolved from (if available).
    from agentflow.cli.commands import _resolved_sources as _src_map
    if as_json:
        rows = []
        for c in checks:
            d = c.to_dict()
            if c.env_var:
                d["source"] = _src_map.get(c.env_var) or "(not loaded — process env)"
            rows.append(d)
        _emit_json(rows)
    else:
        click.echo("Credential health\n" + "-" * 50)
        for cr in checks:
            src = ""
            if cr.env_var:
                resolved = _src_map.get(cr.env_var)
                if resolved:
                    # Show just the parent dir + filename to keep the line short
                    p = Path(resolved)
                    src = f"  [src: {p.parent.name}/{p.name}]"
            click.echo(
                f"  {_icon(cr)} {cr.name:<22} {cr.message[:60]}{src}"
            )
        click.echo()
        click.echo("Readiness gates")
        click.echo("-" * 50)
        # Aggregate readiness for each command
        try:
            _pf.assert_ready_for_review_daemon()
            click.echo("  ✓ review-daemon")
        except _pf.PreflightError as err:
            click.echo(f"  ✗ review-daemon — {err}")
        try:
            _pf.assert_ready_for_hotspots()
            click.echo("  ✓ hotspots / write / fill")
        except _pf.PreflightError as err:
            click.echo(f"  ✗ hotspots / write / fill — {err}")
        try:
            _pf.assert_ready_for_image_gate()
            click.echo("  ✓ image-gate")
        except _pf.PreflightError as err:
            click.echo(f"  ✗ image-gate — {err}")

    if strict:
        try:
            _pf.assert_ready_for_review_daemon()
            _pf.assert_ready_for_hotspots()
        except _pf.PreflightError as err:
            raise click.ClickException(str(err))


# ---------------------------------------------------------------------------
# Auth: gate uid access to the bot
# ---------------------------------------------------------------------------


def _parse_actions_csv(raw: str | None) -> list[str] | None:
    """Parse ``--actions review,edit`` style input. Empty/None → None
    (which auth.add treats as ``["*"]``). Validates against the closed
    vocabulary at the CLI layer for fast feedback."""
    from agentflow.agent_review import auth as _auth

    if raw is None:
        return None
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if not tokens:
        return None
    bad = [t for t in tokens if t not in _auth.ACTION_VOCABULARY]
    if bad:
        raise click.UsageError(
            f"unknown action(s): {', '.join(bad)}. "
            f"Allowed: {', '.join(_auth.ACTION_VOCABULARY)}"
        )
    return tokens


@cli.command(
    "review-auth-add",
    help="Authorize a Telegram uid to interact with the review bot.",
)
@click.argument("uid", type=int)
@click.option("--note", default=None, help="Optional human-readable label.")
@click.option(
    "--actions",
    "actions_csv",
    default=None,
    help="Comma-separated action list (review,write,edit,image,publish,*). "
    "Default: * (full access).",
)
def review_auth_add_cmd(
    uid: int, note: str | None, actions_csv: str | None
) -> None:
    from agentflow.agent_review import auth

    actions = _parse_actions_csv(actions_csv)
    auth.add(uid, note=note, actions=actions)
    eff = ",".join(actions or ["*"])
    click.echo(
        f"authorized uid={uid}{' (' + note + ')' if note else ''} actions={eff}"
    )


@cli.command(
    "review-auth-remove",
    help="Revoke a previously authorized Telegram uid (operator's own uid is non-removable).",
)
@click.argument("uid", type=int)
def review_auth_remove_cmd(uid: int) -> None:
    from agentflow.agent_review import auth

    op = auth.operator_uid()
    if op is not None and int(uid) == int(op):
        raise click.ClickException(
            f"uid {uid} is the operator (TELEGRAM_REVIEW_CHAT_ID); "
            "remove via env var, not this command."
        )
    if auth.remove(uid):
        click.echo(f"revoked uid={uid}")
    else:
        click.echo(f"uid={uid} was not in the authorized list")


@cli.command(
    "review-auth-list",
    help="Show authorized uids (operator's uid + explicit grants).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def review_auth_list_cmd(as_json: bool) -> None:
    from agentflow.agent_review import auth

    op = auth.operator_uid()
    grants = auth.list_authorized()
    if as_json:
        _emit_json({"operator_uid": op, "grants": grants})
        return
    click.echo(
        f"operator uid:  {op or '(unknown — set TELEGRAM_REVIEW_CHAT_ID)'} "
        "(always allowed, actions=*)"
    )
    if grants:
        click.echo(f"explicit grants ({len(grants)}):")
        for entry in grants:
            note = entry.get("note") or ""
            label = f"({note})" if note else ""
            actions = ",".join(entry.get("allowed_actions") or ["*"])
            click.echo(
                f"  +{entry.get('uid')}  {label}  {actions}"
                f"  ({entry.get('authorized_at')})"
            )
    else:
        click.echo("explicit grants: (none)")


@cli.command(
    "review-auth-set-actions",
    help="Overwrite the allowed-actions list on an existing grant "
    "(comma-separated, e.g. review,edit). Use '*' for full access.",
)
@click.argument("uid", type=int)
@click.argument("actions_csv")
def review_auth_set_actions_cmd(uid: int, actions_csv: str) -> None:
    from agentflow.agent_review import auth

    op = auth.operator_uid()
    if op is not None and int(uid) == int(op):
        raise click.ClickException(
            f"uid {uid} is the operator (TELEGRAM_REVIEW_CHAT_ID); "
            "operator implicitly has '*' and cannot be modified."
        )
    actions = _parse_actions_csv(actions_csv) or ["*"]
    if not auth.set_actions(uid, actions):
        raise click.ClickException(
            f"uid={uid} is not in the authorized list; "
            f"add it first with `af review-auth-add {uid}`."
        )
    click.echo(f"updated uid={uid} actions={','.join(actions)}")


# ---------------------------------------------------------------------------
# Document library
# ---------------------------------------------------------------------------


@cli.command(
    "review-list",
    help="List all articles grouped by gate state (a 'document library' view).",
)
@click.option(
    "--state",
    "filter_state",
    default=None,
    help="Filter to a single gate state (e.g. ready_to_publish).",
)
@click.option(
    "--since",
    "since",
    default=None,
    help="Drop articles older than this. Format: '2d' / '1w' / '14d' / 'all'. "
    "Default: show everything (use to cut old fixture noise).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def review_list_cmd(
    filter_state: str | None, since: str | None, as_json: bool
) -> None:
    from datetime import datetime, timezone
    from pathlib import Path
    import json as _json
    import re as _re

    from agentflow.agent_review import state as _state, timeout_state as _ts
    from agentflow.shared.bootstrap import agentflow_home as _home

    drafts_dir = _home() / "drafts"
    if not drafts_dir.exists():
        click.echo("no drafts directory")
        return

    # Parse --since
    since_cutoff: datetime | None = None
    if since and since.strip().lower() not in ("all", ""):
        m = _re.match(r"^\s*(\d+)\s*([dDwW])\s*$", since)
        if not m:
            raise click.UsageError(
                f"invalid --since {since!r}; use '2d' / '1w' / '14d' / 'all'"
            )
        n = int(m.group(1))
        unit = m.group(2).lower()
        days = n if unit == "d" else n * 7
        from datetime import timedelta as _td
        since_cutoff = datetime.now(timezone.utc) - _td(days=days)

    pending_states = {
        _state.STATE_DRAFT_PENDING_REVIEW,
        _state.STATE_IMAGE_PENDING_REVIEW,
    }
    now = datetime.now(timezone.utc)

    by_state: dict[str, list[dict[str, Any]]] = {}
    for sub in sorted(drafts_dir.iterdir()):
        if not sub.is_dir():
            continue
        meta_path = sub / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            data = _json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        cur = _state.current_state(sub.name)
        if filter_state and cur != filter_state:
            continue
        # --since: filter by latest activity (gate_history[-1].timestamp,
        # falling back to metadata's saved_at/updated_at/created_at)
        if since_cutoff is not None:
            latest_iso = (
                ((data.get("gate_history") or [{}])[-1].get("timestamp"))
                or data.get("saved_at")
                or data.get("updated_at")
                or data.get("created_at")
            )
            try:
                latest_ts = datetime.fromisoformat(latest_iso) if latest_iso else None
            except (ValueError, TypeError):
                latest_ts = None
            # save_draft writes naive datetime.now().isoformat() — assume UTC
            if latest_ts is not None and latest_ts.tzinfo is None:
                latest_ts = latest_ts.replace(tzinfo=timezone.utc)
            if latest_ts is None or latest_ts < since_cutoff:
                continue
        publisher = (data.get("publisher_account") or {}).get("brand") or ""
        stuck_hrs: float | None = None
        if cur in pending_states:
            history = data.get("gate_history") or []
            if history:
                try:
                    ts = datetime.fromisoformat(history[-1].get("timestamp") or "")
                    stuck_hrs = (now - ts).total_seconds() / 3600.0
                except (ValueError, TypeError):
                    stuck_hrs = None
        ts_entry = _ts.get(sub.name)
        badge = ""
        if cur == _state.STATE_IMAGE_SKIPPED and ts_entry and ts_entry.get("second_action_taken_at"):
            badge = "auto-skipped"
        elif cur in pending_states and ts_entry and ts_entry.get("first_pinged_at"):
            badge = "pinged"
        by_state.setdefault(cur, []).append({
            "id": sub.name,
            "title": data.get("title") or "(no title)",
            "publisher": publisher,
            "published_url": data.get("published_url"),
            "saved_at": data.get("saved_at") or data.get("updated_at") or data.get("created_at"),
            "stuck_hrs": stuck_hrs,
            "badge": badge,
        })

    if as_json:
        _emit_json(by_state)
        return

    if not by_state:
        click.echo("(no articles match)")
        return

    state_order = [
        _state.STATE_TOPIC_POOL, _state.STATE_TOPIC_APPROVED,
        _state.STATE_DRAFTING, _state.STATE_DRAFT_PENDING_REVIEW,
        _state.STATE_DRAFT_APPROVED, _state.STATE_IMAGE_PENDING_REVIEW,
        _state.STATE_IMAGE_APPROVED, _state.STATE_IMAGE_SKIPPED,
        _state.STATE_READY_TO_PUBLISH, _state.STATE_PUBLISHED,
        _state.STATE_DRAFT_REJECTED, _state.STATE_TOPIC_REJECTED,
        _state.STATE_DRAFTING_LOCKED_HUMAN,
    ]
    def _tail(it: dict[str, Any]) -> str:
        bits: list[str] = []
        if it.get("stuck_hrs") is not None:
            bits.append(f"⏰ stuck {it['stuck_hrs']:.1f}h")
        if it.get("badge"):
            bits.append(it["badge"])
        return ("  " + "  ".join(bits)) if bits else ""

    for st in state_order:
        items = by_state.pop(st, None)
        if not items:
            continue
        click.echo(f"\n[{st}]  {len(items)}")
        for it in items:
            url = f"  -> {it['published_url']}" if it.get("published_url") else ""
            pub = f"({it['publisher']}) " if it["publisher"] else ""
            click.echo(f"  {it['id'][-30:]}  {pub}{it['title'][:60]}{url}{_tail(it)}")
    # Any leftover states (custom / unknown)
    for st, items in by_state.items():
        click.echo(f"\n[{st}]  {len(items)}")
        for it in items:
            click.echo(f"  {it['id'][-30:]}  {it['title'][:60]}{_tail(it)}")


# ---------------------------------------------------------------------------
# Cron: twice-daily auto hotspots scan (macOS launchctl)
# ---------------------------------------------------------------------------


_LAUNCHD_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agentflow.review.hotspots</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>cd {backend_dir} &amp;&amp; set -a &amp;&amp; . ./.env &amp;&amp; set +a &amp;&amp; ./.venv/bin/af hotspots --filter ".*"</string>
    </array>
    <key>StandardOutPath</key>
    <string>{log_dir}/hotspots.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/hotspots.stderr.log</string>
    <key>StartCalendarInterval</key>
    <array>
{calendar_intervals}
    </array>
</dict>
</plist>
"""


@cli.command(
    "review-cron-install",
    help="Install a launchd plist that runs `af hotspots` at fixed times daily.",
)
@click.option(
    "--times",
    default="09:00,18:00",
    show_default=True,
    help="Comma-separated HH:MM (local time) — when to fire af hotspots.",
)
def review_cron_install_cmd(times: str) -> None:
    import os as _os
    import subprocess
    from pathlib import Path

    parts = [t.strip() for t in (times or "").split(",") if t.strip()]
    intervals: list[tuple[int, int]] = []
    for t in parts:
        try:
            h_str, m_str = t.split(":")
            h, m = int(h_str), int(m_str)
            assert 0 <= h < 24 and 0 <= m < 60
            intervals.append((h, m))
        except Exception:
            raise click.UsageError(f"bad time {t!r}; use HH:MM (e.g. 09:00).")
    if not intervals:
        raise click.UsageError("at least one time required")

    backend_dir = Path(__file__).resolve().parents[2]
    log_dir = Path.home() / ".agentflow" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.agentflow.review.hotspots.plist"

    cal_xml = "\n".join(
        f"        <dict><key>Hour</key><integer>{h}</integer>"
        f"<key>Minute</key><integer>{m}</integer></dict>"
        for h, m in intervals
    )
    plist_xml = _LAUNCHD_PLIST_TEMPLATE.format(
        backend_dir=str(backend_dir),
        log_dir=str(log_dir),
        calendar_intervals=cal_xml,
    )
    plist_path.write_text(plist_xml, encoding="utf-8")

    # Unload first if already loaded (idempotent reinstall)
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True, text=True, check=False,
    )
    res = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise click.ClickException(
            f"launchctl load failed: {res.stderr.strip() or 'unknown error'}"
        )
    click.echo(f"installed: {plist_path}")
    click.echo(f"times:     {', '.join(t for t in parts)} (local)")
    click.echo(f"logs:      {log_dir}")
    click.echo(f"check:     af review-cron-status")


@cli.command(
    "review-cron-uninstall",
    help="Unload + delete the launchd plist installed by review-cron-install.",
)
def review_cron_uninstall_cmd() -> None:
    import subprocess
    from pathlib import Path

    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.agentflow.review.hotspots.plist"
    if not plist_path.exists():
        click.echo("(no plist installed)")
        return
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True, text=True, check=False,
    )
    plist_path.unlink()
    click.echo(f"uninstalled: {plist_path}")


@cli.command(
    "review-cron-status",
    help="Show whether the review-cron launchd job is loaded.",
)
def review_cron_status_cmd() -> None:
    import subprocess
    from pathlib import Path

    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.agentflow.review.hotspots.plist"
    click.echo(f"plist:    {plist_path} ({'present' if plist_path.exists() else 'missing'})")
    res = subprocess.run(
        ["launchctl", "list", "com.agentflow.review.hotspots"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode == 0:
        click.echo("status:   loaded")
        click.echo(res.stdout.strip())
    else:
        click.echo("status:   not loaded")


@cli.command(
    "review-dashboard",
    help="Run the agent bridge HTTP API over review/preflight state with "
    "read endpoints and an optional command endpoint. Localhost-bound by default.",
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=7860, show_default=True, type=int)
def review_dashboard_cmd(host: str, port: int) -> None:
    import uvicorn

    from agentflow.agent_review.web import create_app

    app = create_app()
    base = f"http://{host}:{port}"
    click.echo(f"review-dashboard listening on {base}")
    click.echo(f"  index:    {base}/")
    click.echo(f"  health:   {base}/api/health")
    click.echo(f"  articles: {base}/api/articles")
    click.echo(f"  one:      {base}/api/article/<article_id>")
    click.echo(f"  bridge:   {base}/api/bridge")
    click.echo(f"  schema:   {base}/api/bridge/schema")
    click.echo(f"  commands: {base}/api/commands")
    uvicorn.run(app, host=host, port=port, log_level="info")


@cli.command(
    "review-publish-mark",
    help="Mark an article as published (after manual paste into Medium). "
    "Writes publish_history.jsonl, updates metadata, advances state to "
    "published, and posts a confirmation card to TG.",
)
@click.argument("article_id")
@click.argument("published_url")
@click.option(
    "--platform",
    default="medium",
    show_default=True,
    help="Platform identifier for the publish_history record.",
)
@click.option("--notes", default=None, help="Optional free-form note for the audit trail.")
@click.option("--json", "as_json", is_flag=True, default=False)
def review_publish_mark_cmd(
    article_id: str,
    published_url: str,
    platform: str,
    notes: str | None,
    as_json: bool,
) -> None:
    from agentflow.agent_review import triggers

    try:
        result = triggers.mark_published(
            article_id,
            published_url=published_url,
            platform=platform,
            notes=notes,
        )
    except (ValueError, FileNotFoundError) as err:
        raise click.ClickException(str(err))

    if as_json:
        _emit_json(result)
        return
    click.echo(f"article_id:    {result['article_id']}")
    click.echo(f"platform:      {result['platform']}")
    click.echo(f"published_url: {result['published_url']}")
    if result.get("tg_message_id"):
        click.echo(f"tg confirm:    message_id={result['tg_message_id']}")
    if result.get("state_transition"):
        st = result["state_transition"]
        click.echo(
            f"state:         {st.get('from_state')} -> {st.get('to_state')}"
        )


# ---------------------------------------------------------------------------
# Post-publish telemetry: fetch engagement metrics, write back to metadata
# ---------------------------------------------------------------------------


_STATS_HISTORY_CAP = 10  # keep last N snapshots per platform


def _fmt_metric(s: dict[str, Any] | None, *keys: str) -> str:
    if not s:
        return "-"
    for k in keys:
        if s.get(k) is not None:
            return str(s[k])
    return "-"


@cli.command(
    "review-publish-stats",
    help="Fetch engagement metrics (claps/likes/comments/RTs) for an article's "
    "published platforms and write back to metadata.publish_stats.",
)
@click.argument("article_id")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option("--tg", is_flag=True, default=False, help="Post snapshot card to TG.")
@click.option("--all", "fetch_all", is_flag=True, default=False,
              help="Probe all platforms in history including failed ones.")
def review_publish_stats_cmd(
    article_id: str, as_json: bool, tg: bool, fetch_all: bool
) -> None:
    from datetime import datetime, timezone
    import json as _json
    from agentflow.agent_d4.storage import read_publish_history
    from agentflow.agent_review import state as _state, stats_fetchers
    from agentflow.shared.bootstrap import agentflow_home

    meta_path = agentflow_home() / "drafts" / article_id / "metadata.json"
    if not meta_path.exists():
        raise click.ClickException(f"no metadata.json for {article_id!r}")
    meta = _json.loads(meta_path.read_text(encoding="utf-8")) or {}
    cur = _state.current_state(article_id)
    if cur != _state.STATE_PUBLISHED and not as_json:
        click.echo(f"warning: article state is {cur!r}, not 'published' — fetching anyway.")

    # Newest-first; one entry per platform; only successes unless --all.
    seen: dict[str, dict[str, Any]] = {}
    for rec in read_publish_history(article_id):
        plat = (rec.get("platform") or "").lower()
        if not plat or plat in seen or not rec.get("published_url"):
            continue
        if not fetch_all and rec.get("status") != "success":
            continue
        seen[plat] = rec
    if not seen:
        if as_json:
            _emit_json({"article_id": article_id, "platforms": {},
                        "warning": "no successful publish records"})
            return
        click.echo("(no successful publish records to fetch stats for)")
        return

    publish_stats: dict[str, Any] = dict(meta.get("publish_stats") or {})
    fetched: dict[str, dict[str, Any] | None] = {}
    for plat, rec in seen.items():
        result = stats_fetchers.fetch_stats(
            plat, rec.get("published_url") or "", rec.get("platform_post_id"),
        )
        fetched[plat] = result
        if result is None:
            continue  # skip silently (no creds / unsupported); preserve prior
        prev = publish_stats.get(plat) or {}
        hist = list(prev.get("history") or [])
        if prev:
            hist.append({k: v for k, v in prev.items() if k != "history"})
            hist = hist[-_STATS_HISTORY_CAP:]
        entry = dict(result)
        if hist:
            entry["history"] = hist
        publish_stats[plat] = entry

    meta["publish_stats"] = publish_stats
    meta["publish_stats_updated_at"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(
        _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if as_json:
        _emit_json({"article_id": article_id, "platforms": fetched})
    else:
        click.echo(f"article_id: {article_id}")
        click.echo(f"{'platform':<10} {'claps/likes':>12} {'comments':>10} {'status':<14} fetched_at")
        click.echo("-" * 76)
        for plat, st in fetched.items():
            if st is None:
                click.echo(f"{plat:<10} {'-':>12} {'-':>10} {'skipped':<14} -")
                continue
            click.echo(
                f"{plat:<10} {_fmt_metric(st,'claps','likes','retweets'):>12} "
                f"{_fmt_metric(st,'responses','comments','replies'):>10} "
                f"{str(st.get('scrape_status') or '?'):<14} "
                f"{str(st.get('fetched_at') or '')[:19]}"
            )

    if tg:
        try:
            from agentflow.agent_review import daemon as _d, render, tg_client
            chat_id = _d.get_review_chat_id()
            if chat_id is not None:
                title = meta.get("title") or "(untitled)"
                lines = [f"📊 *Stats snapshot*  ·  `{render.escape_md2(article_id)}`",
                         f"*{render.escape_md2(title)}*", ""]
                for plat, st in fetched.items():
                    if st is None:
                        lines.append(f"  • {render.escape_md2(plat)}: _skipped_"); continue
                    lines.append(
                        f"  • {render.escape_md2(plat)}: "
                        f"{render.escape_md2(_fmt_metric(st,'claps','likes','retweets'))} / "
                        f"{render.escape_md2(_fmt_metric(st,'responses','comments','replies'))}  "
                        f"\\({render.escape_md2(str(st.get('scrape_status') or '?'))}\\)"
                    )
                tg_client.send_message(chat_id, "\n".join(lines))
        except Exception as err:
            click.echo(f"(tg post failed: {err})")
