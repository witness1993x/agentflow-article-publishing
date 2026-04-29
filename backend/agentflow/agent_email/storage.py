"""Disk I/O for newsletters under ``~/.agentflow/newsletters/<nid>/``.

Layout:

    ~/.agentflow/newsletters/<nid>/
      metadata.json       — all structured fields (subject, preview_text, ids, status)
      content.html        — final HTML body (with {unsubscribe_link} placeholder)
      content.txt         — plain-text fallback body
      subject.txt         — subject line, one file for quick cat
      images/             — reserved for inline image uploads (v0.3)

Newsletter id format: ``nl_<YYYYMMDDHHMMSS>_<hash8>``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs


def newsletters_root() -> Path:
    ensure_user_dirs()
    root = agentflow_home() / "newsletters"
    root.mkdir(parents=True, exist_ok=True)
    return root


def newsletter_dir(newsletter_id: str) -> Path:
    return newsletters_root() / newsletter_id


def make_newsletter_id(seed: str | None = None) -> str:
    """``nl_<UTC ts 14>_<hash8>``. Seed stabilises the hash for tests."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    basis = (seed or stamp).encode("utf-8")
    short = hashlib.sha1(basis).hexdigest()[:8]
    return f"nl_{stamp}_{short}"


def save_newsletter(
    newsletter_id: str,
    subject: str,
    preview_text: str,
    html_body: str,
    plain_text_body: str,
    *,
    article_id: str | None = None,
    images_used: list[str] | None = None,
    status: str = "draft",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write all files for a newsletter. Returns the newsletter directory."""
    d = newsletter_dir(newsletter_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "images").mkdir(parents=True, exist_ok=True)

    (d / "subject.txt").write_text(subject, encoding="utf-8")
    (d / "content.html").write_text(html_body, encoding="utf-8")
    (d / "content.txt").write_text(plain_text_body, encoding="utf-8")

    metadata_path = d / "metadata.json"
    existing: dict[str, Any] = {}
    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}

    now_iso = datetime.now(timezone.utc).isoformat()
    metadata = {
        **existing,
        "newsletter_id": newsletter_id,
        "article_id": article_id or existing.get("article_id"),
        "subject": subject,
        "preview_text": preview_text,
        "images_used": list(images_used or existing.get("images_used") or []),
        "status": status,
        "created_at": existing.get("created_at") or now_iso,
        "updated_at": now_iso,
    }
    if extra:
        metadata.update(extra)

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return d


def load_newsletter(newsletter_id: str) -> dict[str, Any]:
    """Return the newsletter as a merged dict (metadata + html/txt/subject)."""
    d = newsletter_dir(newsletter_id)
    metadata_path = d / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(
            f"No metadata.json for newsletter {newsletter_id} at {d}"
        )
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    html_path = d / "content.html"
    txt_path = d / "content.txt"
    subj_path = d / "subject.txt"
    data["html_body"] = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    data["plain_text_body"] = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
    if subj_path.exists() and not data.get("subject"):
        data["subject"] = subj_path.read_text(encoding="utf-8").strip()
    return data


def list_newsletters() -> list[dict[str, Any]]:
    root = newsletters_root()
    out: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        metadata_path = child / "metadata.json"
        if not metadata_path.exists():
            continue
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(
            {
                "newsletter_id": data.get("newsletter_id") or child.name,
                "article_id": data.get("article_id"),
                "subject": data.get("subject"),
                "status": data.get("status", "draft"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
            }
        )
    return out
