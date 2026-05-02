"""Auto-trigger Gate cards from pipeline command callsites.

The CLI commands (``af fill``, ``af image-gate``, ``af write --auto-pick``) call
these helpers at the end of a successful run. Each helper:

- short-circuits silently when TG isn't configured (no token / no chat_id),
  so the pipeline never fails on a review-side hiccup
- advances the article through the canonical state path with ``force=True``
  fallbacks so the gate card is always postable
- posts the card + supplementary content (body / cover image)

Module-level functions are also called by the ``af review-post-*`` CLI
wrappers so we have one code path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agentflow.agent_review import (
    daemon as _daemon,
    render,
    self_check,
    short_id as _sid,
    state as _state,
    tg_client,
)
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.logger import get_logger


_log = get_logger("agent_review.triggers")


def _tg_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())


def _read_metadata(article_id: str) -> dict[str, Any]:
    p = agentflow_home() / "drafts" / article_id / "metadata.json"
    if not p.exists():
        raise FileNotFoundError(f"no metadata.json for {article_id}")
    return json.loads(p.read_text(encoding="utf-8")) or {}


def _ensure_state(
    article_id: str,
    *,
    target: str,
    path: list[str],
    gate: str,
    notes: str,
) -> None:
    """Force-walk the article state along ``path`` until it reaches ``target``.

    ``path`` is a sequential list of states (start → ... → target). Each
    forced transition is logged so the audit trail still records that the
    auto-trigger drove the article through.
    """
    cur = _state.current_state(article_id)
    if cur == target:
        return
    if cur not in path:
        # Already past target or in a sad-path state — don't force-rewind.
        return
    cur_idx = path.index(cur)
    target_idx = path.index(target)
    if target_idx <= cur_idx:
        return
    for next_state in path[cur_idx + 1 : target_idx + 1]:
        _state.transition(
            article_id,
            gate=gate,
            to_state=next_state,
            actor="daemon",
            decision="auto_advance",
            notes=notes,
            force=True,
        )


# ---------------------------------------------------------------------------
# Gate A — post a topic-batch review card after `af hotspots`
# ---------------------------------------------------------------------------


def post_gate_a(
    *,
    hotspots: list[Any],
    batch_path: str,
    publisher_brand: str,
    target_series: str = "A",
    top_k: int = 3,
    avoid_terms: list[str] | None = None,
    publisher_account: dict[str, Any] | None = None,
    filter_meta: dict[str, Any] | None = None,
    config_suggestions: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Render and send the Gate A topic-batch card.

    ``hotspots`` is a list of dicts (Hotspot.to_dict() shape). We pick the
    top_k by freshness_score and lay them out as candidate slots.

    When ``publisher_account`` is non-empty, candidates are reranked by a
    composite ``0.4 * freshness + 0.6 * fit`` score so that hotspots with
    poor topic↔publisher overlap get downranked. The composite value is
    surfaced to the Gate A card; candidates with fit < 0.10 also pick up a
    ``low_topic_fit`` red flag.
    """
    if not _tg_configured():
        _log.info("TG not configured — skipping Gate A post")
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning("no review chat_id — skipping Gate A post")
        return None
    if not hotspots:
        _log.info("no hotspots — skipping Gate A post")
        return None

    existing = _sid.find_active(gate="A", batch_path=str(batch_path))
    if existing:
        sid, entry = existing
        _log.info(
            "active Gate A card already exists for %s (short_id=%s); skipping duplicate",
            batch_path,
            sid,
        )
        return {
            "gate": "A",
            "short_id": sid,
            "tg_message_id": entry.get("tg_message_id"),
            "candidate_count": 0,
            "duplicate": True,
        }

    pub = publisher_account or {}
    fit_by_id: dict[int, float] = {}
    if pub:
        from agentflow.agent_d1.topic_fit import score_fit
        for h in hotspots:
            fit_by_id[id(h)] = float(score_fit(h, pub))

    # Composite weight: how much to lean into topic-publisher fit vs raw
    # freshness. Defaults to 0.6 (60% fit / 40% freshness). Tighter brand
    # discipline → bump toward 0.8; broader topic exploration → drop to 0.3.
    try:
        fit_weight = float(os.environ.get("AGENTFLOW_FIT_WEIGHT", "0.6"))
    except (TypeError, ValueError):
        fit_weight = 0.6
    fit_weight = max(0.0, min(1.0, fit_weight))
    fresh_weight = 1.0 - fit_weight

    def _composite(h: Any) -> float:
        freshness = float(h.get("freshness_score") or 0)
        if not pub:
            return freshness
        return fresh_weight * freshness + fit_weight * fit_by_id.get(id(h), 0.0)

    # rank by composite (freshness + fit) when publisher set, else freshness
    ranked = sorted(hotspots, key=_composite, reverse=True)[: max(1, top_k)]

    candidates: list[dict[str, Any]] = []
    avoid_lc = [t.lower() for t in (avoid_terms or [])]
    for h in ranked:
        topic = str(h.get("topic_one_liner") or "(no title)")
        freshness = float(h.get("freshness_score") or 0)
        if pub:
            fit = fit_by_id.get(id(h), 0.0)
            score = fresh_weight * freshness + fit_weight * fit
        else:
            fit = 0.0
            score = freshness
        # Best angle = first SuggestedAngle title
        angle_label = ""
        if h.get("suggested_angles"):
            first = h["suggested_angles"][0] or {}
            angle_label = str(first.get("title") or first.get("angle") or "")
        # age_h: derive from generated_at if usable
        age_h = "?"
        try:
            from datetime import datetime, timezone
            ts = h.get("generated_at")
            if ts:
                gen = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                age_h = f"{(datetime.now(timezone.utc) - gen).total_seconds() / 3600:.1f}"
        except Exception:
            pass
        # source: take first ref source if present
        src = "?"
        if h.get("source_references"):
            ref = h["source_references"][0] or {}
            src = str(ref.get("source") or ref.get("origin") or "?")
        # red flags: any avoid_terms that appear in topic
        haystack = topic.lower()
        flags = [t for t in avoid_lc if t and t in haystack]
        if pub and fit < 0.10:
            flags.append("low_topic_fit")
        candidates.append({
            "title": topic,
            "angle": angle_label,
            "score": f"{score:.2f}",
            "age_h": age_h,
            "source": src,
            "keywords": [],
            "red_flags": flags,
            "hotspot_id": h.get("id"),
        })

    round_summary = None
    worth_reviewing = []
    if filter_meta:
        boundary = (filter_meta.get("boundary") or {}).get("level") or "—"
        matched = int(filter_meta.get("matched") or len(hotspots))
        total = int(filter_meta.get("total") or len(hotspots))
        round_summary = {
            "signals": filter_meta.get("signals") or "?",
            "candidate_count": total,
            "kept": matched,
            "boundary": boundary,
            "recall_health": (filter_meta.get("boundary") or {}).get("recommendation") or "—",
        }
        worth_reviewing = list(filter_meta.get("filtered_out_preview") or [])

    text, kb, sid = render.render_gate_a(
        publisher_brand=publisher_brand,
        target_series=target_series,
        candidates=candidates,
        batch_path=str(batch_path),
        round_summary=round_summary,
        worth_reviewing=worth_reviewing,
        config_suggestions=config_suggestions or [],
    )
    sent = tg_client.send_message(chat_id, text, reply_markup=kb)
    _sid.attach_message_id(sid, sent.get("message_id"))
    return {
        "gate": "A",
        "short_id": sid,
        "tg_message_id": sent.get("message_id"),
        "candidate_count": len(candidates),
    }


def post_profile_setup_prompt(
    *,
    profile_id: str,
    reason: str,
    missing_fields: list[str],
    session_path: str,
) -> dict[str, Any] | None:
    if not _tg_configured():
        _log.info("TG not configured — skipping profile setup prompt")
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning("no review chat_id — skipping profile setup prompt")
        return None
    text, kb, sid = render.render_profile_setup_card(
        profile_id=profile_id,
        reason=reason,
        missing_fields=missing_fields,
        session_path=session_path,
    )
    sent = tg_client.send_message(chat_id, text, reply_markup=kb)
    return {
        "gate": "P",
        "short_id": sid,
        "tg_message_id": sent.get("message_id"),
        "profile_id": profile_id,
        "missing_fields": list(missing_fields),
    }


# ---------------------------------------------------------------------------
# Gate B — post a draft-review card after `af fill`
# ---------------------------------------------------------------------------


_DRAFT_PATH = [
    _state.STATE_TOPIC_POOL,
    _state.STATE_TOPIC_APPROVED,
    _state.STATE_DRAFTING,
    _state.STATE_DRAFT_PENDING_REVIEW,
]


def _revoke_prior_card_keyboard(
    gate: str, article_id: str, chat_id: int | str,
) -> None:
    """v1.0.16: when a fresh Gate B/C/D card is about to land, clear the
    inline keyboard on any prior active card for the same (gate,
    article_id). The operator's TG history then has exactly one
    interactable card per gate, eliminating the "which card do I click,
    the v1 or v2?" confusion the autopost feedback flagged. Also
    revokes the old short_id so callbacks against the now-buttonless
    card surface the soft-revoke "✓ 已处理 (重复点击)" branch instead
    of executing twice.
    """
    try:
        existing = _sid.find_active(gate=gate, article_id=article_id)
    except Exception:
        existing = None
    if not existing:
        return
    old_sid, entry = existing
    old_message_id = entry.get("tg_message_id")
    if old_message_id:
        try:
            tg_client.edit_message_reply_markup(
                chat_id, int(old_message_id), reply_markup={},
            )
        except Exception as err:  # pragma: no cover — best-effort
            _log.info(
                "stale-card keyboard cleanup failed for %s/%s msg=%s: %s",
                gate, article_id, old_message_id, err,
            )
    try:
        _sid.revoke(old_sid)
    except Exception:
        pass


def post_gate_b(article_id: str, *, force: bool = False) -> dict[str, Any] | None:
    """Post the Gate B card to the configured review chat.

    Returns a small summary dict, or ``None`` when TG is not configured /
    chat_id is missing (caller decides whether to surface that)."""
    if not _tg_configured():
        _log.info("TG not configured — skipping Gate B post for %s", article_id)
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning("no review chat_id — skipping Gate B post for %s", article_id)
        return None

    _revoke_prior_card_keyboard("B", article_id, chat_id)

    _ensure_state(
        article_id,
        target=_state.STATE_DRAFT_PENDING_REVIEW,
        path=_DRAFT_PATH,
        gate="B",
        notes="auto-trigger from af fill",
    )

    meta = _read_metadata(article_id)
    title = meta.get("title", "(no title)")
    publisher = meta.get("publisher_account") or {}
    overrides = (meta.get("metadata_overrides") or {}).get("medium") or {}
    sections = meta.get("sections") or []
    word_count = int(
        meta.get("total_word_count") or sum(s.get("word_count") or 0 for s in sections)
    )
    compliance = (
        sum(float(s.get("compliance_score") or 1.0) for s in sections) / max(1, len(sections))
    )

    self_lines, blockers = self_check.check_gate_b(article_id)

    # v1.0.16: warn if the bound topic profile has been edited after this
    # draft was last saved. The snapshot was stamped by agent_d2.main.save_draft.
    snapshot = meta.get("profile_snapshot") or {}
    snap_pid = str(snapshot.get("profile_id") or "")
    snap_updated = str(snapshot.get("last_updated_at") or "")
    if snap_pid and snap_updated:
        try:
            from agentflow.shared.topic_profile_lifecycle import load_user_topic_profiles
            profiles_data = load_user_topic_profiles() or {}
            profiles_map = (
                profiles_data.get("profiles") or {}
                if isinstance(profiles_data, dict) else {}
            )
            cur_profile = profiles_map.get(snap_pid) or {}
            cur_updated = str(cur_profile.get("last_updated_at") or "")
            if cur_updated and cur_updated > snap_updated:
                meta["draft_outdated_by_profile_change"] = True
                self_lines = list(self_lines) + [
                    f"⚠ profile {snap_pid} 已在 draft 之后被更新 — "
                    f"建议 /onboard 之后重跑 af edit / fill 让新约束生效"
                ]
                # Persist the flag so downstream consumers (review-list,
                # publish gates) can also see it.
                try:
                    meta_path = (
                        agentflow_home() / "drafts" / article_id / "metadata.json"
                    )
                    if meta_path.exists():
                        existing = json.loads(meta_path.read_text(encoding="utf-8"))
                        existing["draft_outdated_by_profile_change"] = True
                        meta_path.write_text(
                            json.dumps(existing, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                except Exception:
                    pass
        except Exception as err:  # pragma: no cover — best-effort
            _log.info("profile-outdated check skipped for %s: %s", article_id, err)

    # v1.0.16: language-consistency lint. Surfaces an extra warning line
    # in self_check when the body's CJK/ASCII mix violates the profile's
    # declared output_language. Cheap, regex-only.
    try:
        from agentflow.agent_d2.language_lint import detect_mixed_language
        body_text = "\n\n".join(
            (s.get("content_markdown") or "") for s in sections
        )
        lang_warn = detect_mixed_language(
            body_text,
            (publisher.get("output_language") or "").strip(),
        )
        if lang_warn:
            self_lines = list(self_lines) + [lang_warn]
    except Exception as err:  # pragma: no cover — best-effort
        _log.info("language lint skipped for %s: %s", article_id, err)

    # v1.0.18: specificity / anchoring lint. Catches drafts that "sound
    # specific" (have dates / numbers / product names) but those names
    # are generic AI/Web3 lingo, not the publisher's own brand assets.
    try:
        from agentflow.agent_d2.specificity_lint import detect_specificity_drift
        spec_warn = detect_specificity_drift(sections, publisher)
        if spec_warn:
            self_lines = list(self_lines) + [spec_warn]
    except Exception as err:  # pragma: no cover — best-effort
        _log.info("specificity lint skipped for %s: %s", article_id, err)

    opening = (meta.get("opening") or "").strip()
    if not opening and sections:
        opening = (sections[0].get("content_markdown") or "").strip()

    text, kb, sid = render.render_gate_b(
        article_id=article_id,
        title=title,
        subtitle=overrides.get("subtitle"),
        publisher_brand=publisher.get("brand"),
        voice=publisher.get("voice"),
        word_count=word_count,
        section_count=len(sections),
        compliance_score=compliance,
        tags=list(overrides.get("tags") or []) or list(publisher.get("default_tags") or []),
        self_check_lines=self_lines,
        opening_excerpt=opening,
    )

    sent = tg_client.send_message(chat_id, text, reply_markup=kb)
    message_id = sent.get("message_id")
    _sid.attach_message_id(sid, message_id)

    body_doc = render.export_body_markdown(article_id)
    body_raw = body_doc.read_text(encoding="utf-8")
    tg_client.send_long_text(chat_id, render.escape_md2(body_raw))
    tg_client.send_document(chat_id, body_doc, caption=None, parse_mode=None)

    try:
        _state.transition(
            article_id,
            gate="B",
            to_state=_state.STATE_DRAFT_PENDING_REVIEW,
            actor="daemon",
            decision="post_card",
            tg_chat_id=int(chat_id),
            tg_message_id=int(message_id) if message_id else None,
            notes=f"short_id={sid}, blockers={blockers}",
        )
    except _state.StateError:
        # ensure_state may have already left us at draft_pending_review;
        # transition would re-enter the same state which is not an allowed
        # self-loop. Skipping is fine — the card is on the wire.
        pass

    return {
        "gate": "B",
        "article_id": article_id,
        "short_id": sid,
        "tg_message_id": message_id,
        "blockers": blockers,
    }


# ---------------------------------------------------------------------------
# Manual takeover (L:*) — fired after rewrite round >= 2
# ---------------------------------------------------------------------------


def post_locked_takeover(article_id: str) -> dict[str, Any] | None:
    """Send the Manual Takeover card to the operator (3 buttons: critique /
    edit / give_up).

    Article state should already be ``drafting_locked_human`` when this fires
    (the daemon route that triggers it does the transition before spawning).
    Best-effort: never raises; returns ``None`` if TG isn't configured.
    """
    if not _tg_configured():
        _log.info("TG not configured — skipping locked-takeover post for %s", article_id)
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning(
            "no review chat_id — skipping locked-takeover post for %s", article_id
        )
        return None

    try:
        meta = _read_metadata(article_id)
    except Exception as err:
        _log.warning("locked-takeover: metadata read failed for %s: %s", article_id, err)
        return None
    title = meta.get("title", "(no title)")

    history = list(meta.get("gate_history") or [])
    rewrite_count = sum(
        1 for h in history if isinstance(h, dict) and h.get("decision") == "rewrite_round"
    )

    try:
        text, kb, sid = render.render_locked_takeover(
            article_id=article_id,
            title=title,
            rewrite_count=rewrite_count,
        )
    except Exception as err:
        _log.warning("locked-takeover render failed for %s: %s", article_id, err)
        return None

    try:
        sent = tg_client.send_message(chat_id, text, reply_markup=kb)
    except Exception as err:
        _log.warning("locked-takeover send failed for %s: %s", article_id, err)
        return None

    return {
        "gate": "L",
        "article_id": article_id,
        "short_id": sid,
        "tg_message_id": sent.get("message_id"),
        "rewrite_count": rewrite_count,
    }


def post_critique(article_id: str) -> dict[str, Any] | None:
    """Run an LLM critique of the current draft, send it to the operator, then
    register a pending_edits entry so the operator can ✏️ reply with a fix.

    Best-effort: if the LLM call fails (incl. MOCK_LLM with no fixture) we
    still send the editing-instructions guidance + register pending_edits, so
    takeover doesn't get stuck.
    """
    if not _tg_configured():
        _log.info("TG not configured — skipping critique post for %s", article_id)
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning("no review chat_id — skipping critique post for %s", article_id)
        return None

    draft_path = agentflow_home() / "drafts" / article_id / "draft.md"
    draft_text = ""
    if draft_path.exists():
        try:
            draft_text = draft_path.read_text(encoding="utf-8")
        except Exception as err:
            _log.warning("critique: draft read failed for %s: %s", article_id, err)

    critique_md: str | None = None
    suggestions: list[str] = []
    if draft_text:
        try:
            import asyncio
            from agentflow.shared.llm_client import LLMClient

            prompt = (
                "下面是一篇草稿. 请用中文给出一份 critique, 列具体问题(结构/事实/语气/"
                "节奏 等), 并给可执行改写建议. 输出严格 JSON: "
                '{"critique": "...一段中文...", "suggestions": ["...", "..."]}\n\n'
                "草稿:\n"
                "===\n"
                f"{draft_text[:8000]}\n"
                "==="
            )
            data = asyncio.run(
                LLMClient().chat_json(
                    prompt_family="draft_critique",
                    prompt=prompt,
                    max_tokens=1500,
                )
            )
            if isinstance(data, dict):
                critique_md = str(data.get("critique") or "").strip() or None
                raw_sugg = data.get("suggestions") or []
                if isinstance(raw_sugg, list):
                    suggestions = [str(s).strip() for s in raw_sugg if str(s).strip()]
        except Exception as err:
            _log.warning("critique LLM call failed for %s: %s", article_id, err)

    # Compose + send the critique payload (graceful degradation when missing).
    try:
        if critique_md or suggestions:
            lines = ["🧠 *LLM Critique*", "", f"*Article*  `{render.escape_md2(article_id)}`"]
            if critique_md:
                lines.extend(["", "*Critique*", render.escape_md2(critique_md)])
            if suggestions:
                lines.extend(["", "*Suggestions*"])
                for s in suggestions[:10]:
                    lines.append("• " + render.escape_md2(s))
            tg_client.send_message(chat_id, "\n".join(lines))
        else:
            tg_client.send_message(
                chat_id,
                "🧠 *LLM Critique*\n\n"
                + render.escape_md2("(LLM critique 不可用 — 直接进入手动接管)"),
            )
    except Exception as err:
        _log.warning("critique send failed for %s: %s", article_id, err)

    # Register pending_edits with gate=L + ttl=999999 so the next reply is
    # parsed as an edit instruction even hours later. uid is derived from
    # chat_id (DM uid == chat_id in our setup).
    try:
        from agentflow.agent_review import pending_edits, short_id as _short_id

        sid = _short_id.register(
            gate="L", article_id=article_id, ttl_hours=24 * 30,
        )
        try:
            uid = int(chat_id) if chat_id is not None else 0
        except Exception:
            uid = 0
        if uid:
            pending_edits.register(
                uid=uid,
                article_id=article_id,
                gate="L",
                short_id=sid,
                ttl_minutes=999999,
            )
    except Exception as err:
        _log.warning("critique pending_edits register failed for %s: %s", article_id, err)

    # Always send the guidance message so the operator knows how to reply.
    try:
        tg_client.send_message(
            chat_id,
            "✏️ Manual takeover 编辑模式 \\(无 TTL\\)\\. 请回复:\n"
            "`<scope> <改写指令>`\n"
            "scope 可以是: `title` / `opening` / `closing` / 第几节的整数 \\(0\\-based\\)\n"
            "例: `title 标题再尖锐一点` / `2 第二节改得更口语化`",
            parse_mode="MarkdownV2",
        )
    except Exception as err:
        _log.warning("critique guidance send failed for %s: %s", article_id, err)

    return {
        "gate": "L",
        "article_id": article_id,
        "had_llm_critique": bool(critique_md or suggestions),
    }


# ---------------------------------------------------------------------------
# Gate C — post a cover-review card after `af image-gate`
# ---------------------------------------------------------------------------


_IMAGE_PATH = [
    _state.STATE_TOPIC_POOL,
    _state.STATE_TOPIC_APPROVED,
    _state.STATE_DRAFTING,
    _state.STATE_DRAFT_PENDING_REVIEW,
    _state.STATE_DRAFT_APPROVED,
    _state.STATE_IMAGE_PENDING_REVIEW,
]


def mark_published(
    article_id: str,
    *,
    published_url: str,
    platform: str = "medium",
    notes: str | None = None,
) -> dict[str, Any]:
    """Close the loop: record publish_history entry, update metadata, transition
    to STATE_PUBLISHED, and post a final TG confirmation message.

    Designed for the manual flow: user pastes the article into Medium, copies
    the resulting URL, then runs ``af review-publish-mark <article> <url>``.
    """
    from datetime import datetime
    from agentflow.agent_d4.storage import append_publish_record
    from agentflow.shared.models import PublishResult

    if not published_url or not published_url.startswith(("http://", "https://")):
        raise ValueError(
            f"published_url must be a full http(s) URL, got {published_url!r}"
        )

    # Block obvious mock/test URLs from polluting publish_history.jsonl.
    # Once a fake URL lands here it leaks into Gate D summaries, daily digests,
    # and metadata.published_url forever — and there's no easy "rollback" UI.
    # Operators can opt out by setting ``AGENTFLOW_ALLOW_MOCK_URLS=true`` for
    # smoke-testing scenarios.
    _MOCK_URL_FRAGMENTS = (
        "@mock", "/mock_", "mock.example", ".mock/", ".test/", ".test:",
        "/e2e-test-", "/e2e-simulation-", "test-mock-publish",
        "://localhost", "://127.0.0.1",
    )
    _allow_mock = os.environ.get("AGENTFLOW_ALLOW_MOCK_URLS", "").strip().lower() in {"true", "1", "yes"}
    if not _allow_mock:
        url_lower = published_url.lower()
        for frag in _MOCK_URL_FRAGMENTS:
            if frag in url_lower:
                raise ValueError(
                    f"refusing to mark a mock/test URL as published: {published_url!r}\n"
                    f"matched fragment: {frag!r}\n"
                    "if this is intentional smoke-testing, set "
                    "AGENTFLOW_ALLOW_MOCK_URLS=true and retry."
                )

    # Q3: dedupe — refuse a duplicate mark for the same platform so we never
    # double-append publish_history entries / re-emit the "Published" TG card.
    try:
        meta_pre = _read_metadata(article_id)
        already = list(meta_pre.get("published_platforms") or [])
        if platform in already:
            _log.warning(
                "mark_published: %s already marked on %s (skip duplicate)",
                article_id, platform,
            )
            if _tg_configured():
                chat_id = _daemon.get_review_chat_id()
                if chat_id is not None:
                    try:
                        tg_client.send_message(
                            chat_id,
                            f"⚠ {platform} 已 mark 过 ({article_id}), 不重复 append",
                            parse_mode=None,
                        )
                    except Exception:
                        pass
            existing_urls = meta_pre.get("published_url")
            if isinstance(existing_urls, dict):
                existing_url_for_plat = existing_urls.get(platform)
            elif isinstance(existing_urls, str):
                existing_url_for_plat = existing_urls
            else:
                existing_url_for_plat = None
            return {
                "article_id": article_id,
                "platform": platform,
                "published_url": existing_url_for_plat,
                "status": "duplicate_skipped",
            }
    except Exception:
        pass

    # 1. Append to publish_history.jsonl in the D4 schema so `af report` /
    #    `read_publish_history` see the same shape as API-driven publishes.
    result = PublishResult(
        platform=platform,
        status="success",
        published_url=published_url,
        platform_post_id=None,
        published_at=datetime.now(),
        failure_reason=None,
    )
    append_publish_record(article_id, result)

    # 2. Update metadata.published_platforms + status
    meta = _read_metadata(article_id)
    plats = list(meta.get("published_platforms") or [])
    if platform not in plats:
        plats.append(platform)
    meta["published_platforms"] = plats
    meta["status"] = "published"
    meta["published_at"] = datetime.now().isoformat()
    # Q5a: published_url 迁移到 dict (旧 str 单值 → {platform: url})。保持
    # 多平台兼容; 旧字段无 platform 关联, 默认归到当前 platform 之外的
    # legacy 槽位 (仍可读出原值)。
    pub_urls = meta.get("published_url")
    if isinstance(pub_urls, dict):
        pub_urls = dict(pub_urls)
    elif isinstance(pub_urls, str) and pub_urls:
        legacy_plat = meta.get("status_legacy_platform", "medium")
        pub_urls = {legacy_plat: pub_urls}
    else:
        pub_urls = {}
    pub_urls[platform] = published_url
    meta["published_url"] = pub_urls
    (
        agentflow_home() / "drafts" / article_id / "metadata.json"
    ).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. State machine: ready_to_publish -> published
    cur = _state.current_state(article_id)
    transition_entry: dict[str, Any] | None = None
    try:
        if cur == _state.STATE_PUBLISHED:
            pass  # idempotent
        else:
            transition_entry = _state.transition(
                article_id,
                gate="*",
                to_state=_state.STATE_PUBLISHED,
                actor="human",
                decision="manual_publish",
                notes=notes or f"manual paste; url={published_url}",
                # Use force=True so we accept publish from any state — the
                # user might mark a doc as published without having gone
                # through the gate clicks (legacy / hand-edited articles).
                force=True,
            )
    except _state.StateError as err:
        _log.warning("state transition skipped: %s", err)

    # 4. TG confirmation (best-effort, never raises)
    tg_msg_id: int | None = None
    if _tg_configured():
        chat_id = _daemon.get_review_chat_id()
        if chat_id is not None:
            try:
                title = meta.get("title") or "(untitled)"
                text = (
                    f"📌 *Published*  ·  {render.escape_md2(platform)}\n\n"
                    f"*{render.escape_md2(title)}*\n"
                    f"{render.escape_md2(published_url)}\n\n"
                    f"article\\_id: `{render.escape_md2(article_id)}`"
                )
                sent = tg_client.send_message(chat_id, text)
                tg_msg_id = int(sent.get("message_id") or 0) or None
            except Exception as err:  # pragma: no cover
                _log.warning("publish-mark TG post failed: %s", err)

    return {
        "article_id": article_id,
        "platform": platform,
        "published_url": published_url,
        "tg_message_id": tg_msg_id,
        "state_transition": transition_entry,
    }


def _af_argv(*args: str) -> list[str]:
    """Build an argv that invokes ``af`` reliably from a subprocess.

    Avoids ``python -m agentflow.cli.commands`` because that loads the
    commands module twice (once as ``__main__``, once as
    ``agentflow.cli.commands``), so the optional cli modules
    (medium_commands / review_commands / onboard_commands / …) register
    against the wrong ``cli`` group and their commands look unregistered.

    Prefers the ``af`` entry-point script in the same venv as ``sys.executable``;
    falls back to ``python -c "from agentflow.cli.commands import cli; cli()"``
    for environments without a venv layout.
    """
    import sys
    from pathlib import Path

    af_script = Path(sys.executable).parent / "af"
    if af_script.exists():
        return [str(af_script), *args]
    return [
        sys.executable, "-c",
        "import sys; from agentflow.cli.commands import cli; cli(args=sys.argv[1:])",
        *args,
    ]


def _run_subprocess(
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    label: str,
) -> Any:
    """subprocess.run wrapper that ALWAYS logs non-zero exits + crashes.

    Returns the CompletedProcess on success/non-success. Returns None on
    timeout / OS errors (caller treats as failure).
    """
    import subprocess

    try:
        res = subprocess.run(
            cmd, env=env, check=False, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as err:
        _log.warning("%s timed out after %ds: %s", label, timeout, err)
        return None
    except Exception as err:
        _log.warning("%s subprocess crashed: %s", label, err)
        return None

    if res.returncode != 0:
        # Surface the real failure instead of swallowing it. stderr first
        # (errors live there), stdout as fallback.
        tail = (res.stderr or "").strip().splitlines()[-5:] or (
            res.stdout or ""
        ).strip().splitlines()[-5:]
        snippet = " | ".join(tail)[:600]
        _log.warning(
            "%s exited %d: %s", label, res.returncode, snippet or "(no output)"
        )
    return res


def post_publish_ready(article_id: str) -> dict[str, Any] | None:
    """Final card after Gate C approve: regenerate Medium artifacts, then post
    the cover + caption (title/subtitle/tags/path) + package.md attachment.

    Terminal state for v0.1 — user manually pastes into Medium browser.
    """
    if not _tg_configured():
        _log.info("TG not configured — skipping publish-ready post for %s", article_id)
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning("no review chat_id — skipping publish-ready post for %s", article_id)
        return None

    # Bug2 guard: don't rewind from terminal `published` state. Once an
    # article is marked published (via review-publish-mark or D4), running
    # publish-ready again is a no-op — the package + URL are already locked.
    try:
        cur_state = _state.current_state(article_id)
    except FileNotFoundError:
        _log.warning(
            "publish-ready: no metadata.json for %s — article not found", article_id
        )
        return None
    if cur_state == _state.STATE_PUBLISHED:
        _log.info(
            "publish-ready skipped: %s is already published (terminal)", article_id
        )
        return None

    import subprocess
    import sys

    env = os.environ.copy()
    # 1. Regenerate platform_versions/medium.md with current overrides + cover.
    #    Bug1 fix: surface non-zero exits so callers see what actually failed
    #    instead of getting a cryptic "package.md missing" warning later.
    res = _run_subprocess(
        _af_argv("preview", article_id, "--platforms", "medium"),
        env=env, timeout=120, label="auto-preview",
    )
    if res is None or res.returncode != 0:
        # preview failed — without platform_versions/medium.md the package
        # step will error too. Stop and surface the real reason.
        # Q4: spawn-fail TG 通知
        err_tail = (
            (res.stderr or "").strip()[-500:] if res else "(timeout/oserr)"
        )
        try:
            from agentflow.agent_review.daemon import _notify_spawn_failure
            _notify_spawn_failure(
                "post_publish_ready (preview)",
                article_id,
                res.returncode if res else None,
                err_tail,
            )
        except Exception:
            pass
        return None
    # 2. Build the Medium browser-ops package (export + ops_checklist).
    res = _run_subprocess(
        _af_argv("medium-package", article_id),
        env=env, timeout=60, label="auto-package",
    )
    if res is None or res.returncode != 0:
        # Q4: spawn-fail TG 通知
        err_tail = (
            (res.stderr or "").strip()[-500:] if res else "(timeout/oserr)"
        )
        try:
            from agentflow.agent_review.daemon import _notify_spawn_failure
            _notify_spawn_failure(
                "post_publish_ready (medium-package)",
                article_id,
                res.returncode if res else None,
                err_tail,
            )
        except Exception:
            pass
        return None

    # 3. Read the package + cover for the TG card.
    medium_dir = agentflow_home() / "medium" / article_id
    package_path = medium_dir / "package.md"
    export_path = medium_dir / "export.json"
    if not package_path.exists():
        _log.warning(
            "package.md missing for %s after subprocesses returned 0 — "
            "skipping publish-ready post (check medium-package output)", article_id,
        )
        # Q4: spawn-fail TG 通知 — package.md missing 也算 spawn-fail
        try:
            from agentflow.agent_review.daemon import _notify_spawn_failure
            _notify_spawn_failure(
                "post_publish_ready (package.md missing)",
                article_id,
                0,
                "package.md not produced after medium-package returned 0",
            )
        except Exception:
            pass
        return None
    try:
        export = json.loads(export_path.read_text(encoding="utf-8")) if export_path.exists() else {}
    except Exception:
        export = {}
    preview_meta = (export.get("medium_preview") or {}).get("metadata") or {}
    images = export.get("images") or {}
    cover_path = (
        images.get("cover_image_path") or images.get("first_resolved_path")
    )

    title = preview_meta.get("title") or export.get("source", {}).get("title") or "(no title)"
    subtitle = preview_meta.get("subtitle")
    tags = list(preview_meta.get("tags") or [])
    canonical = preview_meta.get("canonical_url")
    publisher_brand = "default"
    try:
        meta = _read_metadata(article_id)
        publisher_brand = (meta.get("publisher_account") or {}).get("brand") or "default"
    except Exception:
        pass
    warnings = list(export.get("warnings") or [])

    caption, sid, kb = render.render_publish_ready(
        article_id=article_id,
        title=title,
        subtitle=subtitle,
        publisher_brand=publisher_brand,
        tags=tags,
        canonical_url=canonical,
        package_path=str(package_path),
        warnings=warnings,
    )

    if cover_path and Path(cover_path).exists():
        sent = tg_client.send_photo(
            chat_id, cover_path, caption=caption, reply_markup=kb,
        )
    else:
        sent = tg_client.send_message(chat_id, caption, reply_markup=kb)
    tg_client.send_document(chat_id, package_path, parse_mode=None)

    # State: image_approved → ready_to_publish. `af medium-package` may have
    # already advanced the state while generating package.md, so avoid a
    # duplicate ready_to_publish history entry here.
    try:
        if _state.current_state(article_id) not in {
            _state.STATE_READY_TO_PUBLISH,
            _state.STATE_PUBLISHED,
        }:
            _state.transition(
                article_id,
                gate="*",
                to_state=_state.STATE_READY_TO_PUBLISH,
                actor="daemon",
                decision="auto_advance",
                tg_chat_id=int(chat_id),
                tg_message_id=int(sent.get("message_id") or 0) or None,
                notes="auto-package on Gate C approve",
                force=True,
            )
    except _state.StateError:
        pass

    return {
        "gate": "*",
        "article_id": article_id,
        "tg_message_id": sent.get("message_id"),
        "package_path": str(package_path),
    }


def post_gate_c(article_id: str) -> dict[str, Any] | None:
    if not _tg_configured():
        _log.info("TG not configured — skipping Gate C post for %s", article_id)
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning("no review chat_id — skipping Gate C post for %s", article_id)
        return None

    _revoke_prior_card_keyboard("C", article_id, chat_id)

    _ensure_state(
        article_id,
        target=_state.STATE_IMAGE_PENDING_REVIEW,
        path=_IMAGE_PATH,
        gate="C",
        notes="auto-trigger from af image-gate",
    )

    meta = _read_metadata(article_id)
    self_lines, blockers, summary = self_check.check_gate_c(article_id)
    if "cover_path" not in summary:
        _log.warning("no cover image for %s — skipping Gate C post", article_id)
        return None

    title = meta.get("title", "(no title)")
    cover_size = f"{summary.get('width','?')}x{summary.get('height','?')}"
    overlay_status = "ON" if summary.get("brand_overlay_applied") else "off"

    caption, kb, sid = render.render_gate_c(
        article_id=article_id,
        title=title,
        image_mode="cover-only",
        cover_style="cover",
        cover_size=cover_size,
        self_check_lines=self_lines,
        brand_overlay_status=overlay_status,
        brand_overlay_anchor="bottom_left",
        inline_body_count=0,
    )
    sent = tg_client.send_photo(chat_id, summary["cover_path"], caption=caption, reply_markup=kb)
    message_id = sent.get("message_id")
    _sid.attach_message_id(sid, message_id)

    try:
        _state.transition(
            article_id,
            gate="C",
            to_state=_state.STATE_IMAGE_PENDING_REVIEW,
            actor="daemon",
            decision="post_card",
            tg_chat_id=int(chat_id),
            tg_message_id=int(message_id) if message_id else None,
            notes=f"short_id={sid}, blockers={blockers}",
        )
    except _state.StateError:
        pass

    return {
        "gate": "C",
        "article_id": article_id,
        "short_id": sid,
        "tg_message_id": message_id,
        "blockers": blockers,
    }


def post_image_gate_picker(article_id: str) -> dict[str, Any] | None:
    """Send the image-gate picker card after Gate B ✅. Soft prompt — does
    NOT transition state (article stays at draft_approved until the user
    picks a mode or runs ``af image-gate`` manually).
    """
    if not _tg_configured():
        _log.info(
            "TG not configured — skipping image-gate picker for %s", article_id
        )
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning(
            "no review chat_id — skipping image-gate picker for %s", article_id
        )
        return None

    try:
        meta = _read_metadata(article_id)
    except FileNotFoundError:
        _log.warning(
            "no metadata for %s — skipping image-gate picker", article_id
        )
        return None
    title = meta.get("title") or "(no title)"

    try:
        text, kb, sid = render.render_image_gate_picker(
            article_id=article_id, title=title,
        )
    except Exception as err:  # pragma: no cover
        _log.warning(
            "render_image_gate_picker failed for %s: %s", article_id, err
        )
        return None

    try:
        sent = tg_client.send_message(chat_id, text, reply_markup=kb)
    except Exception as err:  # pragma: no cover
        _log.warning(
            "image-gate picker send_message failed for %s: %s", article_id, err
        )
        return None

    _sid.attach_message_id(sid, sent.get("message_id"))
    return {
        "gate": "I",
        "article_id": article_id,
        "short_id": sid,
        "tg_message_id": sent.get("message_id"),
    }


# ---------------------------------------------------------------------------
# Gate D — channel selection (multi-select before dispatch)
# ---------------------------------------------------------------------------


# Per-platform availability probes. Each entry maps to the env vars that MUST
# all be set+non-empty for the platform to be selectable.
_TWITTER_REQS = [
    "TWITTER_API_KEY", "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET",
]
_PLATFORM_ENV_REQS: dict[str, list[str]] = {
    "medium": [],  # always available (manual paste)
    "ghost_wordpress": ["GHOST_ADMIN_API_URL", "GHOST_ADMIN_API_KEY"],
    "linkedin_article": ["LINKEDIN_ACCESS_TOKEN", "LINKEDIN_PERSON_URN"],
    "twitter_thread": _TWITTER_REQS,
    "twitter_single": _TWITTER_REQS,
    "webhook": ["WEBHOOK_PUBLISH_URL"],
}


def _collect_dispatch_results(article_id: str, platforms: list[str]) -> list[dict[str, Any]]:
    """Per-platform {platform,status,url,reason} from publish_history.jsonl."""
    out: list[dict[str, Any]] = []
    if not platforms:
        return out
    from agentflow.agent_d4.storage import read_publish_history
    records = read_publish_history(article_id)
    recent: dict[str, dict[str, Any]] = {}
    for rec in records:
        platform = rec.get("platform")
        if platform in platforms and platform not in recent:
            recent[platform] = rec
    for p in platforms:
        rec = recent.get(p)
        if rec and rec.get("status") == "success":
            out.append({
                "platform": p,
                "status": "success",
                "url": rec.get("published_url"),
                "reason": None,
            })
        elif rec and rec.get("status") == "manual":
            out.append({
                "platform": p,
                "status": "manual",
                "url": None,
                "reason": rec.get("failure_reason") or "manual paste required",
            })
        elif rec:
            out.append({
                "platform": p,
                "status": "failed",
                "url": None,
                "reason": rec.get("failure_reason") or "(unknown)",
            })
        else:
            out.append({
                "platform": p,
                "status": "missing",
                "url": None,
                "reason": "no record (check `af publish` logs)",
            })
    return out


def _detect_available_platforms() -> list[str]:
    """Return the ordered list of platform IDs whose env vars are populated."""
    out: list[str] = []
    for platform, reqs in _PLATFORM_ENV_REQS.items():
        if all(os.environ.get(k, "").strip() for k in reqs):
            out.append(platform)
    return out


def post_gate_d(article_id: str) -> dict[str, Any] | None:
    """Post the Gate D channel-selection card. Mints a short_id whose extra
    carries the per-card ``available`` + ``selected`` lists, so toggle
    callbacks can mutate selection without re-registering.
    """
    if not _tg_configured():
        _log.info("TG not configured — skipping Gate D post for %s", article_id)
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning("no review chat_id — skipping Gate D post for %s", article_id)
        return None

    _revoke_prior_card_keyboard("D", article_id, chat_id)

    try:
        meta = _read_metadata(article_id)
    except FileNotFoundError:
        _log.warning("Gate D: no metadata for %s", article_id)
        return None
    title = meta.get("title", "(no title)")
    overrides = (meta.get("metadata_overrides") or {}).get("gate_d") or {}
    requested = list(overrides.get("default_platforms") or []) or ["medium"]

    available = _detect_available_platforms()
    if not available:
        _log.warning("Gate D: no available platforms detected — falling back to medium-only")
        available = ["medium"]

    selected = [p for p in requested if p in available]
    if not selected:
        selected = ["medium"] if "medium" in available else available[:1]

    # Q5c: 增量发布 — 过滤已发平台 (避免重复发同一渠道)。如果所有可用
    # 平台都已发完, 静默跳过 (不发 Gate D 卡)。
    try:
        meta_for_filter = _read_metadata(article_id)
        already_published = list(meta_for_filter.get("published_platforms") or [])
        if already_published:
            available = [p for p in available if p not in already_published]
            selected = [p for p in selected if p not in already_published]
            if not available:
                _log.info(
                    "post_gate_d: all platforms already published for %s",
                    article_id,
                )
                return None
            if not selected:
                selected = available[:1]
    except Exception:
        pass

    # Q5c: 已 published 状态 → 反转回 channel_pending_review (走新加的边)。
    # state.py 的 _ALLOWED 已加 STATE_PUBLISHED → STATE_CHANNEL_PENDING_REVIEW。
    try:
        cur_for_inc = _state.current_state(article_id)
        if cur_for_inc == _state.STATE_PUBLISHED:
            _state.transition(
                article_id,
                gate="D",
                to_state=_state.STATE_CHANNEL_PENDING_REVIEW,
                actor="daemon",
                decision="incremental_publish",
                force=True,
            )
    except Exception:
        pass

    sid = _sid.register(
        gate="D",
        article_id=article_id,
        ttl_hours=12,
        extra={"available": available, "selected": list(selected)},
    )

    text, kb = render.render_gate_d(
        article_id=article_id,
        title=title,
        available=available,
        selected=set(selected),
        short_id=sid,
    )
    sent = tg_client.send_message(chat_id, text, reply_markup=kb)
    message_id = sent.get("message_id")
    _sid.attach_message_id(sid, message_id)

    try:
        _state.transition(
            article_id,
            gate="D",
            to_state=_state.STATE_CHANNEL_PENDING_REVIEW,
            actor="daemon",
            decision="post_card",
            tg_chat_id=int(chat_id),
            tg_message_id=int(message_id) if message_id else None,
            notes=f"short_id={sid}, available={','.join(available)}",
        )
    except _state.StateError as err:
        _log.warning("Gate D state transition skipped: %s", err)

    return {
        "gate": "D",
        "article_id": article_id,
        "short_id": sid,
        "tg_message_id": message_id,
        "available": available,
        "selected": list(selected),
    }


def post_dispatch_preview(
    article_id: str,
    selected_platforms: list[str],
    *,
    short_id: str,
) -> dict[str, Any] | None:
    """Send the "📋 Dispatch Preview" card after a D:confirm click.

    Does NOT transition state — the article remains in
    ``channel_pending_review`` until the operator clicks PD:dispatch or
    PD:cancel on the preview card. The same ``short_id`` from D:confirm is
    reused on the PD:* buttons so the preview is a 2-step UI on the original
    Gate D decision (not a fresh card).

    Per-platform info is best-effort: we peek at
    ``platform_versions/<X>.md`` if it already exists, otherwise we just
    note "will run preview now". medium gets a "manual paste" flag.
    """
    if not _tg_configured():
        _log.info("TG not configured — skipping dispatch preview for %s", article_id)
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning(
            "no review chat_id — skipping dispatch preview for %s", article_id
        )
        return None

    selected = [p for p in (selected_platforms or []) if p]
    if not selected:
        _log.warning("dispatch preview with empty selection for %s", article_id)
        return None

    title = "(no title)"
    try:
        meta = _read_metadata(article_id)
        title = str(meta.get("title") or "(no title)")
    except FileNotFoundError:
        _log.warning("dispatch preview: no metadata for %s", article_id)
    except Exception as err:  # graceful — preview must not block on meta read
        _log.warning("dispatch preview meta read failed for %s: %s", article_id, err)

    versions_dir = agentflow_home() / "drafts" / article_id / "platform_versions"
    info: dict[str, dict[str, Any]] = {}
    for p in selected:
        slot: dict[str, Any] = {}
        if p == "medium":
            slot["manual"] = True
        try:
            ver_path = versions_dir / f"{p}.md"
            if ver_path.exists():
                body = ver_path.read_text(encoding="utf-8")
                slot["word_count"] = len(body.split())
            else:
                slot["note"] = "will run preview now"
        except Exception as err:
            _log.warning(
                "dispatch preview per-platform read failed (%s/%s): %s",
                article_id, p, err,
            )
        info[p] = slot

    try:
        text, kb = render.render_dispatch_preview(
            article_id=article_id,
            title=title,
            selected_platforms=selected,
            per_platform_info=info,
            short_id=short_id,
        )
    except Exception as err:
        _log.warning("render_dispatch_preview failed for %s: %s", article_id, err)
        return None

    try:
        sent = tg_client.send_message(chat_id, text, reply_markup=kb)
    except Exception as err:
        _log.warning("dispatch preview send failed for %s: %s", article_id, err)
        return None

    return {
        "gate": "D",
        "stage": "dispatch_preview",
        "article_id": article_id,
        "short_id": short_id,
        "tg_message_id": sent.get("message_id"),
        "selected": list(selected),
    }


def _estimate_eta_seconds(platforms: list[str]) -> int:
    """估算 dispatch ETA: base preview + per-platform avg + 特定平台 weight."""
    eta = 30  # base preview
    for p in platforms:
        if p == "medium":
            eta += 30
        elif p == "linkedin_article":
            eta += 90  # 60 + 30 (3-step asset upload)
        elif p in ("twitter_thread", "twitter_single"):
            eta += 80  # 60 + 20 (media upload)
        else:
            eta += 60
    return eta


def post_publish_dispatch(
    article_id: str,
    platforms: list[str],
) -> dict[str, Any] | None:
    """Run preview + (publish for non-medium) + (medium-package for medium),
    then post a summary message. Always advances state to ready_to_publish
    even on partial failure — the manual paste step still has work to do.
    """
    from datetime import datetime
    from agentflow.agent_review.daemon import _audit, _notify_spawn_failure

    if not _tg_configured():
        _log.info("TG not configured — skipping publish dispatch for %s", article_id)
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning("no review chat_id — skipping publish dispatch for %s", article_id)
        return None

    platforms = [p for p in platforms if p]
    if not platforms:
        _log.warning("publish dispatch with empty platforms list for %s", article_id)
        return None

    # Q3: 入口 ETA 自适应通知
    eta = _estimate_eta_seconds(platforms)
    try:
        tg_client.send_message(
            chat_id,
            f"⏳ 分发中... 预计 ~{eta}s ({len(platforms)} 平台 ETA)\n\n云端实际可能浮动。",
            parse_mode=None,
        )
    except Exception:
        pass

    env = os.environ.copy()
    results: dict[str, Any] = {}

    plat_csv = ",".join(platforms)
    res = _run_subprocess(
        _af_argv("preview", article_id, "--platforms", plat_csv),
        env=env, timeout=180, label="dispatch-preview",
    )
    results["preview_exit"] = (res.returncode if res else None)

    # Q2: preview 失败 → 阻断 publish + 通知 operator
    if res is None or res.returncode != 0:
        err_tail = (res.stderr if res else "").strip()[-500:] if res else "(timeout/oserr)"
        try:
            tg_client.send_message(
                chat_id,
                f"❌ preview 失败, dispatch 已阻断  ·  article={article_id}\n\n{err_tail}",
                parse_mode=None,
            )
        except Exception:
            pass
        try:
            _audit({"kind": "dispatch_aborted", "reason": "preview_failed", "article_id": article_id})
        except Exception:
            pass
        return {
            "gate": "D",
            "article_id": article_id,
            "platforms": platforms,
            "aborted": "preview_failed",
            "results": results,
        }

    # Q4: 自适应 publish timeout
    auto_platforms = [p for p in platforms if p != "medium"]
    if auto_platforms:
        publish_timeout = min(300 + 200 * len(auto_platforms), 1800)
        res = _run_subprocess(
            _af_argv("publish", article_id, "--platforms", ",".join(auto_platforms)),
            env=env, timeout=publish_timeout, label="dispatch-publish",
        )
        results["publish_exit"] = (res.returncode if res else None)
        if res and res.stdout:
            results["publish_stdout_tail"] = "\n".join(res.stdout.splitlines()[-12:])

    if "medium" in platforms:
        res = _run_subprocess(
            _af_argv("medium-package", article_id),
            env=env, timeout=120, label="dispatch-medium-package",
        )
        results["medium_package_exit"] = (res.returncode if res else None)

        pkg_path = agentflow_home() / "medium" / article_id / "package.md"
        pkg_exists = pkg_path.exists()

        # Q5: missing 时自动重试 1 次
        if not pkg_exists:
            _log.warning(
                "medium-package missing after first attempt for %s, retrying once",
                article_id,
            )
            retry_res = _run_subprocess(
                _af_argv("medium-package", article_id),
                env=env, timeout=120, label="dispatch-medium-package-retry",
            )
            results["medium_package_retry_exit"] = (
                retry_res.returncode if retry_res else None
            )
            pkg_exists = pkg_path.exists()

            if not pkg_exists:
                try:
                    _notify_spawn_failure(
                        "medium-package",
                        article_id,
                        retry_res.returncode if retry_res else None,
                        "package.md missing after retry",
                    )
                except Exception:
                    pass

    # Per-platform results (used by render_dispatch_summary for retry kb).
    dispatch_results = _collect_dispatch_results(article_id, auto_platforms)
    if "medium" in platforms:
        pkg = agentflow_home() / "medium" / article_id / "package.md"
        if pkg.exists():
            dispatch_results.append({
                "platform": "medium",
                "status": "manual",
                "url": None,
                "reason": f"paste `{pkg.name}` (attached)",
            })
        else:
            dispatch_results.append({
                "platform": "medium",
                "status": "missing_after_retry",
                "url": None,
                "reason": "package.md missing after 2 attempts",
            })

    summary_md, retry_kb, retry_sid = render.render_dispatch_summary(
        article_id=article_id,
        results=dispatch_results,
    )

    # Q7: 摘要超长保护 + dispatch_results.json attach
    SUMMARY_LIMIT = 3500  # TG 4096 限, 留 buffer
    results_path: Path | None = None
    estimated_len = len(summary_md)
    if estimated_len > SUMMARY_LIMIT:
        try:
            results_path = (
                agentflow_home() / "drafts" / article_id / "dispatch_results.json"
            )
            results_path.write_text(
                json.dumps(
                    {
                        "article_id": article_id,
                        "platforms": platforms,
                        "results": dispatch_results,
                        "dispatched_at": datetime.now().isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as err:  # pragma: no cover
            _log.warning("dispatch_results.json write failed: %s", err)
            results_path = None

        try:
            tg_client.send_message(
                chat_id,
                f"ℹ 摘要超长 ({estimated_len} 字符)，完整结果见附件 dispatch_results.json",
                parse_mode=None,
            )
        except Exception:
            pass

        truncated_results = []
        for r in dispatch_results:
            rcopy = dict(r)
            if rcopy.get("reason") and len(rcopy["reason"]) > 100:
                rcopy["reason"] = rcopy["reason"][:97] + "..."
            truncated_results.append(rcopy)
        summary_md, retry_kb, retry_sid = render.render_dispatch_summary(
            article_id=article_id,
            results=truncated_results,
        )

    try:
        tg_client.send_message(
            chat_id, summary_md, reply_markup=retry_kb if retry_kb else None,
        )
    except Exception as err:  # pragma: no cover
        _log.warning("dispatch summary TG send failed: %s", err)

    if results_path and results_path.exists():
        try:
            tg_client.send_document(chat_id, results_path, parse_mode=None)
        except Exception as err:  # pragma: no cover
            _log.warning("dispatch_results.json send_document failed: %s", err)

    if retry_sid:
        results["retry_short_id"] = retry_sid

    if "medium" in platforms:
        try:
            post_publish_ready(article_id)
        except Exception as err:  # pragma: no cover
            _log.warning("post_publish_ready (medium leg) failed: %s", err)

    try:
        if _state.current_state(article_id) not in {
            _state.STATE_READY_TO_PUBLISH,
            _state.STATE_PUBLISHED,
        }:
            _state.transition(
                article_id,
                gate="D",
                to_state=_state.STATE_READY_TO_PUBLISH,
                actor="daemon",
                decision="dispatch",
                notes=f"platforms={','.join(platforms)}",
                force=True,
            )
    except _state.StateError as err:
        _log.warning("post-dispatch state transition skipped: %s", err)

    # Persist the decision into metadata for the audit trail.
    try:
        meta = _read_metadata(article_id)
        meta["gate_d_decision"] = {
            "platforms_selected": list(platforms),
            "dispatched_at": datetime.now().isoformat(),
            "results": results,
        }
        (
            agentflow_home() / "drafts" / article_id / "metadata.json"
        ).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as err:  # pragma: no cover
        _log.warning("gate_d_decision write failed: %s", err)

    # Q5a: auto URL 回报 — D4 publisher 真发布的 url 写回 metadata。
    # ``published_url`` 迁移成 dict {platform: url}; 旧 str 单值兼容保留。
    try:
        meta = _read_metadata(article_id)
        pubs = list(meta.get("published_platforms") or [])
        pub_urls = meta.get("published_url")
        if isinstance(pub_urls, dict):
            pub_urls = dict(pub_urls)
        elif isinstance(pub_urls, str) and pub_urls:
            legacy_plat = meta.get("status_legacy_platform", "medium")
            pub_urls = {legacy_plat: pub_urls}
        else:
            pub_urls = {}

        for r in dispatch_results:
            if r.get("status") == "success" and r.get("url"):
                plat = r["platform"]
                if plat not in pubs:
                    pubs.append(plat)
                pub_urls[plat] = r["url"]

        meta["published_platforms"] = pubs
        meta["published_url"] = pub_urls
        if pubs and meta.get("status") != "published":
            meta["status"] = "published"

        (
            agentflow_home() / "drafts" / article_id / "metadata.json"
        ).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception as err:
        _log.warning("auto URL 回报写 metadata 失败 for %s: %s", article_id, err)

    return {
        "gate": "D",
        "article_id": article_id,
        "platforms": platforms,
        "results": results,
    }


def post_publish_retry(
    article_id: str,
    failed_platforms: list[str],
) -> dict[str, Any] | None:
    """Re-dispatch publish for a list of previously-failed platforms.
    No medium-package, no state transition (article already at ready_to_publish).
    """
    from datetime import datetime

    if not _tg_configured():
        _log.info("TG not configured — skipping publish retry for %s", article_id)
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        _log.warning("no review chat_id — skipping publish retry for %s", article_id)
        return None
    failed_platforms = [p for p in failed_platforms if p]
    if not failed_platforms:
        _log.warning("publish retry with empty platform list for %s", article_id)
        return None

    # Q3: 入口 ETA 自适应通知
    eta = _estimate_eta_seconds(failed_platforms)
    try:
        tg_client.send_message(
            chat_id,
            f"⏳ 重试中... 预计 ~{eta}s ({len(failed_platforms)} 平台 ETA)\n\n云端实际可能浮动。",
            parse_mode=None,
        )
    except Exception:
        pass

    env = os.environ.copy()
    # Q4: 自适应 publish timeout
    publish_timeout = min(300 + 200 * len(failed_platforms), 1800)
    res = _run_subprocess(
        _af_argv("publish", article_id, "--platforms", ",".join(failed_platforms)),
        env=env, timeout=publish_timeout, label="dispatch-retry",
    )
    publish_exit = (res.returncode if res else None)

    dispatch_results = _collect_dispatch_results(article_id, failed_platforms)
    summary_md, retry_kb, retry_sid = render.render_dispatch_summary(
        article_id=article_id,
        results=dispatch_results,
    )

    # Q7: 摘要超长保护 + dispatch_results.json attach
    SUMMARY_LIMIT = 3500
    results_path: Path | None = None
    estimated_len = len(summary_md)
    if estimated_len > SUMMARY_LIMIT:
        try:
            results_path = (
                agentflow_home() / "drafts" / article_id / "dispatch_results.json"
            )
            results_path.write_text(
                json.dumps(
                    {
                        "article_id": article_id,
                        "platforms": failed_platforms,
                        "results": dispatch_results,
                        "dispatched_at": datetime.now().isoformat(),
                        "phase": "retry",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as err:  # pragma: no cover
            _log.warning("dispatch_results.json (retry) write failed: %s", err)
            results_path = None

        try:
            tg_client.send_message(
                chat_id,
                f"ℹ 摘要超长 ({estimated_len} 字符)，完整结果见附件 dispatch_results.json",
                parse_mode=None,
            )
        except Exception:
            pass

        truncated_results = []
        for r in dispatch_results:
            rcopy = dict(r)
            if rcopy.get("reason") and len(rcopy["reason"]) > 100:
                rcopy["reason"] = rcopy["reason"][:97] + "..."
            truncated_results.append(rcopy)
        summary_md, retry_kb, retry_sid = render.render_dispatch_summary(
            article_id=article_id,
            results=truncated_results,
        )

    try:
        tg_client.send_message(
            chat_id, summary_md, reply_markup=retry_kb if retry_kb else None,
        )
    except Exception as err:  # pragma: no cover
        _log.warning("retry summary TG send failed: %s", err)

    if results_path and results_path.exists():
        try:
            tg_client.send_document(chat_id, results_path, parse_mode=None)
        except Exception as err:  # pragma: no cover
            _log.warning("dispatch_results.json (retry) send_document failed: %s", err)

    return {
        "gate": "D",
        "article_id": article_id,
        "platforms": failed_platforms,
        "publish_exit": publish_exit,
        "retry_short_id": retry_sid,
    }


# ---------------------------------------------------------------------------
# Q2 — Daily publish-mark digest (24h cooldown)
# ---------------------------------------------------------------------------


def post_publish_digest() -> dict[str, Any] | None:
    """Daily digest of ``ready_to_publish`` articles older than 24h.

    Sends a single summary message (no per-article ping). Called from
    ``daemon._scan_timeouts`` with a 24h cooldown via the ``__digest__`` key
    in ``timeout_state.json``. Read-only; no buttons. Returns the dict
    ``{"count": N, "items": [...]}`` on send, ``None`` on no-op.
    """
    if not _tg_configured():
        return None
    chat_id = _daemon.get_review_chat_id()
    if chat_id is None:
        return None

    from datetime import datetime, timezone, timedelta

    pending = _state.articles_in_state([_state.STATE_READY_TO_PUBLISH])
    if not pending:
        return None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    items: list[dict[str, Any]] = []
    for aid in pending:
        try:
            history = _state.gate_history(aid)
            if not history:
                continue
            last_ts_raw = history[-1].get("timestamp") or ""
            try:
                last_ts = datetime.fromisoformat(last_ts_raw)
            except ValueError:
                continue
            if last_ts >= cutoff:
                continue  # < 24h old, not stale yet
            age_h = (now - last_ts).total_seconds() / 3600
            try:
                meta = _read_metadata(aid)
            except Exception:
                meta = {}
            title = meta.get("title", "(untitled)")
            items.append({
                "article_id": aid,
                "title": title,
                "age_hours": round(age_h, 1),
            })
        except Exception:
            continue

    if not items:
        return None

    try:
        text = render.render_publish_digest(items)
        tg_client.send_message(chat_id, text, parse_mode="MarkdownV2")
        return {"count": len(items), "items": items}
    except Exception as err:
        _log.warning("post_publish_digest send failed: %s", err)
        return None
