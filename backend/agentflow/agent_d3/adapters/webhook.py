"""Webhook adapter — passthrough markdown + standard metadata.

The webhook publisher receives whatever this adapter emits, so we keep the
markdown structurally faithful to the source draft + carry through:
- Title (publisher_account override or draft.title)
- Subtitle (publisher override → first prose sentence fallback)
- Tags (override → publisher.default_tags → empty)
- Canonical URL (override only)
- cover_image_path (resolved cover-role placeholder, if any)

The receiver does the platform-specific layout (substack rendering / mirror
markdown / dev.to frontmatter / your CMS field mapping).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentflow.agent_d3.adapters.base import BasePlatformAdapter
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.markdown_utils import split_paragraphs
from agentflow.shared.models import DraftOutput, PlatformVersion


class WebhookAdapter(BasePlatformAdapter):
    platform_name = "webhook"

    async def adapt(
        self,
        draft: DraftOutput,
        series: str = "A",
        force_strip_unresolved_images: bool = False,
    ) -> PlatformVersion:
        changes: list[str] = []
        md = self._draft_to_markdown(draft)

        md, notes = self._resolve_images(
            md, draft.image_placeholders, force_strip_unresolved_images
        )
        changes.extend(notes)

        meta = self._build_metadata(draft, md)
        # Carry the cover path through the metadata so the publisher can
        # decode/upload binary without re-walking placeholders.
        cover = next(
            (
                p for p in draft.image_placeholders
                if getattr(p, "role", "body") == "cover" and p.resolved_path
            ),
            None,
        )
        if cover is not None:
            meta["cover_image_path"] = cover.resolved_path

        meta["article_id"] = draft.article_id

        return PlatformVersion(
            platform=self.platform_name,
            content=md.strip() + "\n",
            metadata=meta,
            formatting_changes=changes,
        )

    def _build_metadata(self, draft: DraftOutput, md: str) -> dict[str, Any]:
        # Honour metadata_overrides.webhook (fall back to medium overrides
        # since both target the same Markdown body).
        meta_path = (
            agentflow_home() / "drafts" / draft.article_id / "metadata.json"
        )
        overrides: dict[str, Any] = {}
        publisher: dict[str, Any] = {}
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8")) or {}
                ovs = data.get("metadata_overrides") or {}
                overrides = (ovs.get("webhook") or ovs.get("medium") or {}) if isinstance(ovs, dict) else {}
                publisher = data.get("publisher_account") or {}
            except Exception:
                pass

        title = overrides.get("title") or draft.title[:120]
        subtitle = overrides.get("subtitle") or self._first_sentence(md)
        tags = (
            list(overrides.get("tags") or [])
            or list(publisher.get("default_tags") or [])
        )
        canonical_url = overrides.get("canonical_url")
        return {
            "title": title,
            "subtitle": subtitle,
            "tags": tags,
            "canonical_url": canonical_url,
        }

    @staticmethod
    def _first_sentence(md: str) -> str | None:
        for para in split_paragraphs(md):
            stripped = para.lstrip()
            if (
                stripped.startswith("#")
                or stripped.startswith("```")
                or stripped.startswith(">")
                or stripped.startswith("![")
            ):
                continue
            sentence = re.split(r"(?<=[。！？!?\.])\s*", para, maxsplit=1)[0].strip()
            if sentence:
                return sentence[:200]
        return None
