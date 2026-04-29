"""Twitter (X) adapter — emits the JSON tweet-list shape consumed by
``agentflow.agent_d4.publishers.twitter.TwitterPublisher``.

Two adapters live here:

- ``TwitterAdapter`` (``platform_name = "twitter_thread"``) splits the draft
  body into a thread of ≤270-char tweets, attaches the cover to tweet 1, and
  re-attaches inline ``![alt](path)`` images to the nearest tweet.
- ``TwitterSingleAdapter`` (``platform_name = "twitter_single"``) emits a
  single tweet (cover + first ~270 chars).

The ``PlatformVersion.content`` field is a JSON-encoded list of tweet dicts
``{text, image_slot, image_hint, image_path?}``; ``metadata['tweets']`` carries
the same list (the publisher reads it from there) plus ``form`` for the
single/thread switch.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agentflow.agent_d3.adapters.base import BasePlatformAdapter
from agentflow.shared.models import DraftOutput, PlatformVersion

# Twitter's hard cap is 280; leave ~10 chars of headroom for the
# " (N/M)" position suffix appended on threads.
_MAX_CHARS = 270
_SENT_END_RE = re.compile(r"(?<=[。！？!?\.])\s*")
_IMG_EMBED_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


class TwitterAdapter(BasePlatformAdapter):
    platform_name = "twitter_thread"

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

        # Strip the H1 — Twitter has no title field; the H1 belongs implicitly
        # in tweet 1. We keep the first H2/H3 as plain prose lines.
        md, h1 = _strip_h1(md)
        # Drop remaining heading markers; ## headings become emphasis-free
        # paragraph leads.
        md = re.sub(r"^#{1,6}\s*", "", md, flags=re.MULTILINE)

        cover_path = next(
            (
                p.resolved_path for p in draft.image_placeholders
                if getattr(p, "role", "body") == "cover" and p.resolved_path
            ),
            None,
        )

        # Pull inline image embeds out of the body so they don't eat tweet
        # characters. We keep their order + path and re-attach to whichever
        # tweet the surrounding paragraph lands in.
        body, inline_imgs = _extract_inline_images(md)

        tweets = self._build_tweets(body, h1, cover_path, inline_imgs)

        # Append "(N/M)" suffix when there's an actual thread.
        if len(tweets) > 1:
            total = len(tweets)
            for i, t in enumerate(tweets):
                suffix = f" ({i + 1}/{total})"
                if len(t["text"]) + len(suffix) > 280:
                    t["text"] = t["text"][: 280 - len(suffix)].rstrip() + suffix
                else:
                    t["text"] = t["text"] + suffix
                t["char_count"] = len(t["text"])

        # Re-index now that we know final order.
        for i, t in enumerate(tweets):
            t["index"] = i
            t.setdefault("image_slot", str(i) if t.get("image_path") else None)
            t.setdefault("image_hint", None)
            t.setdefault("char_count", len(t["text"]))

        form = self._form()
        total_chars = sum(t["char_count"] for t in tweets)
        metadata: dict[str, Any] = {
            "form": form,
            "tweets": tweets,
            "tag_count": 0,
            "length_chars": total_chars,
            "title": (draft.title or "")[:100],
        }
        changes.append(
            f"Built {form}: {len(tweets)} tweet(s), {total_chars} chars total"
        )

        return PlatformVersion(
            platform=self.platform_name,
            content=json.dumps(tweets, ensure_ascii=False),
            metadata=metadata,
            formatting_changes=changes,
        )

    # ----------------------------------------------------------------- helpers

    def _form(self) -> str:
        return "thread"

    def _build_tweets(
        self,
        body: str,
        h1: str | None,
        cover_path: str | None,
        inline_imgs: list[tuple[int, str, str]],
    ) -> list[dict[str, Any]]:
        """Split body into ≤270-char tweets; tweet 1 gets the H1 + cover."""
        chunks: list[str] = []
        first_chunk_budget = _MAX_CHARS
        opener = (h1.strip() if h1 else "")
        if opener:
            opener = opener[:_MAX_CHARS]
            first_chunk_budget = max(40, _MAX_CHARS - len(opener) - 2)

        # Paragraph-first split.
        paragraphs = [p for p in re.split(r"\n{2,}", body) if p.strip()]
        for para in paragraphs:
            for piece in _split_paragraph(para.strip(), _MAX_CHARS):
                chunks.append(piece)

        if not chunks and not opener:
            chunks = [""]

        # Merge the H1 into the first chunk if it fits.
        if opener:
            if chunks and len(opener) + 2 + len(chunks[0]) <= _MAX_CHARS:
                chunks[0] = f"{opener}\n\n{chunks[0]}"
            elif chunks and len(chunks[0]) <= first_chunk_budget:
                chunks[0] = f"{opener}\n\n{chunks[0]}"
            else:
                chunks.insert(0, opener)

        tweets: list[dict[str, Any]] = []
        for text in chunks:
            tweets.append({
                "text": text.strip(),
                "image_slot": None,
                "image_hint": None,
            })

        # Tweet 1 gets the cover.
        if tweets and cover_path:
            tweets[0]["image_path"] = cover_path
            tweets[0]["image_hint"] = tweets[0].get("image_hint") or "cover"

        # Re-attach inline images to the closest tweet by char-offset of the
        # original placeholder. We map offset → tweet index by walking the
        # cumulative length of the chunks (post-merge body only).
        if inline_imgs:
            cum = 0
            offsets: list[tuple[int, int]] = []  # (char_offset_end, tweet_idx)
            # Skip the H1-only tweet from the offset map (its content didn't
            # come from the body).
            body_start_idx = 1 if (opener and tweets and tweets[0]["text"] == opener) else 0
            for i in range(body_start_idx, len(tweets)):
                cum += len(tweets[i]["text"])
                offsets.append((cum, i))
            for offset, alt, path in inline_imgs:
                target_idx = body_start_idx
                for end, idx in offsets:
                    target_idx = idx
                    if offset <= end:
                        break
                if "image_path" not in tweets[target_idx]:
                    tweets[target_idx]["image_path"] = path
                    tweets[target_idx]["image_hint"] = alt or tweets[target_idx].get("image_hint")

        return tweets


class TwitterSingleAdapter(TwitterAdapter):
    platform_name = "twitter_single"

    def _form(self) -> str:
        return "single"

    def _build_tweets(
        self,
        body: str,
        h1: str | None,
        cover_path: str | None,
        inline_imgs: list[tuple[int, str, str]],
    ) -> list[dict[str, Any]]:
        opener = (h1 or "").strip()
        # Take the first sentence of the body; combine with H1 if room.
        first_sentence = ""
        for sent in _SENT_END_RE.split(body):
            sent = sent.strip()
            if sent:
                first_sentence = sent
                break
        if opener and first_sentence and len(opener) + 2 + len(first_sentence) <= _MAX_CHARS:
            text = f"{opener}\n\n{first_sentence}"
        elif opener:
            text = opener[:_MAX_CHARS]
        else:
            text = (first_sentence or body.strip())[:_MAX_CHARS]
        text = text[:_MAX_CHARS].rstrip()

        tweet: dict[str, Any] = {
            "text": text,
            "image_slot": None,
            "image_hint": None,
        }
        if cover_path:
            tweet["image_path"] = cover_path
            tweet["image_hint"] = "cover"
        return [tweet]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_h1(md: str) -> tuple[str, str | None]:
    """Pop the first ``# ``-headed line out of ``md``; return (rest, h1_text)."""
    lines = md.splitlines()
    h1: str | None = None
    out: list[str] = []
    for line in lines:
        if h1 is None and line.lstrip().startswith("# ") and not line.lstrip().startswith("##"):
            h1 = line.lstrip()[2:].strip()
            continue
        out.append(line)
    return ("\n".join(out).strip(), h1)


def _extract_inline_images(md: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Pull ``![alt](path)`` embeds out of the body, returning the cleaned
    body + a list of ``(char_offset_in_cleaned_body, alt, path)`` tuples.
    """
    imgs: list[tuple[int, str, str]] = []
    out = []
    pos = 0
    cleaned_len = 0
    for m in _IMG_EMBED_RE.finditer(md):
        out.append(md[pos:m.start()])
        cleaned_len += m.start() - pos
        imgs.append((cleaned_len, m.group(1).strip(), m.group(2).strip()))
        pos = m.end()
    out.append(md[pos:])
    cleaned = "".join(out)
    # Tidy up triple newlines created by removed embeds.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, imgs


def _split_paragraph(para: str, limit: int) -> list[str]:
    """Greedy splitter: paragraph-then-sentence-then-hard-cut."""
    para = para.strip()
    if not para:
        return []
    if len(para) <= limit:
        return [para]

    sentences = [s for s in _SENT_END_RE.split(para) if s and s.strip()]
    chunks: list[str] = []
    buf = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        # Sentence by itself exceeds limit → hard-cut it.
        if len(sent) > limit:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            chunks.extend(_hard_cut(sent, limit))
            continue
        if buf and len(buf) + 1 + len(sent) > limit:
            chunks.append(buf.strip())
            buf = sent
        else:
            buf = (buf + " " + sent).strip() if buf else sent
    if buf:
        chunks.append(buf.strip())
    return chunks


def _hard_cut(text: str, limit: int) -> list[str]:
    """Split at last terminator before ``limit``; final fallback is a hard cut."""
    out: list[str] = []
    while len(text) > limit:
        slice_ = text[:limit]
        # Prefer the last terminator inside the limit.
        idx = max(slice_.rfind(c) for c in "。！？.!?")
        if idx <= 0 or idx < limit // 2:
            idx = limit - 1
        out.append(text[: idx + 1].strip())
        text = text[idx + 1:].lstrip()
    if text:
        out.append(text.strip())
    return out
