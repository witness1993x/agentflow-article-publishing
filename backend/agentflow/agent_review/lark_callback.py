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
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agentflow.agent_review import state as review_state
from agentflow.agent_review import triggers as review_triggers
from agentflow.agent_review.state import (
    STATE_CHANNEL_PENDING_REVIEW,
    STATE_DRAFT_APPROVED,
    STATE_DRAFT_REJECTED,
    STATE_DRAFTING,
    STATE_IMAGE_APPROVED,
    STATE_IMAGE_PENDING_REVIEW,
    STATE_IMAGE_SKIPPED,
    STATE_READY_TO_PUBLISH,
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


# ---------------------------------------------------------------------------
# v1.1.1 — extra helpers for Gate A/C/D + L parity
# ---------------------------------------------------------------------------


def _af_executable() -> list[str]:
    """Resolve the `af` CLI argv prefix the same way agent_review.web does.

    Prefer the `af` shim on PATH; fall back to `python -m agentflow.cli.commands`
    so the Lark bridge works in installs that haven't published `af` to a
    public bin dir (CI, dev venvs, the lark-adapter container).
    """
    af = "af"
    return [af] if _which(af) else [sys.executable, "-m", "agentflow.cli.commands"]


def _which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)


def _spawn_async(argv: list[str], *, article_id: str, action: str) -> bool:
    """Fire-and-forget subprocess. Stdout/stderr piped to /dev/null.

    The subprocess is expected to emit ``agent.command.completed`` /
    ``agent.command.failed`` events via ``emit_agent_event`` when it
    finishes; the OpenClaw Lark plugin listens on the event webhook
    (``AGENTFLOW_AGENT_EVENT_WEBHOOK_URL``) and updates the original card
    when the result lands.
    """
    try:
        subprocess.Popen(  # pragma: no cover — fire-and-forget
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError as err:
        _log.warning(
            "lark_callback _spawn_async failed: action=%s article_id=%s err=%s",
            action,
            article_id,
            err,
        )
        return False


def _read_meta(article_id: str) -> dict[str, Any]:
    p = agentflow_home() / "drafts" / article_id / "metadata.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_meta(article_id: str, meta: dict[str, Any]) -> bool:
    p = agentflow_home() / "drafts" / article_id / "metadata.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as err:
        _log.warning("metadata write failed for %s: %s", article_id, err)
        return False


def _kicked_off_card(*, action: str, article_id: str) -> dict[str, Any]:
    return _make_card(
        title=f"已触发 · {action}",
        body=(
            f"`{article_id}` 的 `{action}` 已在后台启动。\n"
            "完成后 OpenClaw 会通过 event webhook 收到 "
            "`agent.command.completed` / `agent.command.failed`，并更新此卡片。"
        ),
        template="blue",
    )


def _state_error_card(*, action: str, err: Exception) -> dict[str, Any]:
    return _make_card(
        title=f"{action} 已被处理过",
        body=f"State machine 拒绝重复转换: {err}",
        template="grey",
    )


# ---------------------------------------------------------------------------
# Gate A handlers (write / reject_all / expand / defer)
# ---------------------------------------------------------------------------


def _handle_gate_a_write(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Kick off ``af write <hotspot_id> --auto-pick`` in the background.

    ``article_id`` here is actually the hotspot_id selected from the Gate A
    card (the OpenClaw plugin must use Gate A's card meta where the value
    field carries hotspot_id, not article_id). For consistency with the
    handler signature we accept it under article_id.
    """
    response = _empty_response()
    hotspot_id = article_id
    angle_index = int(payload.get("angle_index") or 0)
    target_series = str(payload.get("target_series") or "A")
    argv = _af_executable() + [
        "write",
        hotspot_id,
        "--auto-pick",
        "--angle-index",
        str(angle_index),
        "--target-series",
        target_series,
        "--json",
    ]
    if _spawn_async(argv, article_id=hotspot_id, action="gate_a_write"):
        response["side_effects"].append("gate_a_write_spawned")
        response["reply_card"] = _kicked_off_card(
            action="gate_a_write", article_id=hotspot_id
        )
    else:
        response["side_effects"].append("spawn_failed")
        response["reply_card"] = _make_card(
            title="Gate A 写作启动失败",
            body=f"无法启动子进程，请查看 daemon 日志 (`{hotspot_id}`)",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action="gate_a_write",
        article_id=hotspot_id,
        operator=operator,
        outcome="spawned" if "gate_a_write_spawned" in response["side_effects"] else "spawn_failed",
        extra={"angle_index": angle_index, "target_series": target_series},
    )
    return response


def _handle_gate_a_reject_all(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Reject the whole Gate A card. No state mutation — just telemetry."""
    response = _empty_response()
    response["side_effects"].append("gate_a_reject_all")
    response["reply_card"] = _make_card(
        title="Gate A 已驳回全部",
        body=f"今日候选 ({article_id}) 已标记为不写。下一轮 scan 重新挑选。",
        template="red",
    )
    _telemetry(
        event_kind="card_action",
        action="gate_a_reject_all",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_gate_a_expand(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Return the full hotspot details (mainstream / overlooked / sources).

    The OpenClaw plugin renders this as a long-form card; AgentFlow only
    returns the structured data.
    """
    response = _empty_response()
    hotspot_path = agentflow_home() / "hotspots"
    found: dict[str, Any] = {}
    for f in sorted(hotspot_path.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for h in doc.get("hotspots") or []:
            if str(h.get("id") or "") == article_id:
                found = h
                break
        if found:
            break
    if not found:
        response["side_effects"].append("hotspot_not_found")
        response["reply_card"] = _make_card(
            title="未找到 hotspot",
            body=f"`{article_id}` 不在最近的扫描结果里",
            template="grey",
        )
    else:
        body_lines = [
            f"**{found.get('topic_one_liner') or '(no title)'}**",
            "",
            "**主流观点**:",
        ]
        for v in (found.get("mainstream_views") or [])[:5]:
            body_lines.append(f"- {v}")
        body_lines.append("")
        body_lines.append("**被忽视的角度**:")
        for v in (found.get("overlooked_angles") or [])[:5]:
            body_lines.append(f"- {v}")
        response["reply_card"] = _make_card(
            title="📖 Hotspot 详情",
            body="\n".join(body_lines),
            template="blue",
        )
        response["side_effects"].append("gate_a_expand")
    _telemetry(
        event_kind="card_action",
        action="gate_a_expand",
        article_id=article_id,
        operator=operator,
        outcome="ok" if found else "not_found",
    )
    return response


def _handle_defer(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Generic defer — operator put the decision on hold. No state mutation."""
    response = _empty_response()
    gate = str(payload.get("gate") or "")
    response["side_effects"].append("deferred")
    response["reply_card"] = _make_card(
        title=f"Gate {gate or '?'} 已延后",
        body=f"`{article_id}` 决定延后，可稍后再处理。",
        template="grey",
    )
    _telemetry(
        event_kind="card_action",
        action="defer",
        article_id=article_id,
        operator=operator,
        outcome="ok",
        extra={"gate": gate},
    )
    return response


# ---------------------------------------------------------------------------
# Gate B remaining handlers (rewrite / edit / diff)
# ---------------------------------------------------------------------------


def _handle_gate_b_rewrite(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Spawn ``af d2 fill --rewrite`` in the background, transition state to drafting."""
    response = _empty_response()
    actor = _actor_for(operator)
    try:
        review_state.transition(
            article_id,
            gate="B",
            to_state=STATE_DRAFTING,
            actor=actor,
            decision="rewrite_via_lark",
        )
    except StateError as err:
        response["side_effects"].append("already_handled")
        response["reply_card"] = _state_error_card(action="gate_b_rewrite", err=err)
        _telemetry(
            event_kind="card_action",
            action="gate_b_rewrite",
            article_id=article_id,
            operator=operator,
            outcome="already_handled",
        )
        return response
    argv = _af_executable() + ["fill", article_id, "--rewrite", "--json"]
    spawned = _spawn_async(argv, article_id=article_id, action="gate_b_rewrite")
    if spawned:
        response["side_effects"].append("gate_b_rewrite_spawned")
        response["reply_card"] = _kicked_off_card(
            action="gate_b_rewrite", article_id=article_id
        )
    else:
        response["side_effects"].append("spawn_failed")
        response["reply_card"] = _make_card(
            title="Gate B rewrite 启动失败",
            body=f"无法启动 fill 子进程 (`{article_id}`)",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action="gate_b_rewrite",
        article_id=article_id,
        operator=operator,
        outcome="spawned" if spawned else "spawn_failed",
    )
    return response


def _handle_gate_b_edit(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Open an interactive edit slot.

    Phase 1 implementation: log the request + return a card asking the
    operator to provide the edit prompt as a follow-up message. The
    OpenClaw plugin handles the conversational follow-up via @-bot.
    """
    response = _empty_response()
    section_index = payload.get("section_index")
    paragraph_index = payload.get("paragraph_index")
    try:
        append_memory_event(
            "lark_edit_pending",
            article_id=article_id,
            payload={
                "operator_open_id": operator.get("open_id"),
                "section_index": section_index,
                "paragraph_index": paragraph_index,
            },
        )
    except Exception:
        _log.warning("lark_edit_pending memory append failed", exc_info=True)
    response["side_effects"].append("gate_b_edit_pending")
    response["reply_card"] = _make_card(
        title="✏️ 输入修改指令",
        body=(
            f"`{article_id}` 等待修改指令。\n\n"
            "在 Lark 群里 @bot 发一条消息，OpenClaw agent 会把指令喂给 d2 interactive editor。\n"
            f"目标段: section={section_index}, paragraph={paragraph_index}"
        ),
        template="blue",
    )
    _telemetry(
        event_kind="card_action",
        action="gate_b_edit",
        article_id=article_id,
        operator=operator,
        outcome="ok",
        extra={
            "section_index": section_index,
            "paragraph_index": paragraph_index,
        },
    )
    return response


def _handle_gate_b_diff(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Return the audit diff (pre-vs-post structure_audit changes)."""
    response = _empty_response()
    try:
        events = read_memory_events(article_id=article_id, event_type="d2_structure_audit")
    except Exception as err:
        response["side_effects"].append("audit_read_failed")
        response["reply_card"] = _make_card(
            title="无法读取审计日志",
            body=f"`{article_id}`: {err}",
            template="red",
        )
        return response
    if not events:
        response["reply_card"] = _make_card(
            title="无审计记录",
            body=f"`{article_id}` 还没有审计事件",
            template="grey",
        )
    else:
        latest = events[-1]
        p = latest.get("payload") or {}
        body = (
            f"**verdict**: `{p.get('verdict')}` ({p.get('score', 0):.2f})\n"
            f"**dim_scores**: {json.dumps(p.get('dim_scores') or {}, ensure_ascii=False)}\n\n"
            "**issues**:\n"
            + "\n".join(f"- {x}" for x in (p.get("issues") or [])[:8])
        )
        response["reply_card"] = _make_card(
            title="🔍 D2 audit diff",
            body=body,
            template="blue",
        )
    response["side_effects"].append("gate_b_diff")
    _telemetry(
        event_kind="card_action",
        action="gate_b_diff",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


# ---------------------------------------------------------------------------
# Gate C handlers (approve / skip / regen / relogo / full)
# ---------------------------------------------------------------------------


def _handle_gate_c_approve(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    response = _empty_response()
    actor = _actor_for(operator)
    try:
        review_state.transition(
            article_id,
            gate="C",
            to_state=STATE_IMAGE_APPROVED,
            actor=actor,
            decision="approve_via_lark",
        )
    except StateError as err:
        response["side_effects"].append("already_handled")
        response["reply_card"] = _state_error_card(action="gate_c_approve", err=err)
        _telemetry(
            event_kind="card_action",
            action="gate_c_approve",
            article_id=article_id,
            operator=operator,
            outcome="already_handled",
        )
        return response
    response["side_effects"].append("gate_c_approve")
    response["reply_card"] = _make_card(
        title="Gate C 配图已通过",
        body=f"`{article_id}` 配图通过，进入 Gate D 渠道挑选。",
        template="green",
    )
    _telemetry(
        event_kind="card_action",
        action="gate_c_approve",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_gate_c_skip(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    response = _empty_response()
    actor = _actor_for(operator)
    try:
        review_state.transition(
            article_id,
            gate="C",
            to_state=STATE_IMAGE_SKIPPED,
            actor=actor,
            decision="skip_via_lark",
        )
    except StateError as err:
        response["side_effects"].append("already_handled")
        response["reply_card"] = _state_error_card(action="gate_c_skip", err=err)
        return response
    response["side_effects"].append("gate_c_skip")
    response["reply_card"] = _make_card(
        title="Gate C 配图已跳过",
        body=f"`{article_id}` 不配图直接进入 Gate D。",
        template="grey",
    )
    _telemetry(
        event_kind="card_action",
        action="gate_c_skip",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_gate_c_regen(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Spawn ``af image-gate <id> --mode regen``."""
    response = _empty_response()
    mode = str(payload.get("mode") or "auto")
    argv = _af_executable() + ["image-gate", article_id, "--mode", mode, "--json"]
    if _spawn_async(argv, article_id=article_id, action="gate_c_regen"):
        response["side_effects"].append("gate_c_regen_spawned")
        response["reply_card"] = _kicked_off_card(
            action=f"gate_c_regen({mode})", article_id=article_id
        )
    else:
        response["side_effects"].append("spawn_failed")
        response["reply_card"] = _make_card(
            title="Gate C 重生成失败",
            body=f"image-gate 子进程未能启动 (`{article_id}`)",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action="gate_c_regen",
        article_id=article_id,
        operator=operator,
        outcome="spawned" if "gate_c_regen_spawned" in response["side_effects"] else "spawn_failed",
        extra={"mode": mode},
    )
    return response


def _handle_gate_c_relogo(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Spawn ``af image-gate <id> --logo-only``."""
    response = _empty_response()
    argv = _af_executable() + ["image-gate", article_id, "--logo-only", "--json"]
    if _spawn_async(argv, article_id=article_id, action="gate_c_relogo"):
        response["side_effects"].append("gate_c_relogo_spawned")
        response["reply_card"] = _kicked_off_card(
            action="gate_c_relogo", article_id=article_id
        )
    else:
        response["side_effects"].append("spawn_failed")
        response["reply_card"] = _make_card(
            title="Gate C logo 重生成失败",
            body=f"image-gate 子进程未能启动 (`{article_id}`)",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action="gate_c_relogo",
        article_id=article_id,
        operator=operator,
        outcome="spawned" if "gate_c_relogo_spawned" in response["side_effects"] else "spawn_failed",
    )
    return response


def _handle_gate_c_full(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Return the full image gallery card."""
    response = _empty_response()
    meta = _read_meta(article_id)
    images = meta.get("image_placeholders") or []
    if not images:
        response["reply_card"] = _make_card(
            title="该文章没有图片占位",
            body=f"`{article_id}` 的 image_placeholders 为空",
            template="grey",
        )
    else:
        body_lines = ["**图片占位列表**:", ""]
        for i, ph in enumerate(images[:10], 1):
            desc = ph.get("description") or "(no description)"
            resolved = ph.get("resolved_path") or "(unresolved)"
            body_lines.append(f"{i}. `{desc}` → `{resolved}`")
        if len(images) > 10:
            body_lines.append(f"... ({len(images) - 10} more)")
        response["reply_card"] = _make_card(
            title=f"🖼 配图全景 ({len(images)})",
            body="\n".join(body_lines),
            template="blue",
        )
    response["side_effects"].append("gate_c_full")
    _telemetry(
        event_kind="card_action",
        action="gate_c_full",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


# ---------------------------------------------------------------------------
# Gate D handlers (toggle / select_all / save_default / confirm / cancel /
#                   resume / extend / retry)
# ---------------------------------------------------------------------------


_GATE_D_KEY = "gate_d_selection"


def _handle_gate_d_toggle(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Toggle a single platform in metadata.gate_d_selection."""
    response = _empty_response()
    platform = str(payload.get("platform") or "").strip()
    if not platform:
        response["side_effects"].append("missing_platform")
        response["reply_text"] = "gate_d_toggle requires payload.platform"
        return response
    meta = _read_meta(article_id)
    sel: list[str] = list(meta.get(_GATE_D_KEY) or [])
    if platform in sel:
        sel.remove(platform)
        action_outcome = "off"
    else:
        sel.append(platform)
        action_outcome = "on"
    meta[_GATE_D_KEY] = sel
    _write_meta(article_id, meta)
    response["side_effects"].append(f"gate_d_toggle_{action_outcome}")
    response["reply_card"] = _make_card(
        title=f"{platform} {'已选' if action_outcome == 'on' else '已取消'}",
        body=f"当前选中: `{', '.join(sel) or '(空)'}`",
        template="blue",
    )
    _telemetry(
        event_kind="card_action",
        action="gate_d_toggle",
        article_id=article_id,
        operator=operator,
        outcome=action_outcome,
        extra={"platform": platform, "current_selection": sel},
    )
    return response


def _handle_gate_d_select_all(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    response = _empty_response()
    all_platforms = list(payload.get("platforms") or ["medium", "ghost", "linkedin", "twitter"])
    meta = _read_meta(article_id)
    meta[_GATE_D_KEY] = all_platforms
    _write_meta(article_id, meta)
    response["side_effects"].append("gate_d_select_all")
    response["reply_card"] = _make_card(
        title="已全选",
        body=f"选中: `{', '.join(all_platforms)}`",
        template="green",
    )
    _telemetry(
        event_kind="card_action",
        action="gate_d_select_all",
        article_id=article_id,
        operator=operator,
        outcome="ok",
        extra={"platforms": all_platforms},
    )
    return response


def _handle_gate_d_save_default(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Save the current selection as the operator's default for future drafts."""
    response = _empty_response()
    meta = _read_meta(article_id)
    sel = list(meta.get(_GATE_D_KEY) or [])
    pref_path = agentflow_home() / "preferences.json"
    try:
        prefs = (
            json.loads(pref_path.read_text(encoding="utf-8"))
            if pref_path.exists()
            else {}
        )
    except (json.JSONDecodeError, OSError):
        prefs = {}
    prefs.setdefault("gate_d", {})["default_platforms"] = sel
    try:
        pref_path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        ok = True
    except OSError as err:
        _log.warning("preferences.json write failed: %s", err)
        ok = False
    response["side_effects"].append(
        "gate_d_save_default" if ok else "preferences_write_failed"
    )
    response["reply_card"] = _make_card(
        title="默认渠道已保存" if ok else "保存默认失败",
        body=f"默认 = `{', '.join(sel) or '(空)'}`",
        template="green" if ok else "red",
    )
    _telemetry(
        event_kind="card_action",
        action="gate_d_save_default",
        article_id=article_id,
        operator=operator,
        outcome="ok" if ok else "failed",
        extra={"selection": sel},
    )
    return response


def _handle_gate_d_confirm(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Spawn ``af publish <id> --platforms <selection>`` in the background."""
    response = _empty_response()
    actor = _actor_for(operator)
    meta = _read_meta(article_id)
    sel = list(meta.get(_GATE_D_KEY) or [])
    if not sel:
        response["side_effects"].append("empty_selection")
        response["reply_card"] = _make_card(
            title="未选任何渠道",
            body=f"`{article_id}` Gate D 未选择平台，先 toggle 至少一个再 confirm",
            template="grey",
        )
        return response
    try:
        review_state.transition(
            article_id,
            gate="D",
            to_state=STATE_READY_TO_PUBLISH,
            actor=actor,
            decision="confirm_via_lark",
        )
    except StateError as err:
        response["side_effects"].append("already_handled")
        response["reply_card"] = _state_error_card(action="gate_d_confirm", err=err)
        return response
    argv = _af_executable() + [
        "publish",
        article_id,
        "--platforms",
        ",".join(sel),
        "--json",
    ]
    if _spawn_async(argv, article_id=article_id, action="gate_d_confirm"):
        response["side_effects"].append("gate_d_publish_spawned")
        response["reply_card"] = _kicked_off_card(
            action=f"publish({', '.join(sel)})", article_id=article_id
        )
    else:
        response["side_effects"].append("spawn_failed")
        response["reply_card"] = _make_card(
            title="发布启动失败",
            body=f"publish 子进程无法启动 (`{article_id}`)",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action="gate_d_confirm",
        article_id=article_id,
        operator=operator,
        outcome="spawned" if "gate_d_publish_spawned" in response["side_effects"] else "spawn_failed",
        extra={"platforms": sel},
    )
    return response


def _handle_gate_d_cancel(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Cancel Gate D — clear selection, transition back to image_approved."""
    response = _empty_response()
    actor = _actor_for(operator)
    meta = _read_meta(article_id)
    meta[_GATE_D_KEY] = []
    _write_meta(article_id, meta)
    try:
        review_state.transition(
            article_id,
            gate="D",
            to_state=STATE_IMAGE_APPROVED,
            actor=actor,
            decision="cancel_via_lark",
        )
    except StateError:
        # Already past Gate D — silent ok
        pass
    response["side_effects"].append("gate_d_cancel")
    response["reply_card"] = _make_card(
        title="Gate D 已取消",
        body=f"`{article_id}` 渠道选择已清空，可稍后重新进入 Gate D",
        template="grey",
    )
    _telemetry(
        event_kind="card_action",
        action="gate_d_cancel",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_gate_d_resume(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Resume a deferred Gate D — re-post the channel-selection card."""
    response = _empty_response()
    try:
        result = review_triggers.post_gate_d(article_id)
        if result is None:
            response["side_effects"].append("post_gate_d_skipped")
            response["reply_card"] = _make_card(
                title="Gate D resume 失败",
                body=f"`{article_id}` 当前不在可重启状态",
                template="grey",
            )
        else:
            response["side_effects"].append("gate_d_resumed")
            response["reply_card"] = _make_card(
                title="Gate D 已重新发卡",
                body=f"`{article_id}` 渠道选择卡已重新推送 (TG side)",
                template="green",
            )
    except Exception as err:
        response["side_effects"].append("post_gate_d_failed")
        response["reply_card"] = _make_card(
            title="Gate D resume 异常",
            body=f"`{article_id}`: {err}",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action="gate_d_resume",
        article_id=article_id,
        operator=operator,
        outcome="ok" if "gate_d_resumed" in response["side_effects"] else "failed",
    )
    return response


def _handle_gate_d_extend(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Extend the short_id TTL — purely a memory event, no state transition."""
    response = _empty_response()
    response["side_effects"].append("gate_d_extend")
    response["reply_card"] = _make_card(
        title="Gate D TTL 已延长",
        body=f"`{article_id}` 短码有效期延长一轮",
        template="blue",
    )
    _telemetry(
        event_kind="card_action",
        action="gate_d_extend",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_gate_d_retry(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Retry a failed publish for one or more platforms."""
    response = _empty_response()
    platforms = list(payload.get("platforms") or [])
    if not platforms:
        meta = _read_meta(article_id)
        platforms = list(meta.get(_GATE_D_KEY) or [])
    if not platforms:
        response["side_effects"].append("empty_platforms")
        response["reply_text"] = "gate_d_retry: no platforms in payload or metadata"
        return response
    argv = _af_executable() + [
        "publish",
        article_id,
        "--platforms",
        ",".join(platforms),
        "--json",
    ]
    if _spawn_async(argv, article_id=article_id, action="gate_d_retry"):
        response["side_effects"].append("gate_d_retry_spawned")
        response["reply_card"] = _kicked_off_card(
            action=f"gate_d_retry({', '.join(platforms)})", article_id=article_id
        )
    else:
        response["side_effects"].append("spawn_failed")
        response["reply_card"] = _make_card(
            title="重试启动失败",
            body=f"publish 子进程无法启动 (`{article_id}`)",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action="gate_d_retry",
        article_id=article_id,
        operator=operator,
        outcome="spawned" if "gate_d_retry_spawned" in response["side_effects"] else "spawn_failed",
        extra={"platforms": platforms},
    )
    return response


# ---------------------------------------------------------------------------
# Locked Takeover (L) handlers (critique / edit / give_up)
# ---------------------------------------------------------------------------


def _handle_locked_critique(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Return the latest audit + locked-takeover context as a critique card."""
    response = _empty_response()
    audit_events: list[dict[str, Any]]
    try:
        audit_events = read_memory_events(
            article_id=article_id, event_type="d2_structure_audit"
        )
    except Exception:
        audit_events = []
    latest_audit = (audit_events or [{}])[-1]
    p = (latest_audit or {}).get("payload") or {}
    body = (
        f"**最近一次 audit verdict**: `{p.get('verdict', 'n/a')}` "
        f"(score {p.get('score', 0):.2f})\n\n"
        "**主要问题**:\n"
        + "\n".join(f"- {x}" for x in (p.get("issues") or [])[:6])
    )
    response["reply_card"] = _make_card(
        title="🔍 Locked Takeover · Critique",
        body=body,
        template="blue",
    )
    response["side_effects"].append("locked_critique")
    _telemetry(
        event_kind="card_action",
        action="locked_critique",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_locked_edit(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Open an interactive edit slot from the locked takeover state."""
    response = _empty_response()
    try:
        append_memory_event(
            "lark_locked_edit_pending",
            article_id=article_id,
            payload={
                "operator_open_id": operator.get("open_id"),
            },
        )
    except Exception:
        _log.warning("lark_locked_edit_pending memory append failed", exc_info=True)
    response["side_effects"].append("locked_edit_pending")
    response["reply_card"] = _make_card(
        title="✏️ Locked Takeover · Edit",
        body=(
            f"`{article_id}` 进入手动接管编辑模式。\n\n"
            "在 Lark 群里 @bot 给出新草稿（多段 markdown 都可以），"
            "OpenClaw 把内容写入 d2 interactive editor 完成接管。"
        ),
        template="blue",
    )
    _telemetry(
        event_kind="card_action",
        action="locked_edit",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_locked_give_up(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Give up — transition to draft_rejected (article abandoned)."""
    response = _empty_response()
    actor = _actor_for(operator)
    try:
        review_state.transition(
            article_id,
            gate="L",
            to_state=STATE_DRAFT_REJECTED,
            actor=actor,
            decision="give_up_via_lark",
        )
    except StateError as err:
        response["side_effects"].append("already_handled")
        response["reply_card"] = _state_error_card(action="locked_give_up", err=err)
        _telemetry(
            event_kind="card_action",
            action="locked_give_up",
            article_id=article_id,
            operator=operator,
            outcome="already_handled",
        )
        return response
    response["side_effects"].append("locked_give_up")
    response["reply_card"] = _make_card(
        title="已放弃",
        body=f"`{article_id}` 标记为 `draft_rejected`，不再继续",
        template="red",
    )
    _telemetry(
        event_kind="card_action",
        action="locked_give_up",
        article_id=article_id,
        operator=operator,
        outcome="ok",
    )
    return response


# Action vocabulary — must match the Adapter Service exactly.
_ACTION_HANDLERS = {
    # v1.1.0
    "approve_b": _handle_approve_b,
    "reject_b": _handle_reject_b,
    "refill": _handle_refill,
    "takeover": _handle_takeover,
    "view_audit": _handle_view_audit,
    "view_meta": _handle_view_meta,
    # v1.1.1 — Gate A
    "gate_a_write": _handle_gate_a_write,
    "gate_a_reject_all": _handle_gate_a_reject_all,
    "gate_a_expand": _handle_gate_a_expand,
    # v1.1.1 — Gate B remaining
    "gate_b_rewrite": _handle_gate_b_rewrite,
    "gate_b_edit": _handle_gate_b_edit,
    "gate_b_diff": _handle_gate_b_diff,
    # v1.1.1 — Gate C
    "gate_c_approve": _handle_gate_c_approve,
    "gate_c_skip": _handle_gate_c_skip,
    "gate_c_regen": _handle_gate_c_regen,
    "gate_c_relogo": _handle_gate_c_relogo,
    "gate_c_full": _handle_gate_c_full,
    # v1.1.1 — Gate D
    "gate_d_toggle": _handle_gate_d_toggle,
    "gate_d_select_all": _handle_gate_d_select_all,
    "gate_d_save_default": _handle_gate_d_save_default,
    "gate_d_confirm": _handle_gate_d_confirm,
    "gate_d_cancel": _handle_gate_d_cancel,
    "gate_d_resume": _handle_gate_d_resume,
    "gate_d_extend": _handle_gate_d_extend,
    "gate_d_retry": _handle_gate_d_retry,
    # v1.1.1 — Locked takeover
    "locked_critique": _handle_locked_critique,
    "locked_edit": _handle_locked_edit,
    "locked_give_up": _handle_locked_give_up,
    # v1.1.1 — generic defer (gate carried in payload.gate)
    "defer": _handle_defer,
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
