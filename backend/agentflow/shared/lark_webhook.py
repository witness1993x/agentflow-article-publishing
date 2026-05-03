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
    if failed:
        # Only nudge to TG when there's something the operator must act on.
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
    if not _is_configured():
        return
    body = (
        f"**{title}**\n"
        f"`{article_id}`\n\n"
        f"等待 operator 在 Telegram bot 点 [📌 我已粘贴] 并回 Medium URL."
    )
    send_card(
        title="📌 AgentFlow · 待 publish-mark",
        body_md=body,
        accent="blue",
        url_actions=[
            ("📌 去 TG 标记", _tg_bot_url()),
            ("📊 查看 draft", _dashboard_url(article_id)),
        ],
    )


def notify_hotspots_digest(*, scan_count: int, top_titles: list[str]) -> None:
    """Daily 09:00 / 20:00 scheduled scan completed."""
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
        lines.append("Gate A 卡已推送到 Telegram 待审核.")
        body = "\n".join(lines)
        accent = "green"
        # Only show "去选题" when there's actually something to pick.
        actions = [("📝 去 TG 选题", _tg_bot_url())]
    send_card(
        title="🔎 AgentFlow · 今日热点扫描",
        body_md=body,
        accent=accent,
        url_actions=actions,
    )


def notify_spawn_failure(*, label: str, target_id: str, error_tail: str) -> None:
    """Mirror of daemon._notify_spawn_failure into Lark for the on-call
    channel. Operator sees both TG and Lark; whichever they monitor first
    triggers triage."""
    if not _is_configured():
        return
    tail = error_tail[-_stderr_maxlen():] if error_tail else "(no stderr)"
    body = (
        f"`{label}` 失败 · target=`{target_id}`\n\n"
        f"```\n{tail}\n```"
    )
    send_card(
        title="❌ AgentFlow · 子任务失败",
        body_md=body,
        accent="red",
        url_actions=[
            ("🔧 去 TG 看详情", _tg_bot_url()),
            ("📊 查看 draft", _dashboard_url(target_id)),
        ],
    )
