"""`af keys-*` — operator-side secret-folder inspection and editing.

v1.0.4 introduced a key-folder convention: secrets live at
``~/.agentflow/secrets/.env`` (catch-all) and/or
``~/.agentflow/secrets/<service>.env`` (per-service). The CLI loader auto-
discovers them with explicit precedence; ``backend/.env`` remains a back-compat
fallback.

These commands let an operator inspect what's loaded and edit per-service
files without leaving the terminal:

* ``af keys-where`` — print precedence + which file resolved each var
* ``af keys-show``  — print loaded var names with masked values
* ``af keys-edit <service>`` — open ``$EDITOR`` on the per-service file
                                (creates an empty 0600 file if missing)

These are read-mostly; the actual write happens in the operator's editor.
``af onboard`` is still the canonical wizard for guided initial setup.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click

from agentflow.cli.commands import (
    _SECRET_SERVICES,
    _candidate_secret_files,
    _emit_json,
    _resolved_sources,
    _secrets_dir,
    cli,
)


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:4]}{'*' * max(1, len(value) - 6)}{value[-2:]}"


@cli.command("keys-where", help="Show secret-file precedence and per-var sources.")
@click.option("--json", "as_json", is_flag=True, default=False)
def keys_where_cmd(as_json: bool) -> None:
    candidates = _candidate_secret_files()
    by_var = dict(_resolved_sources)
    secrets_dir = _secrets_dir()

    if as_json:
        _emit_json(
            {
                "secrets_dir": str(secrets_dir),
                "candidates": [str(p) for p in candidates],
                "resolved_sources": by_var,
                "known_services": list(_SECRET_SERVICES),
            }
        )
        return

    click.echo("Secret search precedence (highest to lowest):")
    if not candidates:
        click.echo(
            "  (none — no secret files found; run `af onboard` "
            "or create ~/.agentflow/secrets/.env manually)"
        )
    for i, path in enumerate(candidates, 1):
        click.echo(f"  {i}. {path}")

    click.echo()
    click.echo(f"Secrets dir: {secrets_dir}")
    click.echo(f"Known per-service slots: {', '.join(_SECRET_SERVICES)}")
    click.echo()
    if by_var:
        click.echo(f"Resolved {len(by_var)} env vars from those files:")
        for var in sorted(by_var):
            p = Path(by_var[var])
            click.echo(f"  {var:<32} <- {p.parent.name}/{p.name}")
    else:
        click.echo("No env vars resolved from secret files (process env only).")


@cli.command("keys-show", help="List loaded env-var names with masked values.")
@click.option(
    "--service",
    "service",
    type=str,
    default=None,
    help="Restrict to vars resolved from a specific per-service file.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def keys_show_cmd(service: str | None, as_json: bool) -> None:
    by_var = dict(_resolved_sources)
    if service:
        suffix = f"{service}.env"
        by_var = {k: v for k, v in by_var.items() if v.endswith(suffix)}

    rows: list[dict[str, Any]] = []
    for var in sorted(by_var):
        raw = os.environ.get(var, "")
        rows.append(
            {
                "var": var,
                "value_masked": _mask(raw),
                "is_set": bool(raw),
                "source": by_var[var],
            }
        )

    if as_json:
        _emit_json(rows)
        return

    if not rows:
        click.echo(
            "(no vars match — try `af keys-where` to see what was loaded)"
        )
        return

    width = max(len(r["var"]) for r in rows)
    for r in rows:
        src = Path(r["source"])
        click.echo(
            f"  {r['var']:<{width}}  {r['value_masked']:<22}  "
            f"[{src.parent.name}/{src.name}]"
        )


@cli.command(
    "keys-edit",
    help="Open $EDITOR on a per-service secret file (creates if missing).",
)
@click.argument("service", type=str, required=False, default=None)
def keys_edit_cmd(service: str | None) -> None:
    secrets_dir = _secrets_dir()
    secrets_dir.mkdir(parents=True, exist_ok=True)
    try:
        secrets_dir.chmod(0o700)
    except OSError:
        pass

    if service is None:
        target = secrets_dir / ".env"
    else:
        normalized = service.strip().lower()
        if normalized not in _SECRET_SERVICES:
            raise click.ClickException(
                f"Unknown service '{service}'. "
                f"Known services: {', '.join(_SECRET_SERVICES)}. "
                f"To use a custom service file, edit "
                f"{secrets_dir}/{normalized}.env directly."
            )
        target = secrets_dir / f"{normalized}.env"

    is_new = not target.exists()
    if is_new:
        target.touch()
        try:
            target.chmod(0o600)
        except OSError:
            pass

    editor = (
        os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
        or shutil.which("nano")
        or shutil.which("vi")
    )
    if not editor:
        raise click.ClickException(
            "No editor found. Set $EDITOR (e.g. `export EDITOR=vim`) "
            f"or edit {target} directly."
        )

    click.echo(f"Opening {target} in {editor}...")
    if is_new:
        click.echo("(file is new — write KEY=value lines, save and quit)")
    rc = subprocess.run([editor, str(target)]).returncode
    if rc != 0:
        raise click.ClickException(f"editor exited with status {rc}")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    click.echo(f"Saved {target} (mode 0600).")
