"""Render Gate cards by loading file-based templates + substituting fields.

Card text lives in ``templates/cards/*.tmpl`` (locked, edit those to change
the prompt structure). This module:
- escapes Markdown V2 reserved chars on every dynamic value
- pre-renders variable-length blocks (candidate list, check lines) into a
  single substitution key so the wrapper template stays fixed
- builds the inline keyboard separately (callback_data is short_id-based,
  produced fresh per post)

Public API:
- escape_md2(text)
- render_gate_a(...)
- render_gate_b(...)
- render_gate_c(...)
- render_gate_d(...)
- render_publish_ready(...)
- export_body_markdown(article_id)
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agentflow.agent_review import short_id as _sid
from agentflow.shared.bootstrap import agentflow_home


def _gate_b_ttl_hours() -> float:
    try:
        return float(os.environ.get("REVIEW_GATE_B_SECOND_PING_HOURS", "") or 24)
    except ValueError:
        return 24


def _gate_c_ttl_hours() -> float:
    try:
        return float(os.environ.get("REVIEW_GATE_C_AUTOSKIP_HOURS", "") or 12)
    except ValueError:
        return 12


# ---------------------------------------------------------------------------
# Markdown V2 escape + template loader
# ---------------------------------------------------------------------------

_MD2_RESERVED = r"_*[]()~`>#+-=|{}.!"
_MD2_RESERVED_RE = re.compile("[" + re.escape(_MD2_RESERVED) + "]")

_TEMPLATE_DIR = Path(__file__).parent / "templates" / "cards"
_TEMPLATE_CACHE: dict[str, str] = {}


def escape_md2(text: Any) -> str:
    """Backslash-escape every reserved MarkdownV2 char.

    Apply to dynamic values BEFORE substituting them into a template. Never
    apply to a fully-rendered card (that double-escapes the markup).
    """
    if text is None:
        return ""
    return _MD2_RESERVED_RE.sub(lambda m: "\\" + m.group(0), str(text))


def _load_template(name: str) -> str:
    if name not in _TEMPLATE_CACHE:
        path = _TEMPLATE_DIR / name
        if not path.exists():
            raise FileNotFoundError(f"template not found: {path}")
        _TEMPLATE_CACHE[name] = path.read_text(encoding="utf-8")
    return _TEMPLATE_CACHE[name]


def _substitute(template: str, values: dict[str, Any]) -> str:
    """Simple ``{key}`` substitution. Mirrors agentflow.agent_d2.section_filler._render."""
    out = template
    out = out.replace("{{", "\x00LB\x00").replace("}}", "\x00RB\x00")
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val) if val is not None else "")
    return out.replace("\x00LB\x00", "{").replace("\x00RB\x00", "}")


def _kb(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """Build inline_keyboard reply_markup from rows of (label, callback_data)."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": cb} for label, cb in row]
            for row in rows
        ]
    }


def _now_label() -> str:
    return escape_md2(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"))


# ---------------------------------------------------------------------------
# Gate A — Topic Selection Review
# ---------------------------------------------------------------------------


def render_gate_a(
    *,
    publisher_brand: str,
    target_series: str,
    candidates: list[dict[str, Any]],
    batch_path: str,
    round_summary: dict[str, Any] | None = None,
    worth_reviewing: list[dict[str, Any]] | None = None,
    config_suggestions: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any], str]:
    """Returns (text, reply_markup, short_id)."""
    sid = _sid.register(gate="A", batch_path=batch_path, ttl_hours=24)
    lines: list[str] = [
        "🧭 *Hotspot Decision Panel*",
        "",
        f"*Timestamp*  { _now_label() }",
        f"*Publisher*  {escape_md2(publisher_brand)}",
        f"*Target Series*  {escape_md2(target_series)}",
        f"*Candidates*  {escape_md2(len(candidates))}",
    ]
    if round_summary:
        lines.extend(
            [
                "",
                "*Round Summary*",
                f"• signals: {escape_md2(round_summary.get('signals') or '?')}",
                f"• candidates: {escape_md2(round_summary.get('candidate_count') or len(candidates))}",
                f"• kept: {escape_md2(round_summary.get('kept') or len(candidates))}",
                f"• boundary: {escape_md2(round_summary.get('boundary') or '—')}",
                f"• recall health: {escape_md2(round_summary.get('recall_health') or '—')}",
            ]
        )
    lines.extend(["", "*Why Kept*"])
    for idx, c in enumerate(candidates, start=1):
        flags = c.get("red_flags") or []
        lines.append(
            f"{idx}\\. {escape_md2(c.get('title') or '(no title)')}"
        )
        lines.append(
            "   "
            + escape_md2(
                f"angle={c.get('angle') or '—'} | score={c.get('score') or '?'} | "
                f"source={c.get('source') or '?'} | age={c.get('age_h') or '?'}h"
            )
        )
        if flags:
            lines.append("   " + escape_md2("flags=" + ", ".join(flags)))
    if worth_reviewing:
        lines.extend(["", "*Worth Reviewing*"])
        for item in worth_reviewing[:3]:
            lines.append(
                "• "
                + escape_md2(
                    f"{item.get('id') or '?'} | {item.get('topic_one_liner') or '(no title)'}"
                )
            )
    if config_suggestions:
        lines.extend(["", "*Config Suggestions*"])
        for item in config_suggestions[:3]:
            lines.append(
                "• "
                + escape_md2(
                    f"{item.get('title') or 'Suggestion'} ({item.get('risk_level') or 'low'})"
                )
            )
    text = "\n".join(lines)

    write_row = [
        (f"✅ 起稿 #{i + 1}", f"A:write:{sid}:slot={i}")
        for i in range(len(candidates))
    ]
    rows = [
        write_row,
        [
            ("📋 全文", f"A:expand:{sid}"),
            ("⏰ 4h 后", f"A:defer:{sid}:hours=4"),
        ],
        [("🚫 全拒绝", f"A:reject_all:{sid}")],
    ]
    return text, _kb(rows), sid


def render_profile_setup_card(
    *,
    profile_id: str,
    reason: str,
    missing_fields: list[str],
    session_path: str,
) -> tuple[str, dict[str, Any], str]:
    sid = _sid.register(
        gate="P",
        batch_path=session_path,
        ttl_hours=24,
        extra={"profile_id": profile_id, "reason": reason, "missing_fields": list(missing_fields)},
    )
    text = "\n".join(
        [
            "🧩 *Profile Setup Needed*",
            "",
            f"*Profile*  {escape_md2(profile_id)}",
            f"*Reason*  {escape_md2(reason)}",
            "*Missing Fields*",
            *[
                f"• {escape_md2(field)}"
                for field in (missing_fields or ["publisher_account.brand", "search_queries"])
            ],
            "",
            "Reply flow uses 4 short steps: brand, voice/language defaults, pasted materials, and writing boundaries\\. "
            "Facts, terms, search queries, Do/Don't rules, and avoid terms are derived from those replies\\.",
        ]
    )
    kb = _kb(
        [
            [("📝 Start setup", f"P:start:{sid}"), ("⏰ Later", f"P:later:{sid}")],
        ]
    )
    return text, kb, sid


def render_profile_setup_question(
    *,
    profile_id: str,
    display_name: str | None = None,
    step_label: str,
    prompt: str,
    step_index: int,
    total_steps: int,
) -> str:
    display = str(display_name or "").strip()
    return "\n".join(
        [
            "🧩 *Profile Setup Session*",
            "",
            f"*Profile*  {escape_md2(display or profile_id)}",
            *(
                [f"*Profile ID*  `{escape_md2(profile_id)}`"]
                if display and display != profile_id
                else []
            ),
            f"*Step*  {escape_md2(step_index)}/{escape_md2(total_steps)}",
            f"*Field*  {escape_md2(step_label)}",
            "",
            escape_md2(prompt),
        ]
    )


def render_suggestion_list(
    *,
    suggestions: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    lines = [
        "🧩 *Pending Profile Suggestions*",
        "",
        f"*Count*  {escape_md2(len(suggestions))}",
    ]
    rows: list[list[tuple[str, str]]] = []
    for idx, suggestion in enumerate(suggestions[:8], start=1):
        suggestion_id = str(suggestion.get("id") or "")
        profile_id = str(suggestion.get("profile_id") or "?")
        stage = str(suggestion.get("stage") or "?")
        title = str(suggestion.get("title") or "Suggestion")
        risk = str(suggestion.get("risk_level") or "low")
        lines.append("")
        lines.append(f"{idx}\\. {escape_md2(title)}")
        lines.append(escape_md2(f"   profile={profile_id} stage={stage} risk={risk}"))
        path = str(suggestion.get("path") or "")
        if suggestion_id and path:
            sid = _sid.register(
                gate="S",
                batch_path=path,
                ttl_hours=24,
                extra={"suggestion_id": suggestion_id},
            )
            rows.append([(f"Review #{idx}", f"S:review:{sid}")])
    if not suggestions:
        lines.append("")
        lines.append("No pending suggestions\\.")
    return "\n".join(lines), _kb(rows) if rows else {}


def render_suggestion_review(
    *,
    suggestion: dict[str, Any],
    preview_profile: dict[str, Any],
) -> tuple[str, dict[str, Any], str]:
    suggestion_id = str(suggestion.get("id") or "")
    path = str(suggestion.get("path") or "")
    sid = _sid.register(
        gate="S",
        batch_path=path,
        ttl_hours=24,
        extra={"suggestion_id": suggestion_id},
    )
    proposed = suggestion.get("proposed_patch") if isinstance(suggestion.get("proposed_patch"), dict) else {}
    changed_keys = ", ".join(sorted(proposed.keys())) if proposed else "(none)"
    preview_queries = preview_profile.get("search_queries") or []
    lines = [
        "🧩 *Suggestion Review*",
        "",
        f"*ID*  `{escape_md2(suggestion_id)}`",
        f"*Profile*  {escape_md2(suggestion.get('profile_id') or '?')}",
        f"*Stage*  {escape_md2(suggestion.get('stage') or '?')}",
        f"*Risk*  {escape_md2(suggestion.get('risk_level') or 'low')}",
        "",
        f"*Title*  {escape_md2(suggestion.get('title') or 'Suggestion')}",
        escape_md2(str(suggestion.get("summary") or "")),
        "",
        f"*Changed Keys*  {escape_md2(changed_keys)}",
    ]
    if preview_queries:
        lines.append("")
        lines.append("*Preview Search Queries*")
        for query in preview_queries[:8]:
            lines.append(f"• {escape_md2(query)}")
    rows = [
        [("✅ Apply", f"S:apply:{sid}"), ("🚫 Dismiss", f"S:dismiss:{sid}")],
    ]
    return "\n".join(lines), _kb(rows), sid


# ---------------------------------------------------------------------------
# Gate B — Draft Article Review
# ---------------------------------------------------------------------------


def render_gate_b(
    *,
    article_id: str,
    title: str,
    subtitle: str | None,
    publisher_brand: str | None,
    voice: str | None,
    word_count: int,
    section_count: int,
    compliance_score: float,
    tags: list[str],
    self_check_lines: list[str],
    opening_excerpt: str,
) -> tuple[str, dict[str, Any], str]:
    sid = _sid.register(gate="B", article_id=article_id, ttl_hours=_gate_b_ttl_hours())

    subtitle_line = (
        f"_{escape_md2(subtitle)}_\n" if subtitle else ""
    )
    tags_line = (
        f"tags: {escape_md2(', '.join(tags))}\n" if tags else ""
    )
    excerpt = (opening_excerpt or "").strip()
    if len(excerpt) > 120:
        excerpt = excerpt[:120] + "…"

    text = _substitute(
        _load_template("gate_b.tmpl"),
        {
            "short_id": escape_md2(sid),
            "timestamp": _now_label(),
            "title": escape_md2(title),
            "subtitle_line": subtitle_line,
            "publisher_brand": escape_md2(publisher_brand or "(unset)"),
            "voice": escape_md2(voice or "(unset)"),
            "word_count": escape_md2(word_count),
            "section_count": escape_md2(section_count),
            "compliance_score": escape_md2(f"{compliance_score:.2f}"),
            "tags_line": tags_line,
            "self_check_block": "\n".join(escape_md2(l) for l in self_check_lines),
            "opening_excerpt": escape_md2(excerpt),
        },
    )

    rows = [
        [("✅ 通过", f"B:approve:{sid}"), ("✏️ 编辑", f"B:edit:{sid}")],
        [("🔁 重写", f"B:rewrite:{sid}"), ("📋 diff", f"B:diff:{sid}")],
        [("🚫 拒绝", f"B:reject:{sid}"), ("⏰ 2h 后", f"B:defer:{sid}:hours=2")],
    ]
    return text, _kb(rows), sid


# ---------------------------------------------------------------------------
# Gate C — Cover Image Review
# ---------------------------------------------------------------------------


def render_gate_c(
    *,
    article_id: str,
    title: str,
    image_mode: str,
    cover_style: str,
    cover_size: str,
    self_check_lines: list[str],
    brand_overlay_status: str,
    brand_overlay_anchor: str,
    inline_body_count: int,
) -> tuple[str, dict[str, Any], str]:
    """Returns (caption, reply_markup, short_id). The PHOTO itself is sent
    by the caller via tg_client.send_photo using this caption."""
    sid = _sid.register(gate="C", article_id=article_id, ttl_hours=_gate_c_ttl_hours())

    title_short = title if len(title) <= 60 else title[:57] + "..."

    text = _substitute(
        _load_template("gate_c.tmpl"),
        {
            "short_id": escape_md2(sid),
            "timestamp": _now_label(),
            "title_short": escape_md2(title_short),
            "image_mode": escape_md2(image_mode),
            "cover_style": escape_md2(cover_style),
            "cover_size": escape_md2(cover_size),
            "self_check_block": "\n".join(escape_md2(l) for l in self_check_lines),
            "overlay_status": escape_md2(brand_overlay_status),
            "overlay_anchor": escape_md2(brand_overlay_anchor),
            "inline_body_count": escape_md2(inline_body_count),
        },
    )

    rows = [
        [("✅ 通过", f"C:approve:{sid}"), ("🚫 拒绝", f"C:skip:{sid}")],
        [("🔁 再生成", f"C:regen:{sid}"), ("🎨 换 logo 位置", f"C:relogo:{sid}")],
        [("🖼 全分辨率", f"C:full:{sid}"), ("⏰ 2h 后", f"C:defer:{sid}:hours=2")],
    ]
    return text, _kb(rows), sid


# ---------------------------------------------------------------------------
# Image-gate Picker — soft prompt sent after Gate B ✅
# ---------------------------------------------------------------------------


def render_image_gate_picker(
    *,
    article_id: str,
    title: str,
) -> tuple[str, dict[str, Any], str]:
    """Returns (text, reply_markup, short_id).

    Sent after Gate B ✅ to prompt the user for image-gate mode. Soft prompt —
    user may ignore this card and run ``af image-gate <aid> --mode <X>``
    manually from the CLI; no state transition fires until they act.
    """
    sid = _sid.register(gate="I", article_id=article_id, ttl_hours=12)
    title_short = title if len(title) <= 60 else title[:57] + "..."
    text = "\n".join(
        [
            "📸 *选择封面策略*",
            "",
            f"*Timestamp*  {_now_label()}",
            f"*Article*  `{escape_md2(article_id)}`",
            f"*Title*  {escape_md2(title_short)}",
            "",
            escape_md2("draft 已通过. 选下一步:"),
            escape_md2("• cover-only (默认): 仅生成封面"),
            escape_md2("• cover+body: 封面 + 正文图"),
            escape_md2("• 跳过: 不出图, 直接进 Gate D"),
            "",
            escape_md2("可忽略此卡片，直接 CLI 跑 `af image-gate`."),
        ]
    )
    rows = [
        [
            ("💎 cover-only (默认)", f"I:cover_only:{sid}"),
            ("🎨 cover+body", f"I:cover_plus_body:{sid}"),
            ("🚫 跳过封面", f"I:none:{sid}"),
        ],
    ]
    return text, _kb(rows), sid


# ---------------------------------------------------------------------------
# Gate D — Channel Selection (multi-select)
# ---------------------------------------------------------------------------


def render_gate_d(
    *,
    article_id: str,
    title: str,
    available: list[str],
    selected: set[str] | list[str],
    short_id: str,
) -> tuple[str, dict[str, Any]]:
    """Returns (text, reply_markup). The short_id is minted by the caller
    (so daemon-side toggle handlers can re-render the keyboard against the
    same id without re-registering).

    ``available`` is the ordered list of platform IDs we can offer; toggles
    only show for these. ``selected`` is the current per-card state (read
    from the short_id entry's ``extra``).
    """
    sel_set = set(selected or [])
    title_short = title if len(title) <= 60 else title[:57] + "..."

    text = _substitute(
        _load_template("gate_d.tmpl"),
        {
            "short_id": escape_md2(short_id),
            "timestamp": _now_label(),
            "title_short": escape_md2(title_short),
            "available_count": escape_md2(len(available)),
            "available_list": escape_md2(", ".join(available)) or "—",
        },
    )

    rows: list[list[tuple[str, str]]] = []
    for p in available:
        marker = "✅" if p in sel_set else "☐"
        rows.append([(f"{marker} {p}", f"D:toggle:{short_id}:p={p}")])
    # Quick-select shortcuts (Q1) — toggle every available platform on/off
    # without per-row clicking. Sits above Confirm/Cancel for fast access.
    rows.append([
        ("⚡ 全选", f"D:select_all:{short_id}"),
        ("✖ 全清", f"D:clear_all:{short_id}")
    ])
    # Save-as-default (Q2) — persists the current selection into
    # metadata.metadata_overrides.gate_d.default_platforms so future Gate D
    # cards for this article preselect the same set.
    rows.append([
        ("💾 保存默认", f"D:save_default:{short_id}"),
        ("✅ 通过", f"D:confirm:{short_id}"),
    ])
    rows.append([("🚫 拒绝", f"D:cancel:{short_id}")])
    return text, _kb(rows)


def render_dispatch_preview(
    *,
    article_id: str,
    title: str,
    selected_platforms: list[str],
    per_platform_info: dict[str, dict[str, Any]] | None = None,
    short_id: str,
) -> tuple[str, dict[str, Any]]:
    """Build the "📋 Dispatch Preview" card sent after D:confirm.

    Returns (text, reply_markup). ``short_id`` is REUSED from the D:confirm
    callback so the PD:dispatch / PD:cancel buttons resolve back to the same
    Gate D entry — the preview is a 2-step UI on the same decision, not a
    fresh card.

    ``per_platform_info`` is best-effort metadata (word_count, manual flags,
    etc.) keyed by platform id; missing keys render with neutral defaults.
    """
    info = per_platform_info or {}
    title_short = title if len(title) <= 60 else title[:57] + "..."

    lines: list[str] = []
    lines.append("📋 *Dispatch Preview*")
    if title_short:
        lines.append(f"*{escape_md2(title_short)}*")
    lines.append(f"`{escape_md2(article_id)}`")
    lines.append("")
    lines.append(
        f"选中平台 *{escape_md2(len(selected_platforms))}* 个："
    )
    for p in selected_platforms:
        meta = info.get(p) or {}
        bits: list[str] = []
        wc = meta.get("word_count")
        if wc is not None:
            bits.append(f"{wc} 字")
        if meta.get("manual"):
            bits.append("manual paste")
        if meta.get("note"):
            bits.append(str(meta.get("note")))
        suffix = f" \\({escape_md2(' · '.join(bits))}\\)" if bits else ""
        lines.append(f"• `{escape_md2(p)}`{suffix}")
    lines.append("")
    lines.append("确认即真发布。取消则回 channel\\_pending\\_review。")

    text = "\n".join(lines)
    rows: list[list[tuple[str, str]]] = [
        [
            ("🚀 真发布", f"PD:dispatch:{short_id}"),
            ("🚫 取消", f"PD:cancel:{short_id}"),
        ],
    ]
    return text, _kb(rows)


# ---------------------------------------------------------------------------
# Body markdown export — for Gate B document attachment
# ---------------------------------------------------------------------------


def render_dispatch_summary(
    *,
    article_id: str,
    results: list[dict],
) -> tuple[str, dict, str | None]:
    """Returns (text, reply_markup, retry_short_id_or_None)."""
    lines: list[str] = []
    failed_platforms: list[str] = []
    for r in results:
        platform = str(r.get("platform") or "?")
        status = str(r.get("status") or "")
        url = r.get("url")
        reason = r.get("reason")
        if status == "success":
            lines.append(f"✅ {platform}: {url or '(no url)'}")
        elif status == "failed":
            failed_platforms.append(platform)
            lines.append(f"❌ {platform}: {reason or '(unknown)'}")
        elif status in {"manual", "manual_required"}:
            lines.append(f"📤 {platform}: {reason or 'manual paste required'}")
        else:
            lines.append(f"⚠ {platform}: {reason or 'no record'}")

    # Q6: 全成功标语 (banner already MarkdownV2-safe — no user input)
    failed_count = sum(1 for r in results if r.get("status") == "failed")
    total = len(results)
    if failed_count == 0 and total > 0:
        success_banner = f"🎉 *全部 {total} 平台成功*\n\n"
    else:
        success_banner = ""

    text = (
        success_banner
        + f"📤 *Gate D dispatch*  ·  article `{escape_md2(article_id)}`\n\n"
        + "\n".join(escape_md2(l) for l in (lines or ["(no platforms)"]))
    )

    if not failed_platforms:
        return text, {}, None

    sid = _sid.register(
        gate="D",
        article_id=article_id,
        ttl_hours=12,
        extra={"failed": list(failed_platforms)},
    )
    kb = {
        "inline_keyboard": [[
            {"text": f"🔁 重试 ({len(failed_platforms)})", "callback_data": f"D:retry:{sid}"},
        ]],
    }
    return text, kb, sid


def render_publish_ready(
    *,
    article_id: str,
    title: str,
    subtitle: str | None,
    publisher_brand: str,
    tags: list[str],
    canonical_url: str | None,
    package_path: str,
    warnings: list[str],
) -> tuple[str, str, dict[str, Any]]:
    """Render the 'ready to publish' caption for the cover photo.

    Q6: returns (text, sid, kb) — kb carries the [📌 我已粘贴 + URL] button
    that wires into the daemon's PR:mark callback so the operator can capture
    the published Medium URL after manual paste.
    """
    # Q6: real sid (was cosmetic only) — register so PR:mark callback resolves.
    sid = _sid.register(
        gate="PR",
        article_id=article_id,
        ttl_hours=24 * 30,  # ~30 days, effectively永久
        extra={},
    )

    subtitle_line = f"_{escape_md2(subtitle)}_\n" if subtitle else ""
    canonical_line = (
        f"canonical: {escape_md2(canonical_url)}\n" if canonical_url else ""
    )
    if warnings:
        warnings_block = "*Warnings*\n\n" + "\n".join(
            escape_md2("• " + w) for w in warnings
        ) + "\n\n"
    else:
        warnings_block = ""

    text = _substitute(
        _load_template("publish_ready.tmpl"),
        {
            "short_id": escape_md2(sid),
            "timestamp": _now_label(),
            "title": escape_md2(title),
            "subtitle_line": subtitle_line,
            "publisher_brand": escape_md2(publisher_brand),
            "tags_joined": escape_md2(", ".join(tags)) if tags else "—",
            "canonical_line": canonical_line,
            "warnings_block": warnings_block,
            "package_path": escape_md2(package_path),
        },
    )

    # Q6: keyboard wiring publish-mark callback.
    kb: dict[str, Any] = {
        "inline_keyboard": [[
            {"text": "📌 我已粘贴 + URL", "callback_data": f"PR:mark:{sid}"},
        ]],
    }
    return text, sid, kb


def render_publish_digest(articles: list[dict[str, Any]]) -> str:
    """Q2: render daily digest text for ``ready_to_publish`` articles older
    than 24h. Read-only summary (no buttons; the daily reminder is just a
    nudge — operators run ``af review-publish-mark`` from the CLI).

    ``articles`` items: ``{"article_id": str, "title": str, "age_hours": float}``.
    Returns MarkdownV2 text.
    """
    n = len(articles)
    lines: list[str] = [
        f"📌 *待 publish\\-mark*  ·  {escape_md2(n)} 篇",
        "",
    ]
    for art in articles:
        aid = str(art.get("article_id") or "?")
        aid_short = aid[-8:] if len(aid) > 8 else aid
        title = str(art.get("title") or "(untitled)")
        age = art.get("age_hours")
        try:
            age_label = f"{float(age):.0f}"
        except (TypeError, ValueError):
            age_label = "?"
        lines.append(
            f"• `{escape_md2(aid_short)}` — "
            f"{escape_md2(title)} \\(ready {escape_md2(age_label)} h\\+\\)"
        )
    lines.append("")
    lines.append(
        escape_md2("提示: af review-publish-mark <aid> <url> --platform medium")
    )
    return "\n".join(lines)


def render_locked_takeover(
    *,
    article_id: str,
    title: str,
    rewrite_count: int,
) -> tuple[str, dict[str, Any], str]:
    """Returns (text, reply_markup, short_id).

    Posted when an article hits the rewrite-round limit (>=2) and is bumped
    into ``drafting_locked_human``. The 3 buttons let the operator pick a
    manual takeover path: LLM critique, hands-on edit, or give up.
    """
    sid = _sid.register(
        gate="L",
        article_id=article_id,
        ttl_hours=24 * 30,  # ~30 days, effectively永久
    )
    title_short = title if len(title) <= 80 else title[:77] + "..."
    text = "\n".join(
        [
            "🔒 *Manual Takeover Required*",
            "",
            f"*Timestamp*  {_now_label()}",
            f"*Article*  `{escape_md2(article_id)}`",
            f"*Title*  {escape_md2(title_short)}",
            f"*Rewrites*  {escape_md2(rewrite_count)}",
            "",
            escape_md2(
                f"已重写 {rewrite_count} 次仍不满意 — 请选下一步接管方式。"
            ),
            "",
            escape_md2(
                "• LLM critique: 让模型再读一次草稿, 给具体改写建议"
            ),
            escape_md2(
                "• 接管编辑: 直接 reply <scope> <改写指令> (无 TTL)"
            ),
            escape_md2("• 放弃: 把文章打入 draft_rejected 终态"),
        ]
    )
    rows = [
        [
            ("🧠 LLM critique", f"L:critique:{sid}"),
            ("✏️ 接管编辑", f"L:edit:{sid}"),
            ("🚫 放弃", f"L:give_up:{sid}"),
        ],
    ]
    return text, _kb(rows), sid


def export_body_markdown(article_id: str) -> Path:
    """Write the article's Medium-preview body to a temp file the bot can
    attach to a Gate B card. Returns the path."""
    src = agentflow_home() / "medium" / article_id / "medium_preview.md"
    if not src.exists():
        src = agentflow_home() / "drafts" / article_id / "draft.md"
    out_dir = agentflow_home() / "review" / "outbox"
    out_dir.mkdir(parents=True, exist_ok=True)
    short = article_id[-8:]
    dst = out_dir / f"{short}_draft.md"
    dst.write_text(
        src.read_text(encoding="utf-8") if src.exists() else "(no draft)",
        encoding="utf-8",
    )
    return dst
