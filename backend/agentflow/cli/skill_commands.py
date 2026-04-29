"""`af skill-install` — install AgentFlow skills into the Claude Code (or Cursor)
skill directory via symlink or copy.

Saves the user from running 7 manual `ln -s` commands. Mirrors the README install
recipe but is idempotent and reportable.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import click

from agentflow.cli.commands import _emit_json, cli


# ---------------------------------------------------------------------------
# Repo root resolution
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Locate the repository root containing `backend/` and `.claude/`.

    Mirrors the pattern in onboard_commands._env_path: walk up to find the
    `backend` parent, then go one level higher.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "backend":
            return parent.parent
    # Fallback: cli -> agentflow -> backend -> repo root
    return Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------


def _discover_skills(source_root: Path) -> list[Path]:
    """Return subdirectories of source_root that contain a SKILL.md."""
    if not source_root.exists() or not source_root.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(source_root.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "SKILL.md").is_file():
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Install one skill
# ---------------------------------------------------------------------------


def _install_one(
    *,
    src: Path,
    dst: Path,
    mode: str,
    force: bool,
) -> dict[str, Any]:
    """Install a single skill. Returns a result dict.

    status ∈ {"installed", "skipped", "failed"}.
    """
    result: dict[str, Any] = {
        "name": src.name,
        "src": str(src),
        "dst": str(dst),
        "mode": mode,
        "status": "skipped",
        "reason": None,
    }

    if not src.exists():
        result["status"] = "skipped"
        result["reason"] = "source does not exist"
        return result

    # Existing target?
    if dst.exists() or dst.is_symlink():
        is_link = dst.is_symlink()
        if not force:
            result["status"] = "skipped"
            result["reason"] = (
                "target exists (symlink)" if is_link else "target exists (not a symlink, refusing to overwrite)"
            )
            return result
        # --force path: remove old link/dir
        try:
            if is_link or dst.is_file():
                dst.unlink()
            else:
                shutil.rmtree(dst)
        except OSError as err:
            result["status"] = "failed"
            result["reason"] = f"could not remove existing target: {err}"
            return result

    # Make sure parent exists
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        if mode == "symlink":
            dst.symlink_to(src, target_is_directory=True)
        elif mode == "copy":
            shutil.copytree(src, dst)
        else:  # pragma: no cover
            result["status"] = "failed"
            result["reason"] = f"unknown mode: {mode}"
            return result
    except OSError as err:
        result["status"] = "failed"
        result["reason"] = str(err)
        return result

    result["status"] = "installed"
    return result


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@cli.command("skill-install")
@click.option(
    "--target",
    "target",
    default=None,
    help="Target skills directory. Defaults to ~/.claude/skills/ "
    "(or ~/.cursor/skills/ when --cursor is set).",
)
@click.option(
    "--mode",
    "mode",
    type=click.Choice(["symlink", "copy"]),
    default="symlink",
    show_default=True,
    help="symlink (default) creates a link; copy duplicates the directory.",
)
@click.option(
    "--cursor",
    "cursor",
    is_flag=True,
    default=False,
    help="Install Cursor skills (only agentflow-open-claw) instead of Claude Code skills.",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Remove any existing target before installing.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit results as JSON.",
)
def skill_install(
    target: str | None,
    mode: str,
    cursor: bool,
    force: bool,
    as_json: bool,
) -> None:
    """Install AgentFlow Claude Code (or Cursor) skills into your user skills dir.

    Default behaviour discovers every skill folder under the repo's
    `.claude/skills/` and installs it as a symlink under `~/.claude/skills/`.

    With `--cursor`, installs the lone `agentflow-open-claw` skill from
    `.cursor/skills/` into `~/.cursor/skills/`.
    """
    repo = _repo_root()

    if cursor:
        source_root = repo / ".cursor" / "skills"
        target_root = Path(target).expanduser() if target else Path.home() / ".cursor" / "skills"
        # Cursor: only install agentflow-open-claw
        candidate = source_root / "agentflow-open-claw"
        sources = [candidate] if candidate.is_dir() and (candidate / "SKILL.md").is_file() else []
    else:
        source_root = repo / ".claude" / "skills"
        target_root = Path(target).expanduser() if target else Path.home() / ".claude" / "skills"
        sources = _discover_skills(source_root)

    target_root.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for src in sources:
        dst = target_root / src.name
        results.append(_install_one(src=src, dst=dst, mode=mode, force=force))

    installed = sum(1 for r in results if r["status"] == "installed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = sum(1 for r in results if r["status"] == "failed")

    summary = {
        "source_root": str(source_root),
        "target_root": str(target_root),
        "mode": mode,
        "cursor": cursor,
        "force": force,
        "installed": installed,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }

    if as_json:
        _emit_json(summary)
        return

    if not sources:
        click.echo(f"No skills found under {source_root}")
        return

    click.echo(f"source: {source_root}")
    click.echo(f"target: {target_root}")
    click.echo(f"mode:   {mode}{' (force)' if force else ''}")
    click.echo("")
    for r in results:
        marker = {
            "installed": "OK ",
            "skipped": "-- ",
            "failed": "!! ",
        }.get(r["status"], "?? ")
        line = f"{marker}{r['name']:<28} -> {r['dst']}"
        if r["reason"]:
            line += f"  ({r['reason']})"
        click.echo(line)
    click.echo("")
    click.echo(
        f"summary: installed={installed}  skipped={skipped}  failed={failed}"
    )

    if as_json is False and not click.get_current_context().obj:
        # Mirror style of other CLI commands — also dump JSON to stderr for piping
        # (Disabled: stick to plain text unless --json was passed.)
        pass

    # Non-zero exit when anything failed, so scripts can branch on it
    if failed:
        raise click.ClickException(f"{failed} skill install(s) failed")


__all__ = ["skill_install"]
