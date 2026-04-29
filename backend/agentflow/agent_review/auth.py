"""uid authorization gate for the Telegram review bot.

Default policy: closed. Only the operator (whose Telegram uid equals
``TELEGRAM_REVIEW_CHAT_ID``) is implicitly allowed (with ``["*"]``).
Every other uid must be explicitly added via ``af review-auth-add``.

Per-action grants: each entry carries an optional ``allowed_actions``
drawn from :data:`ACTION_VOCABULARY` (``"*"`` = full access). Legacy
entries without that key are treated as ``["*"]`` and the file is only
re-written when the entry is touched. See agentflow-deploy/SECURITY.md
for the full ``(gate, action) → required`` map.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home


_FILENAME = "auth.json"

# Closed action vocabulary. Keep in sync with agentflow-deploy/SECURITY.md.
#
# v1.0.4 added ``system`` for operator-completeness slash commands that
# mutate user-data layer config (profile init/switch, hotspot scan trigger,
# daemon restart). Default-grant only to the implicit operator uid.
ACTION_VOCABULARY: tuple[str, ...] = (
    "review",
    "write",
    "edit",
    "image",
    "publish",
    "system",
    "*",
)


def _path() -> Path:
    p = agentflow_home() / "review" / _FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {"authorized_uids": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {"authorized_uids": []}
    if not isinstance(data, dict):
        return {"authorized_uids": []}
    return data


def _write(data: dict[str, Any]) -> None:
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_actions(actions: list[str] | None) -> list[str]:
    """Validate + dedupe actions. ``None`` → ``["*"]``. Raises ``ValueError``
    on any token outside :data:`ACTION_VOCABULARY`."""
    if actions is None:
        return ["*"]
    seen: list[str] = []
    for a in actions:
        token = (a or "").strip().lower()
        if not token:
            continue
        if token not in ACTION_VOCABULARY:
            raise ValueError(
                f"unknown action {token!r}; allowed: "
                f"{', '.join(ACTION_VOCABULARY)}"
            )
        if token not in seen:
            seen.append(token)
    return seen or ["*"]


def _entry_actions(entry: dict[str, Any]) -> list[str]:
    """Read effective actions for a stored grant. Missing/empty → ``["*"]``."""
    raw = entry.get("allowed_actions")
    if not raw or not isinstance(raw, list):
        return ["*"]
    out = [str(a).strip().lower() for a in raw if str(a).strip()]
    return out or ["*"]


def operator_uid() -> int | None:
    """Return the implicit operator uid (== review chat_id), if known."""
    raw = os.environ.get("TELEGRAM_REVIEW_CHAT_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    # Fallback to captured config
    cfg_path = agentflow_home() / "review" / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
            cid = cfg.get("review_chat_id")
            if cid is not None:
                return int(cid)
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            return None
    return None


def is_authorized(uid: int | None, action: str | None = None) -> bool:
    """Authorization check.

    - ``action=None``  → "is uid known at all" (used for /start gating).
    - ``action="..."`` → "is uid allowed to perform this verb". A grant of
      ``*`` always wins; the operator implicitly has ``*``.
    """
    if uid is None:
        return False
    op = operator_uid()
    if op is not None and int(uid) == int(op):
        return True
    data = _read()
    for entry in data.get("authorized_uids") or []:
        try:
            if int(entry.get("uid")) != int(uid):
                continue
        except (TypeError, ValueError):
            continue
        if action is None:
            return True
        allowed = _entry_actions(entry)
        if "*" in allowed or action in allowed:
            return True
        return False
    return False


def add(
    uid: int,
    note: str | None = None,
    actions: list[str] | None = None,
) -> dict[str, Any]:
    """Add or update an authorized uid. ``actions=None`` → ``["*"]``.

    If the uid already exists, ``note`` is filled in only when previously
    blank, and ``actions`` (when provided) overwrites ``allowed_actions``.
    """
    normalized = _normalize_actions(actions)
    data = _read()
    items = data.setdefault("authorized_uids", [])
    for entry in items:
        try:
            if int(entry.get("uid")) == int(uid):
                if note and not entry.get("note"):
                    entry["note"] = note
                if actions is not None:
                    entry["allowed_actions"] = normalized
                elif "allowed_actions" not in entry:
                    entry["allowed_actions"] = normalized
                _write(data)
                return data
        except (TypeError, ValueError):
            continue
    items.append({
        "uid": int(uid),
        "note": note,
        "allowed_actions": normalized,
        "authorized_at": datetime.now(timezone.utc).isoformat(),
    })
    _write(data)
    return data


def set_actions(uid: int, actions: list[str]) -> bool:
    """Overwrite ``allowed_actions`` for an existing grant. Returns False
    if the uid wasn't found."""
    normalized = _normalize_actions(actions)
    data = _read()
    items = data.get("authorized_uids") or []
    for entry in items:
        try:
            if int(entry.get("uid")) == int(uid):
                entry["allowed_actions"] = normalized
                _write(data)
                return True
        except (TypeError, ValueError):
            continue
    return False


def remove(uid: int) -> bool:
    data = _read()
    items = list(data.get("authorized_uids") or [])
    kept: list[Any] = []
    removed = False
    for entry in items:
        try:
            if int(entry.get("uid")) == int(uid):
                removed = True
                continue
        except (TypeError, ValueError):
            pass
        kept.append(entry)
    if removed:
        data["authorized_uids"] = kept
        _write(data)
    return removed


def list_authorized() -> list[dict[str, Any]]:
    """Return stored grants. Each entry is annotated with the effective
    ``allowed_actions`` (legacy entries report ``["*"]`` even if the file
    hasn't been migrated yet)."""
    out: list[dict[str, Any]] = []
    for entry in (_read().get("authorized_uids") or []):
        clone = dict(entry)
        clone["allowed_actions"] = _entry_actions(entry)
        out.append(clone)
    return out
