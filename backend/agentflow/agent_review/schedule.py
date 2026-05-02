"""Daemon-internal time-slot scheduler for ``af hotspots``.

The legacy ``af review-cron-install`` is macOS-only (writes a launchd
plist). Linux deployments — including the autopost OpenClaw sandbox and
any systemd VM — had no working scheduling path, so twice-daily
hotspots scans never fired. This module replaces it with a cross-OS
internal scheduler driven by the daemon's existing 60-second
housekeeping tick.

Configuration (env-driven, no extra CLI flags needed):

* ``AGENTFLOW_HOTSPOTS_SCHEDULE`` — comma-separated ``HH:MM`` local
  times. Empty / unset = scheduler disabled.
  Example: ``"09:00,18:00"``.
* ``AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K`` — int, default 3.

State persistence:
``~/.agentflow/review/scheduled_state.json`` keyed by ``"HH:MM"`` →
ISO-8601 timestamp of the last fire. Used to skip a slot when daemon
is restarted within the same minute and to expose status via
``af review-schedule-status``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time as _time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.logger import get_logger


_log = get_logger("agent_review.schedule")

# How close to the slot's HH:MM we still count as "fire now". Keeps
# 60s housekeeping ticks from missing a slot when the tick lands a few
# seconds after the wall-clock minute.
_FIRE_WINDOW_SECONDS = 90.0


def _state_path() -> Path:
    p = agentflow_home() / "review" / "scheduled_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_state() -> dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(data: dict[str, Any]) -> None:
    _state_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _parse_schedule(raw: str | None) -> list[tuple[int, int]]:
    """Parse ``"09:00,18:00"`` into ``[(9, 0), (18, 0)]``. Bad slots
    are dropped with a warning so a typo doesn't bring the daemon down."""
    if not raw:
        return []
    out: list[tuple[int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            h_s, m_s = chunk.split(":")
            h, m = int(h_s), int(m_s)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError(f"out of range: {chunk}")
            out.append((h, m))
        except Exception as err:
            _log.warning("schedule slot %r dropped: %s", chunk, err)
    return out


def _slot_label(slot: tuple[int, int]) -> str:
    return f"{slot[0]:02d}:{slot[1]:02d}"


def _slot_due(
    slot: tuple[int, int],
    now: datetime,
    last_fired: datetime | None,
    *,
    window_seconds: float = _FIRE_WINDOW_SECONDS,
) -> bool:
    """True iff ``now`` is within ``window_seconds`` AFTER today's slot
    AND the slot has not already fired today.

    Symmetric only to the future side: if a daemon starts at 09:05 and
    the slot is 09:00, the slot is *missed* for the day (we don't
    backfire to avoid double-runs after long downtime).
    """
    today_slot = now.replace(
        hour=slot[0], minute=slot[1], second=0, microsecond=0,
    )
    delta = (now - today_slot).total_seconds()
    if delta < 0 or delta > window_seconds:
        return False
    if last_fired is None:
        return True
    if last_fired.tzinfo is None:
        last_fired = last_fired.replace(tzinfo=now.tzinfo or timezone.utc)
    return last_fired < today_slot


def due_slots(
    *,
    schedule: Iterable[tuple[int, int]] | None = None,
    now: datetime | None = None,
    state: dict[str, Any] | None = None,
) -> list[tuple[int, int]]:
    """Return the slots that should fire RIGHT NOW. Pure function — no
    side effects — so the daemon can call this in a hot loop and tests
    can drive it with arbitrary clock + state."""
    if schedule is None:
        schedule = _parse_schedule(
            os.environ.get("AGENTFLOW_HOTSPOTS_SCHEDULE"),
        )
    schedule = list(schedule)
    if not schedule:
        return []
    now = now or datetime.now().astimezone()
    state = state if state is not None else _read_state()
    out: list[tuple[int, int]] = []
    for slot in schedule:
        last_raw = state.get(_slot_label(slot))
        last_dt: datetime | None = None
        if isinstance(last_raw, str) and last_raw:
            try:
                last_dt = datetime.fromisoformat(last_raw)
            except ValueError:
                last_dt = None
        if _slot_due(slot, now, last_dt):
            out.append(slot)
    return out


def stamp_fire(slot: tuple[int, int], *, now: datetime | None = None) -> None:
    """Persist ``slot`` as fired at ``now`` (or wall-clock now)."""
    now = now or datetime.now().astimezone()
    state = _read_state()
    state[_slot_label(slot)] = now.isoformat()
    _write_state(state)


def fire_due(spawn: Callable[[int], None], *, top_k: int | None = None) -> list[str]:
    """Daemon entry point. Resolve ``AGENTFLOW_HOTSPOTS_SCHEDULE`` +
    ``AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K``, fire each due slot via
    ``spawn(top_k)``, stamp it, and return the list of fired slot
    labels for logging.

    ``spawn`` is injected so the daemon can pass its own ``_spawn_hotspots``
    without us importing it (avoids cross-module circular imports
    between schedule.py and daemon.py).
    """
    if top_k is None:
        try:
            top_k = int(os.environ.get("AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K", "3"))
        except (TypeError, ValueError):
            top_k = 3
    fired: list[str] = []
    for slot in due_slots():
        try:
            spawn(top_k)
        except Exception as err:  # pragma: no cover
            _log.warning(
                "scheduled hotspots spawn failed for slot %s: %s",
                _slot_label(slot), err,
            )
            continue
        stamp_fire(slot)
        fired.append(_slot_label(slot))
        _log.info("scheduled hotspots fired for slot %s", _slot_label(slot))
    return fired


def status() -> dict[str, Any]:
    """Snapshot for `af review-schedule-status` and ``af doctor``."""
    raw = os.environ.get("AGENTFLOW_HOTSPOTS_SCHEDULE", "")
    schedule = _parse_schedule(raw)
    state = _read_state()
    now = datetime.now().astimezone()
    rows: list[dict[str, Any]] = []
    for slot in schedule:
        label = _slot_label(slot)
        last_iso = state.get(label) or ""
        today_slot = now.replace(
            hour=slot[0], minute=slot[1], second=0, microsecond=0,
        )
        next_fire = today_slot if today_slot > now else today_slot + timedelta(days=1)
        rows.append({
            "slot": label,
            "last_fired_at": last_iso,
            "next_fire_at": next_fire.isoformat(),
        })
    return {
        "enabled": bool(schedule),
        "raw": raw,
        "top_k": int(os.environ.get("AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K", "3") or 3),
        "slots": rows,
        "now": now.isoformat(),
    }
