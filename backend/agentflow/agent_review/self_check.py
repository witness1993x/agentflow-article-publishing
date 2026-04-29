"""Self-check rules for Gates A / B / C.

Each ``check_*`` returns a list of human-readable lines, prefixed:
- ``✓ ...``     pass
- ``✗ ... — <why>``  fail (warning)
- ``⚠ ...``     blocker (Gate may refuse to advance)

Lines are passed straight into the rendered card. The blocker count is
returned alongside so the daemon can decide whether the ✅ button should
be disabled.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home


# ---------------------------------------------------------------------------
# Gate A — topic candidates
# ---------------------------------------------------------------------------


def check_gate_a(
    candidate: dict[str, Any],
    *,
    publisher_keywords: list[str],
    avoid_terms: list[str],
    recent_titles: list[str],
    heat_window_hours: float = 72,
) -> tuple[list[str], int]:
    """Return (lines, blocker_count) for one topic candidate."""
    lines: list[str] = []
    blockers = 0

    title = (candidate.get("title") or "").lower()
    keywords = [k.lower() for k in (candidate.get("keywords") or [])]

    # Keyword universe match
    if any(kw.lower() in title or kw.lower() in " ".join(keywords) for kw in publisher_keywords):
        lines.append("✓ 命中 publisher 关键词")
    else:
        lines.append("⚠ 未命中 publisher 关键词")
        blockers += 1

    # Recent duplicate
    norm = re.sub(r"\s+", "", title)
    dup = any(norm and norm in re.sub(r"\s+", "", (t or "").lower()) for t in recent_titles)
    if dup:
        lines.append("✗ 7d 内已发过近似选题")
    else:
        lines.append("✓ 无近期重复")

    # Heat freshness
    age_h = candidate.get("age_h")
    try:
        age = float(age_h) if age_h is not None else None
    except (TypeError, ValueError):
        age = None
    if age is None:
        lines.append("⚠ 缺少 age_h，无法判定时效性")
        blockers += 1
    elif age > heat_window_hours:
        lines.append(f"✗ 热度已过窗口 ({age:.0f}h > {heat_window_hours:.0f}h)")
    else:
        lines.append(f"✓ 在热度窗口内 ({age:.0f}h)")

    # Red lines
    red = []
    haystack = " ".join([title, *keywords])
    for term in avoid_terms:
        if term and term.lower() in haystack:
            red.append(term)
    if red:
        lines.append(f"✗ 触碰红线词: {', '.join(red)}")
        blockers += 1
    else:
        lines.append("✓ 无红线词")

    return lines, blockers


# ---------------------------------------------------------------------------
# Gate B — draft content
# ---------------------------------------------------------------------------


def check_gate_b(article_id: str) -> tuple[list[str], int]:
    """Return (lines, blocker_count) for the article's draft."""
    meta_path = agentflow_home() / "drafts" / article_id / "metadata.json"
    if not meta_path.exists():
        return ["⚠ metadata.json missing"], 1
    data = json.loads(meta_path.read_text(encoding="utf-8")) or {}

    lines: list[str] = []
    blockers = 0

    # Voice — does the body actually use first-person plural when publisher
    # demands first_party_brand? Markers come from publisher.pronoun (e.g.
    # 我们 / we / I) + brand name; falls back to a small generic set when
    # the publisher block is unconfigured. NEVER hard-code a brand here.
    publisher = data.get("publisher_account") or {}
    voice = publisher.get("voice")
    if voice == "first_party_brand":
        body = "\n".join(s.get("content_markdown") or "" for s in data.get("sections") or [])
        pronoun = str(publisher.get("pronoun") or "").strip()
        brand = str(publisher.get("brand") or "").strip()
        markers: list[str] = []
        if pronoun:
            markers.append(pronoun)
        if brand:
            markers.append(f"{brand} 做")
            markers.append(f"{brand} 在")
        if not markers:
            # Last-resort generic markers for either Chinese or English voice.
            markers = ["我们", "we ", "We "]
        marker_str = " / ".join(f"「{m}」" for m in markers[:3])
        if any(m in body for m in markers):
            lines.append(f"✓ 视角=first_party_brand (找到 {marker_str})")
        else:
            lines.append(f"⚠ 视角应为 first_party_brand 但正文未出现 {marker_str}")
            blockers += 1
    elif voice:
        lines.append(f"✓ 视角={voice}")
    else:
        lines.append("⚠ 未配置 publisher_account.voice")

    # Compliance score
    sections = data.get("sections") or []
    if sections:
        avg = sum(float(s.get("compliance_score") or 1.0) for s in sections) / len(sections)
        if avg >= 0.85:
            lines.append(f"✓ 合规分均值 {avg:.2f}")
        elif avg >= 0.7:
            lines.append(f"✗ 合规分偏低 {avg:.2f}")
        else:
            lines.append(f"⚠ 合规分过低 {avg:.2f}")
            blockers += 1
    else:
        lines.append("⚠ 无 sections")
        blockers += 1

    # Stale image markers in body
    body_text = "\n".join(s.get("content_markdown") or "" for s in sections)
    if re.search(r"\[IMAGE:\s", body_text):
        lines.append("⚠ 正文仍含 [IMAGE: ...] 标记 (run image-gate or --skip-images)")
        blockers += 1
    else:
        lines.append("✓ 无 [IMAGE: ...] 残留")

    # Tag sanity (length-1 fragments)
    overrides = (data.get("metadata_overrides") or {}).get("medium") or {}
    tags = list(overrides.get("tags") or []) or list(publisher.get("default_tags") or [])
    short = [t for t in tags if len(t) <= 1]
    if short:
        lines.append(f"✗ tags 含长度=1 碎片 {short}")
    elif tags:
        lines.append(f"✓ tags 已锁定 ({len(tags)} 个)")
    else:
        lines.append("✗ 无 tags 来源 (将走 _infer_tags fallback)")

    # canonical_url for SEO
    canonical = overrides.get("canonical_url")
    if canonical:
        lines.append("✓ canonical_url 已填")
    else:
        lines.append("✗ canonical_url 未填")

    # ----- Voice drift checks: when publisher demands first-party voice we
    #      enforce that every section actually uses the configured pronoun /
    #      brand markers, and that the closing CTA points back at the brand.
    if voice == "first_party_brand":
        pronoun = str(publisher.get("pronoun") or "我们")
        brand = str(publisher.get("brand") or "")
        markers = [m for m in (pronoun, f"{brand} 做") if m and m.strip()]

        # 1. Per-section: every section body must contain at least one marker
        drifts: list[str] = []
        for sec in sections:
            body = sec.get("content_markdown") or ""
            if not any(m in body for m in markers):
                drifts.append(sec.get("heading") or "(no heading)")
        if drifts:
            shown = drifts[:2]
            tail = "" if len(drifts) <= 2 else f" +{len(drifts) - 2}"
            lines.append(
                f"⚠ voice 漂移: 这些节没出现「{pronoun}」/「{brand}」: {shown}{tail}"
            )
            blockers += 1
        else:
            lines.append(f"✓ 每节都含 publisher voice marker（{pronoun}）")

        # 2. Closing / CTA must include the publisher subject
        closing = (data.get("closing") or "").strip()
        if closing:
            if any(m in closing for m in markers):
                lines.append("✓ closing 含 publisher 主体")
            else:
                lines.append("✗ closing 是空 CTA, 没把 publisher 拉回来")

        # 3. Section heading style: explanatory phrasing without first-person
        #    is a soft warning (not a blocker), but worth flagging.
        explanatory_words = ["如何", "怎样", "为什么", "怎么"]
        soft_drifts: list[str] = []
        for sec in sections:
            heading = (sec.get("heading") or "").strip()
            if any(w in heading for w in explanatory_words) and not any(
                m in heading for m in markers
            ):
                soft_drifts.append(heading)
        if soft_drifts:
            lines.append(
                f"💡 标题建议: {soft_drifts[:1]} 改写成「{pronoun}如何…」会更稳口吻"
            )

    return lines, blockers


# ---------------------------------------------------------------------------
# Gate C — cover image
# ---------------------------------------------------------------------------


def check_gate_c(article_id: str) -> tuple[list[str], int, dict[str, Any]]:
    """Return (lines, blocker_count, summary) for the article's cover."""
    meta_path = agentflow_home() / "drafts" / article_id / "metadata.json"
    if not meta_path.exists():
        return ["⚠ metadata.json missing"], 1, {}
    data = json.loads(meta_path.read_text(encoding="utf-8")) or {}

    lines: list[str] = []
    blockers = 0
    summary: dict[str, Any] = {}

    placeholders = data.get("image_placeholders") or []
    cover = next((p for p in placeholders if p.get("role") == "cover"), None)
    if not cover:
        return ["⚠ no cover-role placeholder"], 1, summary

    path = cover.get("resolved_path")
    if not path or not Path(path).exists():
        return ["⚠ cover resolved_path missing or file not on disk"], 1, summary

    summary["cover_path"] = path
    summary["description"] = cover.get("description") or ""

    # Dimensions + aspect ratio
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            w, h = im.size
        summary["width"] = w
        summary["height"] = h
        ratio = w / h if h else 0
        if abs(ratio - 16 / 9) < 0.05:
            lines.append(f"✓ 16:9 aspect ratio ({w}x{h})")
        else:
            lines.append(f"✗ aspect ratio = {ratio:.2f} (expected ~1.78 for 16:9)")
        if w >= 1920:
            lines.append(f"✓ 2k resolution ({w}px wide)")
        elif w >= 1280:
            lines.append(f"✗ resolution {w}px wide (below 2k)")
        else:
            lines.append(f"⚠ resolution {w}px wide is too small")
            blockers += 1
    except Exception as err:
        lines.append(f"⚠ PIL inspect failed: {err}")
        blockers += 1

    # Brand overlay status — read from the most recent image_generator emission
    # if persisted, else infer from preferences.
    overlay_applied = bool(data.get("brand_overlay_applied"))
    lines.append(
        "✓ brand wordmark present" if overlay_applied
        else "⚠ brand_overlay_applied flag not set on metadata"
    )
    summary["brand_overlay_applied"] = overlay_applied

    return lines, blockers, summary
