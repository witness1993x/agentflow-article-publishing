"""Build Medium semi-automatic export/package/checklist artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentflow.agent_d2.main import load_draft
from agentflow.agent_d4.storage import read_publish_history
from agentflow.agent_medium.storage import medium_dir, save_json_artifact, save_text_artifact
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.models import D3Output, DraftOutput, PlatformVersion


def build_medium_export(article_id: str) -> dict[str, Any]:
    draft, draft_dir, metadata = _load_draft_context(article_id)
    medium_version, d3_path = _load_medium_version(article_id, draft_dir)
    draft_md_path = draft_dir / "draft.md"
    draft_markdown = (
        draft_md_path.read_text(encoding="utf-8") if draft_md_path.exists() else ""
    )
    history = read_publish_history(article_id)
    publish_context = _publish_context(history)
    images = _image_summary(draft)
    warnings = _warnings_for_export(medium_version, images, publish_context)

    payload: dict[str, Any] = {
        "article_id": article_id,
        "generated_at": _now_iso(),
        "paths": {
            "draft_dir": str(draft_dir),
            "metadata_json": str(draft_dir / "metadata.json"),
            "draft_markdown": str(draft_md_path),
            "d3_output_json": str(d3_path) if d3_path else None,
            "medium_markdown": str(draft_dir / "platform_versions" / "medium.md"),
        },
        "source": {
            "title": draft.title,
            "total_word_count": draft.total_word_count,
            "section_count": len(draft.sections),
            "status": metadata.get("status"),
            "published_platforms": list(metadata.get("published_platforms") or []),
            "opening": metadata.get("opening"),
            "closing": metadata.get("closing"),
            "draft_markdown": draft_markdown,
        },
        "medium_preview": {
            "available": medium_version is not None,
            "content": medium_version.content if medium_version else None,
            "metadata": dict(medium_version.metadata) if medium_version else {},
            "formatting_changes": (
                list(medium_version.formatting_changes) if medium_version else []
            ),
            "source": "d3_output.json" if medium_version else "draft.md",
        },
        "images": images,
        "publish_context": publish_context,
        "warnings": warnings,
    }

    save_json_artifact(article_id, "export.json", payload)
    save_text_artifact(article_id, "source_draft.md", draft_markdown)
    if medium_version:
        save_text_artifact(article_id, "medium_preview.md", medium_version.content)
    return payload


def build_medium_package(
    article_id: str,
    distribution_mode: str = "draft_only",
    canonical_url: str | None = None,
) -> dict[str, Any]:
    export = build_medium_export(article_id)
    preview = export["medium_preview"]
    source = export["source"]
    images = export["images"]
    publish_context = export["publish_context"]
    preview_meta = dict(preview.get("metadata") or {})

    body_markdown = preview.get("content") or source.get("draft_markdown") or ""
    selected_source = "medium_preview" if preview.get("available") else "draft_markdown"
    resolved_canonical = (
        canonical_url
        or preview_meta.get("canonical_url")
        or publish_context.get("ghost_url")
    )
    warnings = list(export["warnings"])
    if not preview.get("available"):
        warnings.append(
            "Missing Medium preview. Run `af preview --platforms medium` for Medium-specific formatting."
        )
    if distribution_mode == "cross_post" and not resolved_canonical:
        warnings.append(
            "Cross-post mode selected but no canonical_url was found from Ghost history or CLI override."
        )

    payload: dict[str, Any] = {
        "article_id": article_id,
        "generated_at": _now_iso(),
        "distribution_mode": distribution_mode,
        "source": selected_source,
        "title": preview_meta.get("title") or source.get("title"),
        "subtitle": preview_meta.get("subtitle") or _first_sentence(body_markdown),
        "tags": list(preview_meta.get("tags") or []),
        "canonical_url": resolved_canonical,
        "body_markdown": body_markdown,
        "cover_image_path": images.get("cover_image_path") or images.get("first_resolved_path"),
        "inline_images": list(images.get("resolved") or []),
        "warnings": warnings,
        "ops": {
            "suggested_next_steps": [
                "Open Medium import/create flow in browser.",
                "Paste or import `package.md` content.",
                "Review title, subtitle, tags, cover image, canonical URL.",
                "Save as draft first, then ask browser operator to continue.",
            ],
            "ghost_url": publish_context.get("ghost_url"),
            "existing_medium_url": publish_context.get("medium_url"),
        },
    }

    save_json_artifact(article_id, "package.json", payload)
    save_text_artifact(article_id, "package.md", body_markdown)
    return payload


def build_medium_manual_publish_package(
    article_id: str,
    distribution_mode: str = "draft_only",
    canonical_url: str | None = None,
) -> dict[str, Any]:
    """Build the same artifacts as ``af medium-package`` for D4 fallback."""
    package = build_medium_package(
        article_id,
        distribution_mode=distribution_mode,
        canonical_url=canonical_url,
    )
    state_transition = mark_medium_package_ready(
        article_id,
        notes="manual publish fallback generated Medium package artifacts",
    )
    artifact_dir = medium_dir(article_id)
    return {
        "manual_required": True,
        "reason": "Medium browser paste required; no MEDIUM_INTEGRATION_TOKEN is configured.",
        "artifact_dir": str(artifact_dir),
        "package_path": str(artifact_dir / "package.md"),
        "package_json_path": str(artifact_dir / "package.json"),
        "export_json_path": str(artifact_dir / "export.json"),
        "distribution_mode": package.get("distribution_mode"),
        "source": package.get("source"),
        "canonical_url": package.get("canonical_url"),
        "title": package.get("title"),
        "warning_count": len(package.get("warnings") or []),
        "state_transition": state_transition,
    }


def mark_medium_package_ready(
    article_id: str,
    *,
    notes: str = "Medium package artifacts generated; package.md ready for manual paste",
) -> dict[str, Any] | None:
    """Advance the review state once package.md exists for manual Medium paste."""
    from agentflow.agent_review import state as _state

    package_path = medium_dir(article_id) / "package.md"
    if not package_path.exists():
        return None

    current = _state.current_state(article_id)
    if current in {
        _state.STATE_READY_TO_PUBLISH,
        _state.STATE_PUBLISHED,
    }:
        return None

    return _state.transition(
        article_id,
        gate="D",
        to_state=_state.STATE_READY_TO_PUBLISH,
        actor="cli",
        decision="medium_package_ready",
        notes=notes,
        force=True,
    )


def build_medium_ops_checklist(article_id: str) -> dict[str, Any]:
    export = build_medium_export(article_id)
    package = build_medium_package(article_id)
    blockers: list[str] = []
    warnings = list(dict.fromkeys(package.get("warnings") or []))

    if not package.get("title"):
        blockers.append("Missing title.")
    if not package.get("body_markdown"):
        blockers.append("Missing body_markdown.")
    if export["images"]["unresolved"]:
        blockers.append("There are unresolved image placeholders.")

    checklist = {
        "article_id": article_id,
        "generated_at": _now_iso(),
        "ready_for_draft_import": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": [
            {
                "id": "content_source",
                "status": "ok" if package.get("body_markdown") else "blocked",
                "detail": (
                    "Using Medium preview content."
                    if export["medium_preview"]["available"]
                    else "Using draft markdown fallback."
                ),
            },
            {
                "id": "images",
                "status": "blocked" if export["images"]["unresolved"] else "ok",
                "detail": (
                    f"resolved={export['images']['resolved_count']}, "
                    f"unresolved={export['images']['unresolved_count']}"
                ),
            },
            {
                "id": "canonical_url",
                "status": "ok" if package.get("canonical_url") else "needs_review",
                "detail": package.get("canonical_url")
                or "No canonical URL yet; acceptable for draft-only flow.",
            },
            {
                "id": "distribution_mode",
                "status": "ok",
                "detail": package.get("distribution_mode"),
            },
        ],
        "manual_steps": [
            "Login to Medium in browser.",
            "Create/import a draft using `package.md`.",
            "Verify title, subtitle, tags, cover image, and canonical URL.",
            "Stop at draft review unless human explicitly confirms publish.",
        ],
    }

    save_json_artifact(article_id, "ops_checklist.json", checklist)
    save_text_artifact(article_id, "ops_checklist.md", _render_checklist_markdown(checklist))
    return checklist


def _load_draft_context(article_id: str) -> tuple[DraftOutput, Path, dict[str, Any]]:
    draft = load_draft(article_id)
    draft_dir = agentflow_home() / "drafts" / article_id
    metadata_path = draft_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    return draft, draft_dir, metadata


def _load_medium_version(
    article_id: str, draft_dir: Path
) -> tuple[PlatformVersion | None, Path | None]:
    d3_path = draft_dir / "d3_output.json"
    if not d3_path.exists():
        return None, None
    d3_output = D3Output.from_dict(json.loads(d3_path.read_text(encoding="utf-8")))
    for version in d3_output.platform_versions:
        if version.platform == "medium":
            return version, d3_path
    return None, d3_path


def _publish_context(history: list[dict[str, Any]]) -> dict[str, Any]:
    ghost_url = None
    medium_url = None
    for rec in history:
        if rec.get("status") != "success":
            continue
        if rec.get("platform") == "ghost_wordpress" and not ghost_url:
            ghost_url = rec.get("published_url")
        if rec.get("platform") == "medium" and not medium_url:
            medium_url = rec.get("published_url")
    return {
        "history_count": len(history),
        "ghost_url": ghost_url,
        "medium_url": medium_url,
        "history": history,
    }


def _image_summary(draft: DraftOutput) -> dict[str, Any]:
    resolved = []
    unresolved = []
    cover_path: str | None = None
    for placeholder in draft.image_placeholders:
        item = {
            "id": placeholder.id,
            "description": placeholder.description,
            "section_heading": placeholder.section_heading,
            "resolved_path": placeholder.resolved_path,
            "role": getattr(placeholder, "role", "body"),
        }
        if placeholder.resolved_path:
            resolved.append(item)
            if cover_path is None and item["role"] == "cover":
                cover_path = placeholder.resolved_path
        else:
            unresolved.append(item)
    # Backwards-compatible fallback: if no cover-role placeholder, the first
    # resolved image is treated as the cover (legacy behavior).
    first_resolved_path = resolved[0]["resolved_path"] if resolved else None
    if cover_path is None:
        cover_path = first_resolved_path
    return {
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "cover_image_path": cover_path,
        "first_resolved_path": first_resolved_path,
        "resolved": resolved,
        "unresolved": unresolved,
    }


def _warnings_for_export(
    medium_version: PlatformVersion | None,
    images: dict[str, Any],
    publish_context: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if medium_version is None:
        warnings.append(
            "No Medium preview was found in d3_output.json; export falls back to the draft source."
        )
    if images["unresolved_count"]:
        warnings.append(
            f"{images['unresolved_count']} image placeholder(s) still unresolved."
        )
    if publish_context.get("ghost_url") is None:
        warnings.append("No Ghost URL found yet; canonical URL may need manual input.")

    if medium_version is not None:
        meta = medium_version.metadata or {}
        # Subtitle sanity: starts with a stray '!' or markdown image embed
        # means the auto-extractor bit on a cover/inline image instead of
        # finding the first prose sentence.
        sub = (meta.get("subtitle") or "").strip()
        if sub.startswith("![") or sub == "!" or sub.startswith("!"):
            warnings.append(
                f"Subtitle looks suspicious ({sub[:20]!r}); set "
                "metadata_overrides.medium.subtitle to lock it."
            )
        # Tags sanity: short CJK fragments are usually noise from _infer_tags.
        noisy = [t for t in (meta.get("tags") or []) if len(t) <= 1]
        if noisy:
            warnings.append(
                f"Tags contain length-1 fragments ({noisy}); set "
                "metadata_overrides.medium.tags or publisher_account.default_tags."
            )
    return warnings


def _first_sentence(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!["):
            continue
        return line[:120]
    return text[:120]


def _render_checklist_markdown(checklist: dict[str, Any]) -> str:
    lines = [
        f"# Medium ops checklist: {checklist['article_id']}",
        "",
        f"- Ready for draft import: {'yes' if checklist['ready_for_draft_import'] else 'no'}",
        "",
        "## Checks",
    ]
    for item in checklist["checks"]:
        lines.append(f"- [{item['status']}] {item['id']}: {item['detail']}")
    if checklist["blockers"]:
        lines.extend(["", "## Blockers"])
        for blocker in checklist["blockers"]:
            lines.append(f"- {blocker}")
    if checklist["warnings"]:
        lines.extend(["", "## Warnings"])
        for warning in checklist["warnings"]:
            lines.append(f"- {warning}")
    lines.extend(["", "## Manual steps"])
    for step in checklist["manual_steps"]:
        lines.append(f"- {step}")
    lines.append("")
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
