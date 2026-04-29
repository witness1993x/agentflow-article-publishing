"""Medium adapter.

Medium supports most markdown natively, so the adapter's job is mostly
paragraph length enforcement (60 words), low emoji density, sentence-case
headings, and metadata extraction (title + subtitle + tags).
"""

from __future__ import annotations

import re
from typing import Any

from agentflow.agent_d3.adapters.base import BasePlatformAdapter
from agentflow.shared.markdown_utils import split_paragraphs
from agentflow.shared.models import DraftOutput, PlatformVersion

_STOPWORDS = {
    "the", "and", "for", "from", "with", "this", "that", "your", "you",
    "are", "was", "were", "but", "not", "all", "any", "have", "has", "into",
    "about", "how", "why", "what", "when", "where", "who", "which", "a",
    "an", "of", "in", "to", "is", "on", "by", "as", "it", "be", "or",
    "至", "于", "在", "是", "的", "了", "和", "或", "与", "一", "有", "也",
    "都", "这", "那", "个", "我", "他", "她", "们", "你", "我们",
}

# Leading/trailing Chinese particles that produce junk tag fragments when a
# section heading is tokenized (mirrors the Ghost adapter's fix).
_ZH_PARTICLES = "的了和或与是在也都这那有被对把从到等并但"


class MediumAdapter(BasePlatformAdapter):
    platform_name = "medium"

    async def adapt(
        self,
        draft: DraftOutput,
        series: str = "A",
        force_strip_unresolved_images: bool = False,
    ) -> PlatformVersion:
        changes: list[str] = []

        md = self._draft_to_markdown(draft)

        # 1. Paragraph length -> 60 words (Medium rule).
        max_words = int(self.rules.get("paragraph_max_words", 60))
        md, notes = await self._adjust_paragraphs(md, max_words)
        changes.extend(notes)

        # 2. Emoji density -> low.
        density = self.rules.get("emoji_density", "low")
        md, notes = self._adjust_emoji(md, density)
        changes.extend(notes)

        # 3. Heading style -> sentence_case.
        md = self._enforce_heading_style(
            md, self.rules.get("heading_style", "sentence_case")
        )
        changes.append(f"Applied heading_style={self.rules.get('heading_style', 'sentence_case')}")

        # 4. Blockquotes are passthrough (Medium renders > natively).
        #    Nothing to do beyond letting the lines through.

        # 5. Images.
        md, notes = self._resolve_images(
            md, draft.image_placeholders, force_strip_unresolved_images
        )
        changes.extend(notes)

        # 6. Metadata.
        metadata = self._build_metadata(draft, md)

        return PlatformVersion(
            platform=self.platform_name,
            content=md.strip() + "\n",
            metadata=metadata,
            formatting_changes=changes,
        )

    # ------------------------------------------------------------------ meta

    def _build_metadata(self, draft: DraftOutput, md: str) -> dict[str, Any]:
        # Tag/subtitle/title resolution order:
        #   1. metadata_overrides.medium.<key>     (hand-set, hard override)
        #   2. publisher_account.<key>             (brand defaults, snapshotted at write time)
        #   3. auto-inferred from body / heading
        #
        # Step 3 is last-resort because for tags it's the noisiest: it can
        # surface CJK fragments like '我最初理解' that need hand-cleanup.
        meta = self._load_metadata(draft.article_id)
        overrides = (meta.get("metadata_overrides") or {}).get("medium") or {}
        publisher = meta.get("publisher_account") or {}

        title = (
            overrides.get("title")
            or draft.title[:100]
        )
        subtitle = overrides.get("subtitle") or self._first_sentence(draft, md)
        tags = (
            list(overrides.get("tags") or [])
            or list(publisher.get("default_tags") or [])
            or self._infer_tags(draft)
        )
        canonical_url = overrides.get("canonical_url")
        return {
            "title": title,
            "subtitle": subtitle,
            "tags": tags,
            "canonical_url": canonical_url,
        }

    def _load_metadata(self, article_id: str) -> dict[str, Any]:
        import json as _json
        from agentflow.shared.bootstrap import agentflow_home

        path = agentflow_home() / "drafts" / article_id / "metadata.json"
        if not path.exists():
            return {}
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    # Back-compat alias kept for any external caller that referenced the old
    # narrower helper.
    def _load_overrides(self, article_id: str) -> dict[str, Any]:
        meta = self._load_metadata(article_id)
        ov = (meta.get("metadata_overrides") or {}).get("medium") or {}
        return ov if isinstance(ov, dict) else {}

    def _first_sentence(self, draft: DraftOutput, md: str) -> str | None:
        """Return the first sentence from the earliest prose paragraph,
        truncated to 120 chars. Used as Medium subtitle.

        Skips: headings (``#``), fenced code (```` ``` ````), blockquotes
        (``>``), image embeds (``![``).
        """
        for para in split_paragraphs(md):
            stripped = para.lstrip()
            if (
                stripped.startswith("#")
                or stripped.startswith("```")
                or stripped.startswith(">")
                or stripped.startswith("![")
            ):
                continue
            sentence = re.split(r"(?<=[。！？!?\.])\s*", para, maxsplit=1)[0]
            sentence = sentence.strip()
            if not sentence:
                continue
            return sentence[:120]
        # Fallback: first section's leading text.
        if draft.sections:
            first = draft.sections[0].content_markdown.strip()
            if first:
                sentence = re.split(r"(?<=[。！？!?\.])\s*", first, maxsplit=1)[0]
                return sentence.strip()[:120]
        return None

    def _infer_tags(self, draft: DraftOutput) -> list[str]:
        """Infer up to 5 tags from section headings + title keywords.

        Simple heuristic:
        - Collect tokens from the title and each section heading.
        - Drop markdown markers and stopwords.
        - Keep unique, order-preserving, capped at max_tags (5).
        """
        max_tags = int(self.rules.get("max_tags", 5))

        def _tokens(text: str) -> list[str]:
            clean = re.sub(r"[#*_`>\[\]()\-!？，。、:：!?,.\"'\\]", " ", text)
            raw = re.findall(r"[A-Za-z]{3,}|[一-鿿]{2,}", clean)
            out: list[str] = []
            for t in raw:
                if not re.match(r"[A-Za-z]", t):
                    t = t.lstrip(_ZH_PARTICLES).rstrip(_ZH_PARTICLES)
                    if len(t) < 2:
                        continue
                if t.lower() in _STOPWORDS:
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
