"""Agent D2.5 — propose image placements for a finished draft.

Pipeline:
1. Load the existing ``DraftOutput`` via ``agent_d2.main.load_draft``.
2. Load ``metadata.json`` for ``hotspot_id`` and raw ``draft.md`` (to compute
   ``section_count`` and pass the full text to the LLM).
3. Render ``backend/prompts/d2_image_proposal.md`` and call
   ``LLMClient().chat_json(prompt_family="d2-image-proposal", ...)``.
4. For each proposed image, insert a ``[IMAGE: <description.zh>]`` line into
   the right section's ``content_markdown`` based on the anchor:

     after_opening          → prepended to section[0]
     before_section:N       → prepended to section[N-1]
     middle_of_section:N    → inserted between paragraphs of section[N-1]
     before_closing         → appended to the last section

5. Rebuild the ``image_placeholders`` list from scratch (walking each section's
   ``content_markdown`` in order) and call ``save_draft`` — this
   automatically regenerates ``draft.md`` on disk.
6. Persist the raw LLM payload + insertion log to
   ``~/.agentflow/drafts/<article_id>/image_proposals.json``.
7. Return a summary dict.

``MOCK_LLM=true`` mode is served by
``agentflow/shared/mocks/d2-image-proposal.json`` via ``LLMClient``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentflow.agent_d2.main import load_draft, save_draft
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.hotspot_store import load_hotspot_refs
from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger
from agentflow.shared.markdown_utils import (
    _IMAGE_PLACEHOLDER_RE,
    count_words,
)
from agentflow.shared.models import FilledSection, ImagePlaceholder

_log = get_logger("agent_d2.image_proposer")

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d2_image_proposal.md"
)

_SECTION_HEADING_RE = re.compile(r"^##\s+(?!#)", re.MULTILINE)


# ---------------------------------------------------------------------------
# Paths / input loading
# ---------------------------------------------------------------------------


def _draft_dir(article_id: str) -> Path:
    return agentflow_home() / "drafts" / article_id


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _load_prompt_template() -> str:
    raw = _PROMPT_PATH.read_text(encoding="utf-8")
    marker = "```text"
    start = raw.find(marker)
    if start == -1:
        return raw
    start += len(marker)
    end = raw.rfind("```")
    if end == -1 or end <= start:
        return raw[start:].strip()
    return raw[start:end].strip()


def _render(template: str, values: dict[str, Any]) -> str:
    out = template
    out = out.replace("{{", "\x00LB\x00").replace("}}", "\x00RB\x00")
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    out = out.replace("\x00LB\x00", "{").replace("\x00RB\x00", "}")
    return out


def _format_references(refs: list[dict[str, Any]], limit: int = 5) -> str:
    if not refs:
        return "(no source references)"
    lines: list[str] = []
    for i, r in enumerate(refs[:limit], start=1):
        title = (r.get("title") or "").strip()
        snippet = (
            r.get("text_snippet") or r.get("text") or ""
        ).strip().replace("\n", " ")
        url = (r.get("url") or "").strip()
        if len(snippet) > 240:
            snippet = snippet[:240] + "..."
        lines.append(f"[{i}] {title}\n    url: {url}\n    snippet: {snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-section insertion
# ---------------------------------------------------------------------------


def _prepend_placeholder(content: str, description_zh: str) -> str:
    placeholder = f"[IMAGE: {description_zh}]"
    content = content.lstrip("\n")
    return f"{placeholder}\n\n{content}"


def _append_placeholder(content: str, description_zh: str) -> str:
    placeholder = f"[IMAGE: {description_zh}]"
    content = content.rstrip("\n")
    return f"{content}\n\n{placeholder}"


def _insert_middle_placeholder(content: str, description_zh: str) -> str:
    """Insert between the middle two paragraphs of ``content``.

    Paragraphs are separated by blank lines. If there are fewer than 2
    paragraphs, fall back to appending.
    """
    placeholder = f"[IMAGE: {description_zh}]"
    # Split on a double-newline boundary to preserve existing paragraph blocks.
    parts = re.split(r"\n\s*\n", content)
    parts = [p for p in parts if p.strip()]
    if len(parts) < 2:
        return _append_placeholder(content, description_zh)
    mid = len(parts) // 2
    head = "\n\n".join(parts[:mid])
    tail = "\n\n".join(parts[mid:])
    return f"{head}\n\n{placeholder}\n\n{tail}"


def _apply_anchor(
    sections: list[FilledSection],
    anchor: str,
    description_zh: str,
) -> tuple[bool, str]:
    """Mutate ``sections`` to insert a placeholder at ``anchor``. Return ``(ok, reason)``."""
    if not sections:
        return False, "no sections to insert into"

    anchor = (anchor or "").strip()
    if anchor == "after_opening":
        target = sections[0]
        target.content_markdown = _prepend_placeholder(
            target.content_markdown, description_zh
        )
        return True, "after_opening -> prepended to section[0]"

    if anchor == "before_closing":
        target = sections[-1]
        target.content_markdown = _append_placeholder(
            target.content_markdown, description_zh
        )
        return True, "before_closing -> appended to last section"

    m = re.match(r"^before_section:(\d+)$", anchor)
    if m:
        n = int(m.group(1))
        if n < 1:
            return False, f"before_section:{n} invalid (must be >= 1)"
        if n > len(sections):
            return False, f"before_section:{n} out of range (have {len(sections)})"
        target = sections[n - 1]
        target.content_markdown = _prepend_placeholder(
            target.content_markdown, description_zh
        )
        return True, f"before_section:{n} -> prepended to section[{n - 1}]"

    m = re.match(r"^middle_of_section:(\d+)$", anchor)
    if m:
        n = int(m.group(1))
        if n < 1 or n > len(sections):
            return False, f"middle_of_section:{n} out of range (have {len(sections)})"
        target = sections[n - 1]
        target.content_markdown = _insert_middle_placeholder(
            target.content_markdown, description_zh
        )
        return True, f"middle_of_section:{n} -> middle of section[{n - 1}]"

    return False, f"unknown anchor {anchor!r}"


# ---------------------------------------------------------------------------
# Placeholder rebuild (runs after all insertions)
# ---------------------------------------------------------------------------


def _rebuild_placeholders(
    sections: list[FilledSection], article_id: str
) -> list[ImagePlaceholder]:
    """Walk sections in order, extracting each [IMAGE:] into an ImagePlaceholder."""
    out: list[ImagePlaceholder] = []
    counter = 0
    for sec in sections:
        for m in _IMAGE_PLACEHOLDER_RE.finditer(sec.content_markdown):
            counter += 1
            out.append(
                ImagePlaceholder(
                    id=f"{article_id}_{counter}",
                    description=m.group(1).strip(),
                    section_heading=sec.heading,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def propose_images(article_id: str) -> dict[str, Any]:
    """Propose image placements for ``article_id``; rewrite draft + return summary."""
    draft_dir = _draft_dir(article_id)
    if not draft_dir.exists():
        raise FileNotFoundError(f"no draft directory at {draft_dir}")

    metadata_path = draft_dir / "metadata.json"
    draft_md_path = draft_dir / "draft.md"
    if not metadata_path.exists():
        raise FileNotFoundError(f"no metadata.json at {metadata_path}")
    if not draft_md_path.exists():
        raise FileNotFoundError(f"no draft.md at {draft_md_path}")

    metadata: dict[str, Any] = json.loads(
        metadata_path.read_text(encoding="utf-8")
    )
    hotspot_id = str(metadata.get("hotspot_id") or "")
    draft_markdown = draft_md_path.read_text(encoding="utf-8")
    section_count = len(_SECTION_HEADING_RE.findall(draft_markdown))
    target_count = max(1, section_count // 2 + 1)

    refs = load_hotspot_refs(hotspot_id)

    substitutions = {
        "user_handle": metadata.get("user_handle") or "作者",
        "draft_markdown": draft_markdown,
        "source_references": _format_references(refs, limit=5),
        "section_count": section_count,
        "target_count": target_count,
    }
    prompt = _render(_load_prompt_template(), substitutions)

    client = LLMClient()
    raw = await client.chat_json(
        prompt_family="d2-image-proposal",
        prompt=prompt,
        max_tokens=2000,
    )

    images = raw.get("images") if isinstance(raw, dict) else None
    if not isinstance(images, list):
        images = []

    # Load the draft so we can mutate section content directly.
    draft = load_draft(article_id)

    insertions: list[dict[str, Any]] = []
    for img in images:
        pos = img.get("position") or {}
        anchor = str(pos.get("anchor") or "").strip()
        desc = img.get("description") or {}
        if isinstance(desc, str):
            description_zh = desc.strip()
        else:
            description_zh = (
                desc.get("zh") or desc.get("en") or ""
            ).strip()
        if not description_zh:
            insertions.append(
                {
                    "anchor": anchor,
                    "skipped": True,
                    "reason": "missing description",
                }
            )
            continue

        ok, note = _apply_anchor(draft.sections, anchor, description_zh)
        entry: dict[str, Any] = {
            "anchor": anchor,
            "description_zh": description_zh,
            "role": img.get("role"),
            "priority": img.get("priority"),
            "note": note,
        }
        if not ok:
            entry["skipped"] = True
            entry["reason"] = note
        insertions.append(entry)

    # Recompute word counts (roughly) so section metadata stays sane.
    for sec in draft.sections:
        sec.word_count = count_words(sec.content_markdown)
    draft.total_word_count = sum(s.word_count for s in draft.sections)

    # Rebuild placeholder list, preserving any prior resolved_path by description.
    resolved_map = {
        p.description: p.resolved_path
        for p in draft.image_placeholders
        if p.resolved_path
    }
    new_placeholders = _rebuild_placeholders(draft.sections, article_id)
    for ph in new_placeholders:
        if ph.description in resolved_map:
            ph.resolved_path = resolved_map[ph.description]
    draft.image_placeholders = new_placeholders

    # Persist: save_draft rewrites both draft.md and metadata.json.
    save_draft(draft)

    # Persist the raw proposal for reproducibility.
    proposals_path = draft_dir / "image_proposals.json"
    proposals_payload = {
        "article_id": article_id,
        "section_count": section_count,
        "target_count": target_count,
        "raw": raw,
        "insertions": insertions,
    }
    proposals_path.write_text(
        json.dumps(proposals_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    required_count = sum(
        1 for i in images if str(i.get("priority") or "").lower() == "required"
    )
    recommended_count = sum(
        1 for i in images if str(i.get("priority") or "").lower() == "recommended"
    )
    optional_count = sum(
        1 for i in images if str(i.get("priority") or "").lower() == "optional"
    )

    inserted_count = sum(1 for i in insertions if not i.get("skipped"))
    skipped_count = sum(1 for i in insertions if i.get("skipped"))

    _log.info(
        "image proposals: article_id=%s, proposed=%d, inserted=%d, skipped=%d",
        article_id,
        len(images),
        inserted_count,
        skipped_count,
    )

    return {
        "article_id": article_id,
        "hotspot_id": hotspot_id,
        "section_count": section_count,
        "target_count": target_count,
        "proposed_count": len(images),
        "inserted_count": inserted_count,
        "skipped_count": skipped_count,
        "required_count": required_count,
        "recommended_count": recommended_count,
        "optional_count": optional_count,
        "image_proposals_path": str(proposals_path),
        "draft_md_path": str(draft_md_path),
        "total_placeholders": len(new_placeholders),
    }
