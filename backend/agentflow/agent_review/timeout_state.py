"""Idempotency book-keeping for the review-daemon timeout sweeper.

Tracks which articles have already been pinged (and which have had a second
action — ping or auto-fallback — taken) so re-running ``_scan_timeouts``
every ~60s never re-spams the operator.

Storage: ``~/.agentflow/review/timeout_state.json``.

Schema::

    {
      "<article_id>": {
        "first_pinged_at": "<iso8601>",
        "second_action_taken_at": "<iso8601> | null"
      },
      ...
    }
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home


_LOCK = threading.Lock()
_FILENAME = "timeout_state.json"


def _path() -> Path:
    p = agentflow_home() / "review" / _FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def mark_first_pinged(article_id: str) -> None:
    """Record that the first stale-cutoff ping was sent for ``article_id``.

    Idempotent: re-calling preserves the original ``first_pinged_at``."""
    with _LOCK:
        data = _read()
        entry = data.get(article_id) or {}
        if not entry.get("first_pinged_at"):
            entry["first_pinged_at"] = _now_iso()
        entry.setdefault("second_action_taken_at", None)
        data[article_id] = entry
        _write(data)


def mark_second_action(article_id: str) -> None:
    """Record that the second escalation (stronger ping or auto-fallback) ran.

    Idempotent: re-calling preserves the original ``second_action_taken_at``."""
    with _LOCK:
        data = _read()
        entry = data.get(article_id) or {}
        if not entry.get("first_pinged_at"):
            entry["first_pinged_at"] = _now_iso()
        if not entry.get("second_action_taken_at"):
            entry["second_action_taken_at"] = _now_iso()
        data[article_id] = entry
        _write(data)


def has_first_pinged(article_id: str) -> bool:
    with _LOCK:
        entry = _read().get(article_id) or {}
        return bool(entry.get("first_pinged_at"))


def has_second_action(article_id: str) -> bool:
    with _LOCK:
        entry = _read().get(article_id) or {}
        return bool(entry.get("second_action_taken_at"))


def clear(article_id: str) -> None:
    """Drop any timeout-tracking state for ``article_id``.

    Called when the article advances out of a ``*_pending_review`` state so a
    future re-entry pings again from scratch."""
    with _LOCK:
        data = _read()
        if article_id in data:
            data.pop(article_id, None)
            _write(data)


def get(article_id: str) -> dict[str, Any] | None:
    """Return the stored entry for ``article_id`` or None.

    Used by ``af review-list`` to surface "pinged" / "auto-skipped" badges."""
    with _LOCK:
        entry = _read().get(article_id)
        return dict(entry) if entry else None
