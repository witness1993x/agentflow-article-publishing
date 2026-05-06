"""Lark Custom Bot webhook fan-out (v1.0.19, path A).

Non-interactive outbound notifications. Lark Custom Bot is push-only —
button clicks can only deep-link to URLs, not call back to a server.
The HITL review loop continues to live on Telegram (`tg_client.py`);
this module sends summary cards (digest, dispatch result, publish-ready,
spawn failures) to a Lark group chat in addition.

Future v2: when migrating to a Lark "self-built application" (机器人应用)
to get callback support, the card builders here become the seed
for `agent_review/lark_render.py`. Keep the public API minimal and
tg-symmetric (`send_text` / `send_card`) so consumers don't care about
the future swap.

Configuration (env-driven, opt-in — empty URL = scheduler off):

* ``LARK_WEBHOOK_URL`` — full webhook URL from Lark group bot setup.
  Empty/unset → all calls are no-ops.
* ``LARK_WEBHOOK_SECRET`` — optional. When set, every request includes
  ``timestamp`` + ``sign`` (HmacSHA256(timestamp + "\n" + secret) → b64).
* ``LARK_WEBHOOK_KEYWORDS`` — optional comma-separated keywords. The
  module will append the first keyword to text bodies missing them
  so the bot's "自定义关键词" security setting doesn't drop posts.

Security / rate-limit guards:

* Body size hard-capped at 19_000 bytes (Lark cap is 20K; we leave
  headroom for sign + timestamp).
* Posts within ±60s of HH:00 / HH:30 are deferred 90s to dodge the
  documented 11232 系统压力限流. Operators can override with
  ``LARK_WEBHOOK_NO_DEFER=true``.
* Per-call exception-isolated; never raises into the caller's hot path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from agentflow.shared.logger import get_logger


_log = get_logger("shared.lark_webhook")

_BODY_HARD_CAP_BYTES = 19_000
_DEFER_DODGE_SECONDS = 60     # how close to HH:00 / HH:30 counts as "rate-limit zone"
_DEFER_TARGET_OFFSET = 90     # how long to wait when in the zone

# v1.0.20 — env-driven defaults for the per-card text-trim caps. Operators
# tune these without code changes when stderr is unusually verbose or when
# they want every reason inline.
_DEFAULT_REASON_MAXLEN = 80
_DEFAULT_STDERR_MAXLEN = 500


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _reason_maxlen() -> int:
    return max(20, _int_env("LARK_WEBHOOK_REASON_MAXLEN", _DEFAULT_REASON_MAXLEN))


def _stderr_maxlen() -> int:
    return max(80, _int_env("LARK_WEBHOOK_STDERR_MAXLEN", _DEFAULT_STDERR_MAXLEN))


def _brand_prefix() -> str:
    raw = (os.environ.get("LARK_WEBHOOK_BRAND_PREFIX") or "").strip()
    if not raw:
        return ""
    # Normalise "[ChainStream]" / "ChainStream" / "[ChainStream] " all to
    # "[ChainStream] " (with one trailing space) so titles read consistently.
    inner = raw.strip("[] ")
    return f"[{inner}] " if inner else ""


def _tg_bot_url() -> str:
    return (os.environ.get("LARK_WEBHOOK_TG_BOT_URL") or "").strip()


def _dashboard_url(article_id: str) -> str:
    """Render the dashboard URL for an article when a template is configured.

    Template form: ``https://dash.example.com/article/{article_id}``.
    Returns "" when no template / format error.
    """
    tmpl = (os.environ.get("LARK_WEBHOOK_DASHBOARD_URL_TEMPLATE") or "").strip()
    if not tmpl or not article_id:
        return ""
    try:
        return tmpl.format(article_id=article_id)
    except (KeyError, IndexError, ValueError):
        return ""

# Single-process serialization so back-to-back fan-outs don't blow the
# documented 5-per-second cap. Lark allows 100/min, 5/s; we keep a soft
# floor of 220ms between calls in this process.
_SEND_LOCK = threading.Lock()
_MIN_INTERVAL_SECONDS = 0.22
_last_send_at: float = 0.0


def _is_configured() -> bool:
    return bool(os.environ.get("LARK_WEBHOOK_URL", "").strip())


def _lark_app_primary() -> bool:
    raw = (os.environ.get("AGENTFLOW_LARK_APP_PRIMARY") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _emit_notify_event(
    event_type: str,
    *,
    article_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        from agentflow.shared.agent_bridge import emit_agent_event

        emit_agent_event(
            source="agentflow.lark_notify",
            event_type=f"notify.{event_type}",
            article_id=article_id,
            payload=payload or {},
        )
    except Exception:  # pragma: no cover — notification fan-out is best-effort
        _log.warning("agent notify event emit failed: %s", event_type, exc_info=True)


def _sign(timestamp: int, secret: str) -> str:
    """HmacSHA256(timestamp + "\\n" + secret) → b64. Per Lark Custom Bot
    spec: the body data passed to HMAC is empty; the secret used as KEY
    is the concatenation, and we then b64-encode the digest."""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        b"",
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _ensure_keyword(text: str, keywords: Iterable[str]) -> str:
    """If the configured Lark bot has '自定义关键词' enabled, posts
    without any of the listed keywords get rejected with code 19024.
    Append the first keyword to the body when none are present so a
    misconfigured / forgotten keyword doesn't silently drop notifications.
    """
    kws = [k.strip() for k in keywords if k and k.strip()]
    if not kws:
        return text
    if any(kw in text for kw in kws):
        return text
    return f"{text}\n\n[{kws[0]}]"


def _in_rate_limit_zone(now: datetime) -> bool:
    """True if ``now`` is within ±_DEFER_DODGE_SECONDS of HH:00 or HH:30.
    Lark's docs explicitly call out 10:00 / 17:30 as common system-load
    spikes that produce 11232 errors; we generalize to any half-hour."""
    minute = now.minute
    second = now.second
    seconds_into_half = (minute % 30) * 60 + second
    distance_to_half = min(seconds_into_half, 30 * 60 - seconds_into_half)
    return distance_to_half <= _DEFER_DODGE_SECONDS


def _truncate(payload: dict[str, Any]) -> dict[str, Any]:
    """Lark caps request body at 20KB. Truncate the longest text field
    in-place rather than refusing the post. Idempotent."""
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode("utf-8")) <= _BODY_HARD_CAP_BYTES:
        return payload
    # Find the longest .content.text or .card.elements[*].text.content
    # and trim it. Crude but adequate for v1.
    cap_marker = "\n\n…(truncated for Lark 20K cap)"
    if isinstance(payload.get("content"), dict):
        text = payload["content"].get("text")
        if isinstance(text, str) and len(text) > 200:
            keep = max(200, _BODY_HARD_CAP_BYTES // 2)
            payload["content"]["text"] = text[:keep] + cap_marker
            return _truncate(payload)
    return payload


def _post(payload: dict[str, Any]) -> None:
    """Sign + size-guard + rate-limit-aware HTTP POST. Best-effort,
    never raises into the caller. Returns silently on non-2xx so a
    flaky Lark endpoint can't break the TG-primary review loop."""
    url = os.environ.get("LARK_WEBHOOK_URL", "").strip()
    if not url:
        return
    secret = os.environ.get("LARK_WEBHOOK_SECRET", "").strip()
    keywords = (os.environ.get("LARK_WEBHOOK_KEYWORDS") or "").split(",")

    # Keyword guard for text bodies (post bodies have their own structure;
    # we leave those alone).
    if payload.get("msg_type") == "text":
        text = payload.get("content", {}).get("text", "")
        payload["content"]["text"] = _ensure_keyword(text, keywords)

    # Optional defer to dodge the integer-half-hour 11232 limit.
    no_defer = (os.environ.get("LARK_WEBHOOK_NO_DEFER") or "").strip().lower() == "true"
    if not no_defer:
        now = datetime.now()
        if _in_rate_limit_zone(now):
            time.sleep(_DEFER_TARGET_OFFSET)

    if secret:
        ts = int(time.time())
        payload = {**payload, "timestamp": str(ts), "sign": _sign(ts, secret)}

    payload = _truncate(payload)

    # Per-process interval guard.
    global _last_send_at
    with _SEND_LOCK:
        elapsed = time.monotonic() - _last_send_at
        if elapsed < _MIN_INTERVAL_SECONDS:
            time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        _last_send_at = time.monotonic()

    try:
        import requests
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            _log.warning(
                "Lark webhook POST non-2xx: %s %s",
                resp.status_code, resp.text[:200],
            )
            return
        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        code = body.get("code")
        if code not in (0, None):
            _log.warning(
                "Lark webhook returned code=%s msg=%s",
                code, body.get("msg"),
            )
    except Exception as err:  # pragma: no cover — best-effort
        _log.warning("Lark webhook send failed: %s", err)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_text(text: str) -> None:
    """Plain text notification. No-op when LARK_WEBHOOK_URL unset."""
    if not _is_configured():
        return
    _post({"msg_type": "text", "content": {"text": text}})


def send_card(
    *,
    title: str,
    body_md: str,
    url_actions: list[tuple[str, str]] | None = None,
    accent: str = "blue",
) -> None:
    """Interactive card with optional URL buttons.

    ``url_actions`` is a list of (label, url) tuples. Lark Custom Bot
    only supports URL buttons — no callback. When the operator clicks,
    the URL opens in their default browser. The button labels follow
    Lark's `lark_md` convention so emoji renders cleanly. Empty / None
    URL entries are dropped silently so callers don't have to gate.

    v1.0.20: prepends ``LARK_WEBHOOK_BRAND_PREFIX`` to the title when set.
    """
    if not _is_configured():
        return

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"content": body_md, "tag": "lark_md"},
        }
    ]
    cleaned_actions = [
        (label, url) for label, url in (url_actions or []) if label and url
    ]
    if cleaned_actions:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"content": label, "tag": "lark_md"},
                    "url": url,
                    "type": "default",
                }
                for label, url in cleaned_actions
            ],
        })

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "content": _brand_prefix() + title,
                    "tag": "plain_text",
                },
                "template": accent,
            },
            "elements": elements,
        },
    }
    _post(payload)


# ---------------------------------------------------------------------------
# Convenience builders for the common AgentFlow notification shapes.
# Triggers in agent_review/triggers.py call these instead of building
# cards inline so the visual style stays consistent.
# ---------------------------------------------------------------------------


def notify_dispatch_result(
    *,
    article_id: str,
    title: str,
    succeeded: list[str],
    failed: list[tuple[str, str]],
) -> None:
    """Post-D4 fan-out: who got the article, who didn't."""
    if _lark_app_primary():
        _emit_notify_event(
            "dispatch_result",
            article_id=article_id,
            payload={
                "title": title,
                "succeeded": list(succeeded),
                "failed": [{"platform": p, "reason": r} for p, r in failed],
            },
        )
        return
    if not _is_configured():
        return
    if failed:
        accent = "red" if not succeeded else "orange"
        status = f"{len(succeeded)}/{len(succeeded) + len(failed)} 平台成功"
    else:
        accent = "green"
        status = f"全部 {len(succeeded)} 平台成功"
    reason_cap = _reason_maxlen()
    body_lines = [
        f"**{title}**",
        f"`{article_id}`",
        "",
        f"📤 {status}",
    ]
    if succeeded:
        body_lines.append("")
        body_lines.append("**已发布**:")
        for plat in succeeded:
            body_lines.append(f"- ✅ {plat}")
    if failed:
        body_lines.append("")
        body_lines.append("**未发布**:")
        for plat, reason in failed:
            r = reason if len(reason) <= reason_cap else reason[: reason_cap - 1] + "…"
            body_lines.append(f"- ❌ {plat} — {r}")
    actions: list[tuple[str, str]] = []
    if failed and not _lark_app_primary():
        # Legacy Custom Bot mode only: nudge to TG. In Lark-app-primary
        # mode the OpenClaw-rendered notify.dispatch_result card already
        # carries Lark-native retry buttons, so a TG link would just split
        # operator attention.
        actions.append(("🔁 去 TG 重试 / 处理", _tg_bot_url()))
    actions.append(("📊 查看 draft", _dashboard_url(article_id)))
    send_card(
        title="📤 AgentFlow · 发布结果",
        body_md="\n".join(body_lines),
        accent=accent,
        url_actions=actions,
    )


def notify_publish_ready(*, article_id: str, title: str) -> None:
    """Medium-only branch: an article is ready for the operator's
    manual paste step. URL-only nudge, no actionable button (the
    real button lives on the TG side as PR:mark)."""
    if _lark_app_primary():
        _emit_notify_event(
            "publish_ready",
            article_id=article_id,
            payload={"title": title},
        )
        return
    if not _is_configured():
        return
    body = (
        f"**{title}**\n"
        f"`{article_id}`\n\n"
        f"等待 operator 在 Telegram bot 点 [📌 我已粘贴] 并回 Medium URL."
    )
    actions: list[tuple[str, str]] = []
    if not _lark_app_primary():
        actions.append(("📌 去 TG 标记", _tg_bot_url()))
    actions.append(("📊 查看 draft", _dashboard_url(article_id)))
    send_card(
        title="📌 AgentFlow · 待 publish-mark",
        body_md=body,
        accent="blue",
        url_actions=actions,
    )


def notify_hotspots_digest(*, scan_count: int, top_titles: list[str]) -> None:
    """Daily 09:00 / 20:00 scheduled scan completed."""
    if _lark_app_primary():
        _emit_notify_event(
            "hotspots_digest",
            payload={"scan_count": scan_count, "top_titles": list(top_titles)},
        )
        return
    if not _is_configured():
        return
    if scan_count == 0:
        body = "今日扫描完成: 暂无可写热点 (上游空 / filter 过窄 / twitter quota)."
        accent = "grey"
        actions: list[tuple[str, str]] = []
    else:
        lines = [f"扫到 {scan_count} 个热点, top {len(top_titles)}:", ""]
        for i, t in enumerate(top_titles, 1):
            lines.append(f"{i}. {t}")
        lines.append("")
        lines.append(
            "这是 legacy Custom Bot 扫描摘要，不是 Lark 审核卡。"
        )
        lines.append(
            "若要在 Lark 内审核，请启用 AGENTFLOW_LARK_APP_PRIMARY=true "
            "并监听 review.gate_a_card。"
        )
        body = "\n".join(lines)
        accent = "orange"
        actions = []
        tg_url = _tg_bot_url()
        if tg_url:
            actions.append(("📝 legacy TG 审核", tg_url))
    send_card(
        title="🔎 AgentFlow · 今日热点扫描",
        body_md=body,
        accent=accent,
        url_actions=actions,
    )


def notify_draft_ready(
    *,
    article_id: str,
    title: str,
    draft_md_path: str | None = None,
    draft_md: str | None = None,
    mirror_url: str | None = None,
    audit_summary: str | None = None,
) -> None:
    """v1.0.30 — Gate B fan-out: send the assembled draft body to Lark.

    Lark Custom Bot has a 20 KB body cap. For drafts under
    ``_BODY_HARD_CAP_BYTES`` we send the full markdown inside an
    interactive card. For larger drafts we send a header card with the
    first ~1500 characters and a "完整稿件" button pointing at
    ``mirror_url`` (a read-only mirror — typically the dashboard's
    draft preview, or the operator's intranet markdown server).

    In legacy Custom Bot mode this remains push-only. In Lark App primary
    mode (``AGENTFLOW_LARK_APP_PRIMARY=true``), this function emits a
    ``notify.draft_ready`` agent event so OpenClaw can render the actionable
    Gate B card.

    Behavior is gated by ``AGENTFLOW_LARK_DRAFT_FANOUT`` (default off).
    Gate B state transitions remain the source of truth; the notification
    surface can be Lark-first with TG as fallback.
    """
    if _lark_app_primary():
        md = draft_md
        if md is None and draft_md_path:
            try:
                with open(draft_md_path, "r", encoding="utf-8") as fh:
                    md = fh.read()
            except OSError:
                md = None
        md = (md or "").strip()
        _emit_notify_event(
            "draft_ready",
            article_id=article_id,
            payload={
                "title": title,
                "audit_summary": audit_summary,
                "mirror_url": mirror_url,
                "draft_md_path": draft_md_path,
                "draft_excerpt": md[:2000],
                "draft_length": len(md),
                "draft_truncated": len(md) > 2000,
            },
        )
        return
    if not _is_configured():
        return
    if not _draft_fanout_enabled():
        return

    md = draft_md
    if md is None and draft_md_path:
        try:
            with open(draft_md_path, "r", encoding="utf-8") as fh:
                md = fh.read()
        except OSError as err:
            _log.warning("notify_draft_ready: cannot read %s: %s", draft_md_path, err)
            return
    md = (md or "").strip()
    if not md:
        _log.info("notify_draft_ready: empty draft, nothing to fan out (id=%s)", article_id)
        return

    # Header is fixed-cost. The actual budget for the body is harder cap
    # minus header / metadata / button overhead — be generous (~2 KB).
    body_budget = _BODY_HARD_CAP_BYTES - 2_000
    md_bytes = md.encode("utf-8")
    truncated = len(md_bytes) > body_budget

    if truncated:
        # Slice on character boundary, not byte, so we don't cut mid-CJK.
        approx_chars = max(800, body_budget // 3)
        slice_text = md[:approx_chars]
        body_md = (
            f"**{title}**\n"
            f"`{article_id}`"
            + (f"  ·  {audit_summary}" if audit_summary else "")
            + "\n\n"
            f"{slice_text}\n\n"
            f"…（截断 · 完整 {len(md):,} 字 / {len(md_bytes):,} bytes）"
        )
        accent = "blue"
    else:
        body_md = (
            f"**{title}**\n"
            f"`{article_id}`"
            + (f"  ·  {audit_summary}" if audit_summary else "")
            + "\n\n---\n\n"
            f"{md}"
        )
        accent = "green"

    actions: list[tuple[str, str]] = []
    if mirror_url:
        actions.append(("📄 完整稿件", mirror_url))
    if not _lark_app_primary():
        # In app-primary mode, the actionable Gate B card is rendered by
        # OpenClaw from the notify.draft_ready event — no TG redirect needed.
        actions.append(("📌 去 TG 审稿", _tg_bot_url()))
    actions.append(("📊 查看 draft", _dashboard_url(article_id)))

    send_card(
        title="📝 AgentFlow · 稿件就绪 (Gate B)",
        body_md=body_md,
        accent=accent,
        url_actions=actions,
    )


def _draft_fanout_enabled() -> bool:
    raw = os.environ.get("AGENTFLOW_LARK_DRAFT_FANOUT", "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


def notify_spawn_failure(*, label: str, target_id: str, error_tail: str) -> None:
    """Mirror of daemon._notify_spawn_failure into Lark for the on-call
    channel. Operator sees both TG and Lark; whichever they monitor first
    triggers triage."""
    if _lark_app_primary():
        tail = error_tail[-_stderr_maxlen():] if error_tail else "(no stderr)"
        _emit_notify_event(
            "spawn_failure",
            article_id=target_id,
            payload={"label": label, "target_id": target_id, "error_tail": tail},
        )
        return
    if not _is_configured():
        return
    tail = error_tail[-_stderr_maxlen():] if error_tail else "(no stderr)"
    body = (
        f"`{label}` 失败 · target=`{target_id}`\n\n"
        f"```\n{tail}\n```"
    )
    actions: list[tuple[str, str]] = []
    if not _lark_app_primary():
        actions.append(("🔧 去 TG 看详情", _tg_bot_url()))
    actions.append(("📊 查看 draft", _dashboard_url(target_id)))
    send_card(
        title="❌ AgentFlow · 子任务失败",
        body_md=body,
        accent="red",
        url_actions=actions,
    )
