"""Persistent table of "uid is in the middle of editing article X" so the
next plain-text reply from that uid is parsed as an edit instruction rather
than treated as a generic message.

Storage: ``~/.agentflow/review/pending_edits.json``.

TTL: 30 minutes by default. Expired entries are cleared on read.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home


_LOCK = threading.Lock()
_FILENAME = "pending_edits.json"


def _path() -> Path:
    p = agentflow_home() / "review" / _FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read() -> dict[str, dict[str, Any]]:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict[str, dict[str, Any]]) -> None:
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_expired(entry: dict[str, Any]) -> bool:
    expires_at = entry.get("expires_at")
    if not expires_at:
        return True
    try:
        ts = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    return _now() >= ts


def register(
    *,
    uid: int,
    article_id: str,
    gate: str,
    short_id: str,
    ttl_minutes: int = 30,
) -> None:
    with _LOCK:
        data = _read()
        data[str(uid)] = {
            "article_id": article_id,
            "gate": gate,
            "short_id": short_id,
            "registered_at": _now().isoformat(),
            "expires_at": (_now() + timedelta(minutes=ttl_minutes)).isoformat(),
        }
        _write(data)


def take(uid: int) -> dict[str, Any] | None:
    """Pop and return the pending entry for uid (consume-once).

    Returns None if no pending entry or expired."""
    with _LOCK:
        data = _read()
        entry = data.pop(str(uid), None)
        if entry is None:
            return None
        if _is_expired(entry):
            _write(data)
            return None
        _write(data)
        return entry


def peek(uid: int) -> dict[str, Any] | None:
    """Read without consuming. None if missing or expired."""
    with _LOCK:
        data = _read()
        entry = data.get(str(uid))
        if entry is None or _is_expired(entry):
            return None
        return entry


def gc() -> int:
    with _LOCK:
        data = _read()
        kept: dict[str, dict[str, Any]] = {}
        removed = 0
        for k, v in data.items():
            if _is_expired(v):
                removed += 1
                continue
            kept[k] = v
        if removed:
            _write(kept)
        return removed
