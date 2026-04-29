"""Ghost / WordPress adapter.

The closest-to-original output: Ghost consumes markdown directly and the
author usually treats their blog as the canonical platform. Paragraph max 70
words, low emoji, title-case headings, no hard length cap.
"""

from __future__ import annotations

import re
from typing import Any

from agentflow.agent_d3.adapters.base import BasePlatformAdapter
from agentflow.shared.markdown_utils import split_paragraphs
from agentflow.shared.models import DraftOutput, PlatformVersion

_GHOST_STOPWORDS = {
    "the", "and", "for", "from", "with", "this", "that", "your", "you",
    "are", "was", "were", "but", "not", "all", "any", "have", "has", "into",
    "about", "how", "why", "what", "when", "where", "who", "which", "a",
    "an", "of", "in", "to", "is", "on", "by", "as", "it", "be", "or",
    "的", "了", "和", "或", "与", "是", "在", "也", "都", "这", "那",
}

# Leading/trailing Chinese particles that produce junk tag fragments when a
# section heading is tokenized (e.g. "在教育领域的应用" → "的应用", "的未来展望").
_ZH_PARTICLES = "的了和或与是在也都这那有被对把从到等并但"


class GhostAdapter(BasePlatformAdapter):
    platform_name = "ghost_wordpress"

    async def adapt(
        self,
        draft: DraftOutput,
        series: str = "A",
        force_strip_unresolved_images: bool = False,
    ) -> PlatformVersion:
        changes: list[str] = []

        md = self._draft_to_markdown(draft)

        # 1. Paragraphs -> 70 words.
        max_words = int(self.rules.get("paragraph_max_words", 70))
        md, notes = await self._adjust_paragraphs(md, max_words)
        changes.extend(notes)

        # 2. Emoji -> low.
        md, notes = self._adjust_emoji(md, self.rules.get("emoji_density", "low"))
        changes.extend(notes)

        # 3. Headings -> title_case.
        md = self._enforce_heading_style(md, self.rules.get("heading_style", "title_case"))
        changes.append(
            f"Applied heading_style={self.rules.get('heading_style', 'title_case')}"
        )

        # 4. Images.
        md, notes = self._resolve_images(
            md, draft.image_placeholders, force_strip_unresolved_images
        )
        changes.extend(notes)

        metadata = self._build_metadata(draft)

        return PlatformVersion(
            platform=self.platform_name,
            content=md.strip() + "\n",
            metadata=metadata,
            formatting_changes=changes,
        )

    # ------------------------------------------------------------------ meta

    def _build_metadata(self, draft: DraftOutput) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "title": draft.title,
            "tags": self._infer_tags(draft),
            "canonical_url": None,  # MVP: set later when Ghost is secondary
        }

        image_policy = self.rules.get("image_policy") or {}
        if image_policy.get("feature_image") == "first":
            for ph in draft.image_placeholders or []:
                if ph.resolved_path:
                    # TODO: upload to Ghost storage — for now we pass the local
                    # path. The Ghost Admin API expects a CDN URL here; until
                    # the uploader is wired in (agent_d4/publishers/ghost.py::
                    # _upload_image), this is a local-path fallback that Ghost
                    # will reject unless resolved_path is already a public URL.
                    metadata["feature_image"] = ph.resolved_path
                    break

        return metadata

    def _infer_tags(self, draft: DraftOutput) -> list[str]:
        max_tags = int(self.rules.get("max_tags", 10))

        def _tokens(text: str) -> list[str]:
            clean = re.sub(r"[#*_`>\[\]()\-!？，。、:：!?,.\"'\\]", " ", text)
            raw = re.findall(r"[A-Za-z]{3,}|[一-鿿]{2,}", clean)
            out: list[str] = []
            for t in raw:
                if not re.match(r"[A-Za-z]", t):
                    t = t.lstrip(_ZH_PARTICLES).rstrip(_ZH_PARTICLES)
                    if len(t) < 2:
                        continue
                if t.lower() in _GHOST_STOPWORDS:
                    continue
                out.append(t)
            return out

        pool: list[str] = []
        pool.extend(_tokens(draft.title))
        for sec in draft.sections:
            pool.extend(_tokens(sec.heading))

        seen: set[str] = set()
        ordered: list[str] = []
        for tok in pool:
            key = tok.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(tok)
            if len(ordered) >= max_tags:
                break
        return ordered
