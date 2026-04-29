"""Twitter draft generation — single tweet or thread from a hotspot / article."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from agentflow.agent_tw import storage
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.hotspot_store import find_hotspot_record
from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger

_log = get_logger("agent_tw.drafter")


def _load_style_profile_yaml() -> str:
    path = agentflow_home() / "style_profile.yaml"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "version: '1.0'\n"


def _load_hotspot(hotspot_id: str) -> dict[str, Any] | None:
    try:
        hotspot, _ = find_hotspot_record(
            hotspot_id,
            limit_days=None,
            include_search_results=True,
        )
        return hotspot
    except KeyError:
        return None


def _load_article(article_id: str) -> dict[str, Any] | None:
    d = agentflow_home() / "drafts" / article_id
    if not d.exists():
        return None
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    draft_md = ""
    draft_p = d / "draft.md"
    if draft_p.exists():
        draft_md = draft_p.read_text(encoding="utf-8")
    meta["_draft_md"] = draft_md
    return meta


def _format_source_from_hotspot(h: dict[str, Any], angle_index: int | None) -> str:
    chunks: list[str] = []
    chunks.append(f"TOPIC: {h.get('topic_one_liner', '')}")
    sa = h.get("suggested_angles") or []
    if sa:
        chunks.append("ANGLES:")
        for i, a in enumerate(sa):
            marker = " *" if angle_index == i else "  "
            if isinstance(a, dict):
                chunks.append(f"{marker}[{i}] {a.get('title', '')}: {a.get('hook', '')}")
    refs = h.get("source_references") or []
    if refs:
        chunks.append("REFERENCES:")
        for i, r in enumerate(refs[:5]):
            if isinstance(r, dict):
                auth = r.get("author", "")
                url = r.get("url", "")
                snip = (r.get("text_snippet") or r.get("title") or "")[:120]
                chunks.append(f"  [{i}] {auth} {url}\n      \"{snip}\"")
    return "\n".join(chunks)


def _format_source_from_article(a: dict[str, Any]) -> str:
    chunks: list[str] = []
    title = ""
    md = a.get("_draft_md") or ""
    for line in md.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    chunks.append(f"ARTICLE TITLE: {title}")
    chunks.append("")
    # First 1500 chars of body
    body_start = md.find("\n\n") + 2 if "\n\n" in md else 0
    chunks.append(md[body_start : body_start + 1500])
    return "\n".join(chunks)


def _render_prompt(
    *,
    form: str,
    source_type: str,
    source_content: str,
    user_handle: str,
    angle_index: int | None,
    style_profile_yaml: str,
) -> str:
    prompt_path = (
        Path(__file__).resolve().parents[2] / "prompts" / "twitter_draft.md"
    )
    template = prompt_path.read_text(encoding="utf-8")
    # The prompt is a spec; we concatenate inputs below.
    values = [
        template,
        "",
        "## ACTUAL INPUT",
        f"- form: {form}",
        f"- source_type: {source_type}",
        f"- user_handle: {user_handle}",
        f"- target_angle_index: {angle_index if angle_index is not None else 'null'}",
        "",
        "### source_content",
        source_content,
        "",
        "### style_profile_yaml",
        "```yaml",
        style_profile_yaml,
        "```",
    ]
    return "\n".join(values)


def _validate_and_trim(payload: dict[str, Any], form: str) -> dict[str, Any]:
    tweets = payload.get("tweets") or []
    if not isinstance(tweets, list) or not tweets:
        raise ValueError("tweets payload missing or empty")
    cleaned: list[dict[str, Any]] = []
    for i, t in enumerate(tweets):
        text = (t.get("text") or "").strip()
        if not text:
            continue
        cleaned.append(
            {
                "index": i,
                "text": text,
                "char_count": len(text),
                "image_slot": t.get("image_slot"),
                "image_hint": t.get("image_hint"),
            }
        )
    if form == "single" and len(cleaned) > 1:
        cleaned = cleaned[:1]
    return {
        "form": form,
        "tweets": cleaned,
        "intended_hook": payload.get("intended_hook", ""),
        "source_refs": payload.get("source_refs") or [],
    }


async def draft_tweet(
    *,
    hotspot_id: str | None = None,
    article_id: str | None = None,
    form: str = "single",
    angle_index: int | None = None,
    user_handle: str | None = None,
) -> dict[str, Any]:
    """Draft a single tweet or thread.

    Must pass exactly one of ``hotspot_id`` or ``article_id``. Saves to
    ``~/.agentflow/tweets/<tweet_id>/`` and returns the full payload dict.
    """
    if bool(hotspot_id) == bool(article_id):
        raise ValueError("pass exactly one of hotspot_id or article_id")
    if form not in {"single", "thread"}:
        raise ValueError(f"form must be 'single' or 'thread', got {form!r}")

    user_handle = user_handle or os.environ.get("TWITTER_HANDLE") or "@me"

    if hotspot_id:
        h = _load_hotspot(hotspot_id)
        if not h:
            raise FileNotFoundError(f"hotspot not found: {hotspot_id}")
        source_content = _format_source_from_hotspot(h, angle_index)
        source_type = "hotspot"
        source_id = hotspot_id
    else:
        a = _load_article(article_id or "")
        if not a:
            raise FileNotFoundError(f"article not found: {article_id}")
        source_content = _format_source_from_article(a)
        source_type = "article"
        source_id = article_id

    prompt = _render_prompt(
        form=form,
        source_type=source_type,
        source_content=source_content,
        user_handle=user_handle,
        angle_index=angle_index,
        style_profile_yaml=_load_style_profile_yaml(),
    )

    client = LLMClient()
    raw = await client.chat_json(
        prompt_family="twitter-draft",
        prompt=prompt,
        max_tokens=2000,
    )
    # LLM may return with wrong form — coerce.
    if raw.get("form") not in {"single", "thread"}:
        raw["form"] = form

    payload = _validate_and_trim(raw, form)

    tweet_id = storage.new_tweet_id()
    to_save = {
        **payload,
        "source_type": source_type,
        "source_id": source_id,
        "status": "draft",
    }
    storage.save(tweet_id, to_save)
    to_save["tweet_id"] = tweet_id
    _log.info(
        "drafted %s %s: tweets=%d hook=%r",
        form,
        tweet_id,
        len(payload["tweets"]),
        payload["intended_hook"][:60],
    )
    return to_save


async def edit_tweet(
    tweet_id: str,
    *,
    index: int | None = None,
    command: str | None = None,
    split_at: int | None = None,
    merge: tuple[int, int] | None = None,
    reorder: list[int] | None = None,
) -> dict[str, Any]:
    """Apply one structural or LLM-based edit and persist. Exactly one op per call."""
    ops_set = sum(x is not None for x in (command, split_at, merge, reorder))
    if ops_set != 1:
        raise ValueError("pass exactly one of --command / --split / --merge / --reorder")

    meta = storage.load(tweet_id)
    tweets: list[dict[str, Any]] = meta.get("tweets") or []

    if reorder is not None:
        if sorted(reorder) != list(range(len(tweets))):
            raise ValueError(
                f"reorder {reorder} must be a permutation of 0..{len(tweets) - 1}"
            )
        tweets = [tweets[i] for i in reorder]
    elif merge is not None:
        i, j = merge
        if j != i + 1 or i < 0 or j >= len(tweets):
            raise ValueError(f"merge must be consecutive indices; got {merge}")
        combined = (tweets[i]["text"] + " " + tweets[j]["text"]).strip()
        tweets[i] = {**tweets[i], "text": combined, "char_count": len(combined)}
        tweets.pop(j)
    elif split_at is not None:
        if split_at < 0 or split_at >= len(tweets):
            raise ValueError(f"split_at index {split_at} out of range")
        text = tweets[split_at]["text"]
        mid = len(text) // 2
        # prefer a sentence-ish boundary
        for sep in ("。", ". ", "！", "? ", "？", "\n"):
            idx = text.find(sep, mid - 40)
            if 0 < idx < len(text) - 10:
                mid = idx + len(sep)
                break
        left = text[:mid].strip()
        right = text[mid:].strip()
        tweets[split_at] = {**tweets[split_at], "text": left, "char_count": len(left)}
        tweets.insert(
            split_at + 1,
            {"index": split_at + 1, "text": right, "char_count": len(right)},
        )
    elif command is not None:
        if index is None or index < 0 or index >= len(tweets):
            raise ValueError(f"--index must be a valid tweet index for --command")
        client = LLMClient()
        edit_prompt = (
            f"你在编辑一条 tweet。用户命令: {command!r}\n\n"
            f"当前内容 ({tweets[index]['char_count']} 字符):\n{tweets[index]['text']}\n\n"
            f"输出要求: 只输出改写后的 tweet 正文,一行,不带引号,不带解释。"
            f"字数控制在 220-275 字符。"
        )
        new_text = await client.chat_text(
            prompt_family="d2-edit",  # reuse existing mock
            prompt=edit_prompt,
            max_tokens=400,
        )
        new_text = new_text.strip().strip('"').strip("'")
        tweets[index] = {
            **tweets[index],
            "text": new_text,
            "char_count": len(new_text),
        }

    # Re-index.
    for i, t in enumerate(tweets):
        t["index"] = i

    storage.save(
        tweet_id,
        {
            **meta,
            "tweets": tweets,
            "status": meta.get("status") or "draft",
        },
    )
    return {"tweet_id": tweet_id, "tweets": tweets}
