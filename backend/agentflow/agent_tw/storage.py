"""Per-tweet on-disk layout under ``~/.agentflow/tweets/<tweet_id>/``."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import blake2b
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs


def tweets_root() -> Path:
    ensure_user_dirs()
    d = agentflow_home() / "tweets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tweet_dir(tweet_id: str) -> Path:
    d = tweets_root() / tweet_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "images").mkdir(parents=True, exist_ok=True)
    return d


def new_tweet_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    h = blake2b(uuid4().bytes, digest_size=4).hexdigest()
    return f"tw_{stamp}_{h}"


def save(tweet_id: str, payload: dict[str, Any]) -> Path:
    d = tweet_dir(tweet_id)

    metadata = {
        "tweet_id": tweet_id,
        "form": payload.get("form"),
        "source_type": payload.get("source_type"),
        "source_id": payload.get("source_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": payload.get("status", "draft"),
        "intended_hook": payload.get("intended_hook"),
        "source_refs": payload.get("source_refs") or [],
    }
    if "published_urls" in payload:
        metadata["published_urls"] = payload["published_urls"]
    if "thread_tweet_ids" in payload:
        metadata["thread_tweet_ids"] = payload["thread_tweet_ids"]
    if "published_at" in payload:
        metadata["published_at"] = payload["published_at"]

    (d / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (d / "tweets.json").write_text(
        json.dumps(payload.get("tweets") or [], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    md_lines: list[str] = [
        f"# Tweet {tweet_id}",
        "",
        f"Form: **{metadata['form']}**",
        f"Hook: {metadata.get('intended_hook') or '(none)'}",
        "",
    ]
    for t in payload.get("tweets") or []:
        md_lines.append(f"---")
        md_lines.append(
            f"## {int(t.get('index', 0)) + 1} "
            f"({t.get('char_count', len(t.get('text', '')))} chars)"
        )
        if t.get("image_slot"):
            hint = t.get("image_hint") or ""
            md_lines.append(f"_[image: {t['image_slot']}] {hint}_")
        md_lines.append("")
        md_lines.append(t.get("text", ""))
        md_lines.append("")
    (d / "tweets.md").write_text("\n".join(md_lines), encoding="utf-8")
    return d


def load(tweet_id: str) -> dict[str, Any]:
    d = tweet_dir(tweet_id)
    metadata = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    tweets = json.loads((d / "tweets.json").read_text(encoding="utf-8"))
    metadata["tweets"] = tweets
    return metadata


def update_status(tweet_id: str, **fields: Any) -> Path:
    d = tweet_dir(tweet_id)
    path = d / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.update(fields)
    path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def list_all() -> list[dict[str, Any]]:
    root = tweets_root()
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(root.iterdir()):
        meta_p = d / "metadata.json"
        if meta_p.exists():
            try:
                out.append(json.loads(meta_p.read_text(encoding="utf-8")))
            except Exception:
                continue
    return out
