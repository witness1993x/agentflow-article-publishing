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
* Write-side actions that may take a while return immediately and spawn the
  corresponding ``blogflow`` command in the background; completion is reported via
  the AgentFlow event webhook.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from agentflow.agent_review import auth as review_auth
from agentflow.agent_review import state as review_state
from agentflow.agent_review import triggers as review_triggers
from agentflow.agent_review.state import (
    STATE_CHANNEL_PENDING_REVIEW,
    STATE_DRAFT_APPROVED,
    STATE_DRAFT_PENDING_REVIEW,
    STATE_DRAFT_REJECTED,
    STATE_DRAFTING,
    STATE_DRAFTING_LOCKED_HUMAN,
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
    chat_id = operator.get("chat_id")
    if chat_id:
        payload["chat_id"] = chat_id
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
# Auto fan-out helpers — parity with daemon._route's TG-side spawn calls.
#
# After a successful Gate B/C transition, TG fires the next gate's card in a
# background thread. The Lark handlers historically returned only a green
# "通过" card and stopped — so the operator was stranded with no follow-up.
# These helpers replicate TG's fan-out so the Lark loop closes too.
# ---------------------------------------------------------------------------


def _spawn_next_gate_card(article_id: str, *, kind: str) -> None:
    """Post the next-gate card in a background thread. ``kind`` is one of
    ``"image_picker"`` / ``"gate_d"`` — the trigger function chosen by
    TG's daemon for the same state transition."""

    def _run() -> None:
        try:
            if kind == "image_picker":
                review_triggers.post_image_gate_picker(article_id)
            elif kind == "gate_d":
                review_triggers.post_gate_d(article_id)
            else:  # pragma: no cover — guard against typos at the call site
                _log.warning("_spawn_next_gate_card: unknown kind %r", kind)
        except Exception as err:  # pragma: no cover — best-effort fan-out
            _log.warning(
                "Lark fan-out %s failed for %s: %s", kind, article_id, err,
                exc_info=True,
            )

    threading.Thread(target=_run, daemon=True).start()


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
    # Auto-fire the image-gate picker (Q3/Q4) so Gate C lands in Lark
    # without operator polling — TG does the same at daemon._route line ~3414.
    _spawn_next_gate_card(article_id, kind="image_picker")
    response["side_effects"].append("approve_b")
    response["side_effects"].append("image_picker_spawned")
    response["reply_card"] = _make_card(
        title="Gate B 已通过",
        body=(
            f"Article `{article_id}` 已批准 "
            f"(操作人 {operator.get('name') or operator.get('open_id')})。"
            f"\n\n稍候 Gate C 配图选择卡会自动到达本群。"
        ),
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
    """Spawn ``blogflow fill <article_id> --skeleton-only --auto-pick`` from Lark."""
    response = _empty_response()
    actor = _actor_for(operator)
    try:
        review_state.transition(
            article_id,
            gate="B",
            to_state=STATE_DRAFTING,
            actor=actor,
            decision="refill_via_lark",
        )
    except StateError as err:
        response["side_effects"].append("already_handled")
        response["reply_card"] = _state_error_card(action="refill", err=err)
        _telemetry(
            event_kind="card_action",
            action="refill",
            article_id=article_id,
            operator=operator,
            outcome="already_handled",
        )
        return response

    try:
        append_memory_event(
            "lark_refill_requested",
            article_id=article_id,
            payload={
                "operator_open_id": operator.get("open_id"),
                "operator_name": operator.get("name"),
                "mode": "skeleton_only_auto_pick",
            },
        )
    except Exception:
        _log.warning("failed to record lark_refill_requested", exc_info=True)

    argv = _af_executable() + [
        "fill",
        article_id,
        "--skeleton-only",
        "--auto-pick",
        "--json",
    ]
    spawned = _spawn_async(argv, article_id=article_id, action="refill")
    if spawned:
        response["side_effects"].append("refill_spawned")
        response["reply_card"] = _kicked_off_card(action="refill", article_id=article_id)
    else:
        response["side_effects"].append("spawn_failed")
        response["reply_card"] = _make_card(
            title="Refill 启动失败",
            body=f"无法启动 fill 子进程 (`{article_id}`)",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action="refill",
        article_id=article_id,
        operator=operator,
        outcome="spawned" if spawned else "spawn_failed",
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
            f"Article `{article_id}` 已发送 Locked Takeover 卡片到可用审核通道。"
            if fired
            else f"Article `{article_id}` 未发送 (审核通道未配置或缺少目标会话)。"
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
    """Resolve the media/blog CLI argv prefix the same way agent_review.web does.

    Prefer the distinct `blogflow` / `mediaflow` shims on PATH; keep `af` only
    as a legacy fallback for old installs. If none exist, fall back to
    `python -m agentflow.cli.commands` so the Lark bridge still works in CI.
    """
    for cli_name in ("blogflow", "mediaflow", "af"):
        if _which(cli_name):
            return [cli_name]
    return [sys.executable, "-m", "agentflow.cli.commands"]


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


def _spawn_publish_dispatch(
    article_id: str,
    platforms: list[str],
    *,
    operator: dict[str, Any],
) -> bool:
    """Run the same Gate D dispatch chain used by Telegram's PD:dispatch."""
    try:
        append_memory_event(
            "lark_gate_d_dispatch_requested",
            article_id=article_id,
            payload={
                "platforms": list(platforms),
                "operator_open_id": operator.get("open_id"),
                "operator_name": operator.get("name"),
            },
        )
    except Exception:
        _log.warning("lark_gate_d_dispatch_requested memory append failed", exc_info=True)

    def _run() -> None:
        try:
            review_triggers.post_publish_dispatch(article_id, list(platforms))
        except Exception:
            _log.warning("Lark Gate D dispatch failed for %s", article_id, exc_info=True)

    try:
        threading.Thread(target=_run, daemon=True).start()
        return True
    except Exception:
        _log.warning("failed to start Lark Gate D dispatch thread", exc_info=True)
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


def _payload_text(payload: dict[str, Any]) -> str:
    """Extract textarea-style user input from common card payload shapes."""
    for key in (
        "comment",
        "edit_text",
        "editText",
        "instruction",
        "prompt",
        "feedback",
        "text",
        "value",
    ):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    form = payload.get("form")
    if isinstance(form, dict):
        return _payload_text(form)
    return ""


def _parse_edit_instruction(text: str) -> tuple[str | None, int | None, str]:
    """Parse TG-compatible edit text: `title ...`, `opening ...`, `2 ...`."""
    import re

    m = re.match(
        r"^(title|opening|closing|\d+)\s+(.+)$",
        text.strip(),
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None, None, text.strip()
    scope = m.group(1).lower()
    body = m.group(2).strip()
    if scope in {"title", "opening", "closing"}:
        return scope, None, body
    return None, int(scope), body


def _spawn_edit_from_payload(
    *,
    article_id: str,
    operator: dict[str, Any],
    payload: dict[str, Any],
    action: str,
    fallback_section_index: Any = None,
    fallback_paragraph_index: Any = None,
) -> dict[str, Any]:
    response = _empty_response()
    edit_text = _payload_text(payload)
    if not edit_text:
        response["side_effects"].append(f"{action}_missing_text")
        response["reply_card"] = _make_card(
            title="缺少修改内容",
            body="没有收到输入框文本或 @bot 消息正文。",
            template="orange",
        )
        return response

    parsed_target, parsed_section, command_text = _parse_edit_instruction(edit_text)
    target = str(payload.get("target") or parsed_target or "").strip().lower()
    section_index = payload.get("section_index")
    if section_index is None:
        section_index = parsed_section if parsed_section is not None else fallback_section_index
    paragraph_index = payload.get("paragraph_index")
    if paragraph_index is None:
        paragraph_index = fallback_paragraph_index

    argv = _af_executable() + [
        "edit",
        article_id,
        "--command",
        command_text,
        "--post-review",
        "--json",
    ]
    if target in {"title", "opening", "closing"}:
        argv.extend(["--target", target])
    elif section_index is not None:
        argv.extend(["--section", str(section_index)])
        if paragraph_index is not None:
            argv.extend(["--paragraph", str(paragraph_index)])
        target = "section"
    else:
        response["side_effects"].append(f"{action}_missing_target")
        response["reply_card"] = _make_card(
            title="修改意见已收到，但缺少目标段落",
            body=(
                f"`{article_id}` 没有可用的 section_index / target。\n\n"
                "请在消息开头带目标，例如 `title 标题更锋利`、"
                "`opening 开头更短`、`2 第二节补数据`。"
            ),
            template="orange",
        )
        _telemetry(
            event_kind="card_action",
            action=action,
            article_id=article_id,
            operator=operator,
            outcome="missing_target",
            extra={"has_text": True},
        )
        return response

    spawned = _spawn_async(argv, article_id=article_id, action=action)
    try:
        append_memory_event(
            "lark_edit_submitted",
            article_id=article_id,
            payload={
                "operator_open_id": operator.get("open_id"),
                "operator_name": operator.get("name"),
                "section_index": section_index,
                "paragraph_index": paragraph_index,
                "target": target,
                "text": command_text,
                "source_action": action,
            },
        )
    except Exception:
        _log.warning("lark_edit_submitted memory append failed", exc_info=True)
    if spawned:
        response["side_effects"].append(f"{action}_spawned")
        response["reply_card"] = _kicked_off_card(action=action, article_id=article_id)
    else:
        response["side_effects"].append("spawn_failed")
        response["reply_card"] = _make_card(
            title="修改启动失败",
            body=f"无法启动 edit 子进程 (`{article_id}`)",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action=action,
        article_id=article_id,
        operator=operator,
        outcome="spawned" if spawned else "spawn_failed",
        extra={
            "section_index": section_index,
            "paragraph_index": paragraph_index,
            "target": target,
            "has_text": True,
        },
    )
    return response


# ---------------------------------------------------------------------------
# Gate A handlers (write / reject_all / expand / defer)
# ---------------------------------------------------------------------------


def _handle_gate_a_write(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Kick off ``blogflow write <hotspot_id> --auto-pick`` in the background.

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
    """Defer the gate by N hours via the deferred-repost store.

    Mirrors `_handle_chrome_defer` (L-3) but for the per-card `lark_defer`
    button. Writes a real entry to ~/.agentflow/review/deferred_reposts.json
    so the daemon sweeper re-emits the gate card on schedule. Without this,
    the button was ack-only — operators saw '已延后' but the card never
    reposted (latent bug discovered during L-3 implementation).

    payload.gate (required): "A" | "B" | "C" | "D"
    payload.hours (default 4): re-post the gate card after this many hours
    """
    response = _empty_response()
    gate = str(payload.get("gate") or "").strip()
    if gate not in {"A", "B", "C", "D"}:
        response["side_effects"].append("bad_gate")
        response["reply_card"] = _make_card(
            title="❌ defer 参数错误",
            body=f"gate 必须是 A/B/C/D，收到: `{gate}`",
            template="red",
        )
        return response
    try:
        hours = float(payload.get("hours") or 4)
    except (TypeError, ValueError):
        hours = 4.0
    if hours <= 0:
        response["side_effects"].append("bad_hours")
        response["reply_card"] = _make_card(
            title="❌ defer hours 参数错",
            body=f"hours 必须为正数: `{hours}`",
            template="red",
        )
        return response
    # Wire to the real deferred-repost store (same one TG /defer and
    # chrome_defer feed). Lazy import to keep daemon out of module-load chain.
    try:
        from agentflow.agent_review import daemon as _daemon_mod
        _daemon_mod._schedule_deferred_repost(
            gate=gate,
            article_id=article_id,
            batch_path=None,
            hours=hours,
            source_sid=f"lark_button:{operator.get('open_id') or '?'}",
        )
    except Exception as err:
        response["side_effects"].append("schedule_failed")
        response["reply_card"] = _make_card(
            title="❌ defer 调度失败",
            body=str(err)[:300],
            template="red",
        )
        _telemetry(
            event_kind="card_action",
            action="defer",
            article_id=article_id,
            operator=operator,
            outcome="schedule_failed",
            extra={"hours": hours, "gate": gate, "error": str(err)[:200]},
        )
        return response
    response["side_effects"].append("deferred")
    response["reply_card"] = _make_card(
        title=f"⏰ Gate {gate} 已推迟 {hours}h",
        body=f"`{article_id}` Gate {gate} 已推迟 `{hours}h`，到时会重新推卡。",
        template="grey",
    )
    _telemetry(
        event_kind="card_action",
        action="defer",
        article_id=article_id,
        operator=operator,
        outcome="ok",
        extra={"gate": gate, "hours": hours},
    )
    return response


# ---------------------------------------------------------------------------
# Gate B remaining handlers (rewrite / edit / diff)
# ---------------------------------------------------------------------------


def _handle_gate_b_rewrite(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Spawn ``blogflow fill --rewrite`` in the background, transition state to drafting."""
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
    """Apply inline edit text, or open an interactive edit slot."""
    response = _empty_response()
    section_index = payload.get("section_index")
    paragraph_index = payload.get("paragraph_index")
    edit_text = _payload_text(payload)

    if edit_text:
        return _spawn_edit_from_payload(
            article_id=article_id,
            operator=operator,
            payload=payload,
            action="gate_b_edit",
            fallback_section_index=section_index,
            fallback_paragraph_index=paragraph_index,
        )

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
    _spawn_next_gate_card(article_id, kind="gate_d")
    response["side_effects"].append("gate_c_approve")
    response["side_effects"].append("gate_d_spawned")
    response["reply_card"] = _make_card(
        title="Gate C 配图已通过",
        body=f"`{article_id}` 配图通过，Gate D 渠道挑选卡稍后自动到达本群。",
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
    _spawn_next_gate_card(article_id, kind="gate_d")
    response["side_effects"].append("gate_c_skip")
    response["side_effects"].append("gate_d_spawned")
    response["reply_card"] = _make_card(
        title="Gate C 配图已跳过",
        body=f"`{article_id}` 不配图直接进入 Gate D，渠道挑选卡稍后到达本群。",
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
    """Spawn ``blogflow image-gate <id> --mode regen``."""
    response = _empty_response()
    mode = str(payload.get("mode") or "auto")
    argv = _af_executable() + ["image-gate", article_id, "--mode", mode, "--json"]
    prompt = _payload_text(payload)
    if prompt:
        argv.extend(["--cover-description", prompt])
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
        extra={"mode": mode, "has_inline_prompt": bool(prompt)},
    )
    return response


def _handle_gate_c_relogo(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Spawn ``blogflow image-gate <id> --logo-only``."""
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


def _handle_image_gate_pick(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Handle the soft image-gate picker sent after Gate B approval."""
    response = _empty_response()
    mode = str(payload.get("mode") or "cover-only").strip()
    prompt = _payload_text(payload)
    if mode in {"none", "skip", "off"}:
        actor = _actor_for(operator)
        try:
            review_state.transition(
                article_id,
                gate="C",
                to_state=STATE_IMAGE_SKIPPED,
                actor=actor,
                decision="image_skip_via_lark",
            )
        except StateError:
            response["side_effects"].append("already_handled")
        try:
            review_triggers.post_gate_d(article_id)
        except Exception:
            _log.warning(
                "post_gate_d after image skip failed for %s", article_id, exc_info=True
            )
        response["side_effects"].append("image_gate_skipped")
        response["reply_card"] = _make_card(
            title="已跳过配图",
            body=f"`{article_id}` 已进入 Gate D 渠道选择。",
            template="grey",
        )
    else:
        argv = _af_executable() + ["image-gate", article_id, "--mode", mode, "--json"]
        if prompt:
            argv.extend(["--cover-description", prompt])
        if _spawn_async(argv, article_id=article_id, action="image_gate_pick"):
            response["side_effects"].append("image_gate_pick_spawned")
            response["reply_card"] = _kicked_off_card(
                action=f"image_gate({mode})", article_id=article_id
            )
        else:
            response["side_effects"].append("spawn_failed")
            response["reply_card"] = _make_card(
                title="图片流程启动失败",
                body=f"image-gate 子进程无法启动 (`{article_id}`)",
                template="red",
            )
    _telemetry(
        event_kind="card_action",
        action="image_gate_pick",
        article_id=article_id,
        operator=operator,
        outcome=(
            "spawned"
            if "image_gate_pick_spawned" in response["side_effects"]
            else "ok"
        ),
        extra={"mode": mode, "has_inline_prompt": bool(prompt)},
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
    """Run the full Gate D dispatch chain for the selected platforms."""
    response = _empty_response()
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
    if _spawn_publish_dispatch(article_id, sel, operator=operator):
        response["side_effects"].append("gate_d_dispatch_spawned")
        response["reply_card"] = _kicked_off_card(
            action=f"dispatch({', '.join(sel)})", article_id=article_id
        )
    else:
        response["side_effects"].append("spawn_failed")
        response["reply_card"] = _make_card(
            title="分发启动失败",
            body=f"Gate D dispatch 无法启动 (`{article_id}`)",
            template="red",
        )
    _telemetry(
        event_kind="card_action",
        action="gate_d_confirm",
        article_id=article_id,
        operator=operator,
        outcome="spawned" if "gate_d_dispatch_spawned" in response["side_effects"] else "spawn_failed",
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
                body=f"`{article_id}` 渠道选择卡已重新推送。",
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


def _latest_pending_edit(article_id: str, operator: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Find the latest pending Lark edit slot for this article/operator."""
    operator_id = operator.get("open_id")
    consumed: set[tuple[str, str]] = set()
    try:
        consumed_events = read_memory_events(
            article_id=article_id, event_type="lark_pending_edit_consumed"
        )
    except Exception:
        consumed_events = []
    for ev in consumed_events:
        payload = ev.get("payload") or {}
        event_type = str(payload.get("pending_event_type") or "")
        event_ts = str(payload.get("pending_event_ts") or "")
        if event_type and event_ts:
            consumed.add((event_type, event_ts))

    candidates: list[tuple[str, dict[str, Any]]] = []
    for event_type in ("lark_edit_pending", "lark_locked_edit_pending"):
        try:
            events = read_memory_events(article_id=article_id, event_type=event_type)
        except Exception:
            events = []
        for ev in events:
            event_ts = str(ev.get("ts") or "")
            if (event_type, event_ts) in consumed:
                continue
            payload = ev.get("payload") or {}
            if operator_id and payload.get("operator_open_id") not in {None, operator_id}:
                continue
            candidates.append((event_type, ev))
    if not candidates:
        return None
    candidates.sort(key=lambda item: str(item[1].get("ts") or ""))
    return candidates[-1]


def _handle_apply_pending_edit(
    *, article_id: str, operator: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Apply the next @-bot message to the latest pending Lark edit slot."""
    pending = _latest_pending_edit(article_id, operator)
    if pending is None:
        response = _empty_response()
        response["side_effects"].append("pending_edit_not_found")
        response["reply_card"] = _make_card(
            title="没有待处理的修改槽位",
            body=f"`{article_id}` 没有找到最近的 Lark edit pending 事件。",
            template="grey",
        )
        _telemetry(
            event_kind="message",
            action="apply_pending_edit",
            article_id=article_id,
            operator=operator,
            outcome="pending_not_found",
        )
        return response

    event_type, event = pending
    pending_payload = event.get("payload") or {}
    response = _spawn_edit_from_payload(
        article_id=article_id,
        operator=operator,
        payload=payload,
        action="apply_pending_edit",
        fallback_section_index=pending_payload.get("section_index"),
        fallback_paragraph_index=pending_payload.get("paragraph_index"),
    )
    try:
        append_memory_event(
            "lark_pending_edit_consumed",
            article_id=article_id,
            payload={
                "operator_open_id": operator.get("open_id"),
                "pending_event_type": event_type,
                "pending_event_ts": event.get("ts"),
                "side_effects": list(response.get("side_effects") or []),
            },
        )
    except Exception:
        _log.warning("lark_pending_edit_consumed memory append failed", exc_info=True)
    return response


# ---------------------------------------------------------------------------
# Suggestions (Gate S) — Lark parity with daemon._route's S:review/apply/dismiss
# branches. The store is on-disk JSON under ``constraint_suggestions_dir()``;
# the daemon-side mutation primitives (``review_suggestion`` / ``apply_suggestion``
# / ``update_suggestion_status`` / ``list_suggestions``) are reused as-is.
# ---------------------------------------------------------------------------


def _suggestion_id_from(payload: dict[str, Any]) -> str:
    """Extract ``suggestion_id`` from a card payload (tolerates a few aliases
    the OpenClaw side may post)."""
    for key in ("suggestion_id", "id", "sid"):
        raw = payload.get(key)
        if raw:
            return str(raw)
    return ""


def _missing_suggestion_card(operator: dict[str, Any]) -> dict[str, Any]:
    return _make_card(
        title="❌ 缺少 suggestion_id",
        body=(
            f"操作者 `{operator.get('name') or operator.get('open_id')}` 触发了 "
            f"suggestion 操作，但 payload 没有 `suggestion_id`。"
        ),
        template="red",
    )


def _handle_suggestion_list(
    *,
    operator: dict[str, Any],
    payload: dict[str, Any],
    article_id: str | None = None,
) -> dict[str, Any]:
    """Re-emit the pending-suggestions list card (the "返回列表" button)."""
    response = _empty_response()
    try:
        from agentflow.shared.topic_profile_lifecycle import list_suggestions

        suggestions = list_suggestions(status="pending")
    except Exception as err:  # pragma: no cover — store I/O best-effort
        response["side_effects"].append("suggestion_list_error")
        response["reply_text"] = f"无法读取 suggestion 列表: {err}"
        _telemetry(
            event_kind="card_action",
            action="suggestion_list",
            article_id=article_id,
            operator=operator,
            outcome="error",
            extra={"error": str(err)},
        )
        return response
    try:
        review_triggers._emit_lark_suggestion_list_card(suggestions=suggestions)
    except Exception:  # pragma: no cover — emit is best-effort
        _log.warning("suggestion list re-emit failed", exc_info=True)
    response["side_effects"].append("suggestion_list_emitted")
    response["reply_card"] = _make_card(
        title="📋 Pending Suggestions",
        body=f"已刷新 suggestion 列表（{len(suggestions)} 条）。",
        template="blue",
    )
    _telemetry(
        event_kind="card_action",
        action="suggestion_list",
        article_id=article_id,
        operator=operator,
        outcome="ok",
        extra={"count": len(suggestions)},
    )
    return response


def _handle_suggestion_review(
    *,
    operator: dict[str, Any],
    payload: dict[str, Any],
    article_id: str | None = None,
) -> dict[str, Any]:
    """Open a single suggestion in detail (parity with TG ``S:review``).

    Loads via ``review_suggestion`` (which also computes preview_profile) and
    re-emits ``review.suggestion_review_card`` so OpenClaw can render the
    detail card. Returns a small ack card to the operator.
    """
    response = _empty_response()
    suggestion_id = _suggestion_id_from(payload)
    if not suggestion_id:
        response["side_effects"].append("missing_suggestion_id")
        response["reply_card"] = _missing_suggestion_card(operator)
        _telemetry(
            event_kind="card_action",
            action="suggestion_review",
            article_id=article_id,
            operator=operator,
            outcome="missing_suggestion_id",
        )
        return response
    try:
        from agentflow.shared.topic_profile_lifecycle import review_suggestion

        data = review_suggestion(suggestion_id)
        suggestion = data.get("suggestion") or {}
    except Exception as err:
        response["side_effects"].append("suggestion_review_error")
        response["reply_text"] = f"读取 suggestion 失败: {err}"
        _telemetry(
            event_kind="card_action",
            action="suggestion_review",
            article_id=article_id,
            operator=operator,
            outcome="error",
            extra={"error": str(err), "suggestion_id": suggestion_id},
        )
        return response
    try:
        review_triggers._emit_lark_suggestion_review_card(suggestion=suggestion)
    except Exception:  # pragma: no cover — emit is best-effort
        _log.warning("suggestion review emit failed", exc_info=True)
    response["side_effects"].append("suggestion_review_emitted")
    response["reply_card"] = _make_card(
        title="🧩 Suggestion Review",
        body=(
            f"已打开 suggestion `{suggestion_id}` 的详情卡。\n"
            f"profile=`{suggestion.get('profile_id') or '?'}` "
            f"stage=`{suggestion.get('stage') or '?'}`"
        ),
        template="blue",
    )
    _telemetry(
        event_kind="card_action",
        action="suggestion_review",
        article_id=article_id,
        operator=operator,
        outcome="ok",
        extra={"suggestion_id": suggestion_id},
    )
    return response


def _handle_suggestion_apply(
    *,
    operator: dict[str, Any],
    payload: dict[str, Any],
    article_id: str | None = None,
) -> dict[str, Any]:
    """Apply a suggestion to the user topic profile (parity with TG ``S:apply``).

    This actually mutates the profile via ``apply_suggestion`` and marks the
    suggestion ``status="applied"``. Auth is gated on ``edit`` (not ``review``)
    because applying is a write.
    """
    response = _empty_response()
    suggestion_id = _suggestion_id_from(payload)
    if not suggestion_id:
        response["side_effects"].append("missing_suggestion_id")
        response["reply_card"] = _missing_suggestion_card(operator)
        _telemetry(
            event_kind="card_action",
            action="suggestion_apply",
            article_id=article_id,
            operator=operator,
            outcome="missing_suggestion_id",
        )
        return response
    try:
        from agentflow.shared.topic_profile_lifecycle import apply_suggestion

        result = apply_suggestion(suggestion_id)
        suggestion = result.get("suggestion") or {}
    except Exception as err:
        response["side_effects"].append("suggestion_apply_error")
        response["reply_card"] = _make_card(
            title="❌ 应用 suggestion 失败",
            body=f"`{suggestion_id}` 应用失败：{err}",
            template="red",
        )
        _telemetry(
            event_kind="card_action",
            action="suggestion_apply",
            article_id=article_id,
            operator=operator,
            outcome="error",
            extra={"error": str(err), "suggestion_id": suggestion_id},
        )
        return response
    response["side_effects"].append("suggestion_applied")
    profile_id = str(suggestion.get("profile_id") or "?")
    response["reply_card"] = _make_card(
        title="✅ Suggestion 已应用",
        body=(
            f"`{suggestion_id}` 已合并到 profile `{profile_id}` "
            f"(操作人 {operator.get('name') or operator.get('open_id')})。"
        ),
        template="green",
    )
    _telemetry(
        event_kind="card_action",
        action="suggestion_apply",
        article_id=article_id,
        operator=operator,
        outcome="ok",
        extra={"suggestion_id": suggestion_id, "profile_id": profile_id},
    )
    return response


def _handle_suggestion_dismiss(
    *,
    operator: dict[str, Any],
    payload: dict[str, Any],
    article_id: str | None = None,
) -> dict[str, Any]:
    """Mark a suggestion as dismissed (parity with TG ``S:dismiss``)."""
    response = _empty_response()
    suggestion_id = _suggestion_id_from(payload)
    if not suggestion_id:
        response["side_effects"].append("missing_suggestion_id")
        response["reply_card"] = _missing_suggestion_card(operator)
        _telemetry(
            event_kind="card_action",
            action="suggestion_dismiss",
            article_id=article_id,
            operator=operator,
            outcome="missing_suggestion_id",
        )
        return response
    try:
        from agentflow.shared.topic_profile_lifecycle import update_suggestion_status

        update_suggestion_status(suggestion_id, "dismissed")
    except Exception as err:
        response["side_effects"].append("suggestion_dismiss_error")
        response["reply_card"] = _make_card(
            title="❌ 忽略 suggestion 失败",
            body=f"`{suggestion_id}` 忽略失败：{err}",
            template="red",
        )
        _telemetry(
            event_kind="card_action",
            action="suggestion_dismiss",
            article_id=article_id,
            operator=operator,
            outcome="error",
            extra={"error": str(err), "suggestion_id": suggestion_id},
        )
        return response
    response["side_effects"].append("suggestion_dismissed")
    response["reply_card"] = _make_card(
        title="🚫 Suggestion 已忽略",
        body=(
            f"`{suggestion_id}` 已标记为 dismissed "
            f"(操作人 {operator.get('name') or operator.get('open_id')})。"
        ),
        template="grey",
    )
    _telemetry(
        event_kind="card_action",
        action="suggestion_dismiss",
        article_id=article_id,
        operator=operator,
        outcome="ok",
        extra={"suggestion_id": suggestion_id},
    )
    return response


# ---------------------------------------------------------------------------
# Profile multi-turn follow-up (Gate P) — daemon-driven question advance.
#
# TG drives multi-turn profile setup via ``render_profile_setup_question``:
# every operator reply triggers another question card until all missing
# fields are collected. On Lark, the same flow is captured by emitting
# ``review.profile_setup_card`` in *question-advance* form per question.
#
# The handler here owns the daemon side: extract the answer, write it to
# the session's ``collected`` map, find the next missing field, and either
# emit the next question card or release the session + emit
# ``notify.profile_setup_done``.
# ---------------------------------------------------------------------------


# Field key → operator-facing question. Missing-field keys are produced by
# ``topic_profile_lifecycle.profile_missing_fields``; this table covers all
# the keys it can return. Keys not in this table fall back to a generic
# template so a future field addition doesn't crash the handler.
_PROFILE_FIELD_QUESTIONS: dict[str, str] = {
    "publisher_account.brand": (
        "请输入品牌/账号显示名（产品名、公司名或专栏名）。"
    ),
    "publisher_account.voice": (
        "请确认写作身份（first_party_brand / observer / personal）。"
    ),
    "publisher_account.output_language": (
        "请输入默认输出语言（zh-Hans / zh-Hant / en / bilingual）。"
    ),
    "publisher_account.product_facts": (
        "请粘贴产品事实/账号说明/可学习语料，每行一条。"
    ),
    "keyword_groups.core": (
        "请列出核心关键词（每行一条），用于热点扫描和内容定位。"
    ),
    "search_queries": (
        "请列出热点搜索 query（每行一条），用于 D1 扫描。"
    ),
    "avoid_terms": (
        "请列出需要规避的词或话题（每行一条），可跳过。"
    ),
}


def _question_text_for(field: str) -> str:
    """Map a missing-field key to an operator-facing question string."""
    canned = _PROFILE_FIELD_QUESTIONS.get(field)
    if canned:
        return canned
    return f"请输入 `{field}` 的值。"


# Map: dotted-key (used in Lark missing_fields[] / collected[]) → friendly
# slot name (used by ``build_patch_from_answers``). The friendly slots are
# the keys ``build_patch_from_answers`` reads from the ``answers`` dict:
# brand / voice / output_language / do / dont / product_facts / core_terms
# / search_queries / avoid_terms. The dotted keys come from
# ``profile_missing_fields`` and the ``_PROFILE_FIELD_QUESTIONS`` table.
_PROFILE_FIELD_TO_SLOT: dict[str, str] = {
    "publisher_account.brand": "brand",
    "publisher_account.voice": "voice",
    "publisher_account.output_language": "output_language",
    "publisher_account.product_facts": "product_facts",
    "publisher_account.do": "do",
    "publisher_account.dont": "dont",
    "publisher_account.default_tags": "default_tags",
    "keyword_groups.core": "core_terms",
    "search_queries": "search_queries",
    "avoid_terms": "avoid_terms",
}


# Friendly slots that should be parsed as a list. ``build_patch_from_answers``
# routes these through ``_flatten_terms`` which keeps a single string as a
# single-item list — so we must pre-split comma/newline-separated free-form
# operator answers before handing them off.
_LIST_VALUED_SLOTS: frozenset[str] = frozenset(
    {
        "do",
        "dont",
        "product_facts",
        "default_tags",
        "core_terms",
        "search_queries",
        "avoid_terms",
    }
)


def _split_profile_terms(raw: str) -> list[str]:
    """Split a free-form operator answer into a list of terms.

    Mirrors TG's ``daemon._split_profile_terms`` separator policy: comma
    (ASCII + 中文), semicolon (ASCII + 中文), enumeration comma 、, newline.
    Strips per-line bullet/leading dash whitespace.
    """
    text = (
        str(raw or "")
        .replace("；", "\n")
        .replace(";", "\n")
        .replace("、", "\n")
        .replace(",", "\n")
        .replace("，", "\n")
    )
    lines = [line.strip(" -\t") for line in text.splitlines()]
    return [line for line in lines if line]


def _collected_to_slot_dict(collected: dict[str, Any]) -> dict[str, Any]:
    """Translate dotted-key answers to a friendly-slot ``answers`` dict.

    Unknown dotted keys pass through unchanged so a future field addition
    doesn't silently drop the answer. List-valued slots receive a
    comma/newline split if the operator submitted a single string blob.
    """
    out: dict[str, Any] = {}
    for key, value in (collected or {}).items():
        slot = _PROFILE_FIELD_TO_SLOT.get(str(key), str(key))
        if slot in _LIST_VALUED_SLOTS and isinstance(value, str):
            out[slot] = _split_profile_terms(value)
        else:
            out[slot] = value
    return out


def _session_id_from_path(session_path: str) -> str:
    """Extract the session id from a session JSON path.

    ``constraint_sessions_dir() / "<id>.json"`` — strip the parent + ``.json``
    suffix. Missing/blank paths yield ``""`` so the caller can short-circuit
    with a deny card.
    """
    raw = str(session_path or "").strip()
    if not raw:
        return ""
    name = Path(raw).name
    if name.endswith(".json"):
        name = name[: -len(".json")]
    return name


def _answer_from_payload(payload: dict[str, Any]) -> str:
    """Extract the operator's free-form answer from the card payload.

    Aliases (priority order): ``payload.text`` then ``payload.answer``.
    """
    for key in ("text", "answer"):
        raw = payload.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return text
    return ""


def _missing_answer_card(operator: dict[str, Any]) -> dict[str, Any]:
    name = operator.get("name") or operator.get("open_id") or "(unknown)"
    return _make_card(
        title="❌ 请输入回答",
        body=(
            f"操作者 `{name}` 触发了 profile 推进，但 payload 没有 `text`/`answer` 文本。"
        ),
        template="red",
    )


def _missing_session_card(operator: dict[str, Any]) -> dict[str, Any]:
    name = operator.get("name") or operator.get("open_id") or "(unknown)"
    return _make_card(
        title="❌ 找不到 profile session",
        body=(
            f"无法为 `{name}` 找到或创建 active profile session — payload 是否缺少 `session_path`？"
        ),
        template="red",
    )


def _next_missing_field(session: dict[str, Any]) -> str | None:
    """Compute the next field still needing an answer in this session.

    Strategy: prefer the session's pre-computed ``missing_fields`` list,
    skipping anything already present in ``collected``. If
    ``missing_fields`` isn't set we re-derive from the profile via
    ``profile_missing_fields``.
    """
    collected = session.get("collected") or {}
    if not isinstance(collected, dict):
        collected = {}
    missing = session.get("missing_fields") or []
    if not isinstance(missing, list) or not missing:
        try:
            from agentflow.shared.topic_profile_lifecycle import (
                profile_missing_fields,
                user_profile_bootstrap_state,
            )

            state_data = user_profile_bootstrap_state(
                str(session.get("profile_id") or "")
            )
            missing = list(state_data.get("missing_fields") or [])
        except Exception:
            missing = []
    for field in missing:
        if str(field) in collected:
            continue
        return str(field)
    return None


def _handle_profile_advance(
    *,
    operator: dict[str, Any],
    payload: dict[str, Any],
    article_id: str | None = None,
) -> dict[str, Any]:
    """Daemon-driven profile multi-turn follow-up.

    Flow:
      1. Validate the answer + session_path are present.
      2. Auth via ``_authorize_or_deny_v2`` with action="profile_advance"
         (mapped to required="review").
      3. Find or claim active session (prefer existing claim by open_id;
         else claim the session named in ``session_path``).
      4. Write the answer into ``session["collected"][question_field]``
         and persist.
      5. If a next missing field exists → emit a fresh
         ``review.profile_setup_card`` (question-advance form) for it and
         return a "下一题已发出" ack card.
      6. Otherwise → release the session with status="completed", emit
         ``notify.profile_setup_done``, return a green summary card.
    """
    response = _empty_response()

    profile_id = str(payload.get("profile_id") or "").strip()
    session_path = str(payload.get("session_path") or "").strip()
    question_field = str(payload.get("question_field") or "").strip()
    answer = _answer_from_payload(payload)

    # Intro form (no question_field yet) is allowed to arrive without an
    # answer — it just claims the session and emits the first question.
    if question_field and not answer:
        response["side_effects"].append("missing_answer")
        response["reply_card"] = _missing_answer_card(operator)
        _telemetry(
            event_kind="card_action",
            action="profile_advance",
            article_id=article_id,
            operator=operator,
            outcome="missing_answer",
        )
        return response

    session_id = _session_id_from_path(session_path)
    if not session_id:
        response["side_effects"].append("missing_session_path")
        response["reply_card"] = _missing_session_card(operator)
        _telemetry(
            event_kind="card_action",
            action="profile_advance",
            article_id=article_id,
            operator=operator,
            outcome="missing_session_path",
        )
        return response

    open_id = str(operator.get("open_id") or "")
    chat_id = str(operator.get("chat_id") or "")

    try:
        from agentflow.shared.topic_profile_lifecycle import (
            claim_session_lark,
            find_active_session_lark,
            load_session,
            release_session_lark,
            save_session,
        )
    except Exception as err:  # pragma: no cover — import-time issues
        response["side_effects"].append("profile_lifecycle_import_error")
        response["reply_text"] = f"profile lifecycle import failed: {err}"
        return response

    # Resolve session: prefer the operator's already-claimed active session
    # (if it matches profile_id / session_id), else claim by session_path.
    session: dict[str, Any] | None = None
    try:
        active = find_active_session_lark(open_id) if open_id else None
        if active is not None:
            active_id = str(active.get("id") or "")
            if active_id == session_id or (
                profile_id and str(active.get("profile_id") or "") == profile_id
            ):
                session = active
    except Exception:  # pragma: no cover — store I/O best-effort
        session = None

    if session is None:
        try:
            session = claim_session_lark(session_id, open_id, chat_id)
        except FileNotFoundError:
            response["side_effects"].append("session_not_found")
            response["reply_card"] = _missing_session_card(operator)
            _telemetry(
                event_kind="card_action",
                action="profile_advance",
                article_id=article_id,
                operator=operator,
                outcome="session_not_found",
            )
            return response
        except Exception as err:
            response["side_effects"].append("session_claim_error")
            response["reply_text"] = f"无法 claim session: {err}"
            return response

    # Apply the answer to session.collected. ``collected`` may not exist on
    # older sessions (pre-Wave-2); default to empty dict.
    collected = session.get("collected")
    if not isinstance(collected, dict):
        collected = {}
    if question_field:
        collected[question_field] = answer
        session["collected"] = collected
        try:
            save_session(session)
        except Exception as err:  # pragma: no cover — persist failure
            response["side_effects"].append("session_save_error")
            response["reply_text"] = f"保存 session 失败: {err}"
            return response

    next_field = _next_missing_field(session)
    if next_field is not None:
        # More questions remain — emit the next question card.
        try:
            from agentflow.shared.topic_profile_lifecycle import (
                profile_missing_fields,
                user_profile_bootstrap_state,
            )

            missing_total = (
                session.get("missing_fields")
                or list(user_profile_bootstrap_state(
                    str(session.get("profile_id") or "")
                ).get("missing_fields") or [])
            )
        except Exception:
            missing_total = session.get("missing_fields") or [next_field]
        total = len(missing_total)
        try:
            index = list(missing_total).index(next_field)
        except ValueError:
            index = len(collected)

        try:
            review_triggers._emit_lark_profile_question_card(
                session_path=session_path,
                profile_id=str(session.get("profile_id") or profile_id),
                question_field=next_field,
                question_text=_question_text_for(next_field),
                question_index=index,
                total_questions=total,
            )
        except Exception:  # pragma: no cover — emit best-effort
            _log.warning("profile question emit failed", exc_info=True)

        response["side_effects"].append("profile_advance_next_question")
        response["reply_card"] = _make_card(
            title="🧩 已收到，下一题已发出",
            body=(
                f"已记录 `{question_field or '(intro)'}` 的回答；"
                f"下一题 `{next_field}` 已推送（{index + 1}/{total}）。"
            ),
            template="blue",
        )
        _telemetry(
            event_kind="card_action",
            action="profile_advance",
            article_id=article_id,
            operator=operator,
            outcome="next_question",
            extra={
                "session_id": session_id,
                "question_field": question_field,
                "next_field": next_field,
                "index": index,
                "total": total,
            },
        )
        return response

    # No more questions — apply answers + release + notify.
    completed_fields = sorted((session.get("collected") or {}).keys())

    # Writeback: translate dotted-key collected[] → friendly-slot answers
    # dict, build the profile patch, and persist it to topic_profiles.yaml.
    # Failures are non-fatal — the session must still be released and the
    # downstream notify event must still emit so the rest of the pipeline
    # (D1 scan, etc.) is not blocked. The card body surfaces the warning.
    writeback_warning: str | None = None
    writeback_field_count: int | None = None
    try:
        from agentflow.shared.topic_profile_lifecycle import (
            build_patch_from_answers,
            seed_profile,
            upsert_profile,
            user_profile_bootstrap_state,
        )

        target_profile_id = str(session.get("profile_id") or profile_id or "").strip()
        if not target_profile_id:
            raise ValueError("session has no profile_id; cannot write back")
        slot_answers = _collected_to_slot_dict(session.get("collected") or {})
        existing = user_profile_bootstrap_state(target_profile_id).get("current_profile")
        if not isinstance(existing, dict):
            existing = seed_profile(target_profile_id)
        patch = build_patch_from_answers(
            target_profile_id,
            slot_answers,
            existing_profile=existing,
        )
        upsert_profile(
            target_profile_id,
            patch,
            replace_lists=False,
            source=f"lark_profile_advance:{session_id}",
        )
        writeback_field_count = len(completed_fields)
    except Exception as err:
        _log.warning("profile yaml writeback failed", exc_info=True)
        writeback_warning = (
            f"答案已保存，但 profile 写回失败: {str(err)[:200]}"
        )

    try:
        release_session_lark(session_id, status="completed")
    except Exception:  # pragma: no cover — release best-effort
        _log.warning("release_session_lark failed", exc_info=True)

    try:
        from agentflow.shared.agent_bridge import emit_agent_event

        emit_agent_event(
            source="agentflow.review",
            event_type="notify.profile_setup_done",
            article_id=str(session.get("profile_id") or profile_id),
            payload={
                "profile_id": str(session.get("profile_id") or profile_id),
                "completed_fields": completed_fields,
                "session_path": session_path,
                "next_action": "d1_scan",
            },
        )
    except Exception:  # pragma: no cover — emit best-effort
        _log.warning("notify.profile_setup_done emit failed", exc_info=True)

    response["side_effects"].append("profile_advance_completed")
    if writeback_warning is None:
        response["side_effects"].append("profile_yaml_written")
    else:
        response["side_effects"].append("profile_yaml_writeback_failed")
    body_lines = [
        f"Profile `{session.get('profile_id') or profile_id}` 已补全 "
        f"{len(completed_fields)} 项。",
    ]
    if completed_fields:
        body_lines.append("已收集字段：" + "、".join(f"`{f}`" for f in completed_fields))
    if writeback_warning is None and writeback_field_count is not None:
        body_lines.append(f"✓ profile 已更新 {writeback_field_count} 个字段")
    elif writeback_warning is not None:
        body_lines.append(writeback_warning)
    response["reply_card"] = _make_card(
        title="✅ Profile setup 完成",
        body="\n".join(body_lines),
        template="green",
    )
    _telemetry(
        event_kind="card_action",
        action="profile_advance",
        article_id=article_id,
        operator=operator,
        outcome="completed",
        extra={
            "session_id": session_id,
            "completed_fields": completed_fields,
        },
    )
    return response


# ---------------------------------------------------------------------------
# Chrome (operator slash-command parity) handlers — GAP-CHROME.
#
# These are the 12 operator-completeness intents (status / list / published /
# scan / jobs / audit_list / auth_debug / suggestions / skip / defer /
# publish_mark / cancel). They are routed into via:
#
#   * the free-text @-bot path (``_route_message_intent`` → keyword match)
#   * the OpenClaw command bridge (``lark_chrome_*`` commands in web.py)
#
# Each handler is fail-closed via ``_authorize_or_deny_v2`` — read-only
# intents need ``review``; mutating ones need ``edit``.
# ---------------------------------------------------------------------------


def _chrome_unauthorized(action: str, operator: dict[str, Any]) -> dict[str, Any] | None:
    """Run the v2 fail-closed auth check; return a deny response or None."""
    return _authorize_or_deny_v2(
        action=action,
        operator=operator,
        article_id=None,
        event_kind="message",
    )


def _read_audit_tail(limit: int = 20) -> list[dict[str, Any]]:
    """Tail the daemon's review/audit.jsonl. Returns newest-first."""
    p = agentflow_home() / "review" / "audit.jsonl"
    if not p.exists():
        return []
    try:
        raw_lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw_lines[-(limit * 4):]:  # over-read; filter below
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(obj)
    return list(reversed(out[-limit:]))


def _read_heartbeat_iso() -> str | None:
    p = agentflow_home() / "review" / "last_heartbeat.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        ts = data.get("timestamp")
        return str(ts) if ts else None
    except (json.JSONDecodeError, OSError):
        return None


def _scan_drafts_meta() -> list[tuple[str, dict[str, Any]]]:
    """Walk ``~/.agentflow/drafts/*/metadata.json`` once; return (id, meta)."""
    drafts = agentflow_home() / "drafts"
    if not drafts.exists():
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for sub in sorted(drafts.iterdir()):
        if not sub.is_dir():
            continue
        meta_path = sub / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            continue
        out.append((sub.name, data))
    return out


def _last_state_of(meta: dict[str, Any]) -> str:
    history = meta.get("gate_history") or []
    if not history:
        return ""
    last = history[-1]
    return str(last.get("to_state") or "")


def _last_ts_of(meta: dict[str, Any]) -> str:
    history = meta.get("gate_history") or []
    if not history:
        return ""
    return str(history[-1].get("timestamp") or "")


def _handle_chrome_status(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_status", operator)
    if deny is not None:
        return deny
    try:
        ids = review_state.articles_in_state(_PENDING_REVIEW_STATES) or []
    except Exception:
        ids = []
    in_review = len(ids)
    last_events = _read_audit_tail(limit=5)
    heartbeat_iso = _read_heartbeat_iso()
    try:
        review_triggers._emit_lark_status_card(
            in_review=in_review,
            last_events=last_events,
            heartbeat_iso=heartbeat_iso,
        )
    except Exception:  # pragma: no cover — emit is best-effort
        _log.warning("chrome_status emit failed", exc_info=True)
    response["side_effects"].append("chrome_status_emitted")
    response["reply_card"] = _make_card(
        title="📊 Daemon Status",
        body=(
            f"Pending review: `{in_review}` 篇\n"
            f"Heartbeat: `{heartbeat_iso or '(unknown)'}`\n"
            f"Recent events: {len(last_events)}"
        ),
        template="blue",
    )
    _telemetry(
        event_kind="message",
        action="chrome_status",
        article_id=None,
        operator=operator,
        outcome="ok",
        extra={"in_review": in_review},
    )
    return response


def _handle_chrome_list(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_list", operator)
    if deny is not None:
        return deny
    items: list[dict[str, Any]] = []
    for aid, meta in _scan_drafts_meta():
        cur = _last_state_of(meta)
        if "_pending_review" not in cur:
            continue
        items.append({
            "article_id": aid,
            "title": str(meta.get("title") or "(no title)"),
            "current_state": cur,
            "last_ts": _last_ts_of(meta),
        })
    items.sort(key=lambda x: x["last_ts"], reverse=True)
    try:
        review_triggers._emit_lark_article_list_card(articles=items)
    except Exception:  # pragma: no cover
        _log.warning("chrome_list emit failed", exc_info=True)
    response["side_effects"].append("chrome_list_emitted")
    response["reply_card"] = _make_card(
        title="📋 In-Review Articles",
        body=f"`{len(items)}` 篇 pending_review。",
        template="blue",
    )
    _telemetry(
        event_kind="message",
        action="chrome_list",
        article_id=None,
        operator=operator,
        outcome="ok",
        extra={"count": len(items)},
    )
    return response


def _handle_chrome_published(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_published", operator)
    if deny is not None:
        return deny
    items: list[dict[str, Any]] = []
    for aid, meta in _scan_drafts_meta():
        cur = _last_state_of(meta)
        if cur != "published":
            continue
        items.append({
            "article_id": aid,
            "title": str(meta.get("title") or "(no title)"),
            "published_at": str(meta.get("published_at") or ""),
            "published_url": meta.get("published_url"),
            "platforms": list(meta.get("published_platforms") or []),
        })
    items.sort(key=lambda x: x["published_at"], reverse=True)
    items = items[:20]
    try:
        review_triggers._emit_lark_published_list_card(articles=items)
    except Exception:  # pragma: no cover
        _log.warning("chrome_published emit failed", exc_info=True)
    response["side_effects"].append("chrome_published_emitted")
    response["reply_card"] = _make_card(
        title="🚀 Recently Published",
        body=f"最近 `{len(items)}` 篇 published。",
        template="blue",
    )
    _telemetry(
        event_kind="message",
        action="chrome_published",
        article_id=None,
        operator=operator,
        outcome="ok",
        extra={"count": len(items)},
    )
    return response


def _handle_chrome_scan(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_scan", operator)
    if deny is not None:
        return deny
    spawned = False
    err_msg: str | None = None
    # Prefer the daemon's _spawn_hotspots (threaded subprocess + spawn-failure
    # notification). Falls back to direct subprocess fire-and-forget.
    try:
        from agentflow.agent_review import daemon as _daemon_mod
        _daemon_mod._spawn_hotspots(top_k=5)
        spawned = True
    except Exception as err1:
        try:
            argv = _af_executable() + [
                "article-hotspots",
                "--gate-a-top-k",
                "5",
                "--json",
            ]
            spawned = _spawn_async(argv, article_id="manual_scan", action="chrome_scan")
        except Exception as err2:  # pragma: no cover — defensive
            err_msg = f"{err1!r}; fallback {err2!r}"
            spawned = False
    try:
        review_triggers._emit_lark_scan_kicked_card(top_k=5, batch_path=None)
    except Exception:  # pragma: no cover
        _log.warning("chrome_scan emit failed", exc_info=True)
    if spawned:
        response["side_effects"].append("chrome_scan_spawned")
        response["reply_card"] = _make_card(
            title="🔎 已开始扫描热点",
            body="`article-hotspots` 已在后台启动，结果将通过 Gate A 卡片回到本群。",
            template="blue",
        )
        _telemetry(
            event_kind="message",
            action="chrome_scan",
            article_id=None,
            operator=operator,
            outcome="spawned",
        )
    else:
        response["side_effects"].append("chrome_scan_failed")
        response["reply_card"] = _make_card(
            title="❌ 扫描启动失败",
            body=f"无法启动 article-hotspots: {err_msg or '(unknown)'}",
            template="red",
        )
        _telemetry(
            event_kind="message",
            action="chrome_scan",
            article_id=None,
            operator=operator,
            outcome="spawn_failed",
            extra={"error": err_msg or ""},
        )
    return response


def _handle_chrome_jobs(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_jobs", operator)
    if deny is not None:
        return deny
    # The daemon's TG /jobs branch shells out to ``blogflow review-cron-status``
    # rather than tracking in-flight subprocesses in-memory. We mirror that
    # contract: jobs surfaced here are the launchd/cron schedule entries.
    # Fail-soft: when the CLI is unavailable, return an empty list rather
    # than crashing the operator's @-bot conversation.
    jobs: list[dict[str, Any]] = []
    detail: str | None = None
    try:
        from agentflow.agent_review.triggers import _af_argv, _run_subprocess  # type: ignore[attr-defined]
        res = _run_subprocess(
            _af_argv("review-cron-status"),
            env=os.environ.copy(),
            timeout=10,
            label="cron-status",
        )
        if res is not None and getattr(res, "returncode", 1) == 0:
            detail = (res.stdout or "").strip()
            installed = "present" in (detail or "") and "loaded" in (detail or "") and "not loaded" not in (detail or "")
            if installed:
                jobs.append({
                    "kind": "launchd_cron",
                    "status": "installed",
                    "raw": (detail or "")[:500],
                })
    except Exception:
        detail = None
    try:
        review_triggers._emit_lark_jobs_card(jobs=jobs)
    except Exception:  # pragma: no cover
        _log.warning("chrome_jobs emit failed", exc_info=True)
    response["side_effects"].append("chrome_jobs_emitted")
    body = (
        f"In-flight jobs: `{len(jobs)}`."
        if jobs
        else "暂无 in-flight 任务（cron 未安装或 review-cron-status 不可用）。"
    )
    response["reply_card"] = _make_card(
        title="⏰ Daemon Jobs",
        body=body,
        template="grey" if not jobs else "blue",
    )
    _telemetry(
        event_kind="message",
        action="chrome_jobs",
        article_id=None,
        operator=operator,
        outcome="ok",
        extra={"count": len(jobs)},
    )
    return response


def _resolve_article_for_chrome(raw_id: str) -> str | None:
    """Map an operator-typed token to a real article_id.

    Accepts the full id or a ``short_id`` resolvable via
    ``agentflow.agent_review.short_id.resolve``. Empty / unresolvable → None.
    """
    raw = (raw_id or "").strip()
    if not raw:
        return None
    # Direct article-id match (drafts/<id>/metadata.json exists)?
    drafts = agentflow_home() / "drafts"
    if (drafts / raw / "metadata.json").exists():
        return raw
    try:
        from agentflow.agent_review import short_id as _sid

        entry = _sid.resolve(raw)
        if entry and entry.get("article_id"):
            return str(entry["article_id"])
    except Exception:
        pass
    return None


def _handle_chrome_skip(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    article_id: str | None = None,
) -> dict[str, Any]:
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_skip", operator)
    if deny is not None:
        return deny
    aid = _resolve_article_for_chrome(article_id or "")
    if not aid:
        response["side_effects"].append("missing_article_id")
        response["reply_card"] = _make_card(
            title="❌ /skip 用法错误",
            body=f"未识别 article id: `{article_id or ''}`",
            template="red",
        )
        return response
    try:
        cur = review_state.current_state(aid)
    except Exception as err:
        response["side_effects"].append("no_article")
        response["reply_card"] = _make_card(
            title="❌ /skip 失败",
            body=f"未知 article: {err}",
            template="red",
        )
        return response
    if cur != STATE_IMAGE_PENDING_REVIEW:
        response["side_effects"].append("wrong_state")
        response["reply_card"] = _make_card(
            title="❌ /skip 不适用",
            body=f"`{aid}` 当前 state=`{cur}`，仅 image_pending_review 可 skip。",
            template="red",
        )
        return response
    try:
        review_state.transition(
            aid,
            gate="C",
            to_state=STATE_IMAGE_SKIPPED,
            actor=_actor_for(operator),
            decision="chrome_skip_via_lark",
            force=True,
        )
    except StateError as err:
        response["side_effects"].append("transition_failed")
        response["reply_card"] = _make_card(
            title="❌ skip 失败", body=str(err), template="red",
        )
        return response
    response["side_effects"].append("chrome_skip_applied")
    response["reply_card"] = _make_card(
        title="🚫 已 skip image-gate",
        body=f"`{aid}` 已 skip。",
        template="blue",
    )
    _telemetry(
        event_kind="message",
        action="chrome_skip",
        article_id=aid,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_chrome_defer(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    article_id: str | None = None,
    hours: float | None = None,
) -> dict[str, Any]:
    """Defer the article's *current* gate by ``hours``. Writes a real entry
    into the deferred-repost store (same path TG ``/defer`` uses); the daemon
    sweeper drains it on schedule and re-emits the gate card via
    ``triggers.post_gate_b`` / ``post_gate_c`` (which already dual-emit on
    both TG + Lark surfaces — design (b), no schema change required).
    """
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_defer", operator)
    if deny is not None:
        return deny
    aid = _resolve_article_for_chrome(article_id or "")
    if not aid:
        response["side_effects"].append("missing_article_id")
        response["reply_card"] = _make_card(
            title="❌ /defer 用法",
            body="用法: `推迟 <article_id> <hours>`",
            template="red",
        )
        return response
    if hours is None or hours <= 0:
        response["side_effects"].append("bad_hours")
        response["reply_card"] = _make_card(
            title="❌ /defer hours 参数错",
            body=f"hours 必须为正数: `{hours}`",
            template="red",
        )
        return response
    # Resolve the article's current gate. Defer is only valid in
    # *_pending_review states (mirrors TG ``_slash_defer`` semantics).
    try:
        cur = review_state.current_state(aid)
    except Exception:
        cur = ""
    gate_for_state = {
        STATE_DRAFT_PENDING_REVIEW: "B",
        STATE_IMAGE_PENDING_REVIEW: "C",
        STATE_CHANNEL_PENDING_REVIEW: "D",
    }
    gate = gate_for_state.get(str(cur or ""))
    if gate is None:
        response["side_effects"].append("wrong_state")
        response["reply_card"] = _make_card(
            title="❌ /defer 状态错误",
            body=f"`/defer` 仅对 *_pending_review 生效, 当前 state=`{cur}`",
            template="red",
        )
        _telemetry(
            event_kind="message",
            action="chrome_defer",
            article_id=aid,
            operator=operator,
            outcome="wrong_state",
            extra={"hours": float(hours), "state": str(cur)},
        )
        return response
    # Wire to the real deferred-repost store (same one TG ``/defer`` and the
    # ``lark_defer`` button feed). Import locally to avoid a hard import-time
    # dep on daemon.py.
    try:
        from agentflow.agent_review import daemon as _daemon_mod
        _daemon_mod._schedule_deferred_repost(
            gate=gate,
            article_id=aid,
            batch_path=None,
            hours=float(hours),
            source_sid=f"lark_chrome:{operator.get('open_id') or '?'}",
        )
    except Exception as err:
        response["side_effects"].append("schedule_failed")
        response["reply_card"] = _make_card(
            title="❌ defer 调度失败",
            body=str(err)[:300],
            template="red",
        )
        _telemetry(
            event_kind="message",
            action="chrome_defer",
            article_id=aid,
            operator=operator,
            outcome="schedule_failed",
            extra={"hours": float(hours), "gate": gate, "error": str(err)[:200]},
        )
        return response
    try:
        append_memory_event(
            "lark_chrome_defer",
            article_id=aid,
            payload={
                "operator_open_id": operator.get("open_id"),
                "hours": float(hours),
                "gate": gate,
            },
        )
    except Exception:  # pragma: no cover — telemetry only
        _log.warning("chrome_defer audit append failed", exc_info=True)
    response["side_effects"].append("chrome_defer_applied")
    response["reply_card"] = _make_card(
        title=f"⏰ Gate {gate} 已推迟 {hours}h",
        body=(
            f"`{aid}` Gate {gate} 已推迟 `{hours}h`，到时会重新推卡。"
        ),
        template="grey",
    )
    _telemetry(
        event_kind="message",
        action="chrome_defer",
        article_id=aid,
        operator=operator,
        outcome="ok",
        extra={"hours": float(hours), "gate": gate},
    )
    return response


def _handle_chrome_publish_mark(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    article_id: str | None = None,
) -> dict[str, Any]:
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_publish_mark", operator)
    if deny is not None:
        return deny
    aid = _resolve_article_for_chrome(article_id or "")
    if not aid:
        response["side_effects"].append("missing_article_id")
        response["reply_card"] = _make_card(
            title="❌ /publish-mark 用法",
            body="用法: `标记已发 <article_id>`",
            template="red",
        )
        return response
    try:
        review_state.transition(
            aid,
            gate="D",
            to_state="published",
            actor=_actor_for(operator),
            decision="chrome_publish_mark_via_lark",
            force=True,
        )
    except StateError as err:
        response["side_effects"].append("transition_failed")
        response["reply_card"] = _make_card(
            title="❌ publish-mark 失败",
            body=str(err),
            template="red",
        )
        return response
    response["side_effects"].append("chrome_publish_mark_applied")
    response["reply_card"] = _make_card(
        title="📌 已标记 published",
        body=f"`{aid}` → published。",
        template="green",
    )
    _telemetry(
        event_kind="message",
        action="chrome_publish_mark",
        article_id=aid,
        operator=operator,
        outcome="ok",
    )
    return response


def _handle_chrome_cancel(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    article_id: str | None = None,
) -> dict[str, Any]:
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_cancel", operator)
    if deny is not None:
        return deny
    aid = _resolve_article_for_chrome(article_id or "")
    if not aid:
        response["side_effects"].append("missing_article_id")
        response["reply_card"] = _make_card(
            title="❌ /cancel 用法",
            body="用法: `取消 <article_id>`",
            template="red",
        )
        return response
    try:
        review_state.transition(
            aid,
            gate="X",
            to_state=STATE_DRAFT_REJECTED,
            actor=_actor_for(operator),
            decision="chrome_cancel_via_lark",
            force=True,
        )
    except StateError as err:
        response["side_effects"].append("transition_failed")
        response["reply_card"] = _make_card(
            title="❌ cancel 失败",
            body=str(err),
            template="red",
        )
        return response
    response["side_effects"].append("chrome_cancel_applied")
    response["reply_card"] = _make_card(
        title="🚫 已取消",
        body=f"`{aid}` 已置为 draft_rejected (终态)。",
        template="grey",
    )
    _telemetry(
        event_kind="message",
        action="chrome_cancel",
        article_id=aid,
        operator=operator,
        outcome="ok",
    )
    return response


_AUDIT_LIST_MAX_N = 100
_AUDIT_LIST_DEFAULT_N = 20


def _handle_view_audit_recent(
    *,
    operator: dict[str, Any],
    payload: dict[str, Any],
    article_id: str | None = None,
) -> dict[str, Any]:
    """Render recent audit-events list card. Article-id-optional.

    Payload shape:
        ``n``       — int, default 20, capped at ``_AUDIT_LIST_MAX_N``
        ``kind``    — optional str, filters entries by their ``kind`` field

    Auth: ``review`` via ``_authorize_or_deny_v2`` (fail-closed).

    On success calls :func:`triggers._emit_lark_audit_list_card` (canonical
    contract per ``templates/lark_review_cards.md``) and returns a green ack
    card. The chrome free-text path (``_handle_chrome_audit_list``) delegates
    here so there is exactly one implementation.
    """
    response = _empty_response()
    deny = _authorize_or_deny_v2(
        action="view_audit_recent",
        operator=operator,
        article_id=None,
        event_kind="card_action",
    )
    if deny is not None:
        return deny

    # Parse + clamp ``n``.
    n_raw = payload.get("n") if isinstance(payload, dict) else None
    try:
        n = int(n_raw) if n_raw is not None else _AUDIT_LIST_DEFAULT_N
    except (TypeError, ValueError):
        n = _AUDIT_LIST_DEFAULT_N
    if n <= 0:
        n = _AUDIT_LIST_DEFAULT_N
    if n > _AUDIT_LIST_MAX_N:
        n = _AUDIT_LIST_MAX_N

    kind_filter = None
    if isinstance(payload, dict):
        kind_filter_raw = payload.get("kind")
        if kind_filter_raw:
            kind_filter = str(kind_filter_raw)

    # Over-read so a kind filter still has a chance to fill ``n`` items.
    raw_pool = _read_audit_tail(
        limit=(_AUDIT_LIST_MAX_N if kind_filter else n)
    )
    if kind_filter:
        filtered = [
            ev for ev in raw_pool
            if str(ev.get("kind") or "") == kind_filter
        ]
        entries = filtered[:n]
    else:
        entries = raw_pool[:n]

    try:
        review_triggers._emit_lark_audit_list_card(
            entries=entries,
            filter_kind=kind_filter,
            n=n,
        )
    except Exception:  # pragma: no cover
        _log.warning("view_audit_recent emit failed", exc_info=True)

    response["side_effects"].append("audit_list_emitted")
    title_extra = f" — kind={kind_filter}" if kind_filter else ""
    response["reply_card"] = _make_card(
        title=f"📋 Audit (last {n}){title_extra}",
        body=f"已刷新 audit list（{len(entries)} 条）。",
        template="blue",
    )
    _telemetry(
        event_kind="card_action",
        action="view_audit_recent",
        article_id=None,
        operator=operator,
        outcome="ok",
        extra={"count": len(entries), "n": n, "filter_kind": kind_filter},
    )
    return response


def _handle_chrome_audit_list(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    """No-id audit list mode (chrome free-text path).

    Thin wrapper that delegates to :func:`_handle_view_audit_recent` to keep
    the render contract one-place. Auth is checked twice (here via
    ``chrome_audit_list``, then again inside the delegate via
    ``view_audit_recent``) — both require ``review`` so the second pass is a
    no-op for any allowlisted operator.
    """
    deny = _chrome_unauthorized("chrome_audit_list", operator)
    if deny is not None:
        return deny
    response = _handle_view_audit_recent(
        operator=operator, payload=payload or {}, article_id=None,
    )
    # Preserve the legacy side-effect token so the chrome happy-path test
    # (test_audit_list_keyword_emits_audit_list_card) keeps passing.
    if "chrome_audit_list_emitted" not in response["side_effects"]:
        response["side_effects"].append("chrome_audit_list_emitted")
    return response


def _handle_chrome_auth_debug(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_auth_debug", operator)
    if deny is not None:
        return deny
    open_id = str(operator.get("open_id") or "")
    operators_list = review_auth.list_lark_operators()
    in_whitelist = False
    actions: list[str] = []
    for entry in operators_list:
        if str(entry.get("open_id")) == open_id:
            in_whitelist = True
            actions = list(entry.get("actions") or [])
            break
    try:
        review_triggers._emit_lark_auth_debug_card(
            operator_open_id=open_id,
            authorized_actions=actions,
            in_whitelist=in_whitelist,
            action_table=dict(_LARK_ACTION_REQ),
        )
    except Exception:  # pragma: no cover
        _log.warning("chrome_auth_debug emit failed", exc_info=True)
    response["side_effects"].append("chrome_auth_debug_emitted")
    body_lines = [
        f"Operator: `{open_id or '(unknown)'}`",
        f"In whitelist: `{in_whitelist}`",
        f"Allowed actions: `{','.join(actions) or '(none)'}`",
        f"Action table size: `{len(_LARK_ACTION_REQ)}`",
    ]
    response["reply_card"] = _make_card(
        title="🔐 Auth Debug",
        body="\n".join(body_lines),
        template="blue" if in_whitelist else "red",
    )
    _telemetry(
        event_kind="message",
        action="chrome_auth_debug",
        article_id=None,
        operator=operator,
        outcome="ok",
        extra={"in_whitelist": in_whitelist},
    )
    return response


def _handle_chrome_suggestions(
    operator: dict[str, Any],
    payload: dict[str, Any],
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    """Re-emit the suggestion list card. Re-uses GAP-S's emit helper."""
    response = _empty_response()
    deny = _chrome_unauthorized("chrome_suggestions", operator)
    if deny is not None:
        return deny
    try:
        from agentflow.shared.topic_profile_lifecycle import list_suggestions

        suggestions = list_suggestions(status="pending")
    except Exception as err:
        response["side_effects"].append("chrome_suggestions_error")
        response["reply_text"] = f"无法读取 suggestion 列表: {err}"
        return response
    try:
        review_triggers._emit_lark_suggestion_list_card(suggestions=suggestions)
    except Exception:  # pragma: no cover
        _log.warning("chrome_suggestions emit failed", exc_info=True)
    response["side_effects"].append("chrome_suggestions_emitted")
    response["reply_card"] = _make_card(
        title="📋 Pending Suggestions",
        body=f"已刷新 suggestion 列表（{len(suggestions)} 条）。",
        template="blue",
    )
    _telemetry(
        event_kind="message",
        action="chrome_suggestions",
        article_id=None,
        operator=operator,
        outcome="ok",
        extra={"count": len(suggestions)},
    )
    return response


# ---------------------------------------------------------------------------
# Per-action auth — Lark parity with daemon._ACTION_REQ.
#
# The (gate, action) → required-verb mapping is the same as TG's: clicking
# "通过" on Gate B needs ``review``, "重写" needs ``edit``, Gate D ✅ confirm
# needs ``publish``, etc. We keep a separate dict keyed by the lark_callback
# *action* token (no gate prefix) so dispatch is one lookup. To re-use the
# auth verbs, the action key alone is enough — Gate is determined by which
# handler we land in.
# ---------------------------------------------------------------------------
_LARK_ACTION_REQ: dict[str, str] = {
    # Gate B
    "approve_b": "review",
    "reject_b": "review",
    "refill": "review",
    "takeover": "review",
    "view_audit": "review",
    "view_meta": "review",
    "gate_b_rewrite": "edit",
    "gate_b_edit": "edit",
    "gate_b_diff": "review",
    # Gate A
    "gate_a_write": "write",
    "gate_a_reject_all": "review",
    "gate_a_expand": "review",
    # Gate C / image picker
    "gate_c_approve": "review",
    "gate_c_skip": "review",
    "gate_c_regen": "image",
    "gate_c_relogo": "image",
    "gate_c_full": "review",
    "image_gate_pick": "image",
    # Gate D
    "gate_d_toggle": "review",
    "gate_d_select_all": "review",
    "gate_d_save_default": "review",
    "gate_d_confirm": "publish",
    "gate_d_cancel": "review",
    "gate_d_resume": "review",
    "gate_d_extend": "review",
    "gate_d_retry": "publish",
    # Locked takeover
    "locked_critique": "review",
    "locked_edit": "edit",
    "locked_give_up": "review",
    # Misc
    "apply_pending_edit": "edit",
    "defer": "review",
    # Suggestions (Gate S) — fail-closed via _authorize_or_deny_v2.
    "suggestion_list": "review",
    "suggestion_review": "review",
    "suggestion_apply": "edit",
    "suggestion_dismiss": "review",
    # Profile multi-turn follow-up (Gate P) — daemon-driven question advance.
    # Uses ``_authorize_or_deny_v2`` (fail-closed via lark_operators allowlist).
    "profile_advance": "review",
    # Audit list (no article_id) — list-mode equivalent of TG /audit.
    "view_audit_recent": "review",
    # ----- GAP-CHROME (operator slash-command parity) -----
    # All chrome actions also use ``_authorize_or_deny_v2``. Read-only intents
    # require ``review``; mutating ones (skip/defer/publish_mark/cancel)
    # require ``edit``.
    "chrome_status": "review",
    "chrome_list": "review",
    "chrome_published": "review",
    "chrome_scan": "review",
    "chrome_jobs": "review",
    "chrome_audit_list": "review",
    "chrome_auth_debug": "review",
    "chrome_suggestions": "review",
    "chrome_skip": "edit",
    "chrome_defer": "edit",
    "chrome_publish_mark": "edit",
    "chrome_cancel": "edit",
}


def _deny_card(action: str, required: str, operator: dict[str, Any]) -> dict[str, Any]:
    name = operator.get("name") or operator.get("open_id") or "(unknown)"
    return _make_card(
        title="❌ 未授权",
        body=(
            f"`{action}` 需要权限 `{required}`，当前操作者 `{name}` 没有该授权。\n\n"
            f"管理员可在 `~/.agentflow/review/lark_auth.json` 添加，或运行 "
            f"`af review-auth-add` 的 Lark 子命令。"
        ),
        template="red",
    )


def _authorize_or_deny(
    *,
    action: str,
    operator: dict[str, Any],
    article_id: str | None,
    event_kind: str,
) -> dict[str, Any] | None:
    """LEGACY: kept for backwards compatibility with code outside this module.

    All in-module handlers were migrated to :func:`_authorize_or_deny_v2` in
    Phase 2 closure (L-4). New handlers MUST use ``_authorize_or_deny_v2`` —
    the legacy ``is_lark_authorized`` path silently allows traffic when the
    ``lark_auth.json`` file is empty AND no ``LARK_OPERATOR_OPEN_ID`` env is
    set, which is unsafe for Phase 2 deployments where any bridge-token
    holder could otherwise fire arbitrary callbacks.

    This function will be removed in Phase 3 along with the rest of the TG
    path. Until then, leave it importable so external code (e.g. legacy
    tests, third-party adapters) doesn't break unexpectedly.
    """
    required = _LARK_ACTION_REQ.get(action)
    if required is None:
        return None
    open_id = operator.get("open_id")
    if review_auth.is_lark_authorized(str(open_id) if open_id else None, action=required):
        return None
    response = _empty_response()
    response["side_effects"].append("not_authorized")
    response["reply_card"] = _deny_card(action, required, operator)
    _telemetry(
        event_kind=event_kind,
        action=action,
        article_id=article_id,
        operator=operator,
        outcome="not_authorized",
        extra={"required": required},
    )
    return response


def _authorize_or_deny_v2(
    *,
    action: str,
    operator: dict[str, Any],
    article_id: str | None,
    event_kind: str,
) -> dict[str, Any] | None:
    """Fail-closed authorization gate using ``is_authorized_open_id``.

    Mirrors :func:`_authorize_or_deny` but routes through the v2 Lark operator
    allowlist (``lark_operators`` section in ``auth.json``). New handlers
    should prefer this — the legacy path silently allows traffic when the
    file is empty and a deployment hasn't onboarded any operator yet.
    """
    required = _LARK_ACTION_REQ.get(action)
    if required is None:
        return None
    open_id = operator.get("open_id")
    if review_auth.is_authorized_open_id(
        str(open_id) if open_id else None, action=required
    ):
        return None
    response = _empty_response()
    response["side_effects"].append("not_authorized")
    response["reply_card"] = _deny_card(action, required, operator)
    _telemetry(
        event_kind=event_kind,
        action=action,
        article_id=article_id,
        operator=operator,
        outcome="not_authorized",
        extra={"required": required},
    )
    return response


# ---------------------------------------------------------------------------
# Free-text @-mention message routing.
#
# When an operator @-mentions the Lark bot with free text, OpenClaw posts a
# ``lark_message`` command. We translate the message into one of the
# existing card_action verbs so the LLM-side bot never has to fabricate a
# response — it always gets a structured ack from the daemon.
#
# Intent classification is keyword-first and deterministic. Pending-edit
# slots take priority: any free-text message becomes the body of the
# operator's most recent ``lark_*_edit_pending`` event when present, mirroring
# the TG bot's "next plain-text reply applies the edit" muscle memory.
# ---------------------------------------------------------------------------


_AT_MENTION_RE = re.compile(r"@[\w-]+")
_URL_RE = re.compile(r"https?://\S+")


def _normalize_text(raw: str) -> str:
    """Strip @-mentions and URLs before intent classification.

    @-mentions matter because keyword `audit` inside `@CSAuditContentPostBot`
    would otherwise match the `gate_b_diff` intent. We don't want substrings
    of mention/URL tokens to ever drive routing — the operator's actual
    instruction is what's left after these are stripped."""
    text = (raw or "").strip()
    text = _AT_MENTION_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    return " ".join(text.split())


_PENDING_REVIEW_STATES = (
    STATE_DRAFT_PENDING_REVIEW,
    STATE_IMAGE_PENDING_REVIEW,
    STATE_CHANNEL_PENDING_REVIEW,
    STATE_DRAFTING_LOCKED_HUMAN,
)


def _resolve_active_article(hint_article_id: str | None) -> str | None:
    """Pick the most recently transitioned article in any pending-review
    state. Mirrors how TG's /suggestions surfaces "the article waiting for
    you" without an explicit id."""
    if hint_article_id:
        return str(hint_article_id)
    try:
        ids = review_state.articles_in_state(_PENDING_REVIEW_STATES) or []
    except Exception:
        ids = []
    if not ids:
        return None

    def _ts_for(aid: str) -> str:
        try:
            meta_path = agentflow_home() / "drafts" / aid / "metadata.json"
            if not meta_path.exists():
                return ""
            meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
            history = meta.get("gate_history") or []
            return str(history[-1].get("timestamp")) if history else ""
        except Exception:
            return ""

    ids_sorted = sorted(ids, key=_ts_for, reverse=True)
    return ids_sorted[0]


# Keyword → (action, gate-state guard). Order matters: longer / more specific
# patterns first so "拒绝重写" doesn't trip "拒绝".
_INTENT_TABLE: list[tuple[tuple[str, ...], str]] = [
    # Gate D
    (("确认发布", "确认 publish", "publish confirm", "go"), "gate_d_confirm"),
    (("全选平台", "select all", "全选"), "gate_d_select_all"),
    (("取消发布", "取消 d", "d cancel"), "gate_d_cancel"),
    # Gate C
    (("通过封面", "封面通过", "approve cover"), "gate_c_approve"),
    (("跳过封面", "skip cover", "不用封面"), "gate_c_skip"),
    (("重新生成封面", "重生成封面", "regen cover"), "gate_c_regen"),
    # Gate B (default for "通过" / "拒绝" / "重写" / "编辑" / "refill")
    (("approve", "通过", "✅", "ok 推进", "可以发"), "approve_b"),
    (("reject", "驳回", "拒绝", "❌"), "reject_b"),
    (("refill", "重新填充", "重填"), "refill"),
    (("rewrite", "重写", "整篇重写"), "gate_b_rewrite"),
    (("edit", "编辑", "改一下", "调一下"), "gate_b_edit"),
    (("diff", "审计", "audit", "查 audit"), "gate_b_diff"),
    # Gate A
    (("写这条", "起稿", "推进到下个 gate", "推进到下一个 gate", "推进", "advance", "next gate"),
     "_advance"),
    (("全拒绝", "reject all"), "gate_a_reject_all"),
    # Read-only
    (("查看 meta", "view meta", "看 meta"), "view_meta"),
    (("查看 audit", "view audit"), "view_audit"),
]


# ---------------------------------------------------------------------------
# Chrome (operator slash-command) intent table — GAP-CHROME.
#
# False-positive lock (CHANGELOG v1.1.8): each entry must use whole-text
# match after ``_normalize_text`` for read-only intents, and anchored regex
# for verb-with-arg intents. ``_classify_chrome_intent`` enforces this so a
# string like "推进到状态 X" can never trip the bare keyword "状态".
# ---------------------------------------------------------------------------
_CHROME_INTENTS: dict[str, tuple[str, ...]] = {
    "chrome_status":      ("状态", "status", "running 吗", "运行状态"),
    "chrome_list":        ("列表", "list", "在审", "in review"),
    "chrome_published":   ("已发", "published"),
    "chrome_scan":        ("扫一下", "扫扫", "scan", "找选题", "热点"),
    "chrome_jobs":        ("任务", "jobs", "in-flight"),
    "chrome_audit_list":  ("审计列表", "audit list"),
    "chrome_auth_debug":  ("鉴权", "auth", "auth-debug"),
    "chrome_suggestions": ("建议", "suggestions", "改进建议"),
}

# Verb intents — anchored regex on the *normalized* text. Order is irrelevant
# (no overlapping prefixes) but kept stable for readability.
_CHROME_VERB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^跳过\s+(\S+)\s*$"),                          "chrome_skip"),
    (re.compile(r"^skip\s+(\S+)\s*$", re.IGNORECASE),           "chrome_skip"),
    (re.compile(r"^推迟\s+(\S+)\s+(\d+(?:\.\d+)?)h?\s*$"),       "chrome_defer"),
    (re.compile(r"^defer\s+(\S+)\s+(\d+(?:\.\d+)?)h?\s*$",
                re.IGNORECASE),                                  "chrome_defer"),
    (re.compile(r"^标记已发\s+(\S+)\s*$"),                       "chrome_publish_mark"),
    (re.compile(r"^publish-?mark\s+(\S+)\s*$", re.IGNORECASE),  "chrome_publish_mark"),
    (re.compile(r"^取消\s+(\S+)\s*$"),                          "chrome_cancel"),
    (re.compile(r"^cancel\s+(\S+)\s*$", re.IGNORECASE),         "chrome_cancel"),
]


def _classify_chrome_intent(
    text: str,
) -> tuple[str, dict[str, Any]] | None:
    """Match a normalized text against the chrome intent table.

    Returns ``(intent_token, kwargs)`` or ``None``. Verb intents put
    ``article_id`` (and ``hours`` for defer) in ``kwargs``. Read-only
    intents use whole-text exact match (after ``_normalize_text``); verb
    intents use anchored regex. The whole-text contract is what stops a
    phrase like "推进到状态 X" from triggering "状态".
    """
    if not text:
        return None
    stripped = text.strip()
    # Verb patterns first — they require an arg, so a bare "跳过" without an
    # id won't match and falls through to the read-only path (which also
    # rejects it because "跳过" isn't in any read-only keyword set).
    for pattern, action in _CHROME_VERB_PATTERNS:
        m = pattern.match(stripped)
        if m:
            kwargs: dict[str, Any] = {"article_id": m.group(1)}
            if action == "chrome_defer":
                try:
                    kwargs["hours"] = float(m.group(2))
                except (IndexError, ValueError):
                    kwargs["hours"] = None
            return (action, kwargs)
    # Read-only intents — whole-text match only.
    lowered = stripped.lower()
    for action, keywords in _CHROME_INTENTS.items():
        for kw in keywords:
            if kw.lower() == lowered:
                return (action, {})
    return None


_CHROME_HANDLERS: dict[str, Any] = {
    "chrome_status":       _handle_chrome_status,
    "chrome_list":         _handle_chrome_list,
    "chrome_published":    _handle_chrome_published,
    "chrome_scan":         _handle_chrome_scan,
    "chrome_jobs":         _handle_chrome_jobs,
    "chrome_audit_list":   _handle_chrome_audit_list,
    "chrome_auth_debug":   _handle_chrome_auth_debug,
    "chrome_suggestions":  _handle_chrome_suggestions,
    "chrome_skip":         _handle_chrome_skip,
    "chrome_defer":        _handle_chrome_defer,
    "chrome_publish_mark": _handle_chrome_publish_mark,
    "chrome_cancel":       _handle_chrome_cancel,
}


def _is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def _classify_intent(text: str) -> str | None:
    """Return the matching action token (lark_callback vocab) or ``None``.

    ASCII keywords match on word boundaries (so ``audit`` does NOT match
    inside ``CSAuditContentPostBot``). CJK keywords match as substrings
    since CJK has no whitespace word delimiters. Caller is expected to
    have run :func:`_normalize_text` first to strip @-mentions / URLs."""
    if not text:
        return None
    lowered = text.lower()
    for keywords, action in _INTENT_TABLE:
        for kw in keywords:
            kw_l = kw.lower()
            if _is_ascii(kw_l):
                pattern = r"(?:^|\W)" + re.escape(kw_l) + r"(?:$|\W)"
                if re.search(pattern, lowered):
                    return action
            else:
                if kw_l in lowered:
                    return action
    return None


def _help_card() -> dict[str, Any]:
    body = (
        "我没看懂这条指令。可识别的关键词：\n\n"
        "- `通过` / `approve` — 通过当前 Gate\n"
        "- `驳回` / `reject` — 驳回当前 Gate\n"
        "- `重写` / `rewrite` — Gate B 整篇重写\n"
        "- `编辑 ...` / `edit ...` — Gate B 编辑（@bot 后跟具体改动）\n"
        "- `refill` — Gate B 重新填充骨架\n"
        "- `推进到下个 gate` / `advance` — 把当前活跃稿件推进一格\n"
        "- `状态` / `status` — 查看 pending 队列\n\n"
        "你也可以直接在卡片上点按钮。"
    )
    return _make_card(title="🤖 Lark @bot 帮助", body=body, template="grey")


def _no_active_article_card() -> dict[str, Any]:
    return _make_card(
        title="没有活跃稿件",
        body=(
            "当前没有在 review 状态的稿件，无法推进。可以先：\n\n"
            "- 在 Gate A 卡片上点 `起稿 #N`，或\n"
            "- 让上游 D1 跑一轮新热点扫描"
        ),
        template="grey",
    )


def _route_message_intent(
    *,
    text: str,
    operator: dict[str, Any],
    payload: dict[str, Any],
    hint_article_id: str | None = None,
) -> dict[str, Any]:
    """Top-level free-text router. Always returns a structured response so
    the Lark-side bot never has to invent one."""
    cleaned = _normalize_text(text)

    # Pending-edit takes precedence: any non-empty body becomes the edit text.
    article_id = _resolve_active_article(hint_article_id)
    if article_id and cleaned:
        pending = _latest_pending_edit(article_id, operator)
        if pending is not None:
            edit_payload = {**(payload or {}), "text": cleaned}
            return _handle_apply_pending_edit(
                article_id=article_id, operator=operator, payload=edit_payload
            )

    if not cleaned:
        response = _empty_response()
        response["side_effects"].append("empty_message")
        response["reply_card"] = _help_card()
        _telemetry(
            event_kind="message",
            action=None,
            article_id=article_id,
            operator=operator,
            outcome="empty",
        )
        return response

    # Chrome (operator slash-command parity) intents — whole-text exact match
    # for read-only and anchored regex for verb intents. Ordering rationale:
    # we run chrome FIRST so an exact "审计列表" / "状态" wins over the existing
    # classifier's substring CJK match, but chrome's whole-text contract makes
    # it impossible to false-positive a phrase like "推进到状态 X" — that won't
    # equal any chrome keyword, so it falls through to the existing
    # ``_classify_intent`` and routes to ``_advance`` as before.
    chrome_match = _classify_chrome_intent(cleaned)
    if chrome_match is not None:
        chrome_intent, chrome_kwargs = chrome_match
        return _CHROME_HANDLERS[chrome_intent](
            operator, payload or {}, **chrome_kwargs
        )

    intent = _classify_intent(cleaned)
    if intent is None:
        response = _empty_response()
        response["side_effects"].append("unknown_intent")
        response["reply_card"] = _help_card()
        _telemetry(
            event_kind="message",
            action=None,
            article_id=article_id,
            operator=operator,
            outcome="unknown_intent",
            extra={"text_head": cleaned[:80]},
        )
        return response

    if intent == "_advance":
        return _route_advance(article_id=article_id, operator=operator, payload=payload)

    if article_id is None and intent != "view_meta":
        response = _empty_response()
        response["side_effects"].append("no_active_article")
        response["reply_card"] = _no_active_article_card()
        _telemetry(
            event_kind="message",
            action=intent,
            article_id=None,
            operator=operator,
            outcome="no_active_article",
        )
        return response

    deny = _authorize_or_deny_v2(
        action=intent,
        operator=operator,
        article_id=article_id,
        event_kind="message",
    )
    if deny is not None:
        return deny

    handler = _ACTION_HANDLERS.get(intent)
    if handler is None:
        response = _empty_response()
        response["side_effects"].append("unmapped_intent")
        response["reply_card"] = _help_card()
        return response

    return handler(article_id=article_id, operator=operator, payload=payload or {})


def _route_advance(
    *,
    article_id: str | None,
    operator: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Map "推进到下个 gate" to the right handler given current state.

    draft_pending_review  → approve_b
    image_pending_review  → gate_c_approve
    channel_pending_review → asks user to pick platforms (no auto-advance)
    drafting_locked_human → locked_critique (surface the audit reason)
    """
    if article_id is None:
        response = _empty_response()
        response["side_effects"].append("no_active_article")
        response["reply_card"] = _no_active_article_card()
        return response

    try:
        cur = review_state.current_state(article_id)
    except Exception:
        cur = None

    if cur == STATE_DRAFT_PENDING_REVIEW:
        deny = _authorize_or_deny_v2(
            action="approve_b", operator=operator,
            article_id=article_id, event_kind="message",
        )
        if deny is not None:
            return deny
        return _handle_approve_b(
            article_id=article_id, operator=operator, payload=payload or {}
        )
    if cur == STATE_IMAGE_PENDING_REVIEW:
        deny = _authorize_or_deny_v2(
            action="gate_c_approve", operator=operator,
            article_id=article_id, event_kind="message",
        )
        if deny is not None:
            return deny
        return _handle_gate_c_approve(
            article_id=article_id, operator=operator, payload=payload or {}
        )
    if cur == STATE_CHANNEL_PENDING_REVIEW:
        return _make_advance_help(article_id, "Gate D 需要你先选择平台再 ✅ 确认发布")
    if cur == STATE_DRAFTING_LOCKED_HUMAN:
        deny = _authorize_or_deny_v2(
            action="locked_critique", operator=operator,
            article_id=article_id, event_kind="message",
        )
        if deny is not None:
            return deny
        return _handle_locked_critique(
            article_id=article_id, operator=operator, payload=payload or {}
        )

    return _make_advance_help(
        article_id,
        f"当前状态 `{cur or '?'}` 没有自动推进的下一步；可用 `通过` / `驳回` / `重写` 等指令。",
    )


def _make_advance_help(article_id: str, body: str) -> dict[str, Any]:
    response = _empty_response()
    response["side_effects"].append("advance_help")
    response["reply_card"] = _make_card(
        title="无法自动推进",
        body=f"`{article_id}`\n\n{body}",
        template="orange",
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
    "image_gate_pick": _handle_image_gate_pick,
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
    "apply_pending_edit": _handle_apply_pending_edit,
    # v1.1.1 — generic defer (gate carried in payload.gate)
    "defer": _handle_defer,
}


# Suggestion (Gate S) handlers. Kept separate because they are not bound to an
# article_id and use the v2 fail-closed authorization gate.
_SUGGESTION_HANDLERS = {
    "suggestion_list": _handle_suggestion_list,
    "suggestion_review": _handle_suggestion_review,
    "suggestion_apply": _handle_suggestion_apply,
    "suggestion_dismiss": _handle_suggestion_dismiss,
}


# Profile (Gate P) multi-turn follow-up handlers. Like suggestions, these are
# not bound to an article_id (they are per-profile) and use the v2 fail-closed
# authorization gate. Routed early so the article_id guard doesn't fire.
_PROFILE_HANDLERS = {
    "profile_advance": _handle_profile_advance,
}


# Audit list handlers (no article_id; OPS gate). Same early-route pattern as
# suggestions/profile so the article_id guard below doesn't fire.
_AUDIT_HANDLERS = {
    "view_audit_recent": _handle_view_audit_recent,
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
        text = str(payload.get("text") or payload.get("body") or "")
        # Plumb chat_id so downstream notify.* events can target the same
        # Lark chat (parity with TG callback's chat_id capture).
        chat_id = payload.get("chat_id")
        operator_with_chat = (
            {**operator, "chat_id": chat_id} if chat_id else operator
        )
        return _route_message_intent(
            text=text,
            operator=operator_with_chat,
            payload=payload,
            hint_article_id=article_id,
        )

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

    action_str = str(action or "")

    # Suggestion (Gate S) actions are not bound to an article_id and use the
    # fail-closed v2 authorization gate. Route them ahead of the standard
    # _ACTION_HANDLERS path so the article_id guard below doesn't fire.
    if action_str in _SUGGESTION_HANDLERS:
        deny = _authorize_or_deny_v2(
            action=action_str,
            operator=operator,
            article_id=article_id,
            event_kind="card_action",
        )
        if deny is not None:
            return deny
        return _SUGGESTION_HANDLERS[action_str](
            operator=operator, payload=payload, article_id=article_id
        )

    # Profile (Gate P) multi-turn follow-up — same early-route pattern as
    # suggestions: per-profile (not per-article) + v2 fail-closed auth.
    if action_str in _PROFILE_HANDLERS:
        deny = _authorize_or_deny_v2(
            action=action_str,
            operator=operator,
            article_id=article_id,
            event_kind="card_action",
        )
        if deny is not None:
            return deny
        return _PROFILE_HANDLERS[action_str](
            operator=operator, payload=payload, article_id=article_id
        )

    # Audit list (OPS gate) — also article-id-optional. The handler runs its
    # own ``_authorize_or_deny_v2`` so we don't double-deny here.
    if action_str in _AUDIT_HANDLERS:
        return _AUDIT_HANDLERS[action_str](
            operator=operator, payload=payload, article_id=article_id
        )

    handler = _ACTION_HANDLERS.get(action_str)
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

    deny = _authorize_or_deny_v2(
        action=str(action),
        operator=operator,
        article_id=article_id,
        event_kind="card_action",
    )
    if deny is not None:
        return deny

    return handler(article_id=article_id, operator=operator, payload=payload)
