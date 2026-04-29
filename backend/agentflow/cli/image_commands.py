"""`af propose-images` (and image-related helpers) — self-registering CLI module.

Imported lazily from ``agentflow.cli.commands`` at package import time. Adds
subcommands to the shared ``cli`` group.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Any

import click

from agentflow.cli.commands import cli


def _emit_json(obj: Any) -> None:
    click.echo(_json.dumps(obj, ensure_ascii=False, indent=2, default=str))


@cli.command(
    "image-generate",
    help="Generate draft images via AtlasCloud (GPT Image 2 relay) and auto-attach them.",
)
@click.argument("article_id")
@click.option(
    "--only",
    "only_placeholder_id",
    type=str,
    default=None,
    help="Generate just one placeholder id instead of all unresolved placeholders.",
)
@click.option(
    "--style",
    type=click.Choice(["editorial", "diagram", "cover"]),
    default="editorial",
    show_default=True,
    help="Prompt preset for the generated image.",
)
@click.option(
    "--size",
    type=click.Choice(
        ["1:1", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5", "16:9", "9:16", "2:1", "1:2", "21:9", "9:21"]
    ),
    default="16:9",
    show_default=True,
    help="AtlasCloud aspect ratio.",
)
@click.option(
    "--resolution",
    type=click.Choice(["1k", "2k", "4k"]),
    default="2k",
    show_default=True,
    help="AtlasCloud output resolution tier.",
)
@click.option(
    "--skip-body",
    is_flag=True,
    default=False,
    help="Skip body placeholders; only generate cover-role placeholders.",
)
@click.option(
    "--skip-cover",
    is_flag=True,
    default=False,
    help="Skip cover placeholders; only generate body-role placeholders.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def image_generate(
    article_id: str,
    only_placeholder_id: str | None,
    style: str,
    size: str,
    resolution: str,
    skip_body: bool,
    skip_cover: bool,
    as_json: bool,
) -> None:
    from agentflow.agent_d2.image_generator import generate_images
    from agentflow.shared.memory import append_memory_event

    try:
        summary = generate_images(
            article_id,
            only_placeholder_id=only_placeholder_id,
            size=size,
            resolution=resolution,
            style=style,
            skip_body=skip_body,
            skip_cover=skip_cover,
        )
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    except Exception as err:  # pragma: no cover - surface unexpected failures
        raise click.ClickException(f"image-generate failed: {err}")

    append_memory_event(
        "images_generated",
        article_id=article_id,
        payload={
            "style": style,
            "size": size,
            "resolution": resolution,
            "generated_count": summary.get("generated_count", 0),
            "remaining_unresolved_count": summary.get("remaining_unresolved_count", 0),
            "output_dir": summary.get("output_dir"),
            "only_placeholder_id": only_placeholder_id,
        },
    )

    if as_json:
        _emit_json(summary)
        return

    click.echo(f"article_id:            {article_id}")
    click.echo(f"output_dir:            {summary.get('output_dir')}")
    click.echo(f"style:                 {style}")
    click.echo(f"size:                  {size}")
    click.echo(f"resolution:            {resolution}")
    click.echo(f"generated:             {summary.get('generated_count', 0)}")
    click.echo(
        f"remaining_unresolved:  {summary.get('remaining_unresolved_count', 0)}"
    )
    if summary.get("generated"):
        click.echo("generated_files:")
        for item in summary["generated"][:10]:
            click.echo(
                f"  - {item['placeholder_id']} -> {item['saved_path']}"
            )


@cli.command(
    "image-auto-resolve",
    help="Auto-match unresolved [IMAGE: ...] placeholders to local library files.",
)
@click.argument("article_id")
@click.option(
    "--library",
    "library_path",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
    help="Local image library root. Defaults to AGENTFLOW_IMAGE_LIBRARY or ~/Pictures/agentflow.",
)
@click.option(
    "--min-score",
    type=float,
    default=0.55,
    show_default=True,
    help="Minimum filename/keyword match score required to auto-resolve a placeholder.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def image_auto_resolve(
    article_id: str,
    library_path: Path | None,
    min_score: float,
    as_json: bool,
) -> None:
    from agentflow.agent_d2.image_auto_resolver import (
        auto_resolve_images,
        default_image_library,
    )
    from agentflow.shared.memory import append_memory_event

    if min_score < 0 or min_score > 1:
        raise click.UsageError("--min-score must be between 0 and 1.")

    try:
        summary = auto_resolve_images(
            article_id,
            library=library_path or default_image_library(),
            min_score=min_score,
        )
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    except Exception as err:  # pragma: no cover - surface unexpected failures
        raise click.ClickException(f"image-auto-resolve failed: {err}")

    append_memory_event(
        "images_auto_resolved",
        article_id=article_id,
        payload={
            "library": summary.get("library"),
            "min_score": summary.get("min_score"),
            "scanned_files": summary.get("scanned_files"),
            "auto_resolved_count": summary.get("auto_resolved_count"),
            "already_resolved_count": summary.get("already_resolved_count"),
            "remaining_unresolved_count": summary.get("remaining_unresolved_count"),
        },
    )

    if as_json:
        _emit_json(summary)
        return

    click.echo(f"article_id:            {article_id}")
    click.echo(f"library:               {summary.get('library')}")
    click.echo(f"scanned_files:         {summary.get('scanned_files', 0)}")
    click.echo(f"already_resolved:      {summary.get('already_resolved_count', 0)}")
    click.echo(f"auto_resolved:         {summary.get('auto_resolved_count', 0)}")
    click.echo(
        f"remaining_unresolved:  {summary.get('remaining_unresolved_count', 0)}"
    )
    if summary.get("matches"):
        click.echo("matches:")
        for item in summary["matches"][:10]:
            click.echo(
                f"  - {item['placeholder_id']} -> {item['matched_path']} "
                f"(score={item['score']})"
            )
    if summary.get("remaining_unresolved_count"):
        click.echo("next:                  af image-resolve <article_id> <placeholder_id> <file_path>")


@cli.command(
    "propose-images",
    help="Run Agent D2.5 image proposer: read draft + refs, insert "
    "[IMAGE: ...] placeholders into draft.md at recommended anchors.",
)
@click.argument("article_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def propose_images(article_id: str, as_json: bool) -> None:
    from agentflow.agent_d2.image_proposer import propose_images as _propose
    from agentflow.shared.bootstrap import agentflow_home
    from agentflow.shared.memory import append_memory_event

    drafts_home = agentflow_home() / "drafts" / article_id
    if not drafts_home.exists():
        raise click.ClickException(
            f"propose-images: no draft directory for {article_id!r} at {drafts_home}. "
            "Run `af write` first."
        )

    try:
        summary = asyncio.run(_propose(article_id))
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    except Exception as err:  # pragma: no cover - surface unexpected failures
        raise click.ClickException(f"propose-images failed: {err}")

    hotspot_id = summary.get("hotspot_id") or ""

    append_memory_event(
        "images_proposed",
        article_id=article_id,
        hotspot_id=hotspot_id or None,
        payload={
            "count": summary.get("inserted_count", 0),
            "proposed_count": summary.get("proposed_count", 0),
            "skipped_count": summary.get("skipped_count", 0),
            "total_sections": summary.get("section_count", 0),
            "required_count": summary.get("required_count", 0),
            "recommended_count": summary.get("recommended_count", 0),
            "optional_count": summary.get("optional_count", 0),
        },
    )

    if as_json:
        _emit_json(summary)
        return

    click.echo(f"article_id:        {article_id}")
    click.echo(f"sections:          {summary.get('section_count', 0)}")
    click.echo(
        f"proposed:          {summary.get('proposed_count', 0)} "
        f"(required={summary.get('required_count', 0)}, "
        f"recommended={summary.get('recommended_count', 0)}, "
        f"optional={summary.get('optional_count', 0)})"
    )
    click.echo(
        f"inserted:          {summary.get('inserted_count', 0)} "
        f"placeholder(s) into draft.md"
    )
    if summary.get("skipped_count"):
        click.echo(f"skipped:           {summary['skipped_count']} (see image_proposals.json)")
    click.echo(f"proposals saved:   {summary.get('image_proposals_path')}")
    click.echo(
        "next:              af image-resolve <article_id> <placeholder_id> <file_path>"
    )


# ---------------------------------------------------------------------------
# af image-cover-add — seed a cover-role placeholder
# ---------------------------------------------------------------------------


_COVER_DEFAULT_DESCRIPTION = (
    "Abstract editorial cover image for a long-form article. "
    "Premium dark navy and deep teal background, glowing cyan and electric-blue "
    "luminous filaments, faint hexagonal grid, soft cinematic depth, subtle "
    "volumetric light, editorial hero quality. No text, no readable letters or "
    "numbers, no logos, no UI screenshots, no diagrams or boxes-and-arrows, no "
    "flowchart, no candlestick charts, no coins, no rockets, no cartoon robots."
)


@cli.command(
    "image-cover-add",
    help="Seed a cover-role placeholder on a draft (idempotent).",
)
@click.argument("article_id")
@click.option(
    "--description",
    "description",
    type=str,
    default=None,
    help="Override the cover image description fed to the generator.",
)
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help="If a cover placeholder already exists, clear its resolved_path so the next "
    "image-generate run will regenerate it.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def image_cover_add(
    article_id: str,
    description: str | None,
    reset: bool,
    as_json: bool,
) -> None:
    from agentflow.agent_d2.main import load_draft, save_draft
    from agentflow.shared.models import ImagePlaceholder

    try:
        draft = load_draft(article_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    cover_id = f"{draft.article_id}_cover"
    desc = description or _COVER_DEFAULT_DESCRIPTION

    existing = next(
        (p for p in draft.image_placeholders if p.role == "cover"), None
    )
    if existing is None:
        ph = ImagePlaceholder(
            id=cover_id, description=desc, section_heading="", role="cover"
        )
        # Cover always sits at index 0 so legacy first_resolved_path callers
        # also see it.
        draft.image_placeholders.insert(0, ph)
        action = "added"
    else:
        if description is not None:
            existing.description = desc
        if reset:
            existing.resolved_path = None
        action = "reset" if reset else "unchanged"

    save_draft(draft)

    payload = {
        "article_id": article_id,
        "cover_placeholder_id": (existing or ph).id,
        "action": action,
    }
    if as_json:
        _emit_json(payload)
        return
    click.echo(f"article_id:    {article_id}")
    click.echo(f"cover id:      {payload['cover_placeholder_id']}")
    click.echo(f"action:        {action}")
    click.echo("next:          af image-generate <article_id> --style cover --skip-body")


# ---------------------------------------------------------------------------
# af image-gate — explicit, optional image-generation gate
# ---------------------------------------------------------------------------


@cli.command(
    "image-gate",
    help="Explicit image-generation gate. Pick one mode and the gate orchestrates "
    "the underlying image steps. Use this between `af fill` and `af preview`.",
)
@click.argument("article_id")
@click.option(
    "--mode",
    type=click.Choice(["none", "cover-only", "cover-plus-body"]),
    default=None,
    help="Image generation mode. Defaults to "
    "preferences.image_generation.default_mode, then 'cover-only'.",
)
@click.option(
    "--cover-style",
    type=click.Choice(["editorial", "diagram", "cover"]),
    default="cover",
    show_default=True,
    help="Style preset for the cover image.",
)
@click.option(
    "--cover-size",
    type=click.Choice(
        ["1:1", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5", "16:9", "9:16", "2:1", "1:2", "21:9", "9:21"]
    ),
    default="16:9",
    show_default=True,
)
@click.option(
    "--cover-resolution",
    type=click.Choice(["1k", "2k", "4k"]),
    default="2k",
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True, default=False)
def image_gate(
    article_id: str,
    mode: str | None,
    cover_style: str,
    cover_size: str,
    cover_resolution: str,
    as_json: bool,
) -> None:
    from agentflow.agent_d2.image_generator import generate_images
    from agentflow.agent_d2.main import load_draft, save_draft
    from agentflow.shared import preferences as _prefs
    from agentflow.shared.memory import append_memory_event
    from agentflow.shared.models import ImagePlaceholder

    if mode is None:
        prefs = _prefs.load() or {}
        mode = (
            (prefs.get("image_generation") or {}).get("default_mode")
            or "cover-only"
        )

    try:
        draft = load_draft(article_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    summary: dict[str, Any] = {
        "article_id": article_id,
        "mode": mode,
        "actions": [],
    }

    if mode != "none":
        # Preflight: make sure ATLASCLOUD_API_KEY is set before we waste time.
        try:
            from agentflow.agent_review import preflight as _pf
            _pf.assert_ready_for_image_gate()
        except Exception as _err:
            raise click.ClickException(
                f"image-gate preflight failed: {_err}\n"
                "Run `af doctor` for details, or use --mode none to skip image generation."
            )

    if mode == "none":
        summary["actions"].append(
            "skipped image generation; preview will need --skip-images to strip markers"
        )
        # Move state into image_skipped (force, can come from any mid-state) and trigger Gate D.
        try:
            from agentflow.agent_review import state as _state
            if _state.current_state(article_id) != _state.STATE_IMAGE_SKIPPED:
                _state.transition(
                    article_id,
                    gate="C",
                    to_state=_state.STATE_IMAGE_SKIPPED,
                    actor="cli",
                    decision="image_mode_none",
                    notes="af image-gate --mode none",
                    force=True,
                )
        except Exception as _err:  # pragma: no cover
            summary["actions"].append(f"state transition to image_skipped failed: {_err}")
        try:
            from agentflow.agent_review import triggers as _triggers
            tg_summary = _triggers.post_gate_d(article_id)
            if tg_summary:
                summary["gate_d_short_id"] = tg_summary["short_id"]
                summary["actions"].append(
                    f"Gate D card posted (short_id={tg_summary['short_id']})"
                )
        except Exception as _err:  # pragma: no cover
            summary["actions"].append(f"Gate D auto-post skipped: {_err}")
    else:
        # Ensure cover placeholder exists at index 0
        if not any(p.role == "cover" for p in draft.image_placeholders):
            draft.image_placeholders.insert(
                0,
                ImagePlaceholder(
                    id=f"{draft.article_id}_cover",
                    description=_COVER_DEFAULT_DESCRIPTION,
                    section_heading="",
                    role="cover",
                ),
            )
            save_draft(draft)
            summary["actions"].append("added cover placeholder")

        skip_body = mode == "cover-only"
        try:
            gen_summary = generate_images(
                article_id,
                size=cover_size,
                resolution=cover_resolution,
                style=cover_style,
                skip_body=skip_body,
            )
        except Exception as err:  # pragma: no cover
            raise click.ClickException(f"image-gate failed: {err}")

        # Auto-trigger Gate C card if TG is configured. Failures must NOT
        # break the image-gate command — the cover is on disk regardless.
        try:
            from agentflow.agent_review import triggers as _triggers
            tg_summary = _triggers.post_gate_c(article_id)
            if tg_summary:
                summary["gate_c_short_id"] = tg_summary["short_id"]
                summary["actions"].append(
                    f"Gate C card posted (short_id={tg_summary['short_id']})"
                )
        except Exception as _err:  # pragma: no cover
            summary["actions"].append(f"Gate C auto-post skipped: {_err}")
        summary["generated"] = gen_summary.get("generated", [])
        summary["skipped"] = gen_summary.get("skipped", [])
        summary["actions"].append(
            f"generated {gen_summary.get('generated_count', 0)} image(s) "
            f"(skip_body={skip_body})"
        )

    append_memory_event(
        "image_gate",
        article_id=article_id,
        payload={"mode": mode},
    )

    if as_json:
        _emit_json(summary)
        return
    click.echo(f"article_id: {article_id}")
    click.echo(f"mode:       {mode}")
    for action in summary["actions"]:
        click.echo(f"  - {action}")
    click.echo("next:       af preview --platforms medium  (add --skip-images for none-mode)")
