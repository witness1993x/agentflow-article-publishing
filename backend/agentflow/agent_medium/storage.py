"""Storage helpers for Medium semi-automatic publishing artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs


def medium_root() -> Path:
    ensure_user_dirs()
    root = agentflow_home() / "medium"
    root.mkdir(parents=True, exist_ok=True)
    return root


def medium_dir(article_id: str) -> Path:
    target = medium_root() / article_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def save_json_artifact(article_id: str, name: str, data: dict[str, Any]) -> Path:
    target = medium_dir(article_id) / name
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return target


def save_text_artifact(article_id: str, name: str, content: str) -> Path:
    target = medium_dir(article_id) / name
    target.write_text(content, encoding="utf-8")
    return target


def load_json_artifact(article_id: str, name: str) -> dict[str, Any]:
    target = medium_dir(article_id) / name
    if not target.exists():
        raise FileNotFoundError(f"medium artifact not found: {target}")
    return json.loads(target.read_text(encoding="utf-8"))
