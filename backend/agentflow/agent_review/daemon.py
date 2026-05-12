"""TG review daemon — long-poll loop, callback router, timeout sweeper.

Runs in foreground. ``af review-daemon`` is the entry point.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentflow.agent_review import (
    auth,
    pending_edits,
    self_check,
    short_id as _sid,
    state,
    timeout_state,
)

# Phase 3 Wave C: TG dispatch (poll loop, _handle_message, _handle_callback,
# _route, slash handlers, command registry) is gone. The Lark callback path
# in lark_callback.py owns the entire operator surface. daemon.py no longer
# imports tg_client; the few surviving helpers that previously side-channeled
# notifications to TG (spawn-failure, downtime, mock-mode warning, deferred
# Gate A repost) now log only — Lark fan-out is handled via lark_webhook
# where applicable. tg_client.py and render.py are removed in Wave D.
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.logger import get_logger
from agentflow.shared.topic_profile_lifecycle import (
    apply_suggestion,
    build_patch_from_answers,
    find_active_session_for_uid,
    list_suggestions,
    load_session,
    review_suggestion,
    save_session,
    seed_profile,
    normalize_output_language,
    update_suggestion_status,
    user_profile_bootstrap_state,
)

_log = get_logger("agent_review.daemon")
_REVIEW_HOME = agentflow_home() / "review"

_BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "start", "description": "注册/检查 review chat"},
    {"command": "help", "description": "查看 gate 定义 + 按钮图例 + 授权矩阵"},
    {"command": "status", "description": "列出 *_pending_review 文章 + 等待时长"},
    {"command": "queue", "description": "队列前 5 条最久未审"},
    {"command": "list", "description": "列出待处理 B/C/D/Ready 卡片"},
    {"command": "published", "description": "列最近 N 天发布的文章 (default 7d)"},
    {"command": "suggestions", "description": "查看待确认 profile 建议"},
    {"command": "scan", "description": "主动触发 hotspots 扫描 (top-k)"},
    {"command": "jobs", "description": "查看 cron 定时任务状态"},
    {"command": "skip", "description": "跳过 image-gate, 直接 Gate D"},
    {"command": "defer", "description": "推迟 Gate B/C/D N 小时"},
    {"command": "publish_mark", "description": "标记 published (manual paste)"},
    {"command": "audit", "description": "最近 20 条 callback/slash 决策"},
    {"command": "auth_debug", "description": "查看 uid 在每个动作上的授权"},
    {"command": "cancel", "description": "作废一个 pending short_id"},
]

_PROFILE_SETUP_STEPS: list[dict[str, str]] = [
    {
        "key": "brand",
        "label": "Brand",
        "prompt": (
            "请输入品牌/账号显示名。后续问题会优先显示这个名称。\n"
            "示例：你的产品名、公司名或个人专栏名。"
        ),
    },
    {
        "key": "writing_defaults",
        "label": "Voice & Language",
        "prompt": (
            "确认写作身份和输出语言。可直接回复 skip 使用默认：first_party_brand + 简体中文。\n"
            "也可以一行写完，例如：first_party_brand，简体中文，技术营销但少夸张。"
        ),
    },
    {
        "key": "source_materials",
        "label": "Facts & Materials",
        "prompt": (
            "粘贴可学习语料/账号说明/产品事实/关键词，每行一条即可。\n"
            "系统会自动拆成 product facts、core terms 和 search queries。\n"
            "规则：写事实和名词，不写空泛口号。示例：核心机制名；产品功能术语；竞品/赛道关键词。"
        ),
    },
    {
        "key": "rules",
        "label": "Rules & Boundaries",
        "prompt": (
            "补充写作规则和边界，可多行，可跳过。\n"
            "写法示例：\n"
            "Do: 先讲协议机制，再讲市场影响\n"
            "Don't: 不要承诺收益\n"
            "Avoid: celebrity crypto, price prediction"
        ),
    },
]


def _config_path() -> Path:
    p = _REVIEW_HOME / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _offset_path() -> Path:
    return _REVIEW_HOME / "poll_offset.json"


def _audit_path() -> Path:
    return _REVIEW_HOME / "audit.jsonl"


def _read_json(p: Path, default: Any) -> Any:
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _audit(event: dict[str, Any]) -> None:
    ap = _audit_path()
    ap.parent.mkdir(parents=True, exist_ok=True)
    event = {**event, "audit_ts": datetime.now(timezone.utc).isoformat()}
    with ap.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _heartbeat_path() -> Path:
    return _REVIEW_HOME / "last_heartbeat.json"


def _write_heartbeat() -> None:
    try:
        _write_json(_heartbeat_path(), {"timestamp": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass  # heartbeat is best-effort, never blocks the loop


def _parse_profile_list_reply(text: str) -> list[str]:
    raw = (text or "").replace("；", "\n").replace(";", "\n").replace("、", "\n")
    lines = [line.strip(" -\t") for line in raw.splitlines()]
    return [line for line in lines if line]


def _is_skip_reply(text: str) -> bool:
    return str(text or "").strip().lower() in {"", "skip", "跳过", "略过", "默认", "default"}


def _split_profile_terms(text: str) -> list[str]:
    raw = (
        str(text or "")
        .replace("；", "\n")
        .replace(";", "\n")
        .replace("、", "\n")
        .replace(",", "\n")
        .replace("，", "\n")
    )
    lines = [line.strip(" -\t") for line in raw.splitlines()]
    return [line for line in lines if line]


def _normalize_voice(text: str) -> str | None:
    raw = str(text or "").strip().lower()
    mapping = {
        "first_party_brand": "first_party_brand",
        "brand": "first_party_brand",
        "observer": "observer",
        "personal": "personal",
    }
    return mapping.get(raw)


def _parse_writing_defaults(text: str) -> dict[str, Any]:
    if _is_skip_reply(text):
        return {"voice": "first_party_brand", "output_language": "zh-Hans"}
    chunks = _split_profile_terms(text)
    voice = None
    output_language = None
    do: list[str] = []
    for chunk in chunks or [str(text or "").strip()]:
        voice = voice or _normalize_voice(chunk)
        output_language = output_language or normalize_output_language(chunk)
        if _normalize_voice(chunk) is None and normalize_output_language(chunk) is None:
            do.append(chunk)
    return {
        "voice": voice or "first_party_brand",
        "output_language": output_language or "zh-Hans",
        "do": do,
    }


def _parse_source_materials(text: str, *, brand: str) -> dict[str, list[str]]:
    if _is_skip_reply(text):
        return {
            "product_facts": [f"{brand} topic profile"] if brand else [],
            "core_terms": [brand] if brand else [],
            "search_queries": [brand] if brand else [],
        }
    items = _split_profile_terms(text)
    if not items:
        return {"product_facts": [], "core_terms": [], "search_queries": []}
    core_terms = items[:8]
    search_queries = [item for item in items if " " in item or "-" in item][:6]
    if not search_queries:
        search_queries = items[:4]
    if brand and brand not in core_terms:
        core_terms.insert(0, brand)
    return {
        "product_facts": items,
        "core_terms": core_terms[:8],
        "search_queries": search_queries,
    }


def _parse_rules(text: str) -> dict[str, list[str]]:
    if _is_skip_reply(text):
        return {"do": [], "dont": [], "avoid_terms": []}
    do: list[str] = []
    dont: list[str] = []
    avoid: list[str] = []
    for item in _split_profile_terms(text):
        low = item.lower()
        if low.startswith(("do:", "do：")):
            value = item.split(":", 1)[-1].split("：", 1)[-1].strip()
            if value:
                do.append(value)
        elif low.startswith(("don't:", "dont:", "don’t:", "不要", "禁止")):
            value = item.split(":", 1)[-1].split("：", 1)[-1].strip()
            dont.append(value or item)
        elif low.startswith(("avoid:", "avoid：")):
            value = item.split(":", 1)[-1].split("：", 1)[-1].strip()
            avoid.extend(_split_profile_terms(value))
        elif any(marker in item for marker in ["不要", "避免", "禁止"]):
            dont.append(item)
        else:
            do.append(item)
    return {"do": do, "dont": dont, "avoid_terms": avoid}


def _session_display_name(session: dict[str, Any]) -> str:
    answers = session.get("answers") or {}
    if isinstance(answers, dict):
        brand = str(answers.get("brand") or "").strip()
        if brand:
            return brand
    return str(session.get("profile_id") or "")


def _send_profile_setup_question(session: dict[str, Any]) -> None:
    chat_id = session.get("active_chat_id") or session.get("chat_id")
    if chat_id is None:
        return
    step_index = int(session.get("step_index") or 0)
    if step_index >= len(_PROFILE_SETUP_STEPS):
        return
    # Phase 3: TG send removed. The Lark profile-advance card flow in
    # lark_callback.py is the live path; this helper is preserved only as
    # a no-op so existing tests that mock at this seam keep passing until
    # Wave D rewrites them.
    return None


def _spawn_apply_profile_session(session_id: str, chat_id: int | str | None) -> None:
    import subprocess

    def _run() -> None:
        if chat_id is None:
            return
        try:
            session = load_session(session_id)
            from agentflow.agent_review.triggers import _af_argv

            cmd = str(session.get("mode") or "update")
            profile_id = str(session.get("profile_id") or "")
            result = subprocess.run(
                _af_argv(
                    "topic-profile",
                    cmd,
                    "--profile",
                    profile_id,
                    "--from-session",
                    session_id,
                    "--json",
                ),
                env=os.environ.copy(),
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                _log.warning(
                    "profile setup failed for %s: %s",
                    profile_id,
                    (result.stderr or result.stdout)[:500],
                )
                session["status"] = "failed"
                save_session(session)
                return
            session["status"] = "applied"
            save_session(session)
            _log.info("profile setup applied for %s", profile_id)
        except Exception as err:
            _log.warning("profile session apply crashed for %s: %s", session_id, err)

    threading.Thread(target=_run, daemon=True).start()


def _maybe_handle_profile_session_reply(
    *,
    chat_id: int | None,
    uid: int | None,
    text: str,
) -> bool:
    if uid is None:
        return False
    session = find_active_session_for_uid(uid)
    if not session:
        return False
    step_index = int(session.get("step_index") or 0)
    if step_index >= len(_PROFILE_SETUP_STEPS):
        return False
    step = _PROFILE_SETUP_STEPS[step_index]
    key = str(step.get("key") or "")
    answers = session.get("answers") or {}
    if not isinstance(answers, dict):
        answers = {}

    if key == "brand":
        value = str(text or "").strip()
    elif key == "writing_defaults":
        parsed = _parse_writing_defaults(text)
        answers["voice"] = parsed["voice"]
        answers["output_language"] = parsed["output_language"]
        if parsed.get("do"):
            answers["do"] = [*list(answers.get("do") or []), *list(parsed["do"])]
        value = str(text or "").strip()
    elif key == "source_materials":
        parsed = _parse_source_materials(
            text,
            brand=str(answers.get("brand") or session.get("profile_id") or ""),
        )
        for field, parsed_value in parsed.items():
            if parsed_value:
                answers[field] = [*list(answers.get(field) or []), *parsed_value]
        value = str(text or "").strip()
    elif key == "rules":
        parsed = _parse_rules(text)
        for field, parsed_value in parsed.items():
            if parsed_value:
                answers[field] = [*list(answers.get(field) or []), *parsed_value]
        value = str(text or "").strip()
    else:
        value = _parse_profile_list_reply(text)
    answers[key] = value
    session["answers"] = answers
    session["step_index"] = step_index + 1
    if int(session["step_index"]) >= len(_PROFILE_SETUP_STEPS):
        existing = user_profile_bootstrap_state(str(session.get("profile_id") or "")).get("current_profile")
        session["profile_patch"] = build_patch_from_answers(
            str(session.get("profile_id") or ""),
            answers,
            existing_profile=existing if isinstance(existing, dict) else seed_profile(str(session.get("profile_id") or "")),
        )
        session["status"] = "completed"
        save_session(session)
        _spawn_apply_profile_session(str(session.get("id") or ""), chat_id)
        return True
    session["status"] = "collecting"
    save_session(session)
    _send_profile_setup_question(session)
    return True


# ---------------------------------------------------------------------------
# chat_id resolution
# ---------------------------------------------------------------------------


def get_review_chat_id() -> int | None:
    """Resolve chat id from (in priority order):
    1. ``TELEGRAM_REVIEW_CHAT_ID`` env var
    2. ``~/.agentflow/review/config.json::review_chat_id``
    """
    env = os.environ.get("TELEGRAM_REVIEW_CHAT_ID", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    cfg = _read_json(_config_path(), {}) or {}
    cid = cfg.get("review_chat_id")
    return int(cid) if cid is not None else None


def set_review_chat_id(chat_id: int) -> None:
    cfg = _read_json(_config_path(), {}) or {}
    cfg["review_chat_id"] = int(chat_id)
    cfg["captured_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(_config_path(), cfg)


def configure_bot_menu(chat_id: int | None = None) -> None:
    """No-op stub kept for back-compat (Phase 3 removed the TG bot menu).

    Lark cards are event-driven; there is no equivalent of a slash-command
    menu to register. The function signature is preserved so test fixtures
    and any historical operator scripts that call it still import cleanly.
    """
    return None


# ---------------------------------------------------------------------------
# Deferred re-post store — backs *:defer callbacks. Sweeper drains entries
# whose ``due_at`` has passed by reposting a fresh Gate card. Idempotent:
# each entry is removed after being acted upon.
# ---------------------------------------------------------------------------


def _deferred_path() -> Path:
    p = _REVIEW_HOME / "deferred_reposts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_deferred() -> list[dict[str, Any]]:
    raw = _read_json(_deferred_path(), [])
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _write_deferred(items: list[dict[str, Any]]) -> None:
    _write_json(_deferred_path(), items)


def _parse_defer_hours(extra: str) -> float | None:
    """Pull ``hours=N`` from a ``defer`` callback's ``extra`` field. Returns
    None if missing/malformed."""
    if not extra:
        return None
    for token in extra.split(":"):
        token = token.strip()
        if token.startswith("hours="):
            try:
                return float(token.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _schedule_deferred_repost(
    *,
    gate: str,
    article_id: str | None,
    batch_path: str | None,
    hours: float,
    source_sid: str,
) -> None:
    """Append a deferred-repost entry. The sweeper picks it up after ``hours``
    elapse. Idempotent at the (gate, target) tuple — a second defer for the
    same article extends the timer rather than queuing a duplicate."""
    if hours <= 0:
        raise ValueError(f"defer hours must be positive, got {hours!r}")
    due = datetime.now(timezone.utc) + timedelta(hours=hours)
    items = _read_deferred()
    target = article_id or batch_path or ""
    new_items: list[dict[str, Any]] = []
    for item in items:
        if item.get("gate") == gate and (
            item.get("article_id") == article_id
            and item.get("batch_path") == batch_path
        ):
            continue
        new_items.append(item)
    new_items.append({
        "gate": gate,
        "article_id": article_id,
        "batch_path": batch_path,
        "due_at": due.isoformat(),
        "hours": float(hours),
        "source_short_id": source_sid,
        "scheduled_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
    })
    _write_deferred(new_items)


def _drain_deferred_reposts() -> int:
    """Drain due entries from the deferred-repost store. Returns the number
    of entries acted upon. Each acted-upon entry is removed regardless of
    repost success — failures land in the audit log so the operator can
    re-trigger manually."""
    items = _read_deferred()
    if not items:
        return 0
    now = datetime.now(timezone.utc)
    kept: list[dict[str, Any]] = []
    fired = 0
    for item in items:
        due_iso = item.get("due_at")
        try:
            due = datetime.fromisoformat(str(due_iso)) if due_iso else None
        except ValueError:
            due = None
        if due is None or due > now:
            kept.append(item)
            continue
        fired += 1
        gate = str(item.get("gate") or "")
        article_id = item.get("article_id")
        batch_path = item.get("batch_path")
        try:
            from agentflow.agent_review import triggers as _triggers
            if gate == "B" and article_id:
                _triggers.post_gate_b(article_id, force=True)
            elif gate == "C" and article_id:
                _triggers.post_gate_c(article_id)
            elif gate == "A" and batch_path:
                # Re-render Gate A by re-running hotspots on the saved batch
                # (the batch JSON survives) — for safety we just notify the
                # operator instead of re-running the full pipeline. Gate A
                # batches contain raw hotspots that still resolve via the sid
                # index until they expire.
                _log.info(
                    "Gate A defer expired for batch %s — operator re-review needed",
                    batch_path,
                )
            _audit({
                "kind": "deferred_repost_fired",
                "gate": gate,
                "article_id": article_id,
                "batch_path": batch_path,
            })
        except Exception as err:  # pragma: no cover
            _log.warning("deferred repost failed (gate=%s aid=%s): %s",
                         gate, article_id, err)
            _audit({
                "kind": "deferred_repost_failed",
                "gate": gate,
                "article_id": article_id,
                "batch_path": batch_path,
                "error": str(err)[:300],
            })
    if fired:
        _write_deferred(kept)
    return fired


# ---------------------------------------------------------------------------
# Slash command helpers (v1.0.3 menu set: status / queue / help / skip /
# defer / publish-mark / audit / auth-debug). Each returns None on success
# and an error label on failure (for audit-event annotation).
# ---------------------------------------------------------------------------


def _gate_label_for_state(cur: str | None) -> str:
    return {
        state.STATE_DRAFT_PENDING_REVIEW: "B",
        state.STATE_IMAGE_PENDING_REVIEW: "C",
        state.STATE_CHANNEL_PENDING_REVIEW: "D",
        state.STATE_READY_TO_PUBLISH: "Ready",
    }.get(str(cur or ""), "?")


def _article_title(article_id: str) -> str:
    try:
        data = json.loads(
            (agentflow_home() / "drafts" / article_id / "metadata.json")
            .read_text(encoding="utf-8")
        ) or {}
        return str(data.get("title") or "(no title)")
    except Exception:
        return "(no title)"


def _article_age_hours(article_id: str) -> float | None:
    try:
        history = state.gate_history(article_id)
        if not history:
            return None
        ts = datetime.fromisoformat(
            str(history[-1].get("timestamp") or "").replace("Z", "+00:00")
        )
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    except Exception:
        return None


def _pending_articles_with_age() -> list[tuple[str, str, str, float]]:
    """Return (article_id, gate_label, title, age_hours) tuples for every
    article in any *_pending_review state. Stable-sorted oldest-first."""
    pending_states = [
        state.STATE_DRAFT_PENDING_REVIEW,
        state.STATE_IMAGE_PENDING_REVIEW,
        state.STATE_CHANNEL_PENDING_REVIEW,
    ]
    rows: list[tuple[str, str, str, float]] = []
    for aid in state.articles_in_state(pending_states) or []:
        try:
            cur = state.current_state(aid)
        except Exception:
            cur = None
        gate_label = _gate_label_for_state(cur)
        age = _article_age_hours(aid) or 0.0
        rows.append((aid, gate_label, _article_title(aid), age))
    rows.sort(key=lambda row: -row[3])  # oldest first
    return rows


def _format_age(age_hours: float) -> str:
    if age_hours >= 24:
        return f"{int(age_hours / 24)}d+"
    if age_hours >= 1:
        return f"{int(age_hours)}h+"
    return f"{int(age_hours * 60)}m+"


# ---------------------------------------------------------------------------
# Phase 3 Wave C: TG message handlers, slash command registry, callback
# router, and (gate, action) auth map were removed. Lark callback path
# (lark_callback.py) was always independent. The Lark webhook now drives
# all human-in-the-loop interactions; the daemon only owns the timeout +
# bookkeeping main loop below.
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


_STOP = threading.Event()


def _bool_env(name: str) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _lark_app_primary() -> bool:
    return _bool_env("AGENTFLOW_LARK_APP_PRIMARY")


def _embedded_bridge_enabled() -> bool:
    return _lark_app_primary() or _bool_env("AGENTFLOW_REVIEW_DAEMON_BRIDGE_ENABLED")


def _start_embedded_bridge_if_enabled() -> None:
    """Run the agent bridge API inside review-daemon for Lark/OpenClaw.

    Lark card callbacks need `/api/commands`. Historically that endpoint lived
    only in `blogflow review-dashboard`, which made Lark-first deployments
    require a second process. The daemon is the review orchestrator, so in
    Lark-first mode it owns this bridge too.
    """
    if not _embedded_bridge_enabled():
        return
    host = os.environ.get("AGENTFLOW_REVIEW_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    raw_port = os.environ.get("AGENTFLOW_REVIEW_BRIDGE_PORT", "7860").strip() or "7860"
    try:
        port = int(raw_port)
    except ValueError:
        port = 7860

    def _run() -> None:
        try:
            import uvicorn
            from agentflow.agent_review.web import create_app

            uvicorn.run(create_app(), host=host, port=port, log_level="info")
        except Exception as err:  # pragma: no cover - startup failure is logged
            _log.warning("embedded agent bridge failed: %s", err, exc_info=True)

    threading.Thread(target=_run, daemon=True, name="agentflow-bridge-api").start()
    _log.info("embedded agent bridge API starting on http://%s:%s", host, port)


def _notify_spawn_failure(
    label: str,
    target_id: str,
    returncode: int | None,
    stderr_tail: str,
) -> None:
    """Best-effort TG notification when a spawned subprocess fails.

    Without this, subprocess crashes / timeouts only land in the daemon log;
    the operator just sees their card never appear. This surfaces failures
    in-band so they know to investigate. Never raises (best-effort).
    """
    try:
        chat_id = get_review_chat_id()
        rc = "timeout/oserr" if returncode is None else str(returncode)
        tail = (stderr_tail or "").strip()
        tail = tail[-500:] if tail else "(no stderr)"
        # Phase 3: TG side channel removed; Lark Custom Bot fan-out is the
        # on-call surface now.
        try:
            from agentflow.shared import lark_webhook
            lark_webhook.notify_spawn_failure(
                label=label, target_id=target_id, error_tail=tail,
            )
        except Exception:
            pass
        _audit({
            "kind": "spawn_failure",
            "label": label,
            "target_id": target_id,
            "returncode": returncode,
            "stderr_tail": tail,
        })
    except Exception as err:  # pragma: no cover — must never raise
        _log.warning("spawn failure notify failed: %s", err)


def _spawn_rewrite(article_id: str) -> None:
    """Re-run `af fill` on the same article with the stored title/opening/closing
    indices. After fill completes, the existing Gate B auto-trigger fires a
    fresh review card.
    """
    import json
    import subprocess
    import sys

    def _run() -> None:
        try:
            meta_path = (
                agentflow_home() / "drafts" / article_id / "metadata.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
            t = int(meta.get("chosen_title_index", 0) or 0)
            o = int(meta.get("chosen_opening_index", 0) or 0)
            c = int(meta.get("chosen_closing_index", 0) or 0)
            # Force state back to drafting so the post_gate_b state advance
            # walks cleanly to draft_pending_review again. (transition is
            # idempotent; guarded by force=True in triggers._ensure_state.)
            try:
                state.transition(
                    article_id, gate="B",
                    to_state=state.STATE_DRAFTING,
                    actor="daemon", decision="rewrite_round",
                    notes="rewrite via TG 🔁",
                    force=True,
                )
            except state.StateError:
                pass
            from agentflow.agent_review.triggers import _af_argv
            try:
                result = subprocess.run(
                    _af_argv(
                        "fill", article_id,
                        "--title", str(t), "--opening", str(o), "--closing", str(c),
                    ),
                    env=os.environ.copy(),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            except subprocess.TimeoutExpired:
                _notify_spawn_failure(
                    "rewrite", article_id, None, "timeout"
                )
                return
            if result.returncode != 0:
                _notify_spawn_failure(
                    "rewrite",
                    article_id,
                    result.returncode,
                    (result.stderr or "").strip(),
                )
        except Exception as err:  # pragma: no cover
            _log.warning("rewrite subprocess crashed for %s: %s", article_id, err)
            _notify_spawn_failure("rewrite", article_id, None, str(err))

    threading.Thread(target=_run, daemon=True).start()


# Phase 3 Wave C: ``_spawn_edit`` deleted — its only caller was the
# ``_handle_message`` text-edit branch (also removed in this wave). Lark's
# edit flow lives in ``lark_callback._spawn_edit_from_payload``.


def _spawn_publish_ready(article_id: str) -> None:
    """Run preview + medium-package + final TG post in a background thread."""
    from agentflow.agent_review import triggers as _triggers

    def _run() -> None:
        try:
            _triggers.post_publish_ready(article_id)
        except Exception as err:  # pragma: no cover
            _log.warning("publish-ready failed for %s: %s", article_id, err)
            _notify_spawn_failure("publish-ready", article_id, None, str(err))

    threading.Thread(target=_run, daemon=True).start()


def _spawn_hotspots(top_k: int = 5) -> None:
    """Run `blogflow article-hotspots` in a background thread.

    The CLI auto-triggers `post_gate_a` on completion (existing behaviour),
    so this fn just shells out and surfaces failure via _notify_spawn_failure.
    """
    import subprocess

    def _run() -> None:
        try:
            from agentflow.agent_review.triggers import _af_argv
            try:
                result = subprocess.run(
                    _af_argv("article-hotspots", "--gate-a-top-k", str(top_k)),
                    env=os.environ.copy(),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                _notify_spawn_failure("article-hotspots", "manual_scan", None, "timeout 300s")
                return
            if result.returncode != 0:
                _notify_spawn_failure(
                    "article-hotspots",
                    "manual_scan",
                    result.returncode,
                    (result.stderr or "").strip(),
                )
        except Exception as err:  # pragma: no cover
            _log.warning("article-hotspots subprocess crashed: %s", err)
            _notify_spawn_failure("article-hotspots", "manual_scan", None, str(err))

    threading.Thread(target=_run, daemon=True).start()


def _spawn_publish_mark(article_id: str, url: str) -> None:
    """Q6: run mark_published in a background thread.

    Triggered by the operator's URL reply after a [📌 我已粘贴 + URL] click.
    Failures (including dedupe / IO) are surfaced via _notify_spawn_failure
    so the user always sees feedback in the TG chat.
    """
    from agentflow.agent_review import triggers as _triggers

    def _run() -> None:
        try:
            _triggers.mark_published(
                article_id, published_url=url, platform="medium",
            )
        except Exception as err:  # pragma: no cover
            _log.warning("mark_published failed for %s: %s", article_id, err)
            try:
                _notify_spawn_failure("publish-mark", article_id, None, str(err))
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


def _spawn_locked_takeover(article_id: str) -> None:
    """Post the Manual Takeover card (3 buttons: critique / edit / give_up)
    in a background thread. Fires after the rewrite round-limit kicks in."""
    from agentflow.agent_review import triggers as _triggers

    def _run() -> None:
        try:
            _triggers.post_locked_takeover(article_id)
        except Exception as err:  # pragma: no cover
            _log.warning("post_locked_takeover failed for %s: %s", article_id, err)

    threading.Thread(target=_run, daemon=True).start()


def _spawn_locked_critique(article_id: str) -> None:
    """Run LLM critique on the draft, send it to the operator, and register
    a pending_edits entry so a follow-up reply is parsed as an edit. Runs in
    a background thread so the L:critique callback returns immediately."""
    from agentflow.agent_review import triggers as _triggers

    def _run() -> None:
        try:
            _triggers.post_critique(article_id)
        except Exception as err:  # pragma: no cover
            _log.warning("post_critique failed for %s: %s", article_id, err)

    threading.Thread(target=_run, daemon=True).start()


def _spawn_gate_d(article_id: str) -> None:
    """Post the Gate D channel-selection card in a background thread."""
    from agentflow.agent_review import triggers as _triggers

    def _run() -> None:
        try:
            _triggers.post_gate_d(article_id)
        except Exception as err:  # pragma: no cover
            _log.warning("post_gate_d failed for %s: %s", article_id, err)
            _notify_spawn_failure("gate-d", article_id, None, str(err))

    threading.Thread(target=_run, daemon=True).start()


def _spawn_dispatch_preview(
    article_id: str, selected: list[str], *, short_id: str,
) -> None:
    """Post the dispatch-preview card (Q4) in a background thread.

    Reuses the original D-gate ``short_id`` so the preview's PD:* buttons
    resolve back to the same Gate D entry.
    """
    from agentflow.agent_review import triggers as _triggers

    def _run() -> None:
        try:
            _triggers.post_dispatch_preview(
                article_id, list(selected), short_id=short_id,
            )
        except Exception as err:  # pragma: no cover
            _log.warning(
                "post_dispatch_preview failed for %s: %s", article_id, err
            )

    threading.Thread(target=_run, daemon=True).start()


def _spawn_image_gate(article_id: str, mode: str) -> None:
    """Run ``af image-gate <aid> --mode <X>`` in the background. The CLI's
    own glue self-triggers post_gate_c when generation completes (or
    post_gate_d when ``--mode none``), so this helper just spawns and
    surfaces failures."""
    import subprocess

    try:
        timeout_seconds = float(
            os.environ.get("AGENTFLOW_IMAGE_GATE_SUBPROCESS_TIMEOUT_SECONDS", "240")
        )
    except (TypeError, ValueError):
        timeout_seconds = 240.0

    def _run() -> None:
        try:
            from agentflow.agent_review.triggers import _af_argv
            try:
                result = subprocess.run(
                    _af_argv("image-gate", article_id, "--mode", mode),
                    env=os.environ.copy(),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                _notify_spawn_failure(
                    "image-gate",
                    article_id,
                    None,
                    f"timeout after {timeout_seconds:.0f}s",
                )
                return
            if result.returncode != 0:
                _notify_spawn_failure(
                    "image-gate",
                    article_id,
                    result.returncode,
                    (result.stderr or "").strip(),
                )
        except Exception as err:  # pragma: no cover
            _log.warning("image-gate subprocess crashed for %s: %s", article_id, err)
            _notify_spawn_failure("image-gate", article_id, None, str(err))

    threading.Thread(target=_run, daemon=True).start()


def _spawn_image_gate_picker(article_id: str) -> None:
    """Send the image-gate picker card (Q3/Q4) in a background thread."""
    from agentflow.agent_review import triggers as _triggers

    def _run() -> None:
        try:
            _triggers.post_image_gate_picker(article_id)
        except Exception as err:  # pragma: no cover
            _log.warning(
                "post_image_gate_picker failed for %s: %s", article_id, err
            )

    threading.Thread(target=_run, daemon=True).start()


def _spawn_relogo(article_id: str) -> None:
    """Cycle brand_overlay anchor on the existing cover image (no AtlasCloud
    re-call). Re-applies the wordmark at the next anchor in the cycle, writes
    the updated cover.png, and re-triggers Gate C so a fresh card appears.

    Anchor cycle: bottom_left → bottom_right → top_left → top_right → center
    → bottom_left.
    """
    def _run() -> None:
        try:
            anchors = [
                "bottom_left",
                "bottom_right",
                "top_left",
                "top_right",
                "center",
            ]
            meta_path = (
                agentflow_home() / "drafts" / article_id / "metadata.json"
            )
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
            except Exception:
                meta = {}

            # Locate the cover-role placeholder + its resolved_path. This is
            # the canonical cover image on disk (image_generator writes it).
            cover_path: str | None = None
            placeholders = meta.get("image_placeholders") or []
            for ph in placeholders:
                if isinstance(ph, dict) and ph.get("role") == "cover":
                    cover_path = ph.get("resolved_path")
                    break
            if not cover_path or not Path(cover_path).exists():
                _notify_spawn_failure(
                    "relogo", article_id, None,
                    "cover image not on disk (no resolved_path)",
                )
                return

            # Pick the next anchor in the cycle, falling back to bottom_left
            # when the current value is missing or unrecognized.
            cur = (meta.get("brand_overlay") or {}).get("anchor") or "bottom_left"
            try:
                idx = anchors.index(cur)
            except ValueError:
                idx = -1
            next_anchor = anchors[(idx + 1) % len(anchors)]

            # Build the brand_overlay config — start from preferences (logo
            # path + width/padding ratios) and override the anchor to the
            # next slot in the cycle. Without a logo_path there's nothing to
            # overlay; bail with a notify so the operator knows why.
            try:
                from agentflow.shared import preferences as _prefs
                prefs = _prefs.load() or {}
            except Exception as err:
                _notify_spawn_failure(
                    "relogo", article_id, None,
                    f"preferences load failed: {err}",
                )
                return
            base_cfg = (
                ((prefs.get("image_generation") or {}).get("brand_overlay")) or {}
            )
            if not base_cfg.get("logo_path"):
                _notify_spawn_failure(
                    "relogo", article_id, None,
                    "preferences.image_generation.brand_overlay.logo_path missing",
                )
                return
            cfg = dict(base_cfg)
            cfg["anchor"] = next_anchor

            try:
                from agentflow.agent_d2 import brand_overlay as _bo
            except ImportError as err:
                _notify_spawn_failure(
                    "relogo", article_id, None,
                    f"brand_overlay import failed: {err}",
                )
                return
            try:
                _bo.apply_overlay(cover_path, cfg)
            except Exception as err:
                _notify_spawn_failure("relogo", article_id, None, str(err))
                return

            # Persist the new anchor so the next 🎨 click cycles forward
            # rather than restarting from base_cfg's default.
            try:
                meta.setdefault("brand_overlay", {})["anchor"] = next_anchor
                meta["brand_overlay_applied"] = True
                meta_path.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as err:
                _log.warning("relogo metadata write failed: %s", err)

            try:
                from agentflow.agent_review import triggers as _triggers
                _triggers.post_gate_c(article_id)
            except Exception as err:
                _log.warning("relogo post_gate_c failed: %s", err)
        except Exception as err:  # pragma: no cover
            _log.warning("_spawn_relogo crashed for %s: %s", article_id, err)
            _notify_spawn_failure("relogo", article_id, None, str(err))

    threading.Thread(target=_run, daemon=True).start()


def _spawn_publish_dispatch(article_id: str, platforms: list[str]) -> None:
    """Run the multi-channel dispatch (preview + publish + medium-package)
    triggered by Gate D ✅ Confirm. Runs in a background thread so the
    callback handler returns immediately."""
    from agentflow.agent_review import triggers as _triggers

    def _run() -> None:
        try:
            _triggers.post_publish_dispatch(article_id, platforms)
        except Exception as err:  # pragma: no cover
            # post_publish_dispatch normally posts its own summary card (incl.
            # per-platform failures). Only notify here when it never returned
            # cleanly — otherwise we'd double-report.
            _log.warning("post_publish_dispatch failed for %s: %s", article_id, err)
            _notify_spawn_failure("dispatch", article_id, None, str(err))

    threading.Thread(target=_run, daemon=True).start()


def _spawn_publish_retry(article_id: str, failed_platforms: list[str]) -> None:
    """Re-run publish for the previously-failed platforms only. Posts a fresh
    dispatch summary message (with a new retry kb if anything still fails)."""
    from agentflow.agent_review import triggers as _triggers

    def _run() -> None:
        try:
            _triggers.post_publish_retry(article_id, failed_platforms)
        except Exception as err:  # pragma: no cover
            # Same rationale as dispatch: retry posts its own summary, only
            # surface here when the call itself blew up.
            _log.warning("post_publish_retry failed for %s: %s", article_id, err)
            _notify_spawn_failure("retry", article_id, None, str(err))

    threading.Thread(target=_run, daemon=True).start()


def _spawn_write_and_fill(hotspot_id: str) -> None:
    """Detach an `af write <id> --auto-pick` subprocess from the callback
    handler so the user gets quick feedback while the LLM crunches.

    The auto-pick path runs skeleton + fill in one shot. ``af fill`` in turn
    triggers the Gate B post (see triggers.post_gate_b). Result: user clicks
    Gate A ✅ 起稿 #N, then 30-90s later Gate B lands automatically.
    """
    import subprocess
    import sys

    def _run() -> None:
        try:
            from agentflow.agent_review.triggers import _af_argv
            try:
                result = subprocess.run(
                    _af_argv("write", hotspot_id, "--auto-pick"),
                    env=os.environ.copy(),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            except subprocess.TimeoutExpired:
                _notify_spawn_failure("write+fill", hotspot_id, None, "timeout")
                return
            if result.returncode != 0:
                _notify_spawn_failure(
                    "write+fill",
                    hotspot_id,
                    result.returncode,
                    (result.stderr or "").strip(),
                )
        except Exception as err:  # pragma: no cover
            _log.warning("write+fill subprocess crashed: %s", err)
            _notify_spawn_failure("write+fill", hotspot_id, None, str(err))

    threading.Thread(target=_run, daemon=True).start()


def _signal_handler(signum: int, frame: Any) -> None:  # pragma: no cover
    _log.info("got signal %s, stopping daemon", signum)
    _STOP.set()


def _acquire_singleton_lock() -> Any:
    """Acquire an exclusive flock on a daemon lock file.

    Prevents two daemons from racing the same TG long-poll (which manifests as
    repeated 409 Conflict warnings and missed callbacks). Returns the open
    file handle — the caller MUST keep it alive for the daemon lifetime;
    flock is released when the file is closed or the process exits.
    """
    import fcntl
    lock_path = _REVIEW_HOME / "daemon.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SystemExit(
            f"another review-daemon already holds {lock_path}.\n"
            "if you're sure no other daemon is running (e.g. after a hard kill), "
            f"remove the file and retry: rm {lock_path}"
        )
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _detect_downtime_and_notify() -> None:
    """If the previous heartbeat is stale, surface a TG warning so the operator
    knows that any TG buttons clicked during the gap silently no-op'd."""
    try:
        from datetime import datetime, timezone
        path = _heartbeat_path()
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        ts = data.get("timestamp")
        if not ts:
            return
        last = datetime.fromisoformat(ts)
        gap_minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
        if gap_minutes < 60:
            return
        _log.warning(
            "daemon downtime detected: %.1fh gap since last heartbeat at %s",
            gap_minutes / 60.0,
            ts[:19],
        )
    except Exception as err:
        _log.warning("downtime detection failed (non-fatal): %s", err)


def _warn_if_mock_mode_active() -> None:
    """Loudly warn if we'd silently use mock fixtures or fake publish URLs.

    A production daemon should never run in mock mode — but the bundled
    .env.template historically defaulted MOCK_LLM=true, and operators who
    ``cp .env.template .env`` without reading would unwittingly publish
    ``https://medium.com/@mock/...`` URLs into publish_history.
    """
    flags = []
    if os.environ.get("MOCK_LLM", "").strip().lower() == "true":
        flags.append("MOCK_LLM=true (D0/D1/D2/D3 use deterministic fixtures)")
    if os.environ.get("AGENTFLOW_MOCK_PUBLISHERS", "").strip().lower() == "true":
        flags.append("AGENTFLOW_MOCK_PUBLISHERS=true (publishers return fake URLs)")
    if not flags:
        return
    banner = " | ".join(flags)
    _log.warning("⚠ MOCK MODE ACTIVE: %s", banner)


def run(*, poll_interval: float | None = None, skip_preflight: bool = False) -> None:
    """Foreground daemon. Blocks until SIGINT / SIGTERM."""
    _LOCK_FILE_HANDLE = _acquire_singleton_lock()  # noqa: F841 — keep alive

    if not skip_preflight:
        from agentflow.agent_review import preflight as _pf

        try:
            _pf.assert_ready_for_review_daemon()
        except _pf.PreflightError as err:
            _log.error("preflight failed: %s", err)
            raise SystemExit(
                f"preflight failed: {err}\n"
                "run `af doctor` for details, or `af review-daemon --skip-preflight` "
                "to bypass."
            )

    _start_embedded_bridge_if_enabled()

    interval = poll_interval if poll_interval is not None else float(
        os.environ.get("TELEGRAM_POLL_INTERVAL_SECONDS", "5")
    )
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Phase 3 Wave C: TG poll loop removed. Lark callback (lark_callback.py
    # mounted on the embedded bridge / dashboard) is now the single human
    # surface. The daemon's main loop is bookkeeping-only: short_id GC,
    # timeout-scan pings, deferred-repost drain, and scheduled hotspots.
    if not _lark_app_primary():
        raise SystemExit(
            "Set one review surface: AGENTFLOW_LARK_APP_PRIMARY=true with the "
            "Lark App event webhook configured. (Phase 3 removed the Telegram "
            "fallback path; legacy TELEGRAM_BOT_TOKEN setups are no longer "
            "supported.)"
        )
    _log.info("review daemon started in Lark-first mode")

    # Surface non-fatal warnings BEFORE the bookkeeping loop starts so the
    # operator sees them immediately. Best-effort.
    _detect_downtime_and_notify()
    _warn_if_mock_mode_active()

    last_gc = time.monotonic()
    last_timeout_scan = time.monotonic()

    while not _STOP.is_set():
        _write_heartbeat()
        now = time.monotonic()
        if now - last_gc > 60:
            removed = _sid.gc()
            if removed:
                _log.info("short_id GC removed %d expired entries", removed)
            last_gc = now
        if now - last_timeout_scan > 60:
            _scan_timeouts()
            try:
                fired = _drain_deferred_reposts()
                if fired:
                    _log.info("drained %d deferred reposts", fired)
            except Exception as err:  # pragma: no cover
                _log.warning("deferred repost drain failed: %s", err)
            try:
                from agentflow.agent_review import schedule as _schedule
                fired_slots = _schedule.fire_due(_spawn_hotspots)
                if fired_slots:
                    _log.info(
                        "scheduled hotspots fired for slots: %s",
                        ", ".join(fired_slots),
                    )
            except Exception as err:  # pragma: no cover
                _log.warning("scheduled article-hotspots scan failed: %s", err)
            last_timeout_scan = now
        time.sleep(interval)


def _gate_a_timeout_state_path() -> Path:
    """Per-sid timeout book-keeping for Gate A (no article_id exists yet, so
    we can't reuse ``timeout_state`` which is keyed by article_id and gets
    GC'd against ``articles_in_state``)."""
    p = _REVIEW_HOME / "gate_a_timeout_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _scan_timeouts() -> None:
    """Walk every ``*_pending_review`` article; for each one past the
    first-ping or second-action cutoff, ping the operator (Gate B) or
    auto-fallback to ``image_skipped`` (Gate C). Idempotent — uses
    ``timeout_state`` so re-running every ~60s never re-spams.

    Also scans Gate A short_id entries (which have no article_id yet) and
    pings / auto-rejects the batch via a sid-keyed state file.

    Cutoffs come from env (defaults match templates/state_machine.md):
      REVIEW_GATE_A_PING_HOURS         (default 12)
      REVIEW_GATE_A_AUTOREJECT_HOURS   (default 24)
      REVIEW_GATE_B_PING_HOURS         (default 12)
      REVIEW_GATE_B_SECOND_PING_HOURS  (default 24)
      REVIEW_GATE_C_PING_HOURS         (default 6)
      REVIEW_GATE_C_AUTOSKIP_HOURS     (default 12)
    """
    def _hrs(env_key: str, default: float) -> float:
        try:
            return float(os.environ.get(env_key, "") or default)
        except ValueError:
            return default

    a_first = _hrs("REVIEW_GATE_A_PING_HOURS", 12)
    a_autoreject = _hrs("REVIEW_GATE_A_AUTOREJECT_HOURS", 24)
    b_first = _hrs("REVIEW_GATE_B_PING_HOURS", 12)
    b_second = _hrs("REVIEW_GATE_B_SECOND_PING_HOURS", 24)
    c_first = _hrs("REVIEW_GATE_C_PING_HOURS", 6)
    c_autoskip = _hrs("REVIEW_GATE_C_AUTOSKIP_HOURS", 12)
    d_autocancel = _hrs("REVIEW_GATE_D_AUTOCANCEL_HOURS", 12)

    pending_states = {
        state.STATE_DRAFT_PENDING_REVIEW,
        state.STATE_IMAGE_PENDING_REVIEW,
        state.STATE_CHANNEL_PENDING_REVIEW,
    }
    pending = state.articles_in_state(list(pending_states))

    # Clear stale book-keeping for any tracked article that has since left a
    # *_pending_review state (so a future re-entry pings from scratch).
    try:
        tracked = list(timeout_state._read().keys())  # type: ignore[attr-defined]
    except Exception:
        tracked = []
    for aid in tracked:
        # Q2: ``__digest__`` is the special daily-digest cooldown meta key —
        # never article-keyed, never to be GC'd by the per-article cleanup.
        if aid.startswith("__"):
            continue
        if aid in pending:
            continue
        try:
            cur = state.current_state(aid)
        except Exception:
            cur = None
        if cur not in pending_states:
            timeout_state.clear(aid)

    if not pending:
        return

    now = datetime.now(timezone.utc)

    # Phase 3: timeout sweeper is Lark-only. The Lark card for the active
    # gate is already on the operator's screen — re-pinging via a daemon-
    # side message would just spam them. So the sweeper now just records
    # the timeout state + audit, and runs the auto-skip / auto-cancel
    # transitions where applicable. No text rendering, no escape_md2.

    for aid in pending:
        try:
            history = state.gate_history(aid)
        except Exception:
            continue
        if not history:
            continue
        last = history[-1]
        cur = str(last.get("to_state") or "")
        try:
            ts = datetime.fromisoformat(last.get("timestamp") or "")
        except ValueError:
            continue
        hrs = (now - ts).total_seconds() / 3600.0

        if cur == state.STATE_DRAFT_PENDING_REVIEW:
            if hrs >= b_first and not timeout_state.has_first_pinged(aid):
                timeout_state.mark_first_pinged(aid)
                _audit({
                    "kind": "timeout_first_ping",
                    "gate": "B",
                    "article_id": aid,
                    "hrs": round(hrs, 2),
                })
            if (
                hrs >= b_second
                and timeout_state.has_first_pinged(aid)
                and not timeout_state.has_second_action(aid)
            ):
                timeout_state.mark_second_action(aid)
                _audit({
                    "kind": "timeout_second_ping",
                    "gate": "B",
                    "article_id": aid,
                    "hrs": round(hrs, 2),
                })

        elif cur == state.STATE_IMAGE_PENDING_REVIEW:
            if hrs >= c_first and not timeout_state.has_first_pinged(aid):
                timeout_state.mark_first_pinged(aid)
                _audit({
                    "kind": "timeout_first_ping",
                    "gate": "C",
                    "article_id": aid,
                    "hrs": round(hrs, 2),
                })
            if hrs >= c_autoskip and not timeout_state.has_second_action(aid):
                try:
                    state.transition(
                        aid,
                        gate="C",
                        to_state=state.STATE_IMAGE_SKIPPED,
                        actor="daemon",
                        decision="auto_skip_timeout",
                        notes=f"hrs={hrs:.1f}",
                        force=True,
                    )
                except Exception as err:  # pragma: no cover
                    _log.warning("auto_skip transition failed for %s: %s", aid, err)
                    continue
                # Trigger Gate D card after auto-skip so the article doesn't stall.
                try:
                    _spawn_gate_d(aid)
                except Exception as err:  # pragma: no cover
                    _log.warning("_spawn_gate_d after auto_skip failed for %s: %s", aid, err)
                timeout_state.mark_second_action(aid)
                _audit({
                    "kind": "timeout_auto_skip",
                    "gate": "C",
                    "article_id": aid,
                    "hrs": round(hrs, 2),
                })

        elif cur == state.STATE_CHANNEL_PENDING_REVIEW:
            if hrs >= d_autocancel and not timeout_state.has_second_action(aid):
                try:
                    state.transition(
                        aid, gate="D",
                        to_state=state.STATE_IMAGE_APPROVED,
                        actor="daemon",
                        decision="auto_cancel_timeout",
                        notes=f"hrs={hrs:.1f}",
                        force=True,
                    )
                except Exception as err:
                    _log.warning("auto_cancel transition failed for %s: %s", aid, err)
                    continue
                # Mint a fresh extend sid (12h TTL) so the Lark Gate D card's
                # "再延 12h" button has something to resolve against. The
                # handler enforces the per-article 1-extension cap via
                # timeout_state.extended_count.
                try:
                    _sid.register(
                        gate="D",
                        article_id=aid,
                        ttl_hours=12,
                        extra={"extend_only": True},
                    )
                except Exception as err:
                    _log.warning(
                        "Gate D extend sid mint failed for %s: %s", aid, err
                    )
                _log.info(
                    "Gate D auto_cancel extend window opened for %s", aid,
                )
                timeout_state.mark_second_action(aid)
                _audit({
                    "kind": "timeout_auto_cancel",
                    "gate": "D",
                    "article_id": aid,
                    "hrs": round(hrs, 2),
                })

    # ── Gate A timeout sweep ────────────────────────────────────────────
    # Hotspots have no article_id yet, so we drive purely off the short_id
    # index entry's ``created_at`` and a sid-keyed timeout state file.
    try:
        sid_index = _sid._read()  # type: ignore[attr-defined]
    except Exception as err:  # graceful: never crash the daemon loop
        _log.warning("Gate A sweep: short_id read failed: %s", err)
        sid_index = {}

    try:
        ga_path = _gate_a_timeout_state_path()
        ga_state: dict[str, dict[str, Any]] = (
            _read_json(ga_path, {}) if ga_path.exists() else {}
        ) or {}
    except Exception as err:
        _log.warning("Gate A sweep: state file read failed: %s", err)
        ga_state = {}

    ga_dirty = False
    for sid, gentry in list(sid_index.items()):
        if not isinstance(gentry, dict):
            continue
        if gentry.get("gate") != "A":
            continue
        created_at = gentry.get("created_at")
        if not created_at:
            continue
        try:
            ts = datetime.fromisoformat(created_at)
        except ValueError:
            continue
        ga_hrs = (now - ts).total_seconds() / 3600.0
        ga_entry = ga_state.get(sid) or {}
        first_pinged = bool(ga_entry.get("first_pinged_at"))
        second_done = bool(ga_entry.get("second_action_taken_at"))
        batch_path = gentry.get("batch_path") or ""

        if ga_hrs >= a_first and not first_pinged:
            ga_entry["first_pinged_at"] = datetime.now(timezone.utc).isoformat()
            ga_state[sid] = ga_entry
            ga_dirty = True
            _audit({
                "kind": "timeout_first_ping",
                "gate": "A",
                "short_id": sid,
                "batch_path": batch_path,
                "hrs": round(ga_hrs, 2),
            })

        if ga_hrs >= a_autoreject and not second_done:
            # Auto-reject the batch: flag every hotspot as rejected_batch
            # (mirrors the A:reject_all callback path), revoke the sid, and
            # ping the operator. All file-IO is graceful.
            rejected_count = 0
            io_err: str | None = None
            if batch_path:
                try:
                    with open(batch_path, "r", encoding="utf-8") as fh:
                        batch_data = json.load(fh) or {}
                    hotspots = batch_data.get("hotspots") or []
                    if isinstance(hotspots, list):
                        for hs in hotspots:
                            if isinstance(hs, dict):
                                hs["status"] = "rejected_batch"
                                rejected_count += 1
                        batch_data["hotspots"] = hotspots
                        with open(batch_path, "w", encoding="utf-8") as fh:
                            json.dump(batch_data, fh, ensure_ascii=False, indent=2)
                except Exception as err:
                    io_err = str(err)[:200]
                    _log.warning(
                        "Gate A auto-reject batch write failed (path=%s): %s",
                        batch_path, err,
                    )
            else:
                io_err = "missing batch_path"

            try:
                _sid.revoke(sid)
            except Exception as err:
                _log.warning("Gate A auto-reject sid revoke failed (sid=%s): %s", sid, err)

            _audit({
                "kind": "timeout_auto_reject_batch",
                "gate": "A",
                "short_id": sid,
                "batch_path": batch_path,
                "hotspot_count": rejected_count,
                "hrs": round(ga_hrs, 2),
                **({"io_error": io_err} if io_err else {}),
            })
            ga_entry["second_action_taken_at"] = datetime.now(timezone.utc).isoformat()
            ga_state[sid] = ga_entry
            ga_dirty = True

    # Best-effort: drop sid entries that no longer appear in the index
    # (covers manual revoke / GC), so the file doesn't grow unboundedly.
    for stale_sid in [s for s in ga_state.keys() if s not in sid_index]:
        ga_state.pop(stale_sid, None)
        ga_dirty = True

    if ga_dirty:
        try:
            _write_json(_gate_a_timeout_state_path(), ga_state)
        except Exception as err:
            _log.warning("Gate A sweep: state file write failed: %s", err)

    # ── Q2: 每日 publish-mark digest (24h cooldown) ────────────────────────
    # Stored under the special ``__digest__`` key in timeout_state.json so we
    # don't collide with article-keyed entries (article_ids never start with
    # underscores in the AgentFlow corpus).
    try:
        state_data = timeout_state._read()  # type: ignore[attr-defined]
        digest_meta = state_data.get("__digest__") or {}
        last_digest = digest_meta.get("last_digest_at")
        should_run = False
        if not last_digest:
            should_run = True
        else:
            try:
                last_dt = datetime.fromisoformat(last_digest)
                elapsed_hours = (
                    datetime.now(timezone.utc) - last_dt
                ).total_seconds() / 3600
                if elapsed_hours >= 24:
                    should_run = True
            except ValueError:
                should_run = True

        if should_run:
            from agentflow.agent_review import triggers as _triggers
            try:
                result = _triggers.post_publish_digest()
                digest_meta["last_digest_at"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                digest_meta["last_count"] = (result or {}).get("count", 0)
                state_data["__digest__"] = digest_meta
                timeout_state._write(state_data)  # type: ignore[attr-defined]
            except Exception as err:
                _log.warning("daily digest failed: %s", err)
    except Exception:
        pass
