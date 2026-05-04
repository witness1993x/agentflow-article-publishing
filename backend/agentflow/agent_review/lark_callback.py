"""Lark Bot Adapter Service callback handler (Phase 1).

The Lark Bot Adapter Service (component B) terminates the HTTP webhook from
Lark and translates raw Lark payloads into a small, vocabulary-stable shape:

    handle_event(
        event_kind="card_action" | "message" | "url_verify",
        article_id=...,
        action=...,
        payload=...,
        operator={"open_id": ..., "name": ...},
    )

This module is the daemon-side handler (component C). It maps each card-action
vocabulary item into existing AgentFlow review primitives (state machine
transitions / triggers) and returns a structured ack the adapter can convert
back into a Lark interactive-card response.

Design notes (Phase 1):

* The state machine is single-writer; ``review_state.transition`` raises
  :class:`agentflow.agent_review.state.StateError` on illegal transitions.
  Two operators clicking the same card produce one transition + one
  ``side_effects=["already_handled"]`` ack — never two transitions.
* Write-side actions that are dangerous to mirror onto Lark right now
  (``refill``, full rewrite) are intentionally read-only-write parity:
  return a reply card asking the operator to use Telegram. Phase 2 will
  enable these once we trust the auth + idempotency story.
* No subprocesses are spawned from the callback path — the daemon's review
  loop already takes care of long-running work.
"""

from __future__ import annotations

import json
from typing import Any

from agentflow.agent_review import state as review_state
from agentflow.agent_review import triggers as review_triggers
from agentflow.agent_review.state import (
    STATE_DRAFT_APPROVED,
    STATE_DRAFTING,
    StateError,
)
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.logger import get_logger
from agentflow.shared.memory import append_memory_event, read_memory_events

_log = get_logger("agent_review.lark_callback")


# ---------------------------------------------------------------------------
# Public response shape
# ---------------------------------------------------------------------------


def _empty_response() -> dict[str, Any]:
    return {
        "ack": True,
        "reply_card": None,
        "reply_text": None,
        "side_effects": [],
    }


def _make_card(
    *,
    title: str,
    body: str,
    template: str = "blue",
) -> dict[str, Any]:
    """Build a Lark interactive-card payload (subset of the official spec)."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": title, "tag": "plain_text"},
            "template": template,
        },
        "elements": [
            {"tag": "div", "text": {"content": body, "tag": "lark_md"}},
        ],
    }


def _actor_for(operator: dict[str, Any]) -> str:
    open_id = str(operator.get("open_id") or "unknown")
    return f"lark:{open_id}"


def _telemetry(
    *,
    event_kind: str,
    action: str | None,
    article_id: str | None,
    operator: dict[str, Any],
    outcome: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "event_kind": event_kind,
        "action": action,
        "article_id": article_id,
        "operator_open_id": operator.get("open_id"),
        "outcome": outcome,
    }
    if extra:
        payload.update(extra)
    try:
        append_memory_event(
            "lark_callback",
            article_id=article_id,
            payload=payload,
        )
    except Exception:  # pragma: no cover — telemetry must never fail the request
        _log.warning("lark_callback telemetry append failed", exc_info=True)


# ---------------------------------------------------------------------------
# Per-action handlers
# ---------------------------------------------------------------------------


def _handle_approve_b(
    *,
    article_id: str,
    operator: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = _empty_response()
    actor = _actor_for(operator)
    try:
        review_state.transition(
            article_id,
            gate="B",
            to_state=STATE_DRAFT_APPROVED,
            actor=actor,
            decision="approve_via_lark",
        )
    except StateError as err:
        response["side_effects"].append("already_handled")
        response["reply_text"] = f"Gate B already handled: {err}"
        _telemetry(
            event_kind="card_action",
            action="approve_b",
            article_id=article_id,
            operator=operator,
            outcome="already_handled",
        )
        return response
    response["side_effects"].append("approve_b")
    response["reply_card"] = _make_card(
        title="Gate B 已通过",
        body=f"Article `{article_id}` 已批准 (操作人 {operator.get('name') or operator.get('open_id')})",
        template="green",
    )
    _telemetry(
        event_kind="card_action",
        action="approve_b",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_reject_b(
    *,
    article_id: str,
    operator: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = _empty_response()
    actor = _actor_for(operator)
    try:
        review_state.transition(
            article_id,
            gate="B",
            to_state=STATE_DRAFTING,
            actor=actor,
            decision="reject_via_lark",
        )
    except StateError as err:
        response["side_effects"].append("already_handled")
        response["reply_text"] = f"Gate B already handled: {err}"
        _telemetry(
            event_kind="card_action",
            action="reject_b",
            article_id=article_id,
            operator=operator,
            outcome="already_handled",
        )
        return response
    response["side_effects"].append("reject_b")
    response["reply_card"] = _make_card(
        title="Gate B 已驳回",
        body=f"Article `{article_id}` 回到 drafting (操作人 {operator.get('name') or operator.get('open_id')})",
        template="red",
    )
    _telemetry(
        event_kind="card_action",
        action="reject_b",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_refill(
    *,
    article_id: str,
    operator: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Phase 1: read-only-write parity. Do NOT mutate state, do NOT spawn.

    We log the request as a memory event so operators can audit Lark
    refill attempts later, then return a card directing them to Telegram.
    """
    response = _empty_response()
    try:
        append_memory_event(
            "lark_refill_requested",
            article_id=article_id,
            payload={
                "operator_open_id": operator.get("open_id"),
                "operator_name": operator.get("name"),
            },
        )
    except Exception:
        _log.warning("failed to record lark_refill_requested", exc_info=True)
    response["side_effects"].append("refill_deferred_to_tg")
    response["reply_card"] = _make_card(
        title="Refill 暂未在 Lark 端开启",
        body=(
            "Phase 1 还未在 Lark 上开放 refill 写操作。\n\n"
            "请在 Telegram 端完成 refill (Phase 2 将启用)。"
        ),
        template="grey",
    )
    _telemetry(
        event_kind="card_action",
        action="refill",
        article_id=article_id,
        operator=operator,
        outcome="deferred_to_tg",
    )
    return response


def _handle_takeover(
    *,
    article_id: str,
    operator: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = _empty_response()
    fired = False
    try:
        result = review_triggers.post_locked_takeover(article_id)
        fired = result is not None
    except Exception as err:
        _log.warning("post_locked_takeover failed for %s: %s", article_id, err)
        response["side_effects"].append("takeover_error")
        response["reply_card"] = _make_card(
            title="Takeover 触发失败",
            body=f"Article `{article_id}` takeover 触发失败: {err}",
            template="red",
        )
        _telemetry(
            event_kind="card_action",
            action="takeover",
            article_id=article_id,
            operator=operator,
            outcome="error",
            extra={"error": str(err)},
        )
        return response
    response["side_effects"].append("takeover_triggered" if fired else "takeover_skipped")
    response["reply_card"] = _make_card(
        title="人工接管已触发" if fired else "人工接管未触发",
        body=(
            f"Article `{article_id}` 已发送 Locked Takeover 卡片到 Telegram。"
            if fired
            else f"Article `{article_id}` 未发送 (Telegram 未配置或缺少 chat_id)。"
        ),
        template="blue" if fired else "grey",
    )
    _telemetry(
        event_kind="card_action",
        action="takeover",
        article_id=article_id,
        operator=operator,
        outcome="fired" if fired else "skipped",
    )
    return response


def _handle_view_audit(
    *,
    article_id: str,
    operator: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = _empty_response()
    try:
        events = read_memory_events(
            article_id=article_id, event_type="d2_structure_audit"
        )
    except Exception as err:
        _log.warning("view_audit memory read failed for %s: %s", article_id, err)
        events = []
    if not events:
        response["reply_card"] = _make_card(
            title="未找到结构审核记录",
            body=f"Article `{article_id}` 暂无 d2_structure_audit 记录。",
            template="grey",
        )
        _telemetry(
            event_kind="card_action",
            action="view_audit",
            article_id=article_id,
            operator=operator,
            outcome="empty",
        )
        return response
    lines: list[str] = [f"Article `{article_id}` 结构审核历史 (共 {len(events)} 条):", ""]
    for ev in events[-10:]:  # most recent 10 audit events is plenty for a card
        ts = str(ev.get("ts") or "")
        ev_payload = ev.get("payload") or {}
        verdict = ev_payload.get("verdict") or ev_payload.get("status") or "(no verdict)"
        notes = ev_payload.get("notes") or ev_payload.get("summary") or ""
        line = f"- **{ts}** — verdict: `{verdict}`"
        if notes:
            # cap each line so we don't blow past the Lark card body cap
            note_short = str(notes)
            if len(note_short) > 120:
                note_short = note_short[:117] + "..."
            line += f"  \n  {note_short}"
        lines.append(line)
    response["reply_card"] = _make_card(
        title="d2_structure_audit 历史",
        body="\n".join(lines),
        template="blue",
    )
    _telemetry(
        event_kind="card_action",
        action="view_audit",
        article_id=article_id,
        operator=operator,
        outcome="ok",
        extra={"event_count": len(events)},
    )
    return response


def _handle_view_meta(
    *,
    article_id: str,
    operator: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = _empty_response()
    meta_path = agentflow_home() / "drafts" / article_id / "metadata.json"
    if not meta_path.exists():
        response["reply_card"] = _make_card(
            title="未找到 article metadata",
            body=f"未找到 Article `{article_id}` 的 metadata.json。",
            template="grey",
        )
        _telemetry(
            event_kind="card_action",
            action="view_meta",
            article_id=article_id,
            operator=operator,
            outcome="missing",
        )
        return response
    try:
        meta_raw = meta_path.read_text(encoding="utf-8")
        meta = json.loads(meta_raw) or {}
    except (OSError, json.JSONDecodeError) as err:
        _log.warning("view_meta read failed for %s: %s", article_id, err)
        response["reply_card"] = _make_card(
            title="metadata 读取失败",
            body=f"Article `{article_id}` metadata.json 读取失败: {err}",
            template="red",
        )
        _telemetry(
            event_kind="card_action",
            action="view_meta",
            article_id=article_id,
            operator=operator,
            outcome="error",
            extra={"error": str(err)},
        )
        return response
    title = str(meta.get("title") or "(no title)")
    history = list(meta.get("gate_history") or [])
    current = history[-1].get("to_state") if history else "topic_pool"
    rounds = sum(
        1 for h in history if isinstance(h, dict) and h.get("decision") == "rewrite_round"
    )
    body_lines = [
        f"**Title**: {title}",
        f"**Article**: `{article_id}`",
        f"**Current state**: `{current}`",
        f"**Rewrite rounds**: {rounds}",
        f"**Gate history entries**: {len(history)}",
    ]
    response["reply_card"] = _make_card(
        title="Article metadata snapshot",
        body="\n".join(body_lines),
        template="blue",
    )
    _telemetry(
        event_kind="card_action",
        action="view_meta",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


# Action vocabulary — must match the Adapter Service exactly.
_ACTION_HANDLERS = {
    "approve_b": _handle_approve_b,
    "reject_b": _handle_reject_b,
    "refill": _handle_refill,
    "takeover": _handle_takeover,
    "view_audit": _handle_view_audit,
    "view_meta": _handle_view_meta,
}


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def handle_event(
    *,
    event_kind: str,
    article_id: str | None,
    action: str | None,
    payload: dict[str, Any],
    operator: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a Lark adapter event to the right internal handler.

    Parameters
    ----------
    event_kind:
        ``"card_action"`` for interactive-card button clicks, ``"message"``
        for plain-text DMs, ``"url_verify"`` for Lark's webhook ownership
        challenge.
    article_id:
        Article the action targets. ``None`` for ``url_verify`` and for
        message events not bound to an article.
    action:
        Card-action vocab item (see ``_ACTION_HANDLERS``). ``None`` for
        message / url_verify events.
    payload:
        Raw structured payload from the adapter (preserved as-is for handlers
        that need extra context — e.g. message body for future Phase 2 NLU).
    operator:
        ``{"open_id": str, "name": str | None}``. Used for the ``actor``
        field in state transitions and for telemetry.

    Returns
    -------
    dict
        ``{"ack": bool, "reply_card": dict | None, "reply_text": str | None,
        "side_effects": list[str]}``.
    """
    payload = payload or {}
    operator = operator or {}

    if event_kind == "url_verify":
        challenge = payload.get("challenge")
        response = _empty_response()
        response["side_effects"].append("url_verify")
        response["reply_text"] = str(challenge) if challenge else None
        return response

    if event_kind == "message":
        # Phase 1: messages are not used as commands. Acknowledge and log.
        response = _empty_response()
        response["side_effects"].append("message_ignored")
        _telemetry(
            event_kind="message",
            action=None,
            article_id=article_id,
            operator=operator,
            outcome="ignored",
        )
        return response

    if event_kind != "card_action":
        response = _empty_response()
        response["side_effects"].append("unknown_event_kind")
        response["reply_text"] = f"unsupported event_kind: {event_kind!r}"
        _telemetry(
            event_kind=str(event_kind),
            action=action,
            article_id=article_id,
            operator=operator,
            outcome="unknown_event_kind",
        )
        return response

    handler = _ACTION_HANDLERS.get(str(action or ""))
    if handler is None:
        response = _empty_response()
        response["side_effects"].append("unknown_action")
        response["reply_text"] = f"unknown action: {action!r}"
        _telemetry(
            event_kind="card_action",
            action=action,
            article_id=article_id,
            operator=operator,
            outcome="unknown_action",
        )
        return response

    if not article_id:
        response = _empty_response()
        response["side_effects"].append("missing_article_id")
        response["reply_text"] = f"action {action!r} requires an article_id"
        _telemetry(
            event_kind="card_action",
            action=action,
            article_id=None,
            operator=operator,
            outcome="missing_article_id",
        )
        return response

    return handler(article_id=article_id, operator=operator, payload=payload)
