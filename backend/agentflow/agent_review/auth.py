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
_LARK_FILENAME = "lark_auth.json"

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


# ---------------------------------------------------------------------------
# Lark-side authorization (parallel to the TG uid model).
#
# Lark identifies operators by ``open_id`` (chat-scoped string), so we keep a
# separate allowlist file. The implicit operator is the open_id in
# ``LARK_OPERATOR_OPEN_ID`` env (mirrors ``TELEGRAM_REVIEW_CHAT_ID``).
# Stored in ``~/.agentflow/review/lark_auth.json``:
#
#   {"authorized_open_ids": [{"open_id": "ou_xxx", "name": "Alice",
#                              "allowed_actions": ["review","edit"]}]}
#
# Reuses :data:`ACTION_VOCABULARY` so the (gate, action) → required map in
# ``daemon._ACTION_REQ`` works without translation.
# ---------------------------------------------------------------------------


def _lark_path() -> Path:
    p = agentflow_home() / "review" / _LARK_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _lark_read() -> dict[str, Any]:
    p = _lark_path()
    if not p.exists():
        return {"authorized_open_ids": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {"authorized_open_ids": []}
    if not isinstance(data, dict):
        return {"authorized_open_ids": []}
    return data


def lark_operator_open_id() -> str | None:
    raw = (os.environ.get("LARK_OPERATOR_OPEN_ID") or "").strip()
    return raw or None


def is_lark_authorized(open_id: str | None, action: str | None = None) -> bool:
    """Authorization check for Lark operators.

    Mirrors :func:`is_authorized`: ``action=None`` means "is this open_id known
    at all"; an action verb checks the per-grant allowlist. Operator open_id
    (env ``LARK_OPERATOR_OPEN_ID``) implicitly has ``["*"]``.

    When the allowlist file has no entries AND no operator env is set, the
    gate is open (legacy behaviour for fresh installs that haven't onboarded
    a Lark operator yet — matches TG's "any uid is fine if file empty"
    intent). Operators wanting a closed default should set
    ``LARK_OPERATOR_OPEN_ID`` first.
    """
    if open_id is None or not str(open_id).strip():
        return False
    op = lark_operator_open_id()
    if op is not None and str(open_id) == op:
        return True
    data = _lark_read()
    entries = data.get("authorized_open_ids") or []
    if not entries and op is None:
        return True
    for entry in entries:
        try:
            if str(entry.get("open_id")) != str(open_id):
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


def lark_add(
    open_id: str,
    *,
    name: str | None = None,
    allowed_actions: list[str] | None = None,
) -> bool:
    """Add or update a Lark operator grant. Returns True if a new entry was
    appended (existing ones are updated in place)."""
    actions = _normalize_actions(allowed_actions)
    data = _lark_read()
    entries = data.setdefault("authorized_open_ids", [])
    now = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        if str(entry.get("open_id")) == str(open_id):
            entry["name"] = name or entry.get("name")
            entry["allowed_actions"] = actions
            entry["updated_at"] = now
            _lark_write(data)
            return False
    entries.append({
        "open_id": str(open_id),
        "name": name,
        "allowed_actions": actions,
        "added_at": now,
    })
    _lark_write(data)
    return True


def lark_remove(open_id: str) -> bool:
    data = _lark_read()
    entries = data.get("authorized_open_ids") or []
    new_entries = [e for e in entries if str(e.get("open_id")) != str(open_id)]
    if len(new_entries) == len(entries):
        return False
    data["authorized_open_ids"] = new_entries
    _lark_write(data)
    return True


def _lark_write(data: dict[str, Any]) -> None:
    _lark_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
