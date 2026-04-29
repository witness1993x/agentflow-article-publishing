"""Base platform adapter.

Subclasses implement ``adapt`` by composing the helpers here:

- ``_adjust_paragraphs`` — split paragraphs exceeding ``max_words`` by sentence
  boundaries; fall back to ``ai_helpers.semantic_split`` when the naive split
  still leaves a long tail.
- ``_adjust_emoji`` — low / medium / high density policy.
- ``_resolve_images`` — substitute resolved ``ImagePlaceholder`` paths into
  ``![desc](path)`` and optionally strip unresolved ones.
- ``_enforce_heading_style`` — sentence_case / title_case / none / decorative.

Each helper returns ``(new_md, change_notes)`` so the adapter can accumulate a
``formatting_changes`` audit trail.
"""

from __future__ import annotations

import re
from typing import Any

from agentflow.agent_d3.ai_helpers import semantic_split
from agentflow.shared.logger import get_logger
from agentflow.shared.markdown_utils import (
    _IMAGE_PLACEHOLDER_RE,
    count_words,
    replace_image_placeholder,
    split_paragraphs,
    strip_image_placeholders,
)
from agentflow.shared.models import (
    DraftOutput,
    ImagePlaceholder,
    PlatformVersion,
)

_log = get_logger("agent_d3.adapters.base")

# Matches sentence terminators in both Chinese (。！？) and Latin (.!?)
# scripts. Kept here (not in markdown_utils) because only the adapters need
# sentence-level granularity.
_SENT_END_RE = re.compile(r"(?<=[。！？!?\.])\s*")

# Simple emoji + pictograph detection. Covers most emoji blocks and common
# symbolic variants. Not perfect (e.g. composite flags need further work), but
# good enough for a "strip all but first N" policy in MVP.
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport
    "\U0001F700-\U0001F77F"  # alchemical
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"  # misc symbols
    "\U00002700-\U000027BF"  # dingbats
    "]",
    flags=re.UNICODE,
)


class BasePlatformAdapter:
    """Abstract platform adapter. Override ``platform_name`` and ``adapt``."""

    platform_name: str = ""

    def __init__(self, rules: dict[str, Any], style_profile: dict[str, Any]):
        self.rules = rules
        self.style_profile = style_profile or {}

    # ------------------------------------------------------------------ adapt

    async def adapt(
        self,
        draft: DraftOutput,
        series: str = "A",
        force_strip_unresolved_images: bool = False,
    ) -> PlatformVersion:
        raise NotImplementedError

    # --------------------------------------------------------------- helpers

    def _draft_to_markdown(self, draft: DraftOutput) -> str:
        """Concatenate draft sections into a single markdown blob.

        If the draft has a resolved cover-role placeholder, auto-prepend it as
        ``![](<path>)`` right after the H1 so the rendered preview matches the
        Medium feature-image position. Cover suppression happens via
        ``--skip-images`` (which clears placeholders upstream).
        """
        parts: list[str] = [f"# {draft.title}".strip(), ""]
        cover = next(
            (
                p for p in draft.image_placeholders
                if getattr(p, "role", "body") == "cover" and p.resolved_path
            ),
            None,
        )
        if cover is not None:
            parts.append(f"![]({cover.resolved_path})")
            parts.append("")
        for sec in draft.sections:
            heading = sec.heading.strip()
            if heading:
                # Match `agent_d2.main.save_draft` which writes "## {heading}"
                # to draft.md. Without the `##` prefix the d3 adapter's
                # heading-style logic and downstream renderers treat sections
                # as plain paragraphs.
                if not heading.startswith("#"):
                    heading = f"## {heading}"
                parts.append(heading)
                parts.append("")
            body = sec.content_markdown.strip()
            if body:
                parts.append(body)
                parts.append("")
        return "\n".join(parts).strip() + "\n"

    # ---- paragraph length --------------------------------------------------

    async def _adjust_paragraphs(
        self, md: str, max_words: int
    ) -> tuple[str, list[str]]:
        """Split paragraphs exceeding ``max_words`` by sentence boundaries.

        If a single resulting chunk still exceeds ``max_words * 1.2``, fall back
        to ``ai_helpers.semantic_split`` for that chunk only.
        """
        changes: list[str] = []
        paragraphs = split_paragraphs(md)
        rebuilt: list[str] = []
        split_count = 0
        ai_calls = 0

        for para in paragraphs:
            # Headings / fenced code blocks / image placeholder-only lines
            # are left intact.
            if para.startswith("#") or para.startswith("```"):
                rebuilt.append(para)
                continue

            if count_words(para) <= max_words:
                rebuilt.append(para)
                continue

            chunks = _split_by_sentences(para, max_words)

            # If any chunk still too long, go semantic on it.
            final_chunks: list[str] = []
            for chunk in chunks:
                if count_words(chunk) > max_words * 1.2:
                    ai_calls += 1
                    replaced = await semantic_split(chunk, max_words)
                    final_chunks.append(replaced)
                else:
                    final_chunks.append(chunk)

            split_count += 1
            rebuilt.append("\n\n".join(c.strip() for c in final_chunks if c.strip()))

        if split_count:
            changes.append(
                f"Split {split_count} paragraph(s) to max {max_words} words"
                + (f" ({ai_calls} via AI)" if ai_calls else "")
            )

        return "\n\n".join(rebuilt) + "\n", changes

    # ---- emoji -------------------------------------------------------------

    def _adjust_emoji(self, md: str, density: str) -> tuple[str, list[str]]:
        """Adjust emoji density:

        - ``low``: strip all emojis except the first 2 encountered.
        - ``medium``: pass through.
        - ``high``: prepend a neutral emoji to each heading that lacks one.
        """
        changes: list[str] = []

        if density == "medium":
            return md, changes

        if density == "low":
            kept = 0
            stripped = 0

            def _strip(match: re.Match[str]) -> str:
                nonlocal kept, stripped
                if kept < 2:
                    kept += 1
                    return match.group(0)
                stripped += 1
                return ""

            new_md = _EMOJI_RE.sub(_strip, md)
            if stripped:
                changes.append(f"Stripped {stripped} emoji (density=low, kept {kept})")
            return new_md, changes

        if density == "high":
            added = 0
            new_lines: list[str] = []
            heading_emojis = ["✨", "🔥", "💡", "🚀", "🎯", "📝"]
            idx = 0
            for line in md.splitlines():
                stripped_line = line.lstrip()
                if stripped_line.startswith("#") and not _EMOJI_RE.search(line):
                    # Insert emoji right after the heading marker.
                    heading_prefix_match = re.match(r"(#+\s*)", stripped_line)
                    if heading_prefix_match:
                        prefix = heading_prefix_match.group(1)
                        rest = stripped_line[len(prefix):]
                        emoji = heading_emojis[idx % len(heading_emojis)]
                        idx += 1
                        added += 1
                        leading_ws = line[: len(line) - len(stripped_line)]
                        new_lines.append(f"{leading_ws}{prefix}{emoji} {rest}")
                        continue
                new_lines.append(line)
            if added:
                changes.append(f"Added {added} heading emoji (density=high)")
            return "\n".join(new_lines), changes

        return md, changes

    # ---- images ------------------------------------------------------------

    def _resolve_images(
        self,
        md: str,
        placeholders: list[ImagePlaceholder],
        force_strip: bool,
    ) -> tuple[str, list[str]]:
        """Substitute ``[IMAGE: …]`` placeholders.

        - Always replace any placeholder whose ``ImagePlaceholder.resolved_path``
          is set with ``![desc](path)``.
        - If ``force_strip`` is True, strip entire lines that still contain
          unresolved placeholders.
        - Otherwise unresolved placeholders are LEFT IN the content so the
          publish endpoint can block.
        """
        changes: list[str] = []

        # First: replace any resolved placeholders in-order.
        resolved = 0
        for ph in placeholders:
            if ph.resolved_path:
                md = replace_image_placeholder(md, ph.id, ph.resolved_path)
                resolved += 1
        if resolved:
            changes.append(f"Resolved {resolved} image placeholder(s)")

        if force_strip:
            # Count remaining before stripping so we can log it.
            remaining = len(_IMAGE_PLACEHOLDER_RE.findall(md))
            if remaining:
                md = strip_image_placeholders(md)
                changes.append(
                    f"Stripped {remaining} unresolved image placeholder(s) (force_strip)"
                )

        # Stripping/replacing image lines can leave 3+ consecutive newlines.
        # Collapse them so the rendered markdown stays tight (single blank line
        # between blocks is the markdown convention).
        normalized = re.sub(r"\n{3,}", "\n\n", md)
        if normalized != md:
            md = normalized
            changes.append("Normalized paragraph spacing after image resolution")

        return md, changes

    # ---- heading style -----------------------------------------------------

    def _enforce_heading_style(self, md: str, style: str) -> str:
        """Rewrite heading lines to conform to ``style``.

        - ``sentence_case``: capitalize only the first word (Latin text); leave
          CJK untouched.
        - ``title_case``: title-case Latin headings; leave CJK untouched.
        - ``none``: strip leading ``#`` marker(s), converting headings into
          plain (bold) lines. Used for platforms without heading support
          (LinkedIn articles).
        - ``decorative``: prepend a neutral ``◆`` marker.
        """
        if style not in {"sentence_case", "title_case", "none", "decorative"}:
            return md

        new_lines: list[str] = []
        for line in md.splitlines():
            stripped = line.lstrip()
            if not stripped.startswith("#"):
                new_lines.append(line)
                continue

            match = re.match(r"(#+\s*)(.*)$", stripped)
            if not match:
                new_lines.append(line)
                continue

            prefix = match.group(1)
            text = match.group(2).strip()
            leading_ws = line[: len(line) - len(stripped)]

            if not text:
                new_lines.append(line)
                continue

            if style == "sentence_case":
                new_text = _sentence_case(text)
                new_lines.append(f"{leading_ws}{prefix}{new_text}")
            elif style == "title_case":
                new_text = _title_case(text)
                new_lines.append(f"{leading_ws}{prefix}{new_text}")
            elif style == "none":
                # Convert to bold pseudo-heading. Blank line before so it
                # renders as its own block on platforms without markdown headings.
                new_lines.append(f"{leading_ws}**{text}**")
            elif style == "decorative":
                if not text.startswith("◆"):
                    new_lines.append(f"{leading_ws}{prefix}◆ {text}")
                else:
                    new_lines.append(line)

        return "\n".join(new_lines)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _split_by_sentences(paragraph: str, max_words: int) -> list[str]:
    """Naive sentence-level split at CJK + Latin terminators.

    Groups sentences greedily into chunks whose word count stays ≤ max_words.
    If a single sentence is already longer than max_words it becomes its own
    chunk (the caller can then invoke the semantic splitter).
    """
    # Split on terminators while keeping them attached to the preceding
    # sentence. re.split with a lookbehind keeps the punctuation.
    raw_sentences = [s for s in _SENT_END_RE.split(paragraph) if s and s.strip()]
    if not raw_sentences:
        return [paragraph]

    chunks: list[str] = []
    buffer: list[str] = []
    buffer_words = 0

    for sent in raw_sentences:
        words = count_words(sent)
        if buffer and buffer_words + words > max_words:
            chunks.append(" ".join(buffer).strip())
            buffer = [sent.strip()]
            buffer_words = words
        else:
            buffer.append(sent.strip())
            buffer_words += words

    if buffer:
        chunks.append(" ".join(buffer).strip())

    return [c for c in chunks if c]


_CJK_RE = re.compile(r"[　-〿一-鿿぀-ゟ゠-ヿ＀-￯]")


def _is_cjk_dominant(text: str) -> bool:
    """True when the heading contains any CJK character.

    Latin tokens inside a CJK heading are transcribed proper nouns / brand
    names (``Acme Corp``, ``agent``, ``Kafka Streams``) and must NOT be
    re-cased — that mangles "agent" → "Agent" and "dashboard" → "Dashboard".
    """
    return bool(_CJK_RE.search(text))


def _sentence_case(text: str) -> str:
    """Capitalize only the first Latin word; leave CJK alone."""
    # Mixed CJK + Latin: don't touch. Latin tokens are transcribed terms.
    if _is_cjk_dominant(text):
        return text
    # Find first Latin letter and upper-case it; lower the remainder of the
    # first run of letters only.
    for i, ch in enumerate(text):
        if ch.isalpha() and ch.isascii():
            # Lower everything after the first word, upper that first letter.
            # Find extent of first word.
            j = i + 1
            while j < len(text) and (text[j].isalpha() or text[j] == "'"):
                j += 1
            first_word = text[i].upper() + text[i + 1 : j].lower()
            rest = text[j:]
            # Lower-case remaining ASCII words but keep proper-noun-like all-caps acronyms.
            rest = re.sub(
                r"\b([A-Z][a-z]+)",
                lambda m: m.group(1).lower(),
                rest,
            )
            return text[:i] + first_word + rest
    return text  # no ASCII letters -> CJK, leave alone


def _title_case(text: str) -> str:
    """Title-case Latin words; leave CJK untouched.

    Mixed CJK + Latin headings are returned unchanged — Latin tokens in such
    headings are transcribed proper nouns / brand names whose casing must be
    preserved.
    """
    if _is_cjk_dominant(text):
        return text

    def _case_word(match: re.Match[str]) -> str:
        word = match.group(0)
        # Preserve all-caps acronyms of length ≥ 2 (e.g. API, LLM).
        if word.isupper() and len(word) >= 2:
            return word
        return word[:1].upper() + word[1:].lower()

    return re.sub(r"[A-Za-z]+", _case_word, text)
