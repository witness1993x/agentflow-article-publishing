"""D2 orchestration — the four entry points the API / CLI will call.

- ``generate_skeleton_for_hotspot`` — load hotspot + style_profile + content_matrix,
  call ``skeleton_generator.generate_skeleton``, return SkeletonOutput.
- ``fill_all_sections`` — iterate over chosen skeleton sections sequentially
  (each sees the previously filled sections as context), extract image
  placeholders, persist ``~/.agentflow/drafts/<id>/{draft.md,metadata.json}``.
- ``apply_user_edit`` — load draft, run ``interactive_editor.apply_edit``,
  re-save.
- ``load_draft`` / ``save_draft`` — disk I/O helpers for the draft payload.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agentflow.agent_d2.interactive_editor import apply_edit as _apply_edit
from agentflow.agent_d2.section_filler import fill_section
from agentflow.agent_d2.skeleton_generator import generate_skeleton
from agentflow.config.style_loader import load_style_profile
from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.hotspot_store import find_hotspot_record
from agentflow.shared.logger import get_logger
from agentflow.shared.markdown_utils import extract_image_placeholders
from agentflow.shared.memory import (
    append_memory_event,
    intent_query_text,
    load_current_intent,
)
from agentflow.shared.models import (
    DraftOutput,
    FilledSection,
    Hotspot,
    ImagePlaceholder,
    Section,
    SkeletonOutput,
)

_log = get_logger("agent_d2.main")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _drafts_dir() -> Path:
    return agentflow_home() / "drafts"


def _draft_dir_for(article_id: str) -> Path:
    return _drafts_dir() / article_id


def _load_content_matrix() -> dict[str, Any]:
    """Prefer ~/.agentflow/content_matrix.yaml, fall back to style_profile.content_matrix."""
    user_path = agentflow_home() / "content_matrix.yaml"
    if user_path.exists():
        with user_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if isinstance(data, dict):
            return data
    # Fall back to whatever the style profile already embeds.
    profile = load_style_profile()
    embedded = profile.get("content_matrix")
    if isinstance(embedded, dict):
        return embedded
    return {}


def _load_hotspot(hotspot_id: str) -> Hotspot:
    """Load a hotspot from scans or archived search results."""
    try:
        hotspot, _ = find_hotspot_record(
            hotspot_id,
            limit_days=None,
            include_search_results=True,
        )
    except KeyError as err:
        raise KeyError(
            f"Hotspot {hotspot_id!r} not found in ~/.agentflow/hotspots or ~/.agentflow/search_results"
        ) from err
    return Hotspot.from_dict(hotspot)


# ---------------------------------------------------------------------------
# Public: skeleton generation
# ---------------------------------------------------------------------------


async def generate_skeleton_for_hotspot(
    hotspot_id: str,
    chosen_angle_index: int,
    target_series: str = "A",
    target_length_words: int = 1500,
    article_id: str | None = None,
) -> SkeletonOutput:
    """Load hotspot from disk, pick the Nth suggested_angle, run D2 stage 1."""
    ensure_user_dirs()
    hotspot = _load_hotspot(hotspot_id)
    if not hotspot.suggested_angles:
        raise ValueError(f"Hotspot {hotspot_id} has no suggested_angles")
    if chosen_angle_index < 0 or chosen_angle_index >= len(hotspot.suggested_angles):
        raise IndexError(
            f"chosen_angle_index {chosen_angle_index} out of range "
            f"(angles: {len(hotspot.suggested_angles)})"
        )

    profile = load_style_profile()
    content_matrix = _load_content_matrix()
    chosen = hotspot.suggested_angles[chosen_angle_index]

    return await generate_skeleton(
        hotspot=hotspot,
        chosen_angle=chosen,
        style_profile=profile,
        content_matrix=content_matrix,
        target_series=target_series,
        target_length_words=target_length_words,
        article_id=article_id,
    )


# ---------------------------------------------------------------------------
# Public: filling + saving
# ---------------------------------------------------------------------------


async def fill_all_sections(
    skeleton: SkeletonOutput,
    chosen_title: int,
    chosen_opening: int,
    chosen_closing: int,
    style_profile: dict[str, Any],
    article_id: str,
) -> DraftOutput:
    """Iterate sections sequentially, extract image placeholders, persist draft.

    Each section's prompt receives the earlier filled sections so Claude can
    keep callbacks / references coherent.
    """
    ensure_user_dirs()

    if not skeleton.title_candidates:
        raise ValueError("skeleton.title_candidates is empty")
    if not skeleton.opening_candidates:
        raise ValueError("skeleton.opening_candidates is empty")
    if not skeleton.closing_candidates:
        raise ValueError("skeleton.closing_candidates is empty")

    title = skeleton.title_candidates[chosen_title].text
    opening = skeleton.opening_candidates[chosen_opening].opening_text
    closing = skeleton.closing_candidates[chosen_closing].closing_text

    completed: list[FilledSection] = []
    image_placeholders: list[ImagePlaceholder] = []

    # Emit one intent_used_in_write event for the fill pass (not per-section,
    # to avoid flooding the event log). Individual sections still inject the
    # intent block into their prompts via section_filler.load_current_intent.
    _fill_intent = load_current_intent()
    _fill_intent_text = intent_query_text(_fill_intent)
    if _fill_intent_text:
        _fill_profile = ((_fill_intent or {}).get("profile") or {})
        append_memory_event(
            "intent_used_in_write",
            article_id=article_id,
            payload={
                "query": _fill_intent_text,
                "stage": "fill",
                "ttl": ((_fill_intent.get("metadata") or {}) if _fill_intent else {}).get(
                    "ttl"
                ),
                "profile_id": _fill_profile.get("id"),
                "profile_label": _fill_profile.get("label"),
            },
        )

    for section in skeleton.section_outline:
        context = {
            "title": title,
            "opening": opening,
            "closing": closing,
            "full_outline": skeleton.section_outline,
            "previous_sections": [s.to_dict() for s in completed],
            "article_id": article_id,
        }
        filled = await fill_section(section, context, style_profile)
        completed.append(filled)

        # Extract [IMAGE: desc] placeholders inside this section's markdown.
        for ph in extract_image_placeholders(filled.content_markdown):
            image_placeholders.append(
                ImagePlaceholder(
                    id=f"{article_id}_{len(image_placeholders) + 1}",
                    description=ph["description"],
                    section_heading=filled.heading,
                )
            )

    total_words = sum(s.word_count for s in completed)
    draft = DraftOutput(
        article_id=article_id,
        title=title,
        sections=completed,
        total_word_count=total_words,
        image_placeholders=image_placeholders,
    )

    # Also stash opening / closing in metadata so the UI can reconstruct the full MD.
    save_draft(draft, extra_metadata={"opening": opening, "closing": closing})
    _log.info(
        "draft saved: article_id=%s, sections=%d, words=%d, image_placeholders=%d",
        article_id,
        len(completed),
        total_words,
        len(image_placeholders),
    )
    return draft


# ---------------------------------------------------------------------------
# Public: interactive edit
# ---------------------------------------------------------------------------


async def apply_user_edit(
    article_id: str,
    section_index: int,
    paragraph_index: int | None,
    command: str,
) -> FilledSection:
    """Load draft, apply edit to the targeted paragraph, rewrite draft on disk."""
    draft = load_draft(article_id)
    profile = load_style_profile()
    new_section = await _apply_edit(
        article=draft,
        section_index=section_index,
        paragraph_index=paragraph_index,
        command=command,
        style_profile=profile,
    )
    draft.sections[section_index] = new_section

    # Rebuild image placeholder list after edits (a paragraph swap may add/remove placeholders).
    new_placeholders: list[ImagePlaceholder] = []
    for sec in draft.sections:
        for ph in extract_image_placeholders(sec.content_markdown):
            new_placeholders.append(
                ImagePlaceholder(
                    id=f"{article_id}_{len(new_placeholders) + 1}",
                    description=ph["description"],
                    section_heading=sec.heading,
                )
            )
    # Preserve resolved_path for surviving descriptions.
    resolved_map = {
        p.description: p.resolved_path for p in draft.image_placeholders if p.resolved_path
    }
    for ph in new_placeholders:
        if ph.description in resolved_map:
            ph.resolved_path = resolved_map[ph.description]
    draft.image_placeholders = new_placeholders
    draft.total_word_count = sum(s.word_count for s in draft.sections)

    save_draft(draft)
    return new_section


# ---------------------------------------------------------------------------
# Public: draft I/O
# ---------------------------------------------------------------------------


def save_draft(
    draft: DraftOutput, extra_metadata: dict[str, Any] | None = None
) -> None:
    """Write the assembled markdown + JSON metadata to disk."""
    ensure_user_dirs()
    target_dir = _draft_dir_for(draft.article_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    metadata_path = target_dir / "metadata.json"
    existing_meta: dict[str, Any] = {}
    if metadata_path.exists():
        try:
            with metadata_path.open("r", encoding="utf-8") as fh:
                existing_meta = json.load(fh) or {}
        except json.JSONDecodeError:
            existing_meta = {}

    opening = (extra_metadata or {}).get("opening") or existing_meta.get("opening", "")
    closing = (extra_metadata or {}).get("closing") or existing_meta.get("closing", "")

    # Build the full markdown.
    parts: list[str] = [f"# {draft.title}", ""]
    if opening:
        parts.extend([opening, ""])
    for section in draft.sections:
        parts.append(f"## {section.heading}")
        parts.append("")
        parts.append(section.content_markdown)
        parts.append("")
    if closing:
        parts.append(closing)
        parts.append("")

    md_path = target_dir / "draft.md"
    md_path.write_text("\n".join(parts), encoding="utf-8")

    metadata = {
        **existing_meta,
        **(extra_metadata or {}),
        "article_id": draft.article_id,
        "title": draft.title,
        "total_word_count": draft.total_word_count,
        "sections": [s.to_dict() for s in draft.sections],
        "image_placeholders": [p.to_dict() for p in draft.image_placeholders],
        "saved_at": datetime.now().isoformat(),
        "opening": opening,
        "closing": closing,
    }
    with metadata_path.open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, ensure_ascii=False, indent=2)


def load_draft(article_id: str) -> DraftOutput:
    """Read back the DraftOutput from ``~/.agentflow/drafts/<id>/metadata.json``."""
    path = _draft_dir_for(article_id) / "metadata.json"
    if not path.exists():
        raise FileNotFoundError(f"No draft metadata at {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return DraftOutput.from_dict(data)


# ---------------------------------------------------------------------------
# Small helpers used by CLI / tests
# ---------------------------------------------------------------------------


def build_skeleton_from_dict(data: dict[str, Any]) -> SkeletonOutput:
    """Hand-roll a SkeletonOutput from a dict (used by tests / UI posts)."""
    return SkeletonOutput.from_dict(data)


def build_section(data: dict[str, Any]) -> Section:
    return Section.from_dict(data)
