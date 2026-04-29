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
    render as _render,
    self_check,
    short_id as _sid,
    state,
    tg_client,
    timeout_state,
)
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
    chat_id = session.get("active_chat_id")
    if chat_id is None:
        return
    step_index = int(session.get("step_index") or 0)
    if step_index >= len(_PROFILE_SETUP_STEPS):
        return
    step = _PROFILE_SETUP_STEPS[step_index]
    text = _render.render_profile_setup_question(
        profile_id=str(session.get("profile_id") or ""),
        display_name=_session_display_name(session),
        step_label=str(step.get("label") or step.get("key") or "Field"),
        prompt=str(step.get("prompt") or ""),
        step_index=step_index + 1,
        total_steps=len(_PROFILE_SETUP_STEPS),
    )
    tg_client.send_message(chat_id, text, parse_mode="MarkdownV2")


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
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(f"❌ profile setup failed: {result.stderr or result.stdout}"[:3500]),
                    parse_mode="MarkdownV2",
                )
                session["status"] = "failed"
                save_session(session)
                return
            session["status"] = "applied"
            save_session(session)
            tg_client.send_message(
                chat_id,
                _render.escape_md2(
                    f"✅ profile setup applied for {profile_id}. You can now re-run hotspots/search with this profile."
                ),
                parse_mode="MarkdownV2",
            )
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
        tg_client.send_message(
            chat_id,
            "🧾 已收集完成，正在调用 CLI 落盘…",
            parse_mode="MarkdownV2",
        )
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
    """Best-effort Telegram command menu setup for the operator bot.

    v1.0.4 — set the curated 12-command subset built from
    ``_COMMAND_REGISTRY`` rather than the legacy 15-entry ``_BOT_COMMANDS``
    list. Long-tail commands stay accessible via the text dispatcher.
    """
    try:
        commands = _build_set_my_commands_payload() if _COMMAND_REGISTRY else _BOT_COMMANDS
        tg_client.set_my_commands(commands)
        tg_client.set_chat_menu_button(chat_id=chat_id, menu_button={"type": "commands"})
        _log.info("configured Telegram bot command menu")
    except Exception as err:  # pragma: no cover - menu setup must never block daemon
        _log.warning("Telegram bot menu setup skipped: %s", err)


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
                chat_id = get_review_chat_id()
                if chat_id is not None:
                    tg_client.send_message(
                        chat_id,
                        f"⏰ Gate A defer 到期: 重新审阅 batch {batch_path}",
                        parse_mode=None,
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


def _send_status_summary(chat_id: int | None) -> None:
    if chat_id is None:
        return
    rows = _pending_articles_with_age()
    if not rows:
        tg_client.send_message(chat_id, "✨ 无 pending 卡片", parse_mode=None)
        return
    lines: list[str] = [f"📊 *Pending* ({_render.escape_md2(len(rows))})", ""]
    for aid, gate_label, title, age in rows[:30]:
        aid_short = aid[:8] if len(aid) > 8 else aid
        lines.append(
            f"• {_render.escape_md2(gate_label)}: "
            f"`{_render.escape_md2(aid_short)}` — "
            f"{_render.escape_md2(title)} "
            f"\\({_render.escape_md2(_format_age(age))}\\)"
        )
    if len(rows) > 30:
        lines.append("")
        lines.append(_render.escape_md2(f"… 还有 {len(rows) - 30} 条"))
    tg_client.send_message(chat_id, "\n".join(lines), parse_mode="MarkdownV2")


def _send_queue_summary(chat_id: int | None, *, limit: int = 5) -> None:
    if chat_id is None:
        return
    rows = _pending_articles_with_age()
    if not rows:
        tg_client.send_message(chat_id, "✨ 队列空", parse_mode=None)
        return
    lines: list[str] = [
        f"📋 *Queue* (top {_render.escape_md2(min(limit, len(rows)))} oldest)",
        "",
    ]
    for aid, gate_label, title, age in rows[:limit]:
        aid_short = aid[:8] if len(aid) > 8 else aid
        lines.append(
            f"• {_render.escape_md2(gate_label)}: "
            f"`{_render.escape_md2(aid_short)}` — "
            f"{_render.escape_md2(title)} "
            f"\\({_render.escape_md2(_format_age(age))}\\)"
        )
    tg_client.send_message(chat_id, "\n".join(lines), parse_mode="MarkdownV2")


def _slash_skip(
    chat_id: int | None, uid: int | None, target_id: str
) -> str | None:
    if not target_id:
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id, "用法: /skip <article_id>", parse_mode=None,
                )
            except Exception:
                pass
        return "missing_id"
    try:
        cur = state.current_state(target_id)
    except Exception as err:
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id, f"❌ 未知 article_id: {err}"[:300],
                    parse_mode=None,
                )
            except Exception:
                pass
        return "no_article"
    if cur != state.STATE_IMAGE_PENDING_REVIEW:
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id,
                    f"❌ /skip 仅对 image_pending_review 生效, 当前 state={cur}",
                    parse_mode=None,
                )
            except Exception:
                pass
        return "wrong_state"
    try:
        state.transition(
            target_id, gate="C",
            to_state=state.STATE_IMAGE_SKIPPED,
            actor="human", decision="slash_skip",
            notes=f"uid={uid}", force=True,
        )
    except state.StateError as err:
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id, f"❌ skip 失败: {err}"[:300], parse_mode=None,
                )
            except Exception:
                pass
        return "transition_failed"
    timeout_state.clear(target_id)
    try:
        _spawn_gate_d(target_id)
    except Exception as err:
        _log.warning("/skip _spawn_gate_d failed: %s", err)
    if chat_id is not None:
        try:
            tg_client.send_message(
                chat_id,
                f"🚫 已 skip image-gate, 推 Gate D: {target_id}",
                parse_mode=None,
            )
        except Exception:
            pass
    return None


def _slash_defer(
    chat_id: int | None, uid: int | None, target_id: str, hours: float,
) -> str | None:
    try:
        cur = state.current_state(target_id)
    except Exception:
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id, f"❌ 未知 article_id: {target_id}",
                    parse_mode=None,
                )
            except Exception:
                pass
        return "no_article"
    gate_for_state = {
        state.STATE_DRAFT_PENDING_REVIEW: "B",
        state.STATE_IMAGE_PENDING_REVIEW: "C",
        state.STATE_CHANNEL_PENDING_REVIEW: "D",
    }
    gate = gate_for_state.get(str(cur or ""))
    if gate is None:
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id,
                    f"❌ /defer 仅对 *_pending_review 生效, 当前 state={cur}",
                    parse_mode=None,
                )
            except Exception:
                pass
        return "wrong_state"
    try:
        _schedule_deferred_repost(
            gate=gate,
            article_id=target_id,
            batch_path=None,
            hours=float(hours),
            source_sid=f"slash:{uid or '?'}",
        )
    except Exception as err:
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id, f"❌ defer 调度失败: {err}"[:300],
                    parse_mode=None,
                )
            except Exception:
                pass
        return "schedule_failed"
    if chat_id is not None:
        try:
            tg_client.send_message(
                chat_id,
                f"⏰ Gate {gate} 已 defer {hours}h: {target_id}",
                parse_mode=None,
            )
        except Exception:
            pass
    return None


def _slash_publish_mark(
    chat_id: int | None,
    uid: int | None,
    target_id: str,
    url: str,
    platform: str,
) -> str | None:
    if not url.startswith(("http://", "https://")):
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id, "❌ URL 必须以 http(s):// 开头",
                    parse_mode=None,
                )
            except Exception:
                pass
        return "bad_url"
    from agentflow.agent_review import triggers as _triggers
    try:
        result = _triggers.mark_published(
            target_id, published_url=url, platform=platform,
            notes=f"slash_uid={uid}",
        )
    except (ValueError, FileNotFoundError) as err:
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id, f"❌ publish-mark 失败: {err}"[:500],
                    parse_mode=None,
                )
            except Exception:
                pass
        return "mark_failed"
    if chat_id is not None:
        try:
            tg_client.send_message(
                chat_id,
                f"📌 已标记 published: {result.get('article_id')} → "
                f"{result.get('published_url')} ({result.get('platform')})",
                parse_mode=None,
            )
        except Exception:
            pass
    return None


def _send_audit_tail(chat_id: int | None, *, limit: int = 20) -> None:
    if chat_id is None:
        return
    ap = _audit_path()
    if not ap.exists():
        tg_client.send_message(chat_id, "📋 audit 空", parse_mode=None)
        return
    try:
        raw_lines = ap.read_text(encoding="utf-8").splitlines()
    except Exception as err:
        tg_client.send_message(chat_id, f"❌ audit 读失败: {err}"[:300], parse_mode=None)
        return
    decisions: list[dict[str, Any]] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("kind") or ""
        if kind in {
            "callback", "callback_action_done", "callback_action_denied",
            "callback_unauthorized", "slash_command",
        }:
            decisions.append(obj)
    decisions = decisions[-limit:]
    if not decisions:
        tg_client.send_message(chat_id, "📋 audit 暂无 callback/slash 记录", parse_mode=None)
        return
    lines: list[str] = [f"📋 *Audit* (last {_render.escape_md2(len(decisions))})", ""]
    for ev in decisions:
        ts = str(ev.get("audit_ts") or "")[11:19]  # HH:MM:SS
        kind = str(ev.get("kind") or "?")
        if kind == "slash_command":
            label = f"{ev.get('cmd') or '?'}"
        else:
            label = f"{ev.get('gate') or '?'}:{ev.get('action') or '?'}"
        uid_repr = ev.get("uid") or "?"
        target = ev.get("article_id") or ev.get("short_id") or ""
        lines.append(
            f"• `{_render.escape_md2(ts)}` "
            f"{_render.escape_md2(kind)} "
            f"{_render.escape_md2(label)} "
            f"uid\\={_render.escape_md2(uid_repr)} "
            f"{_render.escape_md2(str(target)[:24])}"
        )
    tg_client.send_message(chat_id, "\n".join(lines), parse_mode="MarkdownV2")


def _send_auth_debug(chat_id: int | None, uid: int | None) -> None:
    if chat_id is None:
        return
    rows = sorted(_ACTION_REQ.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    lines: list[str] = [
        f"🔐 *Auth Debug* (uid `{_render.escape_md2(uid)}`)",
        "",
    ]
    for (gate, action), required in rows:
        ok = auth.is_authorized(uid, action=required)
        marker = "✅" if ok else "🚫"
        lines.append(
            f"{marker} {_render.escape_md2(gate)}:"
            f"{_render.escape_md2(action)} "
            f"\\(needs `{_render.escape_md2(required)}`\\)"
        )

    # v1.0.4 — also list slash-command auth resolved per-bucket from the
    # registry so the operator can see at a glance which /commands they can
    # fire.
    lines.append("")
    lines.append("*Slash commands by required grant*")
    by_bucket: dict[str, list[str]] = {}
    for canonical, meta in sorted(_COMMAND_REGISTRY.items()):
        bucket = str(meta.get("auth") or "review")
        by_bucket.setdefault(bucket, []).append(f"/{canonical}")
    for bucket in sorted(by_bucket):
        ok = auth.is_authorized(uid, action=bucket)
        marker = "✅" if ok else "🚫"
        names = " ".join(by_bucket[bucket])
        lines.append(
            f"{marker} `{_render.escape_md2(bucket)}`: "
            f"{_render.escape_md2(names)}"
        )

    tg_client.send_message(chat_id, "\n".join(lines), parse_mode="MarkdownV2")


def _build_help_text() -> str:
    """Render the operator help card. Commands grouped by ``group``, role
    matrix lines below, both regenerated from ``_COMMAND_REGISTRY`` and
    ``_ACTION_REQ`` so they cannot drift."""
    grouped: dict[str, list[tuple[str, str]]] = {}
    group_order: list[str] = []
    for canonical, meta in _COMMAND_REGISTRY.items():
        group = str(meta.get("group") or "Misc")
        if group not in grouped:
            grouped[group] = []
            group_order.append(group)
        grouped[group].append((canonical, str(meta.get("summary") or "")))

    lines: list[str] = []
    for group in group_order:
        lines.append(f"*{_render.escape_md2(group)}*")
        for name, summary in sorted(grouped[group]):
            lines.append(
                f"• `{_render.escape_md2('/' + name)}` — "
                f"{_render.escape_md2(summary)}"
            )
        lines.append("")

    gate_legend = [
        ("Gate A", "选题 (write/expand/defer/reject_all)"),
        ("Gate B", "草稿 (approve/edit/rewrite/diff/reject/defer)"),
        ("Gate C", "封面 (approve/regen/relogo/full/skip/defer)"),
        ("Gate D", "渠道 (toggle/select_all/clear_all/save_default/confirm/cancel/extend/resume)"),
    ]
    lines.append("*Gate Legend*")
    for g, d in gate_legend:
        lines.append(
            f"• *{_render.escape_md2(g)}* — {_render.escape_md2(d)}"
        )
    lines.append("")

    role_rows = sorted(_ACTION_REQ.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    lines.append("*Role Matrix* \\(action → required grant\\)")
    for (g, a), req in role_rows:
        lines.append(
            f"• `{_render.escape_md2(g)}:{_render.escape_md2(a)}` → "
            f"`{_render.escape_md2(req)}`"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# v1.0.4 — Operator-completeness command registry.
#
# Canonical name (no leading slash, hyphens preserved) → metadata dict.
# Each entry carries:
#   aliases    : list[str] — alternate forms (typically the underscored
#                variant, since Telegram setMyCommands rejects hyphens).
#   group      : str        — heading used by /help (Bootstrap, Profile, …).
#   auth       : str        — required action grant for ``auth.is_authorized``.
#                The ``system`` bucket is new in v1.0.4.
#   summary    : str        — one-line description rendered by /help.
#   handler    : Callable   — concrete implementation. Signature:
#                ``handler(chat_id, uid, args, raw_text) -> None``.
#   multi_turn : bool       — True if the handler queues a follow-up reply via
#                ``pending_edits.register``.
#
# The text dispatcher (``_dispatch_v104_command``) accepts both ``/foo-bar``
# and ``/foo_bar`` and routes them to the same handler. Existing v1.0.3
# inline handlers in ``_handle_message`` continue to handle their canonical
# slash strings (``/status``, ``/queue``, …) — registry entries describe
# them so help/auth-debug stay in sync.
# ---------------------------------------------------------------------------


# Canonical setMyCommands subset surfaced in the global Telegram menu.
# Long-tail commands are still accepted by the text dispatcher.
_V104_MENU_NAMES: tuple[str, ...] = (
    "help", "status", "queue", "skip", "defer", "scan",
    "profile", "profiles", "profile_switch", "style",
    "doctor", "audit",
)


_COMMAND_REGISTRY: dict[str, dict[str, Any]] = {}


def _register_command(
    canonical: str,
    *,
    aliases: list[str] | None = None,
    group: str,
    auth_bucket: str,
    summary: str,
    handler: Any,
    multi_turn: bool = False,
) -> None:
    _COMMAND_REGISTRY[canonical] = {
        "aliases": list(aliases or []),
        "group": group,
        "auth": auth_bucket,
        "summary": summary,
        "handler": handler,
        "multi_turn": multi_turn,
    }


def _safe_send(chat_id: int | None, text: str, *, parse_mode: str | None = None) -> None:
    if chat_id is None:
        return
    try:
        tg_client.send_message(chat_id, text, parse_mode=parse_mode)
    except Exception:
        pass


def _audit_slash(cmd: str, uid: int | None, **extra: Any) -> None:
    payload = {"kind": "slash_command", "cmd": cmd, "uid": uid, **extra}
    _audit(payload)


def _norm_command_name(name: str) -> str:
    """Normalize ``/foo-bar`` and ``/foo_bar`` to canonical hyphen form for
    registry lookup. Also handles bare ``foo_bar``."""
    raw = name.strip()
    if raw.startswith("/"):
        raw = raw[1:]
    raw = raw.split("@", 1)[0]  # /foo@BotName -> foo
    return raw.replace("_", "-").lower()


def _resolve_command(text: str) -> tuple[str, list[str]] | None:
    """Look up the registry entry for an inbound message.

    Returns ``(canonical, args)`` or ``None`` if the leading token isn't a
    known v1.0.4 command. Argument parsing is whitespace-split — handlers
    that need richer parsing should re-tokenize ``raw_text`` themselves.
    """
    if not text or not text.startswith("/"):
        return None
    parts = text.split(maxsplit=1)
    head = parts[0]
    norm = _norm_command_name(head)
    canonical: str | None = None
    if norm in _COMMAND_REGISTRY:
        canonical = norm
    else:
        for name, meta in _COMMAND_REGISTRY.items():
            for alias in meta.get("aliases") or []:
                if _norm_command_name(alias) == norm:
                    canonical = name
                    break
            if canonical is not None:
                break
    if canonical is None:
        return None
    args = parts[1].split() if len(parts) > 1 else []
    return canonical, args


# ---------------------------------------------------------------------------
# v1.0.4 handlers — most wrap an existing CLI Click callback so we never
# shell out to ``af``. Pattern: import the Click command, call its
# ``.callback(...)`` directly inside a CliRunner-style guarded block, capture
# stdout via redirect_stdout for the TG reply.
# ---------------------------------------------------------------------------


def _capture_callback(callback: Any, *args: Any, **kwargs: Any) -> tuple[str, str | None]:
    """Run a Click ``.callback(...)`` capturing stdout. Returns
    ``(stdout, error_message)``. ``error_message`` is None on success."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    err_msg: str | None = None
    try:
        with redirect_stdout(buf):
            callback(*args, **kwargs)
    except click.ClickException as exc:
        err_msg = exc.format_message()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            err_msg = f"command exited with code {exc.code}"
    except Exception as exc:  # pragma: no cover — defensive
        err_msg = f"{type(exc).__name__}: {exc}"
    return buf.getvalue(), err_msg


def _trim_for_tg(text: str, *, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ---- Bootstrap ------------------------------------------------------------


def _profile_session_active(uid: int | None) -> bool:
    """Whether ``uid`` already has an in-progress profile session."""
    if uid is None:
        return False
    try:
        session = find_active_session_for_uid(int(uid))
    except Exception:
        return False
    return session is not None


def _start_profile_setup_session(
    chat_id: int | None,
    uid: int | None,
    *,
    profile_id: str | None = None,
) -> None:
    """Create a new bootstrap session and ask Step 1.

    Reuses the existing ``_PROFILE_SETUP_STEPS`` walker so behaviour matches
    auto-init. ``profile_id`` may be ``None`` for /onboard (default profile)
    or a concrete id for /profile-init.
    """
    if chat_id is None or uid is None:
        return
    target_pid = (profile_id or "").strip()
    if not target_pid:
        try:
            from agentflow.cli.commands import _default_topic_profile_id
            target_pid = _default_topic_profile_id() or "default"
        except Exception:
            target_pid = "default"

    try:
        bootstrap_state = user_profile_bootstrap_state(target_pid)
    except Exception:
        bootstrap_state = {}

    session = {
        "uid": int(uid),
        "chat_id": int(chat_id),
        "profile_id": target_pid,
        "step_index": 0,
        "answers": {},
        "bootstrap_state": bootstrap_state,
        "steps": list(_PROFILE_SETUP_STEPS),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source": "v104_slash",
    }
    try:
        save_session(session)
    except Exception as err:
        _safe_send(
            chat_id,
            f"❌ profile session 创建失败: {err}"[:400],
        )
        return
    _send_profile_setup_question(session)


def _handle_onboard(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if _profile_session_active(uid):
        _safe_send(chat_id, "⚙️ 已有进行中的 onboard 会话；继续按提示回复即可。")
        _audit_slash("/onboard", uid, status="already_active")
        return
    _safe_send(
        chat_id, "⚙️ 开始引导式 onboard：将依次询问 brand / voice / sources / rules。"
    )
    _start_profile_setup_session(chat_id, uid)
    _audit_slash("/onboard", uid, status="started")


def _handle_doctor(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    try:
        from agentflow.cli.review_commands import doctor_cmd  # type: ignore
    except Exception as err:
        _safe_send(chat_id, f"❌ doctor 不可用: {err}"[:400])
        _audit_slash("/doctor", uid, error="import_failed")
        return
    out, err = _capture_callback(
        doctor_cmd.callback, strict=False, fresh=False, as_json=False,
    )
    body = out.strip() or "(no output)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"🏥 doctor:\n{body}"))
    _audit_slash("/doctor", uid)


def _handle_scan(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    top_k = 3
    if args:
        try:
            top_k = max(1, min(10, int(args[0])))
        except ValueError:
            _safe_send(chat_id, "❌ /scan 参数应是整数 1-10")
            _audit_slash("/scan", uid, error="bad_arg")
            return
    _safe_send(chat_id, f"🔎 已开始扫描热点… top-{top_k}")
    try:
        _spawn_hotspots(top_k=top_k)
    except Exception as err:  # pragma: no cover — defensive
        _log.warning("/scan _spawn_hotspots failed: %s", err)
        _safe_send(chat_id, f"❌ scan 启动失败: {err}"[:400])
    _audit_slash("/scan", uid, top_k=top_k)


# ---- Profile --------------------------------------------------------------


def _active_profile_id() -> str | None:
    """Resolve currently active profile id (for /profile)."""
    try:
        from agentflow.cli.topic_profile_commands import _read_active_profile_id
        return _read_active_profile_id()
    except Exception:
        return None


def _handle_profile(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    pid = (args[0] if args else None) or _active_profile_id()
    if not pid:
        _safe_send(
            chat_id, "👤 当前没有活跃 profile。用 /profile-switch <id> 切换。",
        )
        _audit_slash("/profile", uid, status="no_active")
        return
    try:
        from agentflow.cli.topic_profile_commands import topic_profile_show
    except Exception as err:
        _safe_send(chat_id, f"❌ profile 命令不可用: {err}"[:400])
        _audit_slash("/profile", uid, error="import_failed")
        return
    out, err = _capture_callback(
        topic_profile_show.callback, profile_id=pid, as_json=False,
    )
    body = out.strip() or "(empty)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"👤 profile {pid}:\n{body}"))
    _audit_slash("/profile", uid, profile_id=pid)


def _handle_profiles(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    try:
        from agentflow.cli.topic_profile_commands import topic_profile_list
    except Exception as err:
        _safe_send(chat_id, f"❌ profiles 命令不可用: {err}"[:400])
        _audit_slash("/profiles", uid, error="import_failed")
        return
    out, err = _capture_callback(topic_profile_list.callback, as_json=False)
    body = out.strip() or "(no profiles)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"👤 *profiles*\n{body}"))
    _audit_slash("/profiles", uid)


def _handle_profile_init(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if not args:
        _safe_send(chat_id, "用法: /profile-init <id>")
        _audit_slash("/profile-init", uid, error="missing_id")
        return
    pid = args[0].strip()
    if _profile_session_active(uid):
        _safe_send(chat_id, "⚙️ 已有进行中的 profile 会话；继续按提示回复即可。")
        _audit_slash("/profile-init", uid, status="already_active")
        return
    _safe_send(chat_id, f"👤 创建 profile {pid}：将依次问 brand / voice / sources / rules。")
    _start_profile_setup_session(chat_id, uid, profile_id=pid)
    _audit_slash("/profile-init", uid, profile_id=pid)


def _handle_profile_update(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if len(args) < 2:
        _safe_send(
            chat_id,
            "用法: /profile-update <field> <value>\n"
            "对多行字段（如 rules），用 /profile-init 重新走 wizard。",
        )
        _audit_slash("/profile-update", uid, error="missing_args")
        return
    field = args[0].strip()
    value = " ".join(args[1:]).strip()
    pid = _active_profile_id()
    if not pid:
        _safe_send(chat_id, "❌ 当前没有活跃 profile，先 /profile-switch <id>。")
        _audit_slash("/profile-update", uid, error="no_active")
        return
    if field.lower() == "rules":
        # Multi-line rules — ask the user to reply with the full block.
        try:
            pending_edits.register(
                uid=int(uid),
                article_id=f"profile_update::{pid}::rules",
                gate="PU",
                short_id=f"slash:{uid}",
                ttl_minutes=15,
            )
        except Exception:
            pass
        _safe_send(
            chat_id,
            "✏️ 请用一条消息回复完整的 rules（多行）。15 分钟内有效。",
        )
        _audit_slash("/profile-update", uid, profile_id=pid, mode="multi_turn_rules")
        return
    # Single-shot field update via upsert_profile.
    try:
        from agentflow.shared.topic_profile_lifecycle import upsert_profile
        patch: dict[str, Any] = {field: value}
        if field in {"brand", "voice", "pronoun", "output_language"}:
            patch = {"publisher_account": {field: value}}
        elif field in {"do", "dont", "product_facts", "perspectives"}:
            patch = {"publisher_account": {field: [v for v in value.split(",") if v]}}
        upsert_profile(
            pid, patch, replace_lists=False,
            source="tg_slash_profile_update",
        )
    except Exception as err:
        _safe_send(chat_id, f"❌ update 失败: {err}"[:400])
        _audit_slash("/profile-update", uid, profile_id=pid, error=str(err)[:120])
        return
    append_memory_event(
        "topic_profile_updated",
        payload={"profile_id": pid, "mode": "tg_slash", "field": field},
    )
    _safe_send(chat_id, f"✅ profile {pid} 字段 {field} 已更新")
    _audit_slash("/profile-update", uid, profile_id=pid, field=field)


def _handle_profile_switch(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if not args:
        _safe_send(chat_id, "用法: /profile-switch <id>")
        _audit_slash("/profile-switch", uid, error="missing_id")
        return
    pid = args[0].strip()
    try:
        from agentflow.cli.topic_profile_commands import topic_profile_set_active
    except Exception as err:
        _safe_send(chat_id, f"❌ set-active 不可用: {err}"[:400])
        _audit_slash("/profile-switch", uid, error="import_failed")
        return
    out, err = _capture_callback(
        topic_profile_set_active.callback, profile_id=pid, as_json=False,
    )
    if err:
        _safe_send(chat_id, f"❌ {err}"[:400])
        _audit_slash("/profile-switch", uid, profile_id=pid, error=err[:120])
        return
    _safe_send(chat_id, _trim_for_tg(f"👤 {out.strip()}"))
    _audit_slash("/profile-switch", uid, profile_id=pid)


# ---- Sources --------------------------------------------------------------


def _mutate_keyword_terms(pid: str, keyword: str, *, add: bool) -> tuple[bool, str]:
    """Append/remove ``keyword`` from the active profile's
    ``keyword_groups.core`` list. Returns ``(changed, message)``."""
    from agentflow.shared.topic_profile_lifecycle import (
        load_user_topic_profiles,
        upsert_profile,
    )

    keyword = (keyword or "").strip()
    if not keyword:
        return False, "missing keyword"
    data = load_user_topic_profiles() or {}
    profiles = data.get("profiles") if isinstance(data, dict) else {}
    if not isinstance(profiles, dict):
        profiles = {}
    profile = profiles.get(pid) or {}
    groups = profile.get("keyword_groups") or {}
    core_terms = list(groups.get("core") or []) if isinstance(groups, dict) else []
    if add:
        if keyword in core_terms:
            return False, f"keyword '{keyword}' already present"
        core_terms.append(keyword)
    else:
        if keyword not in core_terms:
            return False, f"keyword '{keyword}' not present"
        core_terms = [t for t in core_terms if t != keyword]
    upsert_profile(
        pid,
        {"keyword_groups": {"core": core_terms}},
        replace_lists=True,
        source=f"tg_slash_keyword_{'add' if add else 'rm'}",
    )
    return True, "ok"


def _handle_keyword_add(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if not args:
        _safe_send(chat_id, "用法: /keyword-add <keyword>")
        _audit_slash("/keyword-add", uid, error="missing_arg")
        return
    pid = _active_profile_id()
    if not pid:
        _safe_send(chat_id, "❌ 当前没有活跃 profile，先 /profile-switch <id>。")
        _audit_slash("/keyword-add", uid, error="no_active")
        return
    keyword = " ".join(args).strip()
    changed, msg = _mutate_keyword_terms(pid, keyword, add=True)
    if not changed:
        _safe_send(chat_id, f"ℹ️ {msg}")
        _audit_slash("/keyword-add", uid, profile_id=pid, status=msg)
        return
    append_memory_event(
        "topic_profile_updated",
        payload={"profile_id": pid, "mode": "keyword_add", "keyword": keyword},
    )
    _safe_send(chat_id, f"🌐 已加入 keyword_groups.core: {keyword}")
    _audit_slash("/keyword-add", uid, profile_id=pid, keyword=keyword)


def _handle_keyword_rm(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if not args:
        _safe_send(chat_id, "用法: /keyword-rm <keyword>")
        _audit_slash("/keyword-rm", uid, error="missing_arg")
        return
    pid = _active_profile_id()
    if not pid:
        _safe_send(chat_id, "❌ 当前没有活跃 profile，先 /profile-switch <id>。")
        _audit_slash("/keyword-rm", uid, error="no_active")
        return
    keyword = " ".join(args).strip()
    changed, msg = _mutate_keyword_terms(pid, keyword, add=False)
    if not changed:
        _safe_send(chat_id, f"ℹ️ {msg}")
        _audit_slash("/keyword-rm", uid, profile_id=pid, status=msg)
        return
    append_memory_event(
        "topic_profile_updated",
        payload={"profile_id": pid, "mode": "keyword_rm", "keyword": keyword},
    )
    _safe_send(chat_id, f"🌐 已移除 keyword_groups.core: {keyword}")
    _audit_slash("/keyword-rm", uid, profile_id=pid, keyword=keyword)


# ---- Style ----------------------------------------------------------------


def _handle_style(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    try:
        from agentflow.cli.commands import learn_style
    except Exception as err:
        _safe_send(chat_id, f"❌ learn-style 不可用: {err}"[:400])
        _audit_slash("/style", uid, error="import_failed")
        return
    out, err = _capture_callback(
        learn_style.callback,
        dir_=None, file_=tuple(), url=tuple(),
        from_published=False, show=True, recompute=False,
    )
    body = out.strip() or "(no style profile yet — run /style-learn)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"🗣️ style:\n{body}"))
    _audit_slash("/style", uid)


def _handle_style_learn(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if not args:
        # Multi-turn — ask for handle/dir.
        try:
            pending_edits.register(
                uid=int(uid),
                article_id="style_learn::pending",
                gate="SL",
                short_id=f"slash:{uid}",
                ttl_minutes=15,
            )
        except Exception:
            pass
        _safe_send(
            chat_id,
            "🗣️ 回复 handle (例 @medium_author) 或样本目录绝对路径。15min 内有效。",
        )
        _audit_slash("/style-learn", uid, mode="multi_turn")
        return
    arg = args[0].strip()
    if arg.startswith("@") or arg.startswith("http://") or arg.startswith("https://"):
        try:
            from agentflow.cli.commands import learn_from_handle
        except Exception as err:
            _safe_send(chat_id, f"❌ learn-from-handle 不可用: {err}"[:400])
            _audit_slash("/style-learn", uid, error="import_failed")
            return
        out, err = _capture_callback(
            learn_from_handle.callback,
            handle_or_url=arg, max_samples=5, ask_extras=False,
            profile_id=None, dry_run=False, refresh=False, as_json=False,
        )
        body = out.strip() or "(no output)"
        if err:
            body = body + f"\n\n[err] {err}"
        _safe_send(chat_id, _trim_for_tg(f"🗣️ learn-from-handle:\n{body}"))
        _audit_slash("/style-learn", uid, source="handle", arg=arg)
        return
    # Treat as directory path.
    try:
        from agentflow.cli.commands import learn_style
    except Exception as err:
        _safe_send(chat_id, f"❌ learn-style 不可用: {err}"[:400])
        _audit_slash("/style-learn", uid, error="import_failed")
        return
    out, err = _capture_callback(
        learn_style.callback,
        dir_=arg, file_=tuple(), url=tuple(),
        from_published=False, show=False, recompute=False,
    )
    body = out.strip() or "(done)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"🗣️ learn-style --dir {arg}:\n{body}"))
    _audit_slash("/style-learn", uid, source="dir", arg=arg)


# ---- Intent ---------------------------------------------------------------


def _handle_intent(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    try:
        from agentflow.cli.commands import intent_show
    except Exception as err:
        _safe_send(chat_id, f"❌ intent 命令不可用: {err}"[:400])
        _audit_slash("/intent", uid, error="import_failed")
        return
    out, err = _capture_callback(intent_show.callback, as_json=False)
    body = out.strip() or "(no intent set)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"🎯 intent:\n{body}"))
    _audit_slash("/intent", uid)


def _handle_intent_set(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if not args:
        _safe_send(chat_id, "用法: /intent-set <topic>")
        _audit_slash("/intent-set", uid, error="missing_arg")
        return
    topic = " ".join(args).strip()
    try:
        from agentflow.cli.commands import intent_set
    except Exception as err:
        _safe_send(chat_id, f"❌ intent-set 不可用: {err}"[:400])
        _audit_slash("/intent-set", uid, error="import_failed")
        return
    out, err = _capture_callback(
        intent_set.callback,
        query=topic, topic_profile_id=None,
        ttl="session", mode="keyword", as_json=False,
    )
    if err:
        _safe_send(chat_id, f"❌ {err}"[:400])
        _audit_slash("/intent-set", uid, error=err[:120])
        return
    _safe_send(chat_id, _trim_for_tg(f"🎯 {out.strip() or 'intent set'}"))
    _audit_slash("/intent-set", uid, topic=topic)


def _handle_intent_clear(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    try:
        from agentflow.cli.commands import intent_clear
    except Exception as err:
        _safe_send(chat_id, f"❌ intent-clear 不可用: {err}"[:400])
        _audit_slash("/intent-clear", uid, error="import_failed")
        return
    out, err = _capture_callback(intent_clear.callback, as_json=False)
    body = out.strip() or "(cleared)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"🎯 {body}"))
    _audit_slash("/intent-clear", uid)


# ---- Prefs ----------------------------------------------------------------


def _handle_prefs(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    try:
        from agentflow.shared import preferences as _prefs
    except Exception as err:
        _safe_send(chat_id, f"❌ prefs 不可用: {err}"[:400])
        _audit_slash("/prefs", uid, error="import_failed")
        return
    prefs = _prefs.load() or {}
    summary = _prefs.summarize(prefs) if prefs else {}
    sections = summary.get("sections") or {}
    flat: list[tuple[str, Any, int]] = []
    for section, body in sections.items():
        if not isinstance(body, dict):
            continue
        for k, v in body.items():
            evidence = prefs.get(section, {}).get(f"_evidence_{k}") if isinstance(prefs.get(section), dict) else None
            ev_count = len(evidence) if isinstance(evidence, list) else 0
            flat.append((f"{section}.{k}", v, ev_count))
    flat.sort(key=lambda r: -r[2])
    if not flat:
        _safe_send(
            chat_id,
            "🧠 prefs 暂无聚合数据 (run /prefs-rebuild)",
        )
        _audit_slash("/prefs", uid, status="empty")
        return
    lines = ["🧠 *Top 5 prefs*"]
    for key, value, ev in flat[:5]:
        lines.append(f"• {key}: {value!r} (evidence={ev})")
    _safe_send(chat_id, _trim_for_tg("\n".join(lines)))
    _audit_slash("/prefs", uid)


def _handle_prefs_rebuild(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    try:
        from agentflow.cli.prefs_commands import prefs_rebuild
    except Exception as err:
        _safe_send(chat_id, f"❌ prefs-rebuild 不可用: {err}"[:400])
        _audit_slash("/prefs-rebuild", uid, error="import_failed")
        return
    out, err = _capture_callback(prefs_rebuild.callback, dry_run=False, as_json=False)
    body = out.strip() or "(done)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"🧠 prefs-rebuild:\n{body}"))
    _audit_slash("/prefs-rebuild", uid)


def _handle_prefs_explain(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if not args:
        _safe_send(chat_id, "用法: /prefs-explain <key>")
        _audit_slash("/prefs-explain", uid, error="missing_arg")
        return
    key = args[0].strip()
    try:
        from agentflow.cli.prefs_commands import prefs_explain
    except Exception as err:
        _safe_send(chat_id, f"❌ prefs-explain 不可用: {err}"[:400])
        _audit_slash("/prefs-explain", uid, error="import_failed")
        return
    out, err = _capture_callback(prefs_explain.callback, key=key, as_json=False)
    body = out.strip() or "(no evidence)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"🧠 prefs-explain {key}:\n{body}"))
    _audit_slash("/prefs-explain", uid, key=key)


# 2-step confirm store for /prefs-reset and /restart-daemon. Lives in
# pending_edits with a synthetic gate so the existing reply intercept catches
# the "yes/no" follow-up.
def _await_confirm(uid: int | None, *, gate: str, target: str) -> None:
    if uid is None:
        return
    try:
        pending_edits.register(
            uid=int(uid),
            article_id=target,
            gate=gate,
            short_id=f"confirm:{uid}",
            ttl_minutes=2,
        )
    except Exception:
        pass


def _handle_prefs_reset(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    if args:
        # Single-key reset: no confirm needed.
        key = args[0].strip()
        try:
            from agentflow.cli.prefs_commands import prefs_reset
        except Exception as err:
            _safe_send(chat_id, f"❌ prefs-reset 不可用: {err}"[:400])
            _audit_slash("/prefs-reset", uid, error="import_failed")
            return
        out, err = _capture_callback(prefs_reset.callback, key=key, as_json=False)
        body = out.strip() or "(done)"
        if err:
            body = body + f"\n\n[err] {err}"
        _safe_send(chat_id, _trim_for_tg(f"🧠 prefs-reset {key}:\n{body}"))
        _audit_slash("/prefs-reset", uid, key=key)
        return
    # Whole-file reset — 2-step confirm.
    _await_confirm(uid, gate="PREFS_RESET", target="prefs::all")
    _safe_send(chat_id, "⚠️ 确认重置全部 prefs？回 yes/no（2min 有效）")
    _audit_slash("/prefs-reset", uid, status="awaiting_confirm")


# ---- Review-ops -----------------------------------------------------------


def _handle_report(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    try:
        from agentflow.cli.report_commands import report
    except Exception as err:
        _safe_send(chat_id, f"❌ report 不可用: {err}"[:400])
        _audit_slash("/report", uid, error="import_failed")
        return
    out, err = _capture_callback(report.callback, window="7d", as_json=False)
    body = out.strip() or "(empty report)"
    if err:
        body = body + f"\n\n[err] {err}"
    _safe_send(chat_id, _trim_for_tg(f"📊 report (7d):\n{body}"))
    _audit_slash("/report", uid)


# ---- System ---------------------------------------------------------------


def _handle_restart_daemon(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    _await_confirm(uid, gate="RESTART", target="daemon::restart")
    _safe_send(chat_id, "⚙️ 确认重启 review-daemon？回 yes/no（2min 有效）")
    _audit_slash("/restart-daemon", uid, status="awaiting_confirm")


def _trigger_daemon_restart() -> None:
    """Send SIGTERM to ourselves; systemd's Restart=on-failure brings us back."""
    os.kill(os.getpid(), signal.SIGTERM)


def _handle_pending_confirm_reply(
    *, chat_id: int | None, uid: int | None, pending: dict[str, Any], text: str,
) -> bool:
    """Handle replies to 2-step confirm prompts. Returns True if consumed."""
    gate = str(pending.get("gate") or "")
    if gate not in {"PREFS_RESET", "RESTART"}:
        return False
    answer = (text or "").strip().lower()
    if answer not in {"yes", "y", "no", "n"}:
        _safe_send(chat_id, "回 yes 或 no（2min 内有效）")
        return True
    if answer in {"no", "n"}:
        _safe_send(chat_id, "已取消。")
        _audit_slash(f"/{gate.lower()}_confirm", uid, decision="no")
        return True
    if gate == "PREFS_RESET":
        try:
            from agentflow.cli.prefs_commands import prefs_reset
            out, err = _capture_callback(prefs_reset.callback, key=None, as_json=False)
            body = out.strip() or "(done)"
            if err:
                body = body + f"\n\n[err] {err}"
            _safe_send(chat_id, _trim_for_tg(f"🧠 prefs-reset all:\n{body}"))
        except Exception as exc:
            _safe_send(chat_id, f"❌ prefs-reset 失败: {exc}"[:400])
        _audit_slash("/prefs-reset_confirm", uid, decision="yes")
        return True
    if gate == "RESTART":
        _safe_send(chat_id, "⚙️ 正在重启 (SIGTERM)…")
        _audit_slash("/restart-daemon_confirm", uid, decision="yes")
        try:
            _trigger_daemon_restart()
        except Exception as exc:  # pragma: no cover — defensive
            _safe_send(chat_id, f"❌ restart 失败: {exc}"[:400])
        return True
    return False


# ---- Wrappers around v1.0.3 inline handlers (used purely for registry
# completeness so /help and /auth-debug list them too). The actual dispatch
# of these commands stays in ``_handle_message`` for backward compatibility.


def _handle_v103_passthrough(
    chat_id: int | None, uid: int | None, args: list[str], raw_text: str,
) -> None:
    """No-op shim. The v1.0.3 inline branch in ``_handle_message`` runs first
    for canonical hyphen forms and registry forms; this passthrough only fires
    when the user uses an alias the inline branch doesn't recognize, in which
    case we re-emit the canonical form and re-enter the dispatcher."""
    canonical = raw_text.lstrip("/").split(maxsplit=1)[0]
    canonical_norm = _norm_command_name(canonical)
    rebuilt = "/" + canonical_norm
    if args:
        rebuilt += " " + " ".join(args)
    _handle_message_v103_inline(chat_id, uid, rebuilt)


def _handle_message_v103_inline(
    chat_id: int | None, uid: int | None, text: str,
) -> None:
    """Re-enter ``_handle_message`` with a synthetic update so the v1.0.3
    inline branches handle the canonical form. Used by alias passthrough."""
    if chat_id is None or uid is None:
        return
    update = {
        "message": {
            "text": text,
            "chat": {"id": int(chat_id)},
            "from": {"id": int(uid)},
        }
    }
    try:
        _handle_message(update)
    except Exception as err:  # pragma: no cover — defensive
        _log.warning("v103 alias re-entry failed: %s", err)


# ---------------------------------------------------------------------------
# Registry population
# ---------------------------------------------------------------------------


def _populate_command_registry() -> None:
    if _COMMAND_REGISTRY:
        return

    # Bootstrap.
    _register_command(
        "onboard", group="Bootstrap", auth_bucket="system",
        summary="Start guided setup",
        handler=_handle_onboard, multi_turn=True,
    )
    _register_command(
        "doctor", aliases=["preflight"], group="Bootstrap", auth_bucket="review",
        summary="Show readiness matrix",
        handler=_handle_doctor,
    )
    _register_command(
        "scan", group="Bootstrap", auth_bucket="system",
        summary="Trigger hotspots scan",
        handler=_handle_scan,
    )

    # Profile.
    _register_command(
        "profile", group="Profile", auth_bucket="review",
        summary="Show active profile",
        handler=_handle_profile,
    )
    _register_command(
        "profiles", group="Profile", auth_bucket="review",
        summary="List all profiles",
        handler=_handle_profiles,
    )
    _register_command(
        "profile-init", aliases=["profile_init"], group="Profile",
        auth_bucket="system",
        summary="Multi-turn new profile creation",
        handler=_handle_profile_init, multi_turn=True,
    )
    _register_command(
        "profile-update", aliases=["profile_update"], group="Profile",
        auth_bucket="system",
        summary="One-shot field update",
        handler=_handle_profile_update,
    )
    _register_command(
        "profile-switch", aliases=["profile_switch"], group="Profile",
        auth_bucket="system",
        summary="Switch active profile",
        handler=_handle_profile_switch,
    )

    # Sources.
    _register_command(
        "keyword-add", aliases=["keyword_add"], group="Sources",
        auth_bucket="system",
        summary="Append term to keyword_groups.core",
        handler=_handle_keyword_add,
    )
    _register_command(
        "keyword-rm", aliases=["keyword_rm"], group="Sources",
        auth_bucket="system",
        summary="Remove term from keyword_groups.core",
        handler=_handle_keyword_rm,
    )

    # Style.
    _register_command(
        "style", group="Style", auth_bucket="review",
        summary="Show style profile",
        handler=_handle_style,
    )
    _register_command(
        "style-learn", aliases=["style_learn"], group="Style",
        auth_bucket="system",
        summary="Learn style from handle / dir",
        handler=_handle_style_learn, multi_turn=True,
    )

    # Intent.
    _register_command(
        "intent", group="Intent", auth_bucket="review",
        summary="Show current intent",
        handler=_handle_intent,
    )
    _register_command(
        "intent-set", aliases=["intent_set"], group="Intent",
        auth_bucket="system",
        summary="Set current intent",
        handler=_handle_intent_set,
    )
    _register_command(
        "intent-clear", aliases=["intent_clear"], group="Intent",
        auth_bucket="system",
        summary="Clear current intent",
        handler=_handle_intent_clear,
    )

    # Prefs.
    _register_command(
        "prefs", group="Prefs", auth_bucket="review",
        summary="Top-5 prefs + evidence count",
        handler=_handle_prefs,
    )
    _register_command(
        "prefs-rebuild", aliases=["prefs_rebuild"], group="Prefs",
        auth_bucket="system",
        summary="Rebuild prefs from events",
        handler=_handle_prefs_rebuild,
    )
    _register_command(
        "prefs-explain", aliases=["prefs_explain"], group="Prefs",
        auth_bucket="review",
        summary="Show evidence for a prefs key",
        handler=_handle_prefs_explain,
    )
    _register_command(
        "prefs-reset", aliases=["prefs_reset"], group="Prefs",
        auth_bucket="system",
        summary="Reset prefs (key / all w/ confirm)",
        handler=_handle_prefs_reset,
    )

    # Review-ops (v1.0.3 + report).
    _register_command(
        "report", group="Review-ops", auth_bucket="review",
        summary="Daily/weekly report (window=7d)",
        handler=_handle_report,
    )
    _register_command(
        "status", group="Review-ops", auth_bucket="review",
        summary="List *_pending_review articles",
        handler=_handle_v103_passthrough,
    )
    _register_command(
        "queue", group="Review-ops", auth_bucket="review",
        summary="Top-5 oldest pending",
        handler=_handle_v103_passthrough,
    )
    _register_command(
        "help", group="Review-ops", auth_bucket="review",
        summary="This help card",
        handler=_handle_v103_passthrough,
    )
    _register_command(
        "skip", group="Review-ops", auth_bucket="review",
        summary="Skip image-gate",
        handler=_handle_v103_passthrough,
    )
    _register_command(
        "defer", group="Review-ops", auth_bucket="review",
        summary="Defer Gate B/C/D",
        handler=_handle_v103_passthrough,
    )
    _register_command(
        "publish-mark", aliases=["publish_mark"], group="Review-ops",
        auth_bucket="publish",
        summary="Mark article as published",
        handler=_handle_v103_passthrough,
    )
    _register_command(
        "audit", group="Review-ops", auth_bucket="review",
        summary="Last 20 callback/slash events",
        handler=_handle_v103_passthrough,
    )
    _register_command(
        "auth-debug", aliases=["auth_debug"], group="Review-ops",
        auth_bucket="review",
        summary="Per-action authorization debug",
        handler=_handle_v103_passthrough,
    )

    # System.
    _register_command(
        "restart-daemon", aliases=["restart_daemon"], group="System",
        auth_bucket="system",
        summary="Restart daemon (SIGTERM, systemd revives)",
        handler=_handle_restart_daemon,
    )


_populate_command_registry()


def _build_set_my_commands_payload() -> list[dict[str, str]]:
    """Curated subset shown in Telegram's global / menu. Hyphens are
    rejected by setMyCommands so we use the underscored aliases when needed.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for name in _V104_MENU_NAMES:
        meta = _COMMAND_REGISTRY.get(name) or _COMMAND_REGISTRY.get(
            name.replace("_", "-")
        )
        if meta is None:
            continue
        cmd_name = name if "-" not in name else name.replace("-", "_")
        if cmd_name in seen:
            continue
        seen.add(cmd_name)
        out.append({"command": cmd_name, "description": meta["summary"][:100]})
    return out


def _dispatch_v104_command(
    chat_id: int | None, uid: int | None, text: str,
) -> bool:
    """Resolve and dispatch a registry command. Returns True if consumed.

    Auth-gate via the registry's ``auth`` bucket. Calls the handler with
    ``(chat_id, uid, args, raw_text)``.
    """
    resolved = _resolve_command(text)
    if resolved is None:
        return False
    canonical, args = resolved
    meta = _COMMAND_REGISTRY[canonical]
    bucket = str(meta.get("auth") or "review")
    if not auth.is_authorized(uid, action=bucket):
        _safe_send(
            chat_id,
            f"❌ /{canonical} 需要 `{bucket}` 授权 (uid={uid})",
        )
        _audit_slash(
            f"/{canonical}", uid,
            error="not_authorized", bucket=bucket,
        )
        return True
    handler = meta["handler"]
    try:
        handler(chat_id, uid, args, text)
    except Exception as err:  # pragma: no cover — defensive
        _log.warning("v104 handler %s crashed: %s", canonical, err)
        _safe_send(chat_id, f"❌ /{canonical} 失败: {err}"[:400])
    return True


# ---------------------------------------------------------------------------
# Update routing
# ---------------------------------------------------------------------------


def _handle_message(update: dict[str, Any]) -> None:
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    sender = msg.get("from") or {}
    uid = sender.get("id")
    if text == "/start":
        # First /start ever — capture as operator (chat_id == uid in DM).
        if get_review_chat_id() is None and chat_id is not None:
            set_review_chat_id(int(chat_id))
            configure_bot_menu(int(chat_id))
            tg_client.send_message(
                chat_id,
                "✅ chat\\_id 已记录\\. 此后 Gate A/B/C 通知会发到这个会话\\.\n"
                "也可以把这个 id 写到 \\.env 的 `TELEGRAM\\_REVIEW\\_CHAT\\_ID` 持久化\\.",
                parse_mode="MarkdownV2",
            )
            _log.info("captured review chat_id: %s", chat_id)
            _audit({"kind": "start", "chat_id": chat_id, "uid": uid, "captured_operator": True})
            return
        # Subsequent /start — gate by uid auth.
        if not auth.is_authorized(uid):
            tg_client.send_message(
                chat_id,
                f"❌ 未授权\\. 你的 uid 是 `{uid}`\\.\n"
                f"请操作员在终端运行: `af review-auth-add {uid}`",
                parse_mode="MarkdownV2",
            )
            _log.warning("unauthorized /start from uid=%s chat_id=%s", uid, chat_id)
            _audit({"kind": "start_unauthorized", "uid": uid, "chat_id": chat_id})
            return
        tg_client.send_message(chat_id, "review bot 在线\\.", parse_mode="MarkdownV2")
        _audit({"kind": "start", "chat_id": chat_id, "uid": uid})
        return

    # Non-/start text: check if this uid is in the middle of an ✏️ edit flow.
    if not auth.is_authorized(uid):
        # Silent — anonymous chitchat is ignored, /start is the only allowed
        # path for unauthorized users (handled above).
        return

    # Slash command dispatch — must run BEFORE pending_edits.take() so a user
    # who fires `/list` while an edit-reply is queued doesn't have it eaten.
    if text.startswith("/help"):
        try:
            help_text = _build_help_text()
            tg_client.send_message(chat_id, help_text, parse_mode="MarkdownV2")
        except Exception:
            pass
        _audit({"kind": "slash_command", "cmd": "/help", "uid": uid})
        return

    if text.startswith("/list"):
        list_filter = "all"
        total = 0
        try:
            all_states = [
                state.STATE_DRAFT_PENDING_REVIEW,
                state.STATE_IMAGE_PENDING_REVIEW,
                state.STATE_CHANNEL_PENDING_REVIEW,
                state.STATE_READY_TO_PUBLISH,
            ]
            states_by_filter = {
                "all": all_states,
                "b": [state.STATE_DRAFT_PENDING_REVIEW],
                "c": [state.STATE_IMAGE_PENDING_REVIEW],
                "d": [state.STATE_CHANNEL_PENDING_REVIEW],
                "ready": [state.STATE_READY_TO_PUBLISH],
                "publish": [state.STATE_READY_TO_PUBLISH],
            }
            parts = text.split(maxsplit=1)
            list_filter = parts[1].strip().lower() if len(parts) > 1 else "all"
            pending_states = states_by_filter.get(list_filter)
            if pending_states is None:
                help_text = (
                    "用法: /list [all|B|C|D|ready|publish]\n"
                    "Examples: /list, /list B, /list ready"
                )
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(help_text),
                    parse_mode="MarkdownV2",
                )
                _audit({
                    "kind": "slash_command",
                    "cmd": "/list",
                    "uid": uid,
                    "filter": list_filter,
                    "total": total,
                })
                return

            articles = state.articles_in_state(pending_states) or []
            # Group by current state.
            buckets: dict[str, list[str]] = {st: [] for st in all_states}
            for aid in articles:
                try:
                    cur = state.current_state(aid)
                except Exception:
                    cur = None
                if cur in buckets:
                    buckets[cur].append(aid)

            def _title_for(aid: str) -> str:
                try:
                    data = json.loads(
                        (agentflow_home() / "drafts" / aid / "metadata.json")
                        .read_text(encoding="utf-8")
                    ) or {}
                    return str(data.get("title") or "(no title)")
                except Exception:
                    return "(no title)"

            label_for = {
                state.STATE_DRAFT_PENDING_REVIEW: "B",
                state.STATE_IMAGE_PENDING_REVIEW: "C",
                state.STATE_CHANNEL_PENDING_REVIEW: "D",
                state.STATE_READY_TO_PUBLISH: "Ready",
            }
            lines: list[str] = []
            max_rows = 20
            for st in all_states:
                aids = buckets.get(st, [])
                if not aids:
                    continue
                gate_label = label_for.get(st, "?")
                for aid in sorted(aids):
                    total += 1
                    if len(lines) >= max_rows:
                        continue
                    aid_short = aid[:8] if len(aid) > 8 else aid
                    title = _title_for(aid)
                    age_str = "(?h)"
                    try:
                        history = state.gate_history(aid)
                        if history:
                            last_ts = datetime.fromisoformat(
                                str(history[-1].get("timestamp") or "").replace("Z", "+00:00")
                            )
                            if last_ts.tzinfo is None:
                                last_ts = last_ts.replace(tzinfo=timezone.utc)
                            hrs = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
                            if hrs >= 24:
                                age_str = f"({int(hrs / 24)}d+)"
                            elif hrs >= 1:
                                age_str = f"({int(hrs)}h+)"
                            else:
                                age_str = f"({int(hrs * 60)}m+)"
                    except Exception:
                        pass
                    line = (
                        f"• {_render.escape_md2(gate_label)}: "
                        f"`{_render.escape_md2(aid_short)}` — "
                        f"{_render.escape_md2(title)} "
                        f"{_render.escape_md2(age_str)}"
                    )
                    lines.append(line)
            if total == 0:
                body = "✨ no pending cards"
            else:
                remaining = total - len(lines)
                if remaining > 0:
                    lines.append(
                        _render.escape_md2(
                            f"还有 {remaining} 条，使用 /list <gate> 过滤"
                        )
                    )
                body = "\n".join(lines)
            tg_client.send_message(chat_id, body, parse_mode="MarkdownV2")
        except Exception:
            pass
        _audit({
            "kind": "slash_command",
            "cmd": "/list",
            "uid": uid,
            "filter": list_filter,
            "total": total,
        })
        return

    if text.startswith("/published"):
        parts = text.split(maxsplit=1)
        days = 7
        if len(parts) > 1:
            try:
                days = max(1, min(90, int(parts[1].strip())))
            except ValueError:
                try:
                    tg_client.send_message(
                        chat_id,
                        _render.escape_md2(
                            "❌ /published 参数应是天数 (1-90), 如 /published 14"
                        ),
                        parse_mode="MarkdownV2",
                    )
                except Exception:
                    pass
                _audit({
                    "kind": "slash_command",
                    "cmd": "/published",
                    "uid": uid,
                    "error": "bad_arg",
                })
                return

        items: list[dict[str, Any]] = []
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            published_articles = state.articles_in_state([state.STATE_PUBLISHED]) or []

            for aid in published_articles:
                try:
                    meta = json.loads(
                        (agentflow_home() / "drafts" / aid / "metadata.json")
                        .read_text(encoding="utf-8")
                    ) or {}
                    published_at_str = meta.get("published_at")
                    if not published_at_str:
                        continue
                    pub_dt = datetime.fromisoformat(
                        str(published_at_str).replace("Z", "+00:00")
                    )
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue

                    title = meta.get("title", "(no title)")
                    pub_urls = meta.get("published_url", {})
                    if isinstance(pub_urls, str):  # legacy fallback
                        pub_urls = {"medium": pub_urls}
                    if not isinstance(pub_urls, dict):
                        pub_urls = {}
                    platforms = list(meta.get("published_platforms") or pub_urls.keys())

                    age_d = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 86400

                    items.append({
                        "aid": aid,
                        "title": title,
                        "age_d": age_d,
                        "platforms": platforms,
                        "urls": pub_urls,
                    })
                except Exception:
                    continue

            # sort by recency (smallest age first)
            items.sort(key=lambda x: x["age_d"])

            cap = 20
            truncated = len(items) > cap
            items = items[:cap]

            if not items:
                body = f"📭 最近 {days}d 无 published 文章"
            else:
                header = (
                    f"📌 *Published* — 最近 {days}d  ·  {len(items)} 篇"
                    + (" (前 20)" if truncated else "")
                )
                lines = [header]
                for it in items:
                    aid_short = it["aid"][:8] if len(it["aid"]) > 8 else it["aid"]
                    title = it["title"]
                    age_d_val = it["age_d"]
                    age = (
                        f"{int(age_d_val)}d"
                        if age_d_val >= 1
                        else f"{int(age_d_val * 24)}h"
                    )
                    plats = it["platforms"] or []
                    plat_csv = ",".join(plats[:3]) + ("…" if len(plats) > 3 else "")
                    lines.append(
                        f"• `{_render.escape_md2(aid_short)}` — "
                        f"{_render.escape_md2(title)} "
                        f"{_render.escape_md2(f'({age} on {plat_csv})')}"
                    )
                body = "\n".join(lines)

            try:
                tg_client.send_message(chat_id, body, parse_mode="MarkdownV2")
            except Exception:
                pass
        except Exception as err:
            try:
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(f"❌ /published 失败: {err}"[:500]),
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass

        _audit({
            "kind": "slash_command",
            "cmd": "/published",
            "uid": uid,
            "days": days,
            "total": len(items),
        })
        return

    if text.startswith("/scan"):
        parts = text.split(maxsplit=1)
        top_k = 3
        if len(parts) > 1:
            try:
                top_k = max(1, min(10, int(parts[1].strip())))
            except ValueError:
                try:
                    tg_client.send_message(
                        chat_id,
                        _render.escape_md2(
                            "❌ /scan 参数应是 top-k (1-10), 如 /scan 5"
                        ),
                        parse_mode="MarkdownV2",
                    )
                except Exception:
                    pass
                _audit({
                    "kind": "slash_command",
                    "cmd": "/scan",
                    "uid": uid,
                    "error": "bad_arg",
                })
                return

        try:
            tg_client.send_message(
                chat_id,
                _render.escape_md2(
                    f"⏳ 主动扫描 hotspots 中... 预计 ~60-90s, top-{top_k}"
                ),
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass
        try:
            _spawn_hotspots(top_k=top_k)
        except Exception as err:  # pragma: no cover — defensive
            _log.warning("_spawn_hotspots failed to start: %s", err)
            try:
                _notify_spawn_failure("hotspots", "manual_scan", None, str(err))
            except Exception:
                pass
        _audit({
            "kind": "slash_command",
            "cmd": "/scan",
            "uid": uid,
            "top_k": top_k,
        })
        return

    if text.startswith("/jobs"):
        try:
            from agentflow.agent_review.triggers import _af_argv, _run_subprocess
            res = _run_subprocess(
                _af_argv("review-cron-status"),
                env=os.environ.copy(),
                timeout=10,
                label="cron-status",
            )
            if res is None or getattr(res, "returncode", 1) != 0:
                try:
                    tg_client.send_message(
                        chat_id,
                        _render.escape_md2(
                            "❌ /jobs 查询失败 (af review-cron-status 错)"
                        ),
                        parse_mode="MarkdownV2",
                    )
                except Exception:
                    pass
            else:
                # review-cron-status emits text only (launchd / macOS-only):
                #   plist:    <path> (present|missing)
                #   status:   loaded|not loaded
                #   <launchctl list output, on success>
                raw = (res.stdout or "").strip() or "(无输出)"
                installed = "present" in raw and "loaded" in raw and "not loaded" not in raw
                header = (
                    "⏰ Cron 定时任务 (launchd)"
                    if installed
                    else "⏰ 无 cron 定时任务 (用 `af review-cron-install --times \"09:00,18:00\"` 装)"
                )
                # cap raw output to keep TG messages short.
                snippet = "\n".join(raw.splitlines()[:20])[:1500]
                body = (
                    _render.escape_md2(header)
                    + "\n```\n"
                    + _render.escape_md2(snippet)
                    + "\n```"
                )
                try:
                    tg_client.send_message(
                        chat_id, body, parse_mode="MarkdownV2"
                    )
                except Exception:
                    # Fallback: plain text if MarkdownV2 escaping trips up.
                    try:
                        tg_client.send_message(chat_id, raw[:1500], parse_mode=None)
                    except Exception:
                        pass
        except Exception as err:
            try:
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(f"❌ /jobs 失败: {err}"[:500]),
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass

        _audit({"kind": "slash_command", "cmd": "/jobs", "uid": uid})
        return

    if text.startswith("/suggestions"):
        parts = text.split(maxsplit=1)
        profile_id = parts[1].strip() if len(parts) > 1 else None
        try:
            suggestions = list_suggestions(profile_id=profile_id, status="pending")
            body, kb = _render.render_suggestion_list(suggestions=suggestions)
            tg_client.send_message(chat_id, body, reply_markup=kb, parse_mode="MarkdownV2")
        except Exception as err:
            try:
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(f"❌ suggestions failed: {err}"[:3500]),
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass
        _audit({"kind": "slash_command", "cmd": "/suggestions", "uid": uid})
        return

    if text.startswith("/status"):
        try:
            _send_status_summary(chat_id)
        except Exception as err:
            try:
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(f"❌ /status 失败: {err}"[:500]),
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass
        _audit({"kind": "slash_command", "cmd": "/status", "uid": uid})
        return

    if text.startswith("/queue"):
        try:
            _send_queue_summary(chat_id, limit=5)
        except Exception as err:
            try:
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(f"❌ /queue 失败: {err}"[:500]),
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass
        _audit({"kind": "slash_command", "cmd": "/queue", "uid": uid})
        return

    if text.startswith("/skip "):
        target_id = text[len("/skip "):].strip().split()[0] if len(text) > 6 else ""
        result = _slash_skip(chat_id, uid, target_id)
        _audit({
            "kind": "slash_command",
            "cmd": "/skip",
            "uid": uid,
            "article_id": target_id,
            **({"error": result} if result else {}),
        })
        return

    if text.startswith("/defer "):
        parts = text.split()
        if len(parts) < 3:
            try:
                tg_client.send_message(
                    chat_id,
                    "用法: /defer <article_id> <hours>",
                    parse_mode=None,
                )
            except Exception:
                pass
            _audit({"kind": "slash_command", "cmd": "/defer", "uid": uid, "error": "bad_args"})
            return
        target_id = parts[1]
        try:
            hours = float(parts[2])
            if hours <= 0:
                raise ValueError("hours must be positive")
        except ValueError as err:
            try:
                tg_client.send_message(
                    chat_id,
                    f"❌ hours 参数错: {err}",
                    parse_mode=None,
                )
            except Exception:
                pass
            _audit({
                "kind": "slash_command", "cmd": "/defer", "uid": uid,
                "article_id": target_id, "error": "bad_hours",
            })
            return
        result = _slash_defer(chat_id, uid, target_id, hours)
        _audit({
            "kind": "slash_command",
            "cmd": "/defer",
            "uid": uid,
            "article_id": target_id,
            "hours": hours,
            **({"error": result} if result else {}),
        })
        return

    if text.startswith("/publish-mark"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            try:
                tg_client.send_message(
                    chat_id,
                    "用法: /publish-mark <article_id> <url> [platform]",
                    parse_mode=None,
                )
            except Exception:
                pass
            _audit({
                "kind": "slash_command", "cmd": "/publish-mark", "uid": uid,
                "error": "bad_args",
            })
            return
        target_id = parts[1]
        rest = parts[2].split()
        url = rest[0] if rest else ""
        platform = rest[1] if len(rest) > 1 else "medium"
        result = _slash_publish_mark(chat_id, uid, target_id, url, platform)
        _audit({
            "kind": "slash_command",
            "cmd": "/publish-mark",
            "uid": uid,
            "article_id": target_id,
            "platform": platform,
            **({"error": result} if result else {}),
        })
        return

    if text.startswith("/audit"):
        try:
            _send_audit_tail(chat_id, limit=20)
        except Exception as err:
            try:
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(f"❌ /audit 失败: {err}"[:500]),
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass
        _audit({"kind": "slash_command", "cmd": "/audit", "uid": uid})
        return

    if text.startswith("/auth-debug"):
        try:
            _send_auth_debug(chat_id, uid)
        except Exception as err:
            try:
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(f"❌ /auth-debug 失败: {err}"[:500]),
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass
        _audit({"kind": "slash_command", "cmd": "/auth-debug", "uid": uid})
        return

    if text.startswith("/cancel "):
        short_id = text[len("/cancel "):].strip()
        try:
            entry = _sid.resolve(short_id) if short_id else None
            if not entry:
                tg_client.send_message(
                    chat_id,
                    f"❌ short\\_id 已失效或不存在: `{_render.escape_md2(short_id)}`",
                    parse_mode="MarkdownV2",
                )
            else:
                _sid.revoke(short_id)
                tg_client.send_message(
                    chat_id,
                    f"🚫 已取消 short\\_id\\=`{_render.escape_md2(short_id)}`",
                    parse_mode="MarkdownV2",
                )
        except Exception:
            pass
        _audit({"kind": "slash_command", "cmd": "/cancel", "uid": uid})
        return

    # v1.0.4 — registry-driven slash dispatcher catches the long tail of
    # operator-completeness commands (/onboard /doctor /profile* /style*
    # /intent* /prefs* /report /restart-daemon …). v1.0.3 inline branches
    # above own /status /queue /help /list /published /scan /jobs /skip
    # /defer /publish-mark /audit /auth-debug /cancel /suggestions /start.
    if text.startswith("/"):
        if _dispatch_v104_command(chat_id, uid, text):
            return

    if _maybe_handle_profile_session_reply(
        chat_id=chat_id,
        uid=uid,
        text=text,
    ):
        _audit({"kind": "profile_session_reply", "uid": uid})
        return

    pending = pending_edits.take(int(uid)) if uid is not None else None
    # v1.0.4 — multi-turn replies for confirm prompts (PREFS_RESET / RESTART)
    # and field-update gates (PU / SL) bypass the legacy ``edit``-grant check
    # because they don't fan out to ``_spawn_edit``.
    if pending and text:
        v104_gate = str(pending.get("gate") or "")
        if v104_gate in {"PREFS_RESET", "RESTART"}:
            if _handle_pending_confirm_reply(
                chat_id=chat_id, uid=uid, pending=pending, text=text,
            ):
                return
        if v104_gate == "PU":
            target = str(pending.get("article_id") or "")
            try:
                _, pid, field = target.split("::", 2)
            except ValueError:
                pid, field = "", ""
            if pid and field:
                try:
                    from agentflow.shared.topic_profile_lifecycle import upsert_profile
                    upsert_profile(
                        pid,
                        {"publisher_account": {field: [
                            line.strip() for line in text.splitlines() if line.strip()
                        ]}},
                        replace_lists=True,
                        source="tg_slash_profile_update_multi",
                    )
                    append_memory_event(
                        "topic_profile_updated",
                        payload={
                            "profile_id": pid, "mode": "tg_slash_multi",
                            "field": field,
                        },
                    )
                    _safe_send(chat_id, f"✅ profile {pid} 字段 {field} 已更新（多行）")
                except Exception as err:
                    _safe_send(chat_id, f"❌ update 失败: {err}"[:400])
            else:
                _safe_send(chat_id, "❌ pending profile-update 上下文丢失")
            return
        if v104_gate == "SL":
            arg = (text or "").strip()
            if not arg:
                _safe_send(chat_id, "❌ 输入为空")
                return
            try:
                if arg.startswith("@") or arg.startswith("http"):
                    from agentflow.cli.commands import learn_from_handle
                    out, err = _capture_callback(
                        learn_from_handle.callback,
                        handle_or_url=arg, max_samples=5, ask_extras=False,
                        profile_id=None, dry_run=False, refresh=False, as_json=False,
                    )
                else:
                    from agentflow.cli.commands import learn_style
                    out, err = _capture_callback(
                        learn_style.callback,
                        dir_=arg, file_=tuple(), url=tuple(),
                        from_published=False, show=False, recompute=False,
                    )
                body = out.strip() or "(done)"
                if err:
                    body += f"\n\n[err] {err}"
                _safe_send(chat_id, _trim_for_tg(f"🗣️ style-learn:\n{body}"))
            except Exception as err:
                _safe_send(chat_id, f"❌ style-learn 失败: {err}"[:400])
            return

    if pending and text and not auth.is_authorized(uid, action="edit"):
        # The uid was *cleared* of the pending edit by .take(); re-register
        # so a teammate with the right grant can still pick it up if the
        # operator re-routes. Cheap belt-and-suspenders.
        try:
            pending_edits.register(
                uid=int(uid),
                article_id=pending.get("article_id"),
                gate=pending.get("gate") or "B",
                short_id=pending.get("short_id") or "",
                ttl_minutes=30,
            )
        except Exception:
            pass
        try:
            tg_client.send_message(
                chat_id,
                f"❌ 未授权 \\(action\\=edit, uid\\=`{uid}`\\)\\. "
                "请联系操作员授权 `edit` 动作\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass
        _audit({"kind": "edit_reply_denied", "uid": uid, "pending": pending})
        return
    if pending and text:
        # Q6: PR (publish-mark) — operator's reply is the Medium URL.
        if pending.get("gate") == "PR":
            url = (text or "").strip().split()[0] if (text or "").strip() else ""
            if not url.startswith(("http://", "https://")):
                try:
                    tg_client.send_message(
                        chat_id,
                        "❌ URL 格式错误 (需 http/https)。重试 [📌 我已粘贴]",
                        parse_mode=None,
                    )
                except Exception:
                    pass
                return
            _audit({
                "kind": "publish_mark_reply",
                "uid": uid,
                "article_id": pending.get("article_id"),
                "url": url,
            })
            _spawn_publish_mark(pending.get("article_id"), url)
            try:
                tg_client.send_message(
                    chat_id, "📌 URL 已收到, mark 中…", parse_mode=None,
                )
            except Exception:
                pass
            return
        _audit({"kind": "edit_reply", "uid": uid, "pending": pending, "text_head": text[:60]})
        _spawn_edit(
            article_id=pending.get("article_id"),
            instruction=text,
            chat_id=chat_id,
        )
        try:
            tg_client.send_message(
                chat_id,
                "📝 编辑指令已收到, 改写中…完成后会发新一版 Gate B 卡\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass


def _handle_callback(update: dict[str, Any]) -> None:
    cb = update.get("callback_query") or {}
    cb_id = cb.get("id")
    data = (cb.get("data") or "").strip()
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    sender = cb.get("from") or {}
    uid = sender.get("id")
    if not auth.is_authorized(uid):
        tg_client.answer_callback_query(
            cb_id, text=f"❌ 未授权 (uid={uid})", show_alert=True
        )
        _log.warning("unauthorized callback from uid=%s data=%r", uid, data)
        _audit({"kind": "callback_unauthorized", "uid": uid, "callback_data": data})
        return
    parts = data.split(":", 3)
    if len(parts) < 3:
        tg_client.answer_callback_query(cb_id, text="格式错误", show_alert=False)
        return
    gate, action, sid = parts[0], parts[1], parts[2]
    extra = parts[3] if len(parts) > 3 else ""

    entry = _sid.resolve(sid)
    if not entry:
        # Three distinct failure modes hide behind a None resolve. Folding
        # them into one alarming "已失效" message trains the operator to
        # ignore real failures.
        #   (a) Recently soft-revoked → action already ran, this is a replay
        #       (TG retransmit on flaky networks; user double-click).
        #   (b) sid present but TTL'd naturally → card sat in chat too long.
        #   (c) sid never existed in index → daemon was down when card sent,
        #       index file was nuked, or sid is forged.
        if _sid.was_recently_revoked(sid, within_seconds=600.0):
            tg_client.answer_callback_query(
                cb_id, text="✓ 已处理（重复点击）", show_alert=False
            )
            _audit({"kind": "callback_replay", "callback_data": data})
            return
        raw = _sid.peek_raw(sid)
        if raw is not None:
            # Case (b): sid in index but expired (or revoked >10min ago).
            age_hint = ""
            try:
                from datetime import datetime, timezone
                ts = raw.get("expires_at") or raw.get("created_at")
                if ts:
                    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600.0
                    age_hint = f"（{abs(age_h):.0f}h 前）"
            except Exception:
                pass
            tg_client.answer_callback_query(
                cb_id,
                text=f"卡片已超时{age_hint}，用 /list 查看可操作卡片",
                show_alert=True,
            )
            _audit({"kind": "callback_ttl_expired", "callback_data": data, "raw_gate": raw.get("gate")})
        else:
            # Case (c): sid completely unknown.
            tg_client.answer_callback_query(
                cb_id,
                text="未知按键（sid 不在索引中）。daemon 可能重启过——用 /list 查看当前活动卡片",
                show_alert=True,
            )
            _audit({"kind": "callback_unknown_sid", "callback_data": data})
        return
    # PD:* (dispatch-preview confirm chain) reuses the D-gate sid minted at
    # post_gate_d time, so we accept "PD" callbacks against gate=="D" entries.
    entry_gate = entry.get("gate")
    if entry_gate != gate and not (gate == "PD" and entry_gate == "D"):
        tg_client.answer_callback_query(cb_id, text="gate 不匹配", show_alert=True)
        return

    _audit({
        "kind": "callback",
        "gate": gate,
        "action": action,
        "short_id": sid,
        "extra": extra,
        "entry": entry,
        "chat_id": chat_id,
        "message_id": message_id,
    })

    try:
        _route(gate, action, sid, extra, entry, cb_id, chat_id, message_id, uid)
    except Exception as err:  # pragma: no cover
        _log.exception("callback routing failed: %s", err)
        tg_client.answer_callback_query(
            cb_id, text=f"处理失败: {err}"[:180], show_alert=True
        )


# KNOWN STUB CALLBACKS — these go through the catch-all "已记录" branch at
# the end of _route. Buttons appear but pressing them does NOT change state
# or trigger any code path. Wire them up before relying on them in flows:
#   A:expand / A:defer / B:diff / B:defer
#   C:regen / C:relogo / C:full / C:defer
# (mirrored in templates/state_machine.md → "Known stubs" section).
#
# (gate, action) → required action verb. Anything not in this map falls
# through to the legacy "any authorized uid is fine" check (safe default
# since unmapped actions are stubs that don't mutate state).
_ACTION_REQ: dict[tuple[str, str], str] = {
    ("P", "start"): "review",
    ("P", "later"): "review",
    ("S", "review"): "review",
    ("S", "apply"): "review",
    ("S", "dismiss"): "review",
    ("A", "write"): "write",
    ("A", "reject_all"): "review",
    ("A", "expand"): "review",
    ("A", "defer"): "review",
    ("B", "approve"): "review",
    ("B", "reject"): "review",
    ("B", "rewrite"): "edit",
    ("B", "edit"): "edit",
    ("B", "diff"): "review",
    ("B", "defer"): "review",
    ("C", "approve"): "review",
    ("C", "skip"): "review",
    ("C", "regen"): "image",
    ("C", "relogo"): "image",
    ("C", "full"): "review",
    ("C", "defer"): "review",
    # Gate D — channel selection. ``confirm`` requires ``publish`` because it
    # actually fires the D4 dispatch (LinkedIn / Twitter / webhook). Toggle +
    # cancel are review-level actions (no live writes).
    ("D", "toggle"): "review",
    ("D", "confirm"): "publish",
    ("D", "cancel"): "review",
    ("D", "retry"): "publish",
    # Gate D quick-select / save-default / resume / extend (Q1/Q2/Q5/Q6).
    # All four mutate UI/metadata only — no live publish — so review-grade.
    ("D", "select_all"):   "review",
    ("D", "clear_all"):    "review",
    ("D", "save_default"): "review",
    ("D", "resume"):       "review",
    ("D", "extend"):       "review",
    # PD:* — preview-confirm chain spawned by D:confirm. ``dispatch`` is the
    # actual publish trigger (publish-grade); ``cancel`` rewinds without
    # firing D4 (review-grade).
    ("PD", "dispatch"):    "publish",
    ("PD", "cancel"):      "review",
    # Gate L — manual takeover after rewrite-round limit (>=2). critique +
    # give_up are review-level; edit needs the ``edit`` grant since the next
    # plain-text reply is parsed as an edit instruction.
    ("L", "critique"): "review",
    ("L", "edit"): "edit",
    ("L", "give_up"): "review",
    # Image-gate picker (soft prompt sent after Gate B ✅). cover_only /
    # cover_plus_body actually fire `af image-gate`, so they need the image
    # grant. ``none`` just walks state to image_skipped + posts Gate D.
    ("I", "cover_only"): "image",
    ("I", "cover_plus_body"): "image",
    ("I", "none"): "review",
    # Q6 — publish-mark entry. Captures Medium URL after manual paste; the
    # follow-up plain-text reply is handled by ``_handle_message`` (PR gate).
    ("PR", "mark"): "publish",
}


def _route(
    gate: str,
    action: str,
    sid: str,
    extra: str,
    entry: dict[str, Any],
    cb_id: str,
    chat_id: int | None,
    message_id: int | None,
    uid: int | None = None,
) -> None:
    article_id = entry.get("article_id")

    required = _ACTION_REQ.get((gate, action))
    if required is not None and not auth.is_authorized(uid, action=required):
        tg_client.answer_callback_query(
            cb_id,
            text=f"❌ 未授权 (action={required}, uid={uid})",
            show_alert=True,
        )
        _log.warning(
            "callback action denied: gate=%s action=%s required=%s uid=%s",
            gate, action, required, uid,
        )
        _audit({
            "kind": "callback_action_denied",
            "gate": gate,
            "action": action,
            "required": required,
            "uid": uid,
            "short_id": sid,
        })
        return

    # Gate A — pick a slot and kick off the write+fill pipeline as a
    # background subprocess. The Gate B card lands automatically when fill
    # completes (af fill has the auto-trigger glue).
    if gate == "A" and action == "write":
        batch_path = entry.get("batch_path")
        if not batch_path:
            tg_client.answer_callback_query(cb_id, text="批次缺失", show_alert=True)
            return
        slot = 0
        if extra and extra.startswith("slot="):
            try:
                slot = int(extra.split("=", 1)[1])
            except ValueError:
                slot = 0
        try:
            with open(batch_path, "r", encoding="utf-8") as fh:
                batch_data = json.load(fh)
        except Exception as err:
            tg_client.answer_callback_query(
                cb_id, text=f"批次读取失败: {err}"[:180], show_alert=True
            )
            return
        hotspots = batch_data.get("hotspots") or []
        if slot >= len(hotspots):
            tg_client.answer_callback_query(cb_id, text="slot 越界", show_alert=True)
            return
        hotspot_id = (hotspots[slot] or {}).get("id")
        if not hotspot_id:
            tg_client.answer_callback_query(cb_id, text="hotspot id 缺失", show_alert=True)
            return
        _audit({
            "kind": "spawn_write",
            "hotspot_id": hotspot_id,
            "slot": slot,
            "batch_path": batch_path,
        })
        _spawn_write_and_fill(hotspot_id)
        tg_client.answer_callback_query(cb_id, text=f"#{slot + 1} 起稿中… 完成后会推 Gate B")
        # Disable buttons on the card so the user doesn't double-click.
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        _sid.revoke(sid)
        return

    if gate == "A" and action == "reject_all":
        # Hotspots haven't become articles yet, so there's no article_id to
        # transition. Instead we flag every hotspot in the batch with
        # ``status="rejected_batch"`` so the next ``af hotspots`` scan skips
        # them. Errors here MUST NOT crash the daemon — we degrade to an
        # audit-only ack so the user still gets feedback.
        batch_path = entry.get("batch_path")
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
            except Exception as err:  # graceful: never raise
                io_err = str(err)[:200]
                _log.warning(
                    "A:reject_all batch write failed (path=%s): %s",
                    batch_path, err,
                )
        else:
            io_err = "missing batch_path"

        _audit({
            "kind": "batch_rejected",
            "batch_path": batch_path,
            "hotspot_count": rejected_count,
            "uid": uid,
            "short_id": sid,
            **({"io_error": io_err} if io_err else {}),
        })

        _sid.revoke(sid)
        if io_err:
            tg_client.answer_callback_query(
                cb_id,
                text="⚠️ 已记录但 batch 文件读失败"[:180],
                show_alert=False,
            )
        else:
            tg_client.answer_callback_query(
                cb_id, text=f"🚫 整批已拒绝 ({rejected_count})"
            )
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        return

    if gate == "P" and action == "start":
        session_path = entry.get("batch_path")
        if not session_path:
            tg_client.answer_callback_query(cb_id, text="session 缺失", show_alert=True)
            return
        try:
            session = load_session(Path(session_path).stem)
        except Exception as err:
            tg_client.answer_callback_query(cb_id, text=f"session 读取失败: {err}"[:180], show_alert=True)
            return
        session["status"] = "collecting"
        session["active_uid"] = uid
        session["active_chat_id"] = chat_id
        session["step_index"] = 0
        save_session(session)
        tg_client.answer_callback_query(cb_id, text="开始采集 profile 约束")
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        _sid.revoke(sid)
        _send_profile_setup_question(session)
        return

    if gate == "P" and action == "later":
        _sid.revoke(sid)
        tg_client.answer_callback_query(cb_id, text="稍后再补 profile")
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        return

    if gate == "S" and action == "review":
        suggestion_id = str((entry.get("extra") or {}).get("suggestion_id") or "")
        if not suggestion_id:
            tg_client.answer_callback_query(cb_id, text="suggestion 缺失", show_alert=True)
            return
        try:
            payload = review_suggestion(suggestion_id)
            text, kb, _ = _render.render_suggestion_review(
                suggestion=payload["suggestion"],
                preview_profile=payload["preview_profile"],
            )
            if chat_id is not None:
                tg_client.send_message(chat_id, text, reply_markup=kb, parse_mode="MarkdownV2")
            tg_client.answer_callback_query(cb_id, text="已打开 suggestion review")
        except Exception as err:
            tg_client.answer_callback_query(cb_id, text=f"review 失败: {err}"[:180], show_alert=True)
        return

    if gate == "S" and action == "apply":
        suggestion_id = str((entry.get("extra") or {}).get("suggestion_id") or "")
        if not suggestion_id:
            tg_client.answer_callback_query(cb_id, text="suggestion 缺失", show_alert=True)
            return
        try:
            payload = apply_suggestion(suggestion_id)
            profile_id = str(payload["suggestion"].get("profile_id") or "?")
            tg_client.answer_callback_query(cb_id, text="✅ suggestion 已应用")
            if chat_id is not None:
                tg_client.send_message(
                    chat_id,
                    _render.escape_md2(f"✅ Applied suggestion {suggestion_id} to profile {profile_id}."),
                    parse_mode="MarkdownV2",
                )
            if chat_id is not None and message_id is not None:
                try:
                    tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
                except Exception:
                    pass
            _sid.revoke(sid)
        except Exception as err:
            tg_client.answer_callback_query(cb_id, text=f"apply 失败: {err}"[:180], show_alert=True)
        return

    if gate == "S" and action == "dismiss":
        suggestion_id = str((entry.get("extra") or {}).get("suggestion_id") or "")
        if not suggestion_id:
            tg_client.answer_callback_query(cb_id, text="suggestion 缺失", show_alert=True)
            return
        try:
            update_suggestion_status(suggestion_id, "dismissed")
            tg_client.answer_callback_query(cb_id, text="已忽略 suggestion")
            if chat_id is not None and message_id is not None:
                try:
                    tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
                except Exception:
                    pass
            _sid.revoke(sid)
        except Exception as err:
            tg_client.answer_callback_query(cb_id, text=f"dismiss 失败: {err}"[:180], show_alert=True)
        return

    # Gate B
    if gate == "B" and action == "approve" and article_id:
        state.transition(
            article_id,
            gate="B",
            to_state=state.STATE_DRAFT_APPROVED,
            actor="human",
            decision="approve",
            tg_chat_id=chat_id,
            tg_message_id=message_id,
            callback_data=f"{gate}:{action}:{sid}",
        )
        _sid.revoke(sid)
        tg_client.answer_callback_query(cb_id, text="✅ 草稿已通过")
        if chat_id is not None and message_id is not None:
            tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
        # Q3/Q4: send image-gate picker card (soft prompt, can be ignored).
        # State stays at draft_approved until the user picks a mode or runs
        # `af image-gate` from the CLI.
        _spawn_image_gate_picker(article_id)
        return

    if gate == "B" and action == "reject" and article_id:
        state.transition(
            article_id,
            gate="B",
            to_state=state.STATE_DRAFT_REJECTED,
            actor="human",
            decision="reject",
            tg_chat_id=chat_id,
            tg_message_id=message_id,
            callback_data=f"{gate}:{action}:{sid}",
        )
        _sid.revoke(sid)
        tg_client.answer_callback_query(cb_id, text="🚫 草稿已拒绝", show_alert=True)
        if chat_id is not None and message_id is not None:
            tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
        return

    # Gate C
    if gate == "C" and action == "approve" and article_id:
        state.transition(
            article_id,
            gate="C",
            to_state=state.STATE_IMAGE_APPROVED,
            actor="human",
            decision="approve",
            tg_chat_id=chat_id,
            tg_message_id=message_id,
            callback_data=f"{gate}:{action}:{sid}",
        )
        _sid.revoke(sid)
        tg_client.answer_callback_query(cb_id, text="✅ 封面已通过, 选择分发渠道…")
        if chat_id is not None and message_id is not None:
            tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
        # Auto-advance: post the Gate D channel-selection card. The user
        # toggles channels then clicks Confirm to fire the actual dispatch.
        # Runs in a daemon thread so the callback handler returns fast.
        _spawn_gate_d(article_id)
        return

    if gate == "C" and action == "skip" and article_id:
        state.transition(
            article_id,
            gate="C",
            to_state=state.STATE_IMAGE_SKIPPED,
            actor="human",
            decision="skip",
            tg_chat_id=chat_id,
            tg_message_id=message_id,
            callback_data=f"{gate}:{action}:{sid}",
        )
        _sid.revoke(sid)
        tg_client.answer_callback_query(cb_id, text="🚫 不用图, 选择分发渠道…")
        if chat_id is not None and message_id is not None:
            tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
        # Gate D still fires after image_skipped — user picks channels even
        # without a cover.
        _spawn_gate_d(article_id)
        return

    # Gate C — 🔁 re-run AtlasCloud cover generation (overwrites cover.png).
    # The CLI's image-gate command self-triggers post_gate_c when generation
    # finishes, so a fresh Gate C card lands automatically.
    if gate == "C" and action == "regen" and article_id:
        tg_client.answer_callback_query(cb_id, text="🔁 重跑 Atlas (cover-only)…")
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id,
                    f"🔁 已开始重新生成封面：{article_id}\n"
                    "完成后会自动推送新的 Gate C；超时或失败会发错误通知。",
                )
            except Exception:
                pass
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        _sid.revoke(sid)
        _spawn_image_gate(article_id, mode="cover-only")
        return

    # Gate C — 🎨 cycle the brand_overlay anchor on the existing cover (no
    # AtlasCloud re-call). Re-applies the wordmark at the next anchor in the
    # cycle and re-posts Gate C.
    if gate == "C" and action == "relogo" and article_id:
        tg_client.answer_callback_query(cb_id, text="🎨 重叠 logo (cycle anchor)…")
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        _sid.revoke(sid)
        _spawn_relogo(article_id)
        return

    # Gate D — channel selection (multi-select toggle + confirm/cancel)
    if gate == "D" and action == "toggle" and article_id:
        if not extra.startswith("p="):
            tg_client.answer_callback_query(cb_id, text="格式错误", show_alert=False)
            return
        platform = extra.split("=", 1)[1]
        bag = entry.get("extra") or {}
        available = list(bag.get("available") or [])
        if platform not in available:
            tg_client.answer_callback_query(
                cb_id, text=f"{platform} 不可用", show_alert=True
            )
            return
        selected = set(bag.get("selected") or [])
        if platform in selected:
            selected.discard(platform)
            now_on = False
        else:
            selected.add(platform)
            now_on = True
        # Persist back into the short_id entry so the next click sees the
        # updated state, then edit the keyboard in place. We only need the
        # markup — body text is unchanged on toggles.
        _sid.set_extra(sid, "selected", sorted(selected))
        try:
            _, kb = _render.render_gate_d(
                article_id=article_id, title="",
                available=available, selected=selected, short_id=sid,
            )
            if chat_id is not None and message_id is not None:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup=kb)
        except Exception as err:  # pragma: no cover
            _log.warning("gate D toggle re-render failed: %s", err)
        tg_client.answer_callback_query(
            cb_id, text=f"切换 {platform}: {'on' if now_on else 'off'}"
        )
        return

    # Gate D quick-select / quick-clear (Q1) — flips ``selected`` to either
    # the full ``available`` list or empty, persists via set_extra, and
    # re-renders the keyboard in place (same body text). Mirrors the toggle
    # branch but bulk.
    if gate == "D" and action in {"select_all", "clear_all"} and article_id:
        bag = entry.get("extra") or {}
        available = list(bag.get("available") or [])
        if action == "select_all":
            selected = list(available)
            msg = f"⚡ 全选 ({len(selected)} 平台)"
        else:
            selected = []
            msg = "✖ 已清空"
        _sid.set_extra(sid, "selected", sorted(selected))
        try:
            _, kb = _render.render_gate_d(
                article_id=article_id, title="",
                available=available, selected=set(selected), short_id=sid,
            )
            if chat_id is not None and message_id is not None:
                tg_client.edit_message_reply_markup(
                    chat_id, message_id, reply_markup=kb,
                )
        except Exception as err:
            _log.warning("gate D %s re-render failed: %s", action, err)
        tg_client.answer_callback_query(cb_id, text=msg)
        return

    # Gate D save-as-default (Q2) — writes the current ``selected`` set into
    # metadata.metadata_overrides.gate_d.default_platforms so future Gate D
    # cards for this article preselect the same set. Read-modify-write of
    # metadata.json; failures are surfaced as a callback toast (no crash).
    if gate == "D" and action == "save_default" and article_id:
        bag = entry.get("extra") or {}
        selected = list(bag.get("selected") or [])
        if not selected:
            tg_client.answer_callback_query(
                cb_id, text="⚠ 当前未选任何渠道", show_alert=True,
            )
            return
        try:
            meta_path = (
                agentflow_home() / "drafts" / article_id / "metadata.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
            meta.setdefault("metadata_overrides", {}).setdefault(
                "gate_d", {}
            )["default_platforms"] = list(selected)
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _audit({
                "kind": "gate_d_save_default",
                "article_id": article_id,
                "platforms": list(selected),
            })
            tg_client.answer_callback_query(
                cb_id,
                text=f"✅ 已保存默认 ({len(selected)} 平台)",
                show_alert=False,
            )
        except Exception as err:
            _log.warning("gate D save_default failed: %s", err)
            tg_client.answer_callback_query(
                cb_id, text=f"❌ 保存失败: {err}"[:180],
            )
        return

    # Gate D confirm (Q4) — does NOT directly fire publish anymore. Instead
    # we spawn a dispatch-preview card (PD:dispatch / PD:cancel) that reuses
    # the same sid. State stays at channel_pending_review until the operator
    # makes the real decision on the preview card.
    if gate == "D" and action == "confirm" and article_id:
        bag = entry.get("extra") or {}
        selected = list(bag.get("selected") or [])
        if not selected:
            tg_client.answer_callback_query(
                cb_id, text="请至少选一个渠道", show_alert=True
            )
            return
        tg_client.answer_callback_query(
            cb_id, text="📋 生成 dispatch preview…",
        )
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(
                    chat_id, message_id, reply_markup={},
                )
            except Exception:
                pass
        # IMPORTANT: do NOT revoke sid — the preview card's PD:* buttons
        # need the same sid to resolve back to this Gate D entry.
        _spawn_dispatch_preview(article_id, selected, short_id=sid)
        return

    # Gate D cancel (Q5) — instead of just clearing the keyboard, edit (or
    # send) a "🚫 已取消" notice with a [🔄 恢复 Gate D] button so the user
    # can recover without leaving the chat. State still rewinds to
    # image_approved, but the sid is NOT fully revoked yet — D:resume reads
    # article_id back from it.
    if gate == "D" and action == "cancel" and article_id:
        try:
            state.transition(
                article_id,
                gate="D",
                to_state=state.STATE_IMAGE_APPROVED,
                actor="human",
                decision="cancel",
                tg_chat_id=chat_id,
                tg_message_id=message_id,
                callback_data=f"{gate}:{action}:{sid}",
                force=True,
            )
        except state.StateError as err:
            _log.warning("Gate D cancel transition failed: %s", err)
        # soft-revoke so PD:* / D:toggle on the original card no longer
        # resolve, but mint a fresh sid for the resume button (so it never
        # races the revoke window).
        _sid.revoke(sid)
        tg_client.answer_callback_query(cb_id, text="🚫 已取消 Gate D")
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(
                    chat_id, message_id, reply_markup={},
                )
            except Exception:
                pass
            try:
                resume_sid = _sid.register(
                    gate="D",
                    article_id=article_id,
                    ttl_hours=12,
                    extra={"resume_only": True},
                )
                resume_text = (
                    f"🚫 已取消 Gate D · article=`{_render.escape_md2(article_id)}`\n\n"
                    "点 🔄 恢复重新选择渠道。"
                )
                resume_kb = {
                    "inline_keyboard": [[
                        {
                            "text": "🔄 恢复 Gate D",
                            "callback_data": f"D:resume:{resume_sid}",
                        }
                    ]]
                }
                tg_client.send_message(
                    chat_id, resume_text, reply_markup=resume_kb,
                    parse_mode="MarkdownV2",
                )
            except Exception as err:
                _log.warning("Gate D cancel resume-card send failed: %s", err)
        return

    # Gate D resume (Q5 follow-up) — rebuild a fresh Gate D card. State is
    # already at image_approved (the cancel branch put it there); post_gate_d
    # walks it back to channel_pending_review.
    if gate == "D" and action == "resume" and article_id:
        tg_client.answer_callback_query(cb_id, text="🔄 恢复 Gate D…")
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(
                    chat_id, message_id, reply_markup={},
                )
            except Exception:
                pass
        _sid.revoke(sid)
        _spawn_gate_d(article_id)
        return

    # Gate D extend (Q6) — operator-driven 12h extension after the auto-
    # cancel ping has fired. Max 1 extension per article (extended_count
    # field on timeout_state). Resets the per-article timeout markers
    # (first_pinged_at / second_action_taken_at) so the sweeper restarts
    # the clock, force-rewinds state back to channel_pending_review, and
    # re-posts a fresh Gate D card.
    if gate == "D" and action == "extend" and article_id:
        try:
            state_data = timeout_state._read()
        except Exception as err:
            _log.warning("Gate D extend read state failed: %s", err)
            state_data = {}
        art_state = dict(state_data.get(article_id) or {})
        if int(art_state.get("extended_count", 0)) >= 1:
            tg_client.answer_callback_query(
                cb_id, text="⚠ 已延期过 1 次, 不可再延", show_alert=True,
            )
            return
        art_state["extended_count"] = int(art_state.get("extended_count", 0)) + 1
        art_state.pop("second_action_taken_at", None)
        art_state.pop("first_pinged_at", None)
        try:
            state_data[article_id] = art_state
            timeout_state._write(state_data)
        except Exception as err:
            _log.warning("Gate D extend write state failed: %s", err)
        try:
            state.transition(
                article_id, gate="D",
                to_state=state.STATE_CHANNEL_PENDING_REVIEW,
                actor="human", decision="extend_timeout",
                force=True,
            )
        except state.StateError as err:
            _log.warning("Gate D extend transition failed: %s", err)
        _sid.revoke(sid)
        tg_client.answer_callback_query(
            cb_id, text="✅ 已延 12h, 重新计时", show_alert=False,
        )
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(
                    chat_id, message_id, reply_markup={},
                )
            except Exception:
                pass
        _spawn_gate_d(article_id)
        return

    # Dispatch-preview confirm chain (Q4) — PD:dispatch is the real publish
    # trigger; PD:cancel rewinds the same way as D:cancel.
    if gate == "PD" and action == "dispatch" and article_id:
        bag = entry.get("extra") or {}
        selected = list(bag.get("selected") or [])
        if not selected:
            tg_client.answer_callback_query(
                cb_id, text="⚠ 没有选中渠道", show_alert=True,
            )
            return
        tg_client.answer_callback_query(
            cb_id, text=f"🚀 分发中... {', '.join(selected)}",
        )
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(
                    chat_id, message_id, reply_markup={},
                )
            except Exception:
                pass
        _sid.revoke(sid)
        _spawn_publish_dispatch(article_id, selected)
        return

    if gate == "PD" and action == "cancel" and article_id:
        try:
            state.transition(
                article_id, gate="D", to_state=state.STATE_IMAGE_APPROVED,
                actor="human", decision="preview_cancel", force=True,
            )
        except state.StateError as err:
            _log.warning("PD cancel transition failed: %s", err)
        _sid.revoke(sid)
        tg_client.answer_callback_query(
            cb_id, text="🚫 已取消 (preview)", show_alert=True,
        )
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(
                    chat_id, message_id, reply_markup={},
                )
            except Exception:
                pass
        return

    if gate == "D" and action == "retry" and article_id:
        bag = entry.get("extra") or {}
        failed = list(bag.get("failed") or [])
        if not failed:
            tg_client.answer_callback_query(
                cb_id, text="无失败渠道可重试", show_alert=True
            )
            return
        tg_client.answer_callback_query(
            cb_id, text=f"🔁 重试中: {', '.join(failed)}"
        )
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        _sid.revoke(sid)
        _spawn_publish_retry(article_id, failed)
        return

    # Image-gate picker (soft prompt sent after Gate B ✅). Each branch just
    # spawns ``af image-gate <aid> --mode <X>`` — the CLI's own glue
    # transitions state and posts Gate C / Gate D when it finishes.
    if (
        gate == "I"
        and action in {"cover_only", "cover_plus_body", "none"}
        and article_id
    ):
        mode_map = {
            "cover_only": "cover-only",
            "cover_plus_body": "cover-plus-body",
            "none": "none",
        }
        mode = mode_map[action]
        tg_client.answer_callback_query(cb_id, text=f"📸 image-gate mode={mode}…")
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id,
                    f"📸 已开始 image-gate：{article_id} / mode={mode}\n"
                    "完成后会自动推送下一张 Gate 卡；超时或失败会发错误通知。",
                )
            except Exception:
                pass
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        _sid.revoke(sid)
        _spawn_image_gate(article_id, mode=mode)
        return

    # Gate B — 🔁 rewrite: re-fill the same article with stored title/opening/closing.
    # After 2 prior rewrite rounds (i.e. count>=2 on the 3rd click), bump the
    # article to drafting_locked_human and fire the manual-takeover card
    # instead of running another fill.
    if gate == "B" and action == "rewrite" and article_id:
        try:
            history = state.gate_history(article_id) or []
        except Exception as err:
            _log.warning("gate_history read failed for %s: %s", article_id, err)
            history = []
        rewrite_count = sum(
            1 for h in history
            if isinstance(h, dict) and h.get("decision") == "rewrite_round"
        )
        if rewrite_count >= 2:
            try:
                state.transition(
                    article_id,
                    gate="B",
                    to_state=state.STATE_DRAFTING_LOCKED_HUMAN,
                    actor="daemon",
                    decision="locked_takeover_after_rewrites",
                    tg_chat_id=chat_id,
                    tg_message_id=message_id,
                    callback_data=f"{gate}:{action}:{sid}",
                    notes=f"rewrite_count={rewrite_count}",
                    force=True,
                )
            except state.StateError as err:
                _log.warning("locked transition failed for %s: %s", article_id, err)
            _sid.revoke(sid)
            tg_client.answer_callback_query(
                cb_id,
                text="🔒 已重写 2 次，转 manual takeover",
                show_alert=True,
            )
            if chat_id is not None and message_id is not None:
                try:
                    tg_client.edit_message_reply_markup(
                        chat_id, message_id, reply_markup={}
                    )
                except Exception:
                    pass
            _spawn_locked_takeover(article_id)
            return
        # Round 1 (count==0) or round 2 (count==1) — normal rewrite path,
        # with a heads-up on the second click that the next round will lock.
        warning = " ⚠ 下一轮起 manual takeover" if rewrite_count == 1 else ""
        tg_client.answer_callback_query(
            cb_id, text=f"🔁 重写中… 完成后发新一版 Gate B{warning}",
        )
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        _sid.revoke(sid)
        _spawn_rewrite(article_id)
        return

    # Gate B — ✏️ edit: register a pending-edit entry; the user's NEXT plain-text
    # reply will be parsed as the edit instruction.
    if gate == "B" and action == "edit" and article_id:
        # Fall back to chat_id (DM uid == chat_id)
        edit_uid = uid
        if edit_uid is None and chat_id is not None:
            edit_uid = int(chat_id)
        if edit_uid is None:
            tg_client.answer_callback_query(
                cb_id, text="无法识别用户 (uid 缺失)", show_alert=True,
            )
            return
        pending_edits.register(
            uid=int(edit_uid),
            article_id=article_id,
            gate="B",
            short_id=sid,
            ttl_minutes=30,
        )
        tg_client.answer_callback_query(
            cb_id,
            text="✏️ 请回复: <scope> <改写指令> (scope=title/opening/closing/整数)",
        )
        try:
            tg_client.send_message(
                chat_id,
                "✏️ 编辑模式\\. 请回复一条消息, 格式:\n"
                "`<scope> <改写指令>`\n"
                "scope 可以是: `title` / `opening` / `closing` / 第几节的整数 \\(0\\-based\\)\n\n"
                "例:\n"
                "• `title 标题再尖锐一点`\n"
                "• `opening 开头加一个数据点`\n"
                "• `closing 结尾收得更狠一点`\n"
                "• `2 第二节改得更口语化`\n\n"
                "30 分钟内有效, 之后会自动作废\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass
        return

    # Manual takeover (L:*) — fires after rewrite round-limit reached. The
    # article state is already drafting_locked_human at this point.
    if gate == "L" and action == "critique" and article_id:
        tg_client.answer_callback_query(cb_id, text="🧠 critique 中…")
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        # Do NOT revoke sid here — operator may still click L:edit / L:give_up
        # on the takeover card. (sid is L-gate, ttl=30 days.)
        _spawn_locked_critique(article_id)
        return

    if gate == "L" and action == "edit" and article_id:
        uid_l = uid
        if uid_l is None and chat_id is not None:
            uid_l = int(chat_id)
        if uid_l is None:
            tg_client.answer_callback_query(
                cb_id, text="无法识别用户 (uid 缺失)", show_alert=True,
            )
            return
        try:
            pending_edits.register(
                uid=int(uid_l),
                article_id=article_id,
                gate="L",
                short_id=sid,
                ttl_minutes=999999,  # essentially永久 — manual takeover has no TTL
            )
        except Exception as err:
            _log.warning("L:edit pending_edits register failed: %s", err)
        tg_client.answer_callback_query(
            cb_id,
            text="✏️ 请回复: <scope> <改写指令> (永久窗口)",
        )
        try:
            tg_client.send_message(
                chat_id,
                "✏️ Manual takeover 编辑模式\\. 请回复一条消息, 格式:\n"
                "`<scope> <改写指令>`\n"
                "scope 可以是: `title` / `opening` / `closing` / 第几节的整数 \\(0\\-based\\)\\.\n"
                "无 TTL — 任何时候 reply 都会处理\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass
        return

    if gate == "L" and action == "give_up" and article_id:
        try:
            state.transition(
                article_id,
                gate="L",
                to_state=state.STATE_DRAFT_REJECTED,
                actor="human",
                decision="give_up",
                tg_chat_id=chat_id,
                tg_message_id=message_id,
                callback_data=f"{gate}:{action}:{sid}",
                force=True,
            )
        except state.StateError as err:
            _log.warning("give_up transition failed: %s", err)
        _sid.revoke(sid)
        tg_client.answer_callback_query(cb_id, text="🚫 已放弃", show_alert=True)
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(chat_id, message_id, reply_markup={})
            except Exception:
                pass
        return

    # Q6 — publish-mark callback. Registers a no-TTL pending_edits entry
    # under gate="PR"; the operator's next plain-text reply (the Medium URL)
    # is consumed by ``_handle_message`` and dispatched to mark_published.
    if gate == "PR" and action == "mark" and article_id:
        pr_uid = uid
        if pr_uid is None and chat_id is not None:
            pr_uid = int(chat_id)
        if pr_uid is None:
            tg_client.answer_callback_query(
                cb_id, text="无法识别用户 (uid 缺失)", show_alert=True,
            )
            return
        try:
            pending_edits.register(
                uid=int(pr_uid),
                article_id=article_id,
                gate="PR",
                short_id=sid,
                ttl_minutes=999999,  # essentially永久 — manual paste has no TTL
            )
        except Exception as err:
            _log.warning("PR:mark pending_edits register failed: %s", err)
        tg_client.answer_callback_query(
            cb_id, text="📌 请回复 URL (复制 medium 浏览器地址)",
        )
        try:
            tg_client.send_message(
                chat_id,
                "📌 *Publish Mark 模式*\n\n"
                "请回复 medium 文章 URL（http\\:// 或 https\\://）\\.\n"
                "无 TTL — 任何时候 reply 都会处理\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass
        return

    # ── A:expand — render full hotspot batch as a follow-up message ──────
    if gate == "A" and action == "expand":
        batch_path = entry.get("batch_path")
        try:
            with open(batch_path, "r", encoding="utf-8") as fh:
                batch_data = json.load(fh) or {}
        except Exception as err:
            tg_client.answer_callback_query(
                cb_id, text=f"批次读取失败: {err}"[:180], show_alert=True
            )
            return
        hotspots = batch_data.get("hotspots") or []
        lines: list[str] = [f"📋 *Batch* `{_render.escape_md2(batch_path)}`", ""]
        for idx, hs in enumerate(hotspots, start=1):
            if not isinstance(hs, dict):
                continue
            title = str(hs.get("topic_one_liner") or hs.get("title") or "(no title)")
            angles = hs.get("suggested_angles") or []
            angle_titles = [
                str(a.get("title") or a.get("angle") or "")
                for a in angles if isinstance(a, dict)
            ]
            refs = hs.get("source_references") or []
            ref_count = len(refs) if isinstance(refs, list) else 0
            lines.append(f"*{idx}\\. {_render.escape_md2(title)}*")
            if angle_titles:
                lines.append(
                    "   angles: "
                    + _render.escape_md2(", ".join(angle_titles[:5]))
                )
            lines.append(
                _render.escape_md2(
                    f"   id={hs.get('id') or '?'} | refs={ref_count} | "
                    f"freshness={hs.get('freshness_score') or '?'} | "
                    f"depth={hs.get('depth_potential') or '?'}"
                )
            )
            if isinstance(refs, list) and refs:
                first_ref = refs[0] or {}
                snippet = str(first_ref.get("text_snippet") or "")[:200]
                if snippet:
                    lines.append("   " + _render.escape_md2(snippet))
            lines.append("")
        body = "\n".join(lines).rstrip()
        tg_client.answer_callback_query(cb_id, text="📋 已展开")
        if chat_id is not None:
            try:
                tg_client.send_long_text(chat_id, body, parse_mode="MarkdownV2")
            except Exception as err:
                _log.warning("A:expand send failed: %s", err)
        _audit({
            "kind": "callback_action_done",
            "gate": "A",
            "action": "expand",
            "short_id": sid,
            "uid": uid,
            "batch_path": batch_path,
            "hotspot_count": len(hotspots),
        })
        return

    # ── B:diff — render diff between current draft.md and medium_preview.md ──
    if gate == "B" and action == "diff" and article_id:
        from difflib import unified_diff

        draft_path = agentflow_home() / "drafts" / article_id / "draft.md"
        preview_path = (
            agentflow_home() / "medium" / article_id / "medium_preview.md"
        )
        if not draft_path.exists():
            tg_client.answer_callback_query(
                cb_id, text="无 draft.md 可对比", show_alert=True,
            )
            return
        if not preview_path.exists():
            tg_client.answer_callback_query(
                cb_id, text="无前次审阅版 (medium_preview.md 缺失)",
                show_alert=True,
            )
            _audit({
                "kind": "callback_action_done",
                "gate": "B",
                "action": "diff",
                "short_id": sid,
                "uid": uid,
                "article_id": article_id,
                "result": "no_prior_version",
            })
            return
        try:
            current = draft_path.read_text(encoding="utf-8").splitlines()
            previous = preview_path.read_text(encoding="utf-8").splitlines()
        except Exception as err:
            tg_client.answer_callback_query(
                cb_id, text=f"读文件失败: {err}"[:180], show_alert=True,
            )
            return
        diff_lines = list(
            unified_diff(
                previous,
                current,
                fromfile="medium_preview.md",
                tofile="draft.md",
                lineterm="",
                n=3,
            )
        )
        tg_client.answer_callback_query(cb_id, text="📋 diff 已生成")
        if chat_id is not None:
            if not diff_lines:
                try:
                    tg_client.send_message(
                        chat_id,
                        "📋 无差异 \\(draft 与上一审阅版一致\\)",
                        parse_mode="MarkdownV2",
                    )
                except Exception:
                    pass
            else:
                body = "```diff\n" + "\n".join(diff_lines)[:3500] + "\n```"
                try:
                    tg_client.send_message(
                        chat_id, body, parse_mode="MarkdownV2",
                    )
                except Exception:
                    try:
                        tg_client.send_message(
                            chat_id,
                            "\n".join(diff_lines)[:3500],
                            parse_mode=None,
                        )
                    except Exception:
                        pass
        _audit({
            "kind": "callback_action_done",
            "gate": "B",
            "action": "diff",
            "short_id": sid,
            "uid": uid,
            "article_id": article_id,
            "diff_line_count": len(diff_lines),
        })
        return

    # ── C:full — send the original 2k cover.png as a Telegram document ────
    if gate == "C" and action == "full" and article_id:
        cover_path: Path | None = None
        for candidate in (
            agentflow_home() / "drafts" / article_id / "cover.png",
            agentflow_home() / "drafts" / article_id / "cover_2k.png",
            agentflow_home() / "images" / article_id / "cover.png",
        ):
            if candidate.exists():
                cover_path = candidate
                break
        if cover_path is None:
            try:
                meta = json.loads(
                    (agentflow_home() / "drafts" / article_id / "metadata.json")
                    .read_text(encoding="utf-8")
                )
                for ph in meta.get("image_placeholders") or []:
                    rp = ph.get("resolved_path")
                    if rp and Path(rp).exists():
                        cover_path = Path(rp)
                        break
            except Exception:
                pass
        if cover_path is None or not cover_path.exists():
            tg_client.answer_callback_query(
                cb_id, text="未找到 cover 文件", show_alert=True,
            )
            return
        tg_client.answer_callback_query(cb_id, text="🖼 全分辨率发送中…")
        if chat_id is not None:
            try:
                tg_client.send_document(
                    chat_id, cover_path,
                    caption=f"🖼 cover (full): {cover_path.name}",
                    parse_mode=None,
                )
            except Exception as err:
                _log.warning("C:full send_document failed: %s", err)
        _audit({
            "kind": "callback_action_done",
            "gate": "C",
            "action": "full",
            "short_id": sid,
            "uid": uid,
            "article_id": article_id,
            "cover_path": str(cover_path),
        })
        return

    # ── *:defer — schedule a re-post N hours later via the deferred-repost
    # store. Sweeper picks it up in _scan_timeouts.
    if action == "defer" and gate in {"A", "B", "C"}:
        hours = _parse_defer_hours(extra)
        if hours is None or hours <= 0:
            tg_client.answer_callback_query(
                cb_id, text="defer 参数缺失", show_alert=True,
            )
            return
        target = article_id or entry.get("batch_path")
        if not target:
            tg_client.answer_callback_query(
                cb_id, text="无 defer 目标 (article_id/batch_path 缺失)",
                show_alert=True,
            )
            return
        try:
            _schedule_deferred_repost(
                gate=gate,
                article_id=article_id,
                batch_path=entry.get("batch_path"),
                hours=float(hours),
                source_sid=sid,
            )
        except Exception as err:
            _log.warning("defer schedule failed: %s", err)
            tg_client.answer_callback_query(
                cb_id, text=f"defer 调度失败: {err}"[:180], show_alert=True,
            )
            return
        tg_client.answer_callback_query(
            cb_id, text=f"⏰ 已 defer {hours}h, 到期会重发卡片",
        )
        if chat_id is not None and message_id is not None:
            try:
                tg_client.edit_message_reply_markup(
                    chat_id, message_id, reply_markup={},
                )
            except Exception:
                pass
        if chat_id is not None:
            label = article_id or entry.get("batch_path") or "?"
            try:
                tg_client.send_message(
                    chat_id,
                    f"⏰ Gate {gate} 已 defer {hours}h\n"
                    f"target: {label}",
                    parse_mode=None,
                )
            except Exception:
                pass
        _audit({
            "kind": "callback_action_done",
            "gate": gate,
            "action": "defer",
            "short_id": sid,
            "uid": uid,
            "article_id": article_id,
            "batch_path": entry.get("batch_path"),
            "hours": hours,
        })
        return

    # Defer / expand / diff / full / regen / relogo are still stubs — these
    # are usability sugar, not blocking the core publish loop.
    tg_client.answer_callback_query(
        cb_id, text=f"{action} 已记录（动作链下一轮接通）", show_alert=False
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


_STOP = threading.Event()


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
        if chat_id is None:
            return
        rc = "timeout/oserr" if returncode is None else str(returncode)
        tail = (stderr_tail or "").strip()
        tail = tail[-500:] if tail else "(no stderr)"
        text = (
            f"❌ {label} 失败  ·  target={target_id} exit={rc}\n\n"
            f"{tail}"
        )
        # parse_mode=None avoids Markdown-escape headaches for arbitrary stderr.
        tg_client.send_message(chat_id, text, parse_mode=None)
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


def _spawn_edit(article_id: str | None, instruction: str, chat_id: int | str | None) -> None:
    """Parse an inbound edit reply, run `af edit`, and post a fresh Gate B.

    Scope tokens supported:
        - ``title`` / ``opening`` / ``closing`` — meta-level rewrites
        - non-negative integer N — section-level rewrite (legacy)
    """
    import re as _re
    import subprocess
    import sys

    if not article_id or not instruction:
        return
    text = instruction.strip()
    m = _re.match(
        r"^(title|opening|closing|\d+)\s+(.*)$",
        text,
        flags=_re.DOTALL | _re.IGNORECASE,
    )
    if not m:
        if chat_id is not None:
            try:
                tg_client.send_message(
                    chat_id,
                    "格式不对\\. 请回复:\n"
                    "`<scope> <改写指令>`\n"
                    "scope 可以是 `title` / `opening` / `closing` / 第几节的整数 \\(0\\-based\\)\\.\n"
                    "例: `title 标题再尖锐一点` / `2 第二节改得更口语化`",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass
        return
    scope, body = m.group(1).lower(), m.group(2).strip()
    if scope in {"title", "opening", "closing"}:
        edit_args = ["--target", scope, "--command", body]
    else:
        edit_args = ["--section", scope, "--command", body]

    def _run() -> None:
        try:
            from agentflow.agent_review.triggers import _af_argv
            try:
                result = subprocess.run(
                    _af_argv("edit", article_id, *edit_args),
                    env=os.environ.copy(),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            except subprocess.TimeoutExpired:
                _notify_spawn_failure("edit", article_id or "?", None, "timeout")
                return
            if result.returncode != 0:
                _notify_spawn_failure(
                    "edit",
                    article_id or "?",
                    result.returncode,
                    (result.stderr or "").strip(),
                )
                return
            # After edit, re-trigger Gate B so the user sees the new version.
            from agentflow.agent_review import triggers as _triggers
            _triggers.post_gate_b(article_id)
        except Exception as err:  # pragma: no cover
            _log.warning("edit subprocess crashed for %s: %s", article_id, err)
            _notify_spawn_failure("edit", article_id or "?", None, str(err))

    threading.Thread(target=_run, daemon=True).start()


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


def _spawn_hotspots(top_k: int = 3) -> None:
    """Run `af hotspots` in a background thread.

    The CLI auto-triggers `post_gate_a` on completion (existing behaviour),
    so this fn just shells out and surfaces failure via _notify_spawn_failure.
    """
    import subprocess

    def _run() -> None:
        try:
            from agentflow.agent_review.triggers import _af_argv
            try:
                result = subprocess.run(
                    _af_argv("hotspots", "--gate-a-top-k", str(top_k)),
                    env=os.environ.copy(),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                _notify_spawn_failure("hotspots", "manual_scan", None, "timeout 300s")
                return
            if result.returncode != 0:
                _notify_spawn_failure(
                    "hotspots",
                    "manual_scan",
                    result.returncode,
                    (result.stderr or "").strip(),
                )
        except Exception as err:  # pragma: no cover
            _log.warning("hotspots subprocess crashed: %s", err)
            _notify_spawn_failure("hotspots", "manual_scan", None, str(err))

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
        chat_id = get_review_chat_id()
        if chat_id is None:
            return
        tg_client.send_message(
            chat_id,
            f"⚠ daemon 重启了\\. 上次 heartbeat: `{_render.escape_md2(ts[:19])}` "
            f"\\({gap_minutes/60:.1f}h ago\\)\\. "
            "期间所有 TG 按键都已被 Telegram 投递但 daemon 未响应——"
            "用 /list 查看哪些卡片还在等操作\\.",
        )
        _log.warning(
            "daemon downtime detected: %.1fh gap since last heartbeat",
            gap_minutes / 60.0,
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
    try:
        chat_id = get_review_chat_id()
        if chat_id is not None:
            tg_client.send_message(
                chat_id,
                "⚠ *Mock mode active*\n"
                + "\n".join(f"• `{_render.escape_md2(f)}`" for f in flags)
                + "\n\nUnset these in `.env` and restart the daemon for production runs\\.",
            )
    except Exception:
        pass


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

    interval = poll_interval if poll_interval is not None else float(
        os.environ.get("TELEGRAM_POLL_INTERVAL_SECONDS", "5")
    )
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    me = tg_client.get_me()
    _log.info("review daemon started for @%s", me.get("username"))
    chat_id = get_review_chat_id()
    configure_bot_menu(chat_id)
    if chat_id is None:
        _log.warning(
            "no TELEGRAM_REVIEW_CHAT_ID configured — send /start to @%s "
            "in Telegram to capture it",
            me.get("username"),
        )
    else:
        _log.info("review chat_id=%s", chat_id)

    # Surface non-fatal warnings BEFORE we start consuming updates so the
    # operator sees them immediately. Keep these best-effort — a TG hiccup
    # must not prevent the daemon from coming up.
    _detect_downtime_and_notify()
    _warn_if_mock_mode_active()

    offset_state = _read_json(_offset_path(), {}) or {}
    offset = offset_state.get("offset")

    last_gc = time.monotonic()
    last_timeout_scan = time.monotonic()

    while not _STOP.is_set():
        _write_heartbeat()
        try:
            updates = tg_client.get_updates(offset=offset, timeout=25)
        except Exception as err:
            _log.warning("get_updates failed: %s", err)
            time.sleep(min(interval * 2, 15))
            continue

        for upd in updates:
            try:
                if "message" in upd:
                    _handle_message(upd)
                elif "callback_query" in upd:
                    _handle_callback(upd)
            except Exception as err:  # pragma: no cover
                _log.exception("update handler crashed: %s", err)
            offset = int(upd["update_id"]) + 1
            _write_json(_offset_path(), {"offset": offset})

        # housekeeping every ~60s
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
            last_timeout_scan = now

        if not updates:
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

    chat_id = get_review_chat_id()
    now = datetime.now(timezone.utc)

    def _safe_send(text: str) -> bool:
        if chat_id is None:
            return False
        try:
            tg_client.send_message(chat_id, text, parse_mode="MarkdownV2")
            return True
        except Exception as err:  # pragma: no cover
            _log.warning("timeout sweeper TG send failed: %s", err)
            return False

    def _title_of(aid: str) -> str:
        try:
            data = json.loads(
                (agentflow_home() / "drafts" / aid / "metadata.json")
                .read_text(encoding="utf-8")
            ) or {}
            return str(data.get("title") or "(no title)")
        except Exception:
            return "(no title)"

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
        title = _title_of(aid)
        title_md = _render.escape_md2(title)
        aid_md = _render.escape_md2(aid)

        if cur == state.STATE_DRAFT_PENDING_REVIEW:
            if hrs >= b_first and not timeout_state.has_first_pinged(aid):
                text = (
                    f"⏰ 12h\\+ 未审\n\n"
                    f"*{title_md}*\n"
                    f"`{aid_md}`\n"
                    f"请处理 Gate B 卡片\\."
                )
                if _safe_send(text):
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
                text = (
                    f"⏰ 已等 24h\\+，请尽快处理或显式拒绝\n\n"
                    f"*{title_md}*\n"
                    f"`{aid_md}`"
                )
                if _safe_send(text):
                    timeout_state.mark_second_action(aid)
                    _audit({
                        "kind": "timeout_second_ping",
                        "gate": "B",
                        "article_id": aid,
                        "hrs": round(hrs, 2),
                    })

        elif cur == state.STATE_IMAGE_PENDING_REVIEW:
            if hrs >= c_first and not timeout_state.has_first_pinged(aid):
                text = (
                    f"⏰ Cover 6h\\+ 未审\n\n"
                    f"*{title_md}*\n"
                    f"`{aid_md}`\n"
                    f"请处理 Gate C 卡片\\."
                )
                if _safe_send(text):
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
                _safe_send(
                    f"⏰ Cover 12h 未审 → 自动 fallback 到无封面，state→image\\_skipped\n\n"
                    f"*{title_md}*\n"
                    f"`{aid_md}`"
                )
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
                # Attach a [⏰ 再延 12h] extend button (Q6). Mint a fresh
                # sid for the extend callback (12h TTL — matches the new
                # extension window). The handler enforces the per-article
                # 1-extension cap via timeout_state.extended_count.
                extend_text = (
                    f"⏰ Gate D 12h 未确认 → 自动 cancel, state→image\\_approved\n\n"
                    f"*{title_md}*\n"
                    f"`{aid_md}`\n\n"
                    f"想继续？点击 ⏰ 延 12h（限 1 次）。"
                )
                extend_kb: dict[str, Any] | None = None
                try:
                    extend_sid = _sid.register(
                        gate="D",
                        article_id=aid,
                        ttl_hours=12,
                        extra={"extend_only": True},
                    )
                    extend_kb = {
                        "inline_keyboard": [[
                            {
                                "text": "⏰ 再延 12h",
                                "callback_data": f"D:extend:{extend_sid}",
                            }
                        ]]
                    }
                except Exception as err:
                    _log.warning(
                        "Gate D extend sid mint failed for %s: %s", aid, err
                    )
                if chat_id is not None and extend_kb is not None:
                    try:
                        tg_client.send_message(
                            chat_id, extend_text, reply_markup=extend_kb,
                            parse_mode="MarkdownV2",
                        )
                    except Exception as err:
                        _log.warning(
                            "Gate D auto_cancel extend ping failed for %s: %s",
                            aid, err,
                        )
                        _safe_send(extend_text)
                else:
                    _safe_send(extend_text)
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
        batch_md = _render.escape_md2(str(batch_path) or "(no batch)")

        if ga_hrs >= a_first and not first_pinged:
            text = (
                f"⏰ Gate A 12h\\+ 未审，请处理 hotspots batch\n\n"
                f"`{batch_md}`"
            )
            if _safe_send(text):
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
            _safe_send(
                "⏰ Gate A 24h 未审 → 整批自动拒绝（重新跑 `af hotspots` 可恢复）\n\n"
                f"`{batch_md}`"
            )
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
