"""Medium semi-automatic CLI commands."""

from __future__ import annotations

import json as _json
from typing import Any

import click

from agentflow.cli.commands import cli


def _emit_json(obj: Any) -> None:
    click.echo(_json.dumps(obj, ensure_ascii=False, indent=2, default=str))


@cli.command(
    "medium-export",
    help="Export Medium-ready source context into ~/.agentflow/medium/<article_id>/.",
)
@click.argument("article_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def medium_export(article_id: str, as_json: bool) -> None:
    from agentflow.agent_medium.storage import medium_dir
    from agentflow.agent_medium.workflow import build_medium_export
    from agentflow.shared.memory import append_memory_event

    try:
        payload = build_medium_export(article_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    except Exception as err:  # pragma: no cover
        raise click.ClickException(f"medium-export failed: {err}")

    append_memory_event(
        "medium_exported",
        article_id=article_id,
        payload={
            "artifact_dir": str(medium_dir(article_id)),
            "has_medium_preview": payload["medium_preview"]["available"],
            "resolved_images": payload["images"]["resolved_count"],
            "unresolved_images": payload["images"]["unresolved_count"],
        },
    )

    if as_json:
        _emit_json(payload)
        return

    click.echo(f"article_id:         {article_id}")
    click.echo(f"artifact_dir:       {medium_dir(article_id)}")
    click.echo(
        f"medium_preview:     {'yes' if payload['medium_preview']['available'] else 'no'}"
    )
    click.echo(f"resolved_images:    {payload['images']['resolved_count']}")
    click.echo(f"unresolved_images:  {payload['images']['unresolved_count']}")
    click.echo("saved:              export.json, source_draft.md")
    if payload["medium_preview"]["available"]:
        click.echo("saved:              medium_preview.md")


@cli.command(
    "medium-package",
    help="Build a browser-ops package for Medium draft import/review.",
)
@click.argument("article_id")
@click.option(
    "--distribution-mode",
    type=click.Choice(["draft_only", "cross_post"]),
    default="draft_only",
    show_default=True,
    help="Whether this Medium draft stands alone or cross-posts an existing canonical source.",
)
@click.option(
    "--canonical-url",
    type=str,
    default=None,
    help="Explicit canonical URL override. Defaults to package metadata or Ghost publish history.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def medium_package(
    article_id: str,
    distribution_mode: str,
    canonical_url: str | None,
    as_json: bool,
) -> None:
    from agentflow.agent_medium.storage import medium_dir
    from agentflow.agent_medium.workflow import (
        build_medium_package,
        mark_medium_package_ready,
    )
    from agentflow.shared.memory import append_memory_event

    try:
        payload = build_medium_package(
            article_id,
            distribution_mode=distribution_mode,
            canonical_url=canonical_url,
        )
        state_transition = mark_medium_package_ready(article_id)
        if state_transition:
            payload["state_transition"] = state_transition
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    except Exception as err:  # pragma: no cover
        raise click.ClickException(f"medium-package failed: {err}")

    append_memory_event(
        "medium_packaged",
        article_id=article_id,
        payload={
            "artifact_dir": str(medium_dir(article_id)),
            "distribution_mode": distribution_mode,
            "source": payload.get("source"),
            "canonical_url": payload.get("canonical_url"),
            "warning_count": len(payload.get("warnings") or []),
        },
    )

    if as_json:
        _emit_json(payload)
        return

    click.echo(f"article_id:         {article_id}")
    click.echo(f"artifact_dir:       {medium_dir(article_id)}")
    click.echo(f"distribution_mode:  {payload['distribution_mode']}")
    click.echo(f"source:             {payload['source']}")
    click.echo(f"canonical_url:      {payload.get('canonical_url') or '-'}")
    click.echo(f"cover_image_path:   {payload.get('cover_image_path') or '-'}")
    click.echo(f"warnings:           {len(payload.get('warnings') or [])}")
    click.echo("saved:              package.json, package.md")


@cli.command(
    "medium-ops-checklist",
    help="Generate the human/browser-operator checklist for the Medium draft flow.",
)
@click.argument("article_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def medium_ops_checklist(article_id: str, as_json: bool) -> None:
    from agentflow.agent_medium.storage import medium_dir
    from agentflow.agent_medium.workflow import build_medium_ops_checklist

    try:
        payload = build_medium_ops_checklist(article_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    except Exception as err:  # pragma: no cover
        raise click.ClickException(f"medium-ops-checklist failed: {err}")

    if as_json:
        _emit_json(payload)
        return

    click.echo(f"article_id:           {article_id}")
    click.echo(f"artifact_dir:         {medium_dir(article_id)}")
    click.echo(
        "ready_for_draft:      "
        f"{'yes' if payload['ready_for_draft_import'] else 'no'}"
    )
    click.echo(f"blockers:             {len(payload.get('blockers') or [])}")
    click.echo(f"warnings:             {len(payload.get('warnings') or [])}")
    click.echo("saved:                ops_checklist.json, ops_checklist.md")
