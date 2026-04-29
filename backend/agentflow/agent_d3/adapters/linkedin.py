"""LinkedIn Article adapter.

LinkedIn's UGC Post / Article surface has limited markdown support:
- No h2+ headings (we convert them to bold lines).
- Extreme paragraph brevity (≤40 words).
- Medium emoji tolerance.
- 1500-word hard ceiling.
- Closing sentence ideally ends with a question.
"""

from __future__ import annotations

import re
from typing import Any

from agentflow.agent_d3.adapters.base import BasePlatformAdapter
from agentflow.shared.markdown_utils import count_words, split_paragraphs
from agentflow.shared.models import DraftOutput, PlatformVersion


class LinkedInAdapter(BasePlatformAdapter):
    platform_name = "linkedin_article"

    async def adapt(
        self,
        draft: DraftOutput,
        series: str = "A",
        force_strip_unresolved_images: bool = False,
    ) -> PlatformVersion:
        changes: list[str] = []

        md = self._draft_to_markdown(draft)

        # 1. Paragraph length (40 words).
        max_words = int(self.rules.get("paragraph_max_words", 40))
        md, notes = await self._adjust_paragraphs(md, max_words)
        changes.extend(notes)

        # 2. Emoji density -> medium (passthrough).
        md, notes = self._adjust_emoji(md, self.rules.get("emoji_density", "medium"))
        changes.extend(notes)

        # 3. Headings -> stripped / converted to bold (LinkedIn markdown limited).
        md = self._enforce_heading_style(md, "none")
        changes.append("Converted headings to bold (LinkedIn has no h1-h3)")

        # 4. Length hard limit (1500 words total). Condense if needed.
        hard_limit = self.rules.get("length_hard_limit")
        if isinstance(hard_limit, int) and hard_limit > 0:
            total = count_words(md)
            if total > hard_limit:
                md, notes = self._condense(md, hard_limit)
                changes.extend(notes)

        # 5. Ensure closing ends with a question mark.
        md, notes = self._ensure_question_ending(md)
        changes.extend(notes)

        # 6. Images.
        md, notes = self._resolve_images(
            md, draft.image_placeholders, force_strip_unresolved_images
        )
        changes.extend(notes)

        metadata = {
            "title": draft.title[:100],
        }

        return PlatformVersion(
            platform=self.platform_name,
            content=md.strip() + "\n",
            metadata=metadata,
            formatting_changes=changes,
        )

    # ------------------------------------------------------------------ ops

    def _condense(self, md: str, target_words: int) -> tuple[str, list[str]]:
        """Drop the last 20% of each non-heading paragraph section until under target.

        Naive truncation strategy per spec: we iteratively remove the tail 20%
        of each paragraph in reverse section order until we're under the limit.
        """
        paragraphs = split_paragraphs(md)
        # Work through paragraphs from last to first and trim text until under.
        trimmed = list(paragraphs)
        pass_count = 0
        while count_words("\n\n".join(trimmed)) > target_words and pass_count < 5:
            pass_count += 1
            for idx in range(len(trimmed) - 1, -1, -1):
                para = trimmed[idx]
                if para.startswith("#") or para.startswith("```") or para.startswith(">"):
                    continue
                cur_words = count_words(para)
                if cur_words < 10:
                    continue
                new_para = _trim_trailing(para, 0.2)
                if new_para != para:
                    trimmed[idx] = new_para
                if count_words("\n\n".join(trimmed)) <= target_words:
                    break

        new_md = "\n\n".join(p for p in trimmed if p.strip())
        notes = [
            f"Condensed to ≤{target_words} words (naive tail-truncation, {pass_count} pass(es))"
        ]
        return new_md, notes

    def _ensure_question_ending(self, md: str) -> tuple[str, list[str]]:
        notes: list[str] = []
        text = md.rstrip()
        if not text:
            return md, notes
        # Look at the final non-empty line.
        last_line = text.splitlines()[-1].rstrip()
        if last_line.endswith("?") or last_line.endswith("？"):
            return md, notes
        # Append a question prompt on a new paragraph.
        appendix = "你怎么看？"
        new_md = text + "\n\n" + appendix + "\n"
        notes.append("Appended question ending to drive engagement")
        return new_md, notes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trim_trailing(text: str, ratio: float) -> str:
    """Drop the last ``ratio`` fraction of ``text`` at the nearest sentence end."""
    target_words = max(1, int(count_words(text) * (1 - ratio)))
    # Walk sentences from the start and stop when we reach target word count.
    sentences = re.split(r"(?<=[。！？!?\.])\s*", text)
    buf: list[str] = []
    total = 0
    for sent in sentences:
        sent_words = count_words(sent)
        if total + sent_words > target_words and buf:
            break
        buf.append(sent)
        total += sent_words
    if not buf:
        return text
    return " ".join(s.strip() for s in buf if s.strip())
