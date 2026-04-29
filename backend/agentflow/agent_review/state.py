"""Gate state machine — reads/writes ``metadata.json.gate_history``.

The single source of truth is the article's metadata.json. ``gate_history``
is an append-only list of transitions; the current gate state is the
``to_state`` of the last entry. The Telegram bot daemon only reads & writes
through these helpers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agentflow.shared.bootstrap import agentflow_home


# State labels — keep in sync with templates/state_machine.md.
STATE_TOPIC_POOL = "topic_pool"
STATE_TOPIC_APPROVED = "topic_approved"
STATE_TOPIC_REJECTED = "topic_rejected"
STATE_DRAFTING = "drafting"
STATE_DRAFT_PENDING_REVIEW = "draft_pending_review"
STATE_DRAFT_APPROVED = "draft_approved"
STATE_DRAFT_REJECTED = "draft_rejected"
STATE_DRAFTING_LOCKED_HUMAN = "drafting_locked_human"
STATE_IMAGE_PENDING_REVIEW = "image_pending_review"
STATE_IMAGE_APPROVED = "image_approved"
STATE_IMAGE_SKIPPED = "image_skipped"
STATE_CHANNEL_PENDING_REVIEW = "channel_pending_review"
STATE_READY_TO_PUBLISH = "ready_to_publish"
STATE_PUBLISHED = "published"

# Allowed transitions (from -> set[to]). Enforced by ``transition()``.
_ALLOWED: dict[str, set[str]] = {
    STATE_TOPIC_POOL: {STATE_TOPIC_APPROVED, STATE_TOPIC_REJECTED},
    STATE_TOPIC_APPROVED: {STATE_DRAFTING},
    STATE_DRAFTING: {STATE_DRAFT_PENDING_REVIEW, STATE_DRAFTING_LOCKED_HUMAN},
    STATE_DRAFT_PENDING_REVIEW: {
        STATE_DRAFT_APPROVED,
        STATE_DRAFT_REJECTED,
        STATE_DRAFTING,  # rewrite round
        STATE_DRAFTING_LOCKED_HUMAN,
    },
    STATE_DRAFTING_LOCKED_HUMAN: {
        STATE_DRAFT_PENDING_REVIEW,  # after L:edit / L:critique completes
        STATE_DRAFT_REJECTED,        # L:give_up
    },
    STATE_DRAFT_APPROVED: {STATE_IMAGE_PENDING_REVIEW, STATE_READY_TO_PUBLISH},
    STATE_IMAGE_PENDING_REVIEW: {
        STATE_IMAGE_APPROVED,
        STATE_IMAGE_SKIPPED,
        STATE_IMAGE_PENDING_REVIEW,  # regen loop
    },
    # Gate D (channel selection) sits between image_approved and
    # ready_to_publish. The legacy direct edge is preserved so existing
    # articles / `af review-publish-ready` callers still work.
    STATE_IMAGE_APPROVED: {STATE_CHANNEL_PENDING_REVIEW, STATE_READY_TO_PUBLISH, STATE_IMAGE_PENDING_REVIEW},
    STATE_IMAGE_SKIPPED: {STATE_CHANNEL_PENDING_REVIEW, STATE_READY_TO_PUBLISH},
    STATE_CHANNEL_PENDING_REVIEW: {STATE_READY_TO_PUBLISH, STATE_IMAGE_APPROVED},
    STATE_READY_TO_PUBLISH: {STATE_PUBLISHED},
    # Q5c 增量发布: 已发后可重新进 Gate D 加新平台
    STATE_PUBLISHED: {STATE_CHANNEL_PENDING_REVIEW},
}


class StateError(RuntimeError):
    """Raised when a transition is not allowed by the state machine."""


def _metadata_path(article_id: str) -> Path:
    return agentflow_home() / "drafts" / article_id / "metadata.json"


def _read(article_id: str) -> dict[str, Any]:
    p = _metadata_path(article_id)
    if not p.exists():
        raise FileNotFoundError(f"no metadata.json for article {article_id!r}")
    return json.loads(p.read_text(encoding="utf-8")) or {}


def _write(article_id: str, data: dict[str, Any]) -> None:
    _metadata_path(article_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_state(article_id: str) -> str:
    """Return the article's current gate state. Defaults to topic_pool when
    no gate_history has ever been recorded (covers brand-new articles)."""
    data = _read(article_id)
    history: list[dict[str, Any]] = list(data.get("gate_history") or [])
    if not history:
        return STATE_TOPIC_POOL
    last = history[-1]
    return str(last.get("to_state") or STATE_TOPIC_POOL)


def gate_history(article_id: str) -> list[dict[str, Any]]:
    return list(_read(article_id).get("gate_history") or [])


def transition(
    article_id: str,
    *,
    gate: str,
    to_state: str,
    actor: str,
    decision: str,
    tg_chat_id: int | None = None,
    tg_message_id: int | None = None,
    callback_data: str | None = None,
    round_: int = 0,
    notes: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Atomic gate-state transition. Returns the appended history entry.

    ``force=True`` bypasses the allowed-transitions check (used by recovery /
    admin tools like ``af review-resume``)."""
    data = _read(article_id)
    history: list[dict[str, Any]] = list(data.get("gate_history") or [])
    from_state = (
        history[-1].get("to_state") if history else STATE_TOPIC_POOL
    ) or STATE_TOPIC_POOL
    if not force:
        allowed = _ALLOWED.get(from_state, set())
        if to_state not in allowed:
            raise StateError(
                f"transition {from_state!r} -> {to_state!r} not allowed; "
                f"valid next states: {sorted(allowed) or '(terminal)'}"
            )
    entry = {
        "gate": gate,
        "from_state": from_state,
        "to_state": to_state,
        "actor": actor,
        "decision": decision,
        "timestamp": _now_iso(),
        "round": round_,
    }
    if tg_chat_id is not None:
        entry["tg_chat_id"] = tg_chat_id
    if tg_message_id is not None:
        entry["tg_message_id"] = tg_message_id
    if callback_data:
        entry["callback_data"] = callback_data
    if notes:
        entry["notes"] = notes
    history.append(entry)
    data["gate_history"] = history
    _write(article_id, data)
    try:
        from agentflow.shared.agent_bridge import emit_agent_event

        emit_agent_event(
            source="gate",
            event_type="gate.transition",
            article_id=article_id,
            payload=entry,
            occurred_at=str(entry.get("timestamp") or ""),
            source_ref={"store": "drafts/metadata.json", "path": "gate_history"},
            actor={"type": str(actor or "system")},
        )
    except Exception:
        pass
    return entry


def articles_in_state(states: Iterable[str]) -> list[str]:
    """Return all article_ids currently in any of the given states."""
    drafts_dir = agentflow_home() / "drafts"
    if not drafts_dir.exists():
        return []
    wanted = set(states)
    out: list[str] = []
    for sub in drafts_dir.iterdir():
        if not sub.is_dir():
            continue
        meta = sub / "metadata.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            continue
        history = data.get("gate_history") or []
        if not history:
            continue
        last = history[-1]
        if str(last.get("to_state")) in wanted:
            out.append(sub.name)
    return out
