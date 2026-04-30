"""Short-id indirection table for Telegram callback_data.

Telegram's callback_data caps at 64 bytes; full article ids are ~50 chars.
We mint a 6-hex short_id per Gate post, persist a JSON map back to the real
target (article_id or hotspot batch path), and resolve at callback time.

Storage: ``~/.agentflow/review/short_id_index.json``.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home


_LOCK = threading.Lock()
_FILENAME = "short_id_index.json"


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


def _new_id(existing: dict[str, Any]) -> str:
    for _ in range(10):
        sid = secrets.token_hex(3)  # 6 hex chars, ~16M space
        if sid not in existing:
            return sid
    raise RuntimeError("could not allocate unique short_id (improbable)")


def register(
    *,
    gate: str,
    article_id: str | None = None,
    batch_path: str | None = None,
    ttl_hours: float = 24,
    extra: dict[str, Any] | None = None,
) -> str:
    """Mint a fresh short_id pointing at either an article_id or a hotspot batch.

    Pass ``article_id`` for Gate B/C, ``batch_path`` for Gate A.
    """
    if not (article_id or batch_path):
        raise ValueError("register: must pass article_id or batch_path")
    with _LOCK:
        index = _read()
        sid = _new_id(index)
        entry = {
            "gate": gate,
            "created_at": _now_iso(),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
            ).isoformat(),
        }
        if article_id:
            entry["article_id"] = article_id
        if batch_path:
            entry["batch_path"] = batch_path
        if extra:
            entry["extra"] = extra
        index[sid] = entry
        _write(index)
        return sid


def resolve(short_id: str) -> dict[str, Any] | None:
    """Return the entry for short_id, or None if missing / expired / revoked."""
    with _LOCK:
        index = _read()
        entry = index.get(short_id)
        if not entry:
            return None
        if _is_expired(entry):
            return None
        if entry.get("revoked_at"):
            return None
        return entry


def peek_raw(short_id: str) -> dict[str, Any] | None:
    """Return the raw entry without applying expired/revoke gating.

    Used by callback handlers to distinguish three cases for an unresolvable
    short_id: (1) sid never existed, (2) sid existed but TTL'd, (3) sid was
    revoked. Plain :func:`resolve` collapses all three into None which makes
    user-facing error messages misleading.
    """
    with _LOCK:
        return _read().get(short_id)


def attach_message_id(short_id: str, tg_message_id: int | None) -> bool:
    """v1.0.16: stamp the Telegram message_id of the card the sid was
    rendered into onto the entry. Used by triggers._revoke_prior_card_keyboard
    to clear the inline keyboard on a stale card before sending a fresh
    one. Returns False if the entry is missing / expired / revoked.
    """
    if tg_message_id is None:
        return False
    with _LOCK:
        index = _read()
        entry = index.get(short_id)
        if not entry or _is_expired(entry) or entry.get("revoked_at"):
            return False
        entry["tg_message_id"] = int(tg_message_id)
        index[short_id] = entry
        _write(index)
        return True


def set_extra(short_id: str, key: str, value: Any) -> bool:
    """Mutate a single key in entry['extra']. Returns False if entry missing/expired.

    Threadsafe — uses the module ``_LOCK``. The entry is rewritten in-place,
    so callers must read back via :func:`resolve` to see the new value. Used
    by Gate D toggles to persist per-card multi-select state across clicks.
    """
    with _LOCK:
        index = _read()
        entry = index.get(short_id)
        if not entry or _is_expired(entry) or entry.get("revoked_at"):
            return False
        bag = entry.get("extra")
        if not isinstance(bag, dict):
            bag = {}
        bag[key] = value
        entry["extra"] = bag
        index[short_id] = entry
        _write(index)
        return True


def find_active(
    *,
    gate: str,
    article_id: str | None = None,
    batch_path: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Return the newest active short_id matching a gate target, if any."""
    with _LOCK:
        matches: list[tuple[str, dict[str, Any]]] = []
        for sid, entry in _read().items():
            if entry.get("gate") != gate:
                continue
            if article_id is not None and entry.get("article_id") != article_id:
                continue
            if batch_path is not None and entry.get("batch_path") != batch_path:
                continue
            if _is_expired(entry) or entry.get("revoked_at"):
                continue
            matches.append((sid, entry))
        if not matches:
            return None
        matches.sort(key=lambda item: str(item[1].get("created_at") or ""), reverse=True)
        return matches[0]


def revoke(short_id: str) -> None:
    """Soft-revoke a short_id: keep entry in index but stamp ``revoked_at``.

    The entry is preserved (briefly) so that duplicate / replayed Telegram
    callback_query deliveries can be distinguished from genuinely-expired
    short_ids — ``resolve`` still returns None, but
    :func:`was_recently_revoked` can tell the callback handler "operation
    already succeeded; this is a replay" instead of "已失效".

    Stale revoked entries are reaped by :func:`gc` (60s window).
    """
    with _LOCK:
        index = _read()
        entry = index.get(short_id)
        if not entry:
            return
        entry["revoked_at"] = _now_iso()
        index[short_id] = entry
        _write(index)


def was_recently_revoked(short_id: str, *, within_seconds: float = 600.0) -> bool:
    """Return True iff entry exists, has revoked_at, and revoked within window.
    Used by callback handler to distinguish '已处理 (重复点击)' vs '已失效'.

    Default window is 600s (10 min): TG retransmits a callback after the first
    answer_callback_query for several minutes when network is flaky, and slow
    spawn paths (Atlas image-gate, multi-platform dispatch) can keep the user
    looking at a card for >60s. The previous 60s window was too narrow and
    caused legitimate replays to surface as "已失效" alarms.
    """
    with _LOCK:
        index = _read()
        entry = index.get(short_id)
        if not entry:
            return False
        ts = entry.get("revoked_at")
        if not ts:
            return False
        try:
            revoked = datetime.fromisoformat(ts)
        except ValueError:
            return False
        elapsed = (datetime.now(timezone.utc) - revoked).total_seconds()
        return elapsed <= within_seconds


def gc() -> int:
    """Drop expired entries and stale (>60s) revoked entries. Returns count."""
    with _LOCK:
        index = _read()
        kept: dict[str, Any] = {}
        removed = 0
        for sid, entry in index.items():
            if _is_expired(entry) or _is_revoked_stale(entry):
                removed += 1
                continue
            kept[sid] = entry
        if removed:
            _write(kept)
        return removed


def _is_expired(entry: dict[str, Any]) -> bool:
    expires_at = entry.get("expires_at")
    if not expires_at:
        return False
    try:
        ts = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    return datetime.now(timezone.utc) >= ts


def _is_revoked_stale(entry: dict[str, Any], window_seconds: float = 600.0) -> bool:
    """True if entry was soft-revoked more than ``window_seconds`` ago.

    Must stay >= the ``was_recently_revoked`` window — gc must NOT reap a
    revoked entry while the callback handler still wants to recognise it as
    a replay (otherwise replays surface as "未知 callback").
    """
    ts = entry.get("revoked_at")
    if not ts:
        return False
    try:
        revoked = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - revoked).total_seconds() > window_seconds
