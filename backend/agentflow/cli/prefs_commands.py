"""`af prefs-*` commands.

Reads / rebuilds / inspects / resets ``~/.agentflow/preferences.yaml``.
All writes to disk also append a ``preferences_rebuilt`` or
``preferences_reset`` event to ``memory/events.jsonl`` so users can audit
when preferences changed.

``--json`` routes machine-readable output to stdout and pushes human
logs to stderr. Non-JSON mode prints a friendly digest.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any

import click

from agentflow.cli.commands import cli
from agentflow.shared import preferences as prefs_mod
from agentflow.shared.memory import append_memory_event


def _emit_json(obj: Any) -> None:
    click.echo(_json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _echo_err(msg: str) -> None:
    click.echo(msg, err=True)


# ---------------------------------------------------------------------------
# af prefs-rebuild
# ---------------------------------------------------------------------------


@cli.command(
    "prefs-rebuild",
    help="Aggregate memory/events.jsonl -> ~/.agentflow/preferences.yaml.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Compute but don't write to disk.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full merged preferences dict as JSON on stdout.",
)
def prefs_rebuild(dry_run: bool, as_json: bool) -> None:
    fresh = prefs_mod.rebuild_from_events()
    existing = prefs_mod.load()
    merged = prefs_mod.merge_with_existing(fresh, existing)

    path = prefs_mod.DEFAULT_PREFS_PATH
    if not dry_run:
        prefs_mod.save(merged, path=path)
        append_memory_event(
            "preferences_rebuilt",
            payload={
                "path": str(path),
                "source_events": fresh.get("source_events"),
                "sections_emitted": sorted(
                    k for k in ("intent", "write", "preview", "publish") if k in fresh
                ),
            },
        )

    summary = prefs_mod.summarize(merged)
    if as_json:
        out = {
            "ok": True,
            "dry_run": dry_run,
            "path": str(path),
            "preferences": merged,
            "summary": summary,
        }
        _emit_json(out)
        return

    if dry_run:
        _echo_err(f"(dry-run) preferences would be written to {path}")
    else:
        _echo_err(f"preferences rebuilt -> {path}")
    click.echo(f"source_events: {summary['source_events']}")
    sections = summary.get("sections") or {}
    if not sections:
        click.echo(
            "  (no sections emitted — need TopicIntent usage, N>=3 fill_choices, "
            "or N>=3 successful publishes to cross the threshold)"
        )
        return
    for name, body in sections.items():
        click.echo(f"  [{name}]")
        for k, v in body.items():
            click.echo(f"    {k}: {v}")


# ---------------------------------------------------------------------------
# af prefs-show
# ---------------------------------------------------------------------------


@cli.command("prefs-show", help="Print current preferences.yaml (or one key).")
@click.option(
    "--key",
    "key",
    type=str,
    default=None,
    help="Dotted path like write.default_title_index to print a single value.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="JSON output on stdout.",
)
def prefs_show(key: str | None, as_json: bool) -> None:
    prefs = prefs_mod.load()

    if not prefs:
        if as_json:
            _emit_json({"preferences": {}, "path": str(prefs_mod.DEFAULT_PREFS_PATH)})
            return
        click.echo(
            "(no preferences.yaml yet — run `af prefs-rebuild` after "
            "you have at least 3 fill_choices events)"
        )
        return

    if key:
        value = prefs_mod.pick_by_key(key)
        if as_json:
            _emit_json({"key": key, "value": value})
            return
        if value is None:
            click.echo(f"{key}: (not set)")
            return
        if isinstance(value, (dict, list)):
            click.echo(f"{key}:")
            click.echo(
                _json.dumps(value, ensure_ascii=False, indent=2, default=str)
            )
        else:
            click.echo(f"{key}: {value}")
        return

    if as_json:
        _emit_json({"preferences": prefs, "path": str(prefs_mod.DEFAULT_PREFS_PATH)})
        return

    click.echo(f"# preferences.yaml  ({prefs_mod.DEFAULT_PREFS_PATH})")
    click.echo(f"schema_version: {prefs.get('schema_version')}")
    click.echo(f"last_computed : {prefs.get('last_computed')}")
    click.echo(f"source_events : {prefs.get('source_events')}")
    for section in ("intent", "write", "preview", "publish"):
        if section not in prefs:
            continue
        body = prefs[section]
        click.echo(f"\n[{section}]")
        for k, v in body.items():
            if k.startswith("_evidence") or k == "_negative_signals":
                if isinstance(v, list):
                    click.echo(f"  {k}: <{len(v)} event(s) — use `af prefs-explain`>")
                continue
            if isinstance(v, (dict, list)):
                click.echo(f"  {k}: {_json.dumps(v, ensure_ascii=False)}")
            else:
                click.echo(f"  {k}: {v}")


# ---------------------------------------------------------------------------
# af prefs-explain
# ---------------------------------------------------------------------------


@cli.command(
    "prefs-explain",
    help="Show up to 10 evidence events backing a preference key.",
)
@click.argument("key")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="JSON output on stdout.",
)
def prefs_explain(key: str, as_json: bool) -> None:
    evidence = prefs_mod.explain(key)
    if as_json:
        _emit_json({"key": key, "evidence": evidence, "count": len(evidence)})
        return

    if not evidence:
        click.echo(
            f"(no evidence for {key!r}. Either the key is unknown, the "
            "aggregator didn't emit that section, or preferences.yaml is "
            "missing. Run `af prefs-rebuild` first.)"
        )
        return

    click.echo(f"# evidence for {key}  ({len(evidence)} event(s))")
    for i, ev in enumerate(evidence, 1):
        ts = ev.get("event_ts") or ev.get("ts") or "-"
        etype = ev.get("event_type") or "-"
        aid = ev.get("article_id") or "-"
        payload = ev.get("payload") or {}
        click.echo(
            f"  [{i}] {ts}  {etype:<20} article={aid}  "
            f"payload={_json.dumps(payload, ensure_ascii=False)}"
        )


# ---------------------------------------------------------------------------
# af prefs-reset
# ---------------------------------------------------------------------------


@cli.command(
    "prefs-reset",
    help="Remove a preferences key, or delete the whole preferences.yaml.",
)
@click.option(
    "--key",
    "key",
    type=str,
    default=None,
    help="Dotted key to clear. Omit to delete the whole file.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="JSON output on stdout.",
)
def prefs_reset(key: str | None, as_json: bool) -> None:
    if key:
        changed = prefs_mod.clear_key(key)
        result = {"ok": changed, "scope": "key", "key": key}
    else:
        changed = prefs_mod.clear_all()
        result = {"ok": changed, "scope": "all"}

    if changed:
        append_memory_event(
            "preferences_reset",
            payload={"scope": result["scope"], "key": key},
        )

    if as_json:
        _emit_json(result)
        return

    if changed:
        if key:
            click.echo(f"preferences: cleared key {key!r}")
        else:
            click.echo(
                f"preferences: deleted {prefs_mod.DEFAULT_PREFS_PATH} "
                "(next `af prefs-rebuild` will recreate it)"
            )
    else:
        if key:
            click.echo(f"preferences: {key!r} was not set; nothing to clear.")
        else:
            click.echo("preferences: file did not exist; nothing to delete.")
