"""Whole-article structure audit (v1.0.29).

Inserted between ``fill_all_sections`` returning and Gate B firing.
Existing D2 lints (``specificity_lint``, ``topic_spine_lint``,
``compliance_checker``, ``language_lint``) are point checks: they catch
single-paragraph or token-presence failures. None of them score the
article as a whole — section-to-section cohesion, anchor distribution
across the body, opening/closing thesis-callback, or voice drift mid-
piece.

This module fills that gap with a single LLM call and three verdicts:

* ``pass``     — score ≥ patch_threshold; draft unchanged, Gate B fires
* ``patch``    — score in [rewrite_threshold, patch_threshold); offending
                 sections are re-filled with extra structural warnings
                 appended to the per-section prompt; one round only
* ``rewrite``  — score < rewrite_threshold; the auditor itself produces
                 a full replacement draft from the same hotspot + ctx,
                 written to disk in place of the original

Disabled by default-but-on. Set ``AGENTFLOW_D2_AUDIT_ENABLED=false`` to
turn off entirely (always returns ``verdict=skipped``).

Dimensions (4):

  cohesion          — does each section build on / reference the prior?
  anchor_density    — are publisher product_facts/perspectives evenly
                      distributed front-to-back, not just front-loaded?
  thesis_callback   — does the closing restate / deepen / turn the
                      opening's central claim?
  voice_consistency — pronoun + voice stays stable throughout
                      (catches "我们做的" → "行业应该" mid-drift)

Behavior is identical local vs deployed — the operator on a Skill host
flips ``AGENTFLOW_D2_AUDIT_ENABLED=true`` in ``.env`` and restarts the
daemon; no recursive sub-agent spawning required.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger
from agentflow.shared.markdown_utils import count_words
from agentflow.shared.memory import (
    append_memory_event,
    load_current_intent,
)
from agentflow.shared.models import DraftOutput, FilledSection, Hotspot
from agentflow.shared.topic_profiles import (
    render_publisher_account_block,
    resolve_publisher_account_from_intent,
)

_log = get_logger("agent_d2.structure_audit")

_AUDIT_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d2_structure_audit.md"
)
_REWRITE_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d2_full_rewrite.md"
)

_DEFAULT_PATCH_THRESHOLD = 0.75
_DEFAULT_REWRITE_THRESHOLD = 0.50
_DEFAULT_MAX_PATCH_ROUNDS = 1
_AUDIT_DIMS = ("cohesion", "anchor_density", "thesis_callback", "voice_consistency")


@dataclass
class AuditOutcome:
    verdict: str  # "pass" | "patch" | "rewrite" | "skipped" | "error"
    score: float
    dim_scores: dict[str, float] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    rewritten_draft: DraftOutput | None = None
    patched_section_indices: list[int] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": self.score,
            "dim_scores": dict(self.dim_scores),
            "issues": list(self.issues),
            "rewrote_draft": self.rewritten_draft is not None,
            "patched_section_indices": list(self.patched_section_indices),
            "error": self.error,
        }


def _is_enabled() -> bool:
    raw = os.environ.get("AGENTFLOW_D2_AUDIT_ENABLED", "true").strip().lower()
    return raw not in {"false", "0", "no", "off", ""}


def _read_threshold(env_key: str, default: float) -> float:
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        _log.warning("invalid %s=%r; using default %s", env_key, raw, default)
        return default
    return max(0.0, min(1.0, v))


def _max_patch_rounds() -> int:
    raw = (os.environ.get("AGENTFLOW_D2_AUDIT_MAX_PATCH_ROUNDS") or "").strip()
    if not raw:
        return _DEFAULT_MAX_PATCH_ROUNDS
    try:
        return max(0, min(3, int(raw)))
    except ValueError:
        return _DEFAULT_MAX_PATCH_ROUNDS


def _load_prompt(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    marker = "```text"
    start = raw.find(marker)
    if start == -1:
        return raw
    start += len(marker)
    end = raw.rfind("```")
    if end == -1 or end <= start:
        return raw[start:].strip()
    return raw[start:end].strip()


def _format_draft_for_audit(draft: DraftOutput) -> str:
    parts: list[str] = [f"# {draft.title}", ""]
    for idx, sec in enumerate(draft.sections):
        parts.append(f"## [Section {idx}] {sec.heading}")
        parts.append("")
        parts.append(sec.content_markdown.strip())
        parts.append("")
    return "\n".join(parts).strip()


def _format_hotspot_context(hotspot: Hotspot) -> str:
    lines: list[str] = [f"**topic_one_liner**: {hotspot.topic_one_liner or '(none)'}"]
    refs = list(getattr(hotspot, "source_references", None) or [])[:5]
    if refs:
        lines.append("")
        lines.append("**source_references** (前 5):")
        for r in refs:
            if isinstance(r, dict):
                author = str(r.get("author") or "?")
                snippet = str(r.get("text_snippet") or "")[:200]
            else:
                author = str(getattr(r, "author", "?"))
                snippet = str(getattr(r, "text_snippet", ""))[:200]
            lines.append(f"- {author}: {snippet!r}")
    angles = list(getattr(hotspot, "suggested_angles", None) or [])[:3]
    if angles:
        lines.append("")
        lines.append("**suggested_angles** (前 3):")
        for a in angles:
            if isinstance(a, dict):
                lines.append(f"- {a.get('angle') or a.get('title') or ''}")
            else:
                lines.append(f"- {getattr(a, 'angle', '') or getattr(a, 'title', '')}")
    return "\n".join(lines)


def _normalize_score(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


def _parse_audit_response(payload: dict[str, Any]) -> tuple[float, dict[str, float], list[str]]:
    dim_raw = payload.get("dim_scores") or {}
    if not isinstance(dim_raw, dict):
        dim_raw = {}
    dim_scores = {dim: _normalize_score(dim_raw.get(dim, 0.0)) for dim in _AUDIT_DIMS}
    overall_raw = payload.get("score")
    if overall_raw is None:
        score = sum(dim_scores.values()) / len(_AUDIT_DIMS) if dim_scores else 0.0
    else:
        score = _normalize_score(overall_raw)
    issues_raw = payload.get("issues") or []
    if isinstance(issues_raw, list):
        issues = [str(x).strip() for x in issues_raw if str(x or "").strip()]
    else:
        issues = []
    return score, dim_scores, issues[:20]


def _classify_verdict(score: float, patch_threshold: float, rewrite_threshold: float) -> str:
    if score >= patch_threshold:
        return "pass"
    if score >= rewrite_threshold:
        return "patch"
    return "rewrite"


def _section_indices_from_issues(
    issues: list[str], section_count: int
) -> list[int]:
    """Best-effort: pull section indices the issues call out.

    Auditor is asked to prefix issue strings with ``[Section N]`` (matching
    the format we feed it). Anything that doesn't parse, we ignore — the
    patch path falls back to re-filling every section if no indices were
    extracted.
    """
    pat = re.compile(r"\[Section\s+(\d+)\]", re.IGNORECASE)
    found: set[int] = set()
    for line in issues:
        for m in pat.finditer(line):
            try:
                idx = int(m.group(1))
            except ValueError:
                continue
            if 0 <= idx < section_count:
                found.add(idx)
    return sorted(found)


# ---------------------------------------------------------------------------
# Public — audit + apply
# ---------------------------------------------------------------------------


async def audit_draft(
    draft: DraftOutput,
    *,
    hotspot: Hotspot,
    style_profile: dict[str, Any],
    intent: dict[str, Any] | None = None,
    client: LLMClient | None = None,
) -> AuditOutcome:
    """Score a draft on 4 structural dimensions; return verdict + issues.

    Does NOT mutate the draft or call any patch/rewrite path. The caller
    decides what to do with the verdict — see ``audit_and_finalize`` in
    ``agent_d2/main.py`` for the standard wiring.
    """
    if not _is_enabled():
        return AuditOutcome(verdict="skipped", score=1.0)

    if not draft.sections:
        return AuditOutcome(
            verdict="skipped",
            score=0.0,
            error="empty draft (no sections)",
        )

    intent = intent if intent is not None else (load_current_intent() or {})
    publisher = resolve_publisher_account_from_intent(intent)
    publisher_block = render_publisher_account_block(publisher) if publisher else ""

    template = _load_prompt(_AUDIT_PROMPT_PATH)
    try:
        style_yaml = json.dumps(style_profile or {}, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        style_yaml = "{}"

    prompt = template.format(
        publisher_account_block=publisher_block or "(no publisher account configured)",
        style_profile_json=style_yaml,
        hotspot_context=_format_hotspot_context(hotspot),
        article_title=draft.title,
        article_body=_format_draft_for_audit(draft),
        section_count=len(draft.sections),
        total_words=draft.total_word_count,
        audit_dims=", ".join(_AUDIT_DIMS),
    )

    cli = client or LLMClient()
    try:
        payload = await cli.chat_json(
            prompt_family="d2_structure_audit",
            prompt=prompt,
            max_tokens=1500,
        )
    except Exception as err:  # pragma: no cover — network/LLM failure
        _log.warning("audit LLM call failed: %s", err)
        return AuditOutcome(
            verdict="error",
            score=1.0,
            error=str(err),
        )

    score, dim_scores, issues = _parse_audit_response(payload)
    patch_threshold = _read_threshold(
        "AGENTFLOW_D2_AUDIT_PATCH_THRESHOLD", _DEFAULT_PATCH_THRESHOLD
    )
    rewrite_threshold = _read_threshold(
        "AGENTFLOW_D2_AUDIT_REWRITE_THRESHOLD", _DEFAULT_REWRITE_THRESHOLD
    )
    if rewrite_threshold > patch_threshold:
        # User config error — fall back so rewrite remains the lower bar.
        rewrite_threshold = min(rewrite_threshold, patch_threshold)
    verdict = _classify_verdict(score, patch_threshold, rewrite_threshold)

    _log.info(
        "audit verdict=%s score=%.3f dims=%s issues=%d (patch>=%.2f, rewrite>=%.2f)",
        verdict,
        score,
        {k: round(v, 2) for k, v in dim_scores.items()},
        len(issues),
        patch_threshold,
        rewrite_threshold,
    )

    return AuditOutcome(
        verdict=verdict,
        score=score,
        dim_scores=dim_scores,
        issues=issues,
    )


async def rewrite_draft(
    draft: DraftOutput,
    *,
    hotspot: Hotspot,
    style_profile: dict[str, Any],
    audit_issues: list[str],
    intent: dict[str, Any] | None = None,
    client: LLMClient | None = None,
) -> DraftOutput:
    """Auditor-as-writer fallback. Generates a full replacement draft.

    Used only when ``audit_draft`` returns verdict ``rewrite``. The
    rewritten draft uses the same article_id / image_placeholders as the
    original — only ``title`` + ``sections`` + ``total_word_count`` are
    regenerated. Section count is preserved (one heading per original
    section) so downstream image placeholder paths keep working.
    """
    intent = intent if intent is not None else (load_current_intent() or {})
    publisher = resolve_publisher_account_from_intent(intent)
    publisher_block = render_publisher_account_block(publisher) if publisher else ""

    template = _load_prompt(_REWRITE_PROMPT_PATH)
    try:
        style_yaml = json.dumps(style_profile or {}, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        style_yaml = "{}"

    headings = [s.heading for s in draft.sections]
    issues_text = "\n".join(f"- {x}" for x in audit_issues) or "(no specific issues)"

    prompt = template.format(
        publisher_account_block=publisher_block or "(no publisher account configured)",
        style_profile_json=style_yaml,
        hotspot_context=_format_hotspot_context(hotspot),
        article_title=draft.title,
        section_headings="\n".join(f"{i}. {h}" for i, h in enumerate(headings, 1)),
        section_count=len(draft.sections),
        target_total_words=max(800, draft.total_word_count),
        audit_issues=issues_text,
    )

    cli = client or LLMClient()
    try:
        rewritten_md = await cli.chat_text(
            prompt_family="d2_full_rewrite",
            prompt=prompt,
            max_tokens=6000,
        )
    except Exception as err:  # pragma: no cover
        _log.warning("rewrite LLM call failed: %s", err)
        raise

    new_sections = _split_rewritten_into_sections(rewritten_md, headings)
    if not new_sections:
        # Defensive: if parsing fails, keep the original sections rather
        # than ship a broken draft to Gate B.
        _log.warning(
            "rewrite output unparseable; preserving original draft "
            "(article_id=%s, output_len=%d)",
            draft.article_id,
            len(rewritten_md or ""),
        )
        return draft

    new_total = sum(s.word_count for s in new_sections)
    return DraftOutput(
        article_id=draft.article_id,
        title=draft.title,
        sections=new_sections,
        total_word_count=new_total,
        image_placeholders=list(draft.image_placeholders),
    )


def _split_rewritten_into_sections(
    markdown: str, expected_headings: list[str]
) -> list[FilledSection]:
    """Cut the rewritten markdown into FilledSection blocks.

    Strategy: split on ``^## `` lines (the rewrite prompt mandates ## per
    section). If we get the wrong section count, return [] and the caller
    falls back to the original draft.
    """
    if not markdown:
        return []
    chunks = re.split(r"(?m)^##\s+", markdown.strip())
    # The first chunk is anything before the first ##; usually the # title
    # line. Drop it.
    if chunks and not chunks[0].lstrip().startswith("##"):
        chunks = chunks[1:]
    out: list[FilledSection] = []
    for i, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        first_line, _, rest = chunk.partition("\n")
        heading = first_line.strip()
        body = rest.strip()
        if not heading:
            continue
        # Prefer the original heading if order matches — keeps image
        # placeholder section_heading lookups stable.
        if i < len(expected_headings):
            heading = expected_headings[i]
        out.append(
            FilledSection(
                heading=heading,
                content_markdown=body,
                word_count=count_words(body),
                compliance_score=1.0,
            )
        )
    if len(out) != len(expected_headings):
        # Section count mismatch is a parse failure in disguise.
        return []
    return out


# ---------------------------------------------------------------------------
# Patch path — re-fill specific sections with extra structural warnings
# ---------------------------------------------------------------------------


async def patch_draft(
    draft: DraftOutput,
    *,
    hotspot: Hotspot,
    style_profile: dict[str, Any],
    skeleton_sections: list[Any],
    target_indices: list[int],
    audit_issues: list[str],
    article_id: str,
) -> tuple[DraftOutput, list[int]]:
    """Re-run section_filler.fill_section for the flagged sections.

    The audit's issues are appended to the per-section prompt as a
    "structural warning" suffix so the LLM has a concrete steer instead
    of just regenerating the same shape. Returns ``(new_draft, indices)``
    where indices is the list of sections actually re-filled.
    """
    from agentflow.agent_d2.section_filler import fill_section

    if not target_indices:
        # No specific sections called out — patch all that are below
        # average word count, capped at first 3 (avoids regenerating the
        # whole article under the "patch" verdict).
        avg = (
            sum(s.word_count for s in draft.sections) / len(draft.sections)
            if draft.sections
            else 0
        )
        target_indices = [
            i for i, s in enumerate(draft.sections) if s.word_count < avg
        ][:3]
        if not target_indices and draft.sections:
            # Fallback: regenerate the closing section, where most
            # voice/thesis-callback drift shows up.
            target_indices = [len(draft.sections) - 1]

    completed = list(draft.sections)
    issue_suffix = "\n".join(f"- {x}" for x in audit_issues[:8])
    refilled: list[int] = []

    for idx in target_indices:
        if not (0 <= idx < len(skeleton_sections)):
            continue
        section_obj = skeleton_sections[idx]
        # Build the same context fill_section expects, with the rest of
        # the article (in current state) as previous_sections so the LLM
        # can re-stitch cohesion against neighbors.
        previous = [s.to_dict() for j, s in enumerate(completed) if j < idx]
        context: dict[str, Any] = {
            "title": draft.title,
            "opening": "",  # carried via skeleton in normal flow; left blank here
            "closing": "",
            "full_outline": skeleton_sections,
            "previous_sections": previous,
            "article_id": article_id,
            "structure_audit_warnings": (
                f"\n\n## 结构审计反馈（v1.0.29）\n\n上一版本本节存在以下结构问题，"
                f"请这一版重写时刻意修正：\n\n{issue_suffix}\n"
            ),
        }
        try:
            new_section = await fill_section(section_obj, context, style_profile)
        except Exception as err:  # pragma: no cover — bail to original
            _log.warning("patch fill_section failed for idx=%d: %s", idx, err)
            continue
        completed[idx] = new_section
        refilled.append(idx)

    new_total = sum(s.word_count for s in completed)
    new_draft = DraftOutput(
        article_id=draft.article_id,
        title=draft.title,
        sections=completed,
        total_word_count=new_total,
        image_placeholders=list(draft.image_placeholders),
    )
    return new_draft, refilled


# ---------------------------------------------------------------------------
# High-level: run audit + apply outcome + persist + memory event
# ---------------------------------------------------------------------------


async def audit_and_finalize(
    draft: DraftOutput,
    *,
    hotspot: Hotspot,
    style_profile: dict[str, Any],
    skeleton_sections: list[Any] | None = None,
    intent: dict[str, Any] | None = None,
    save_fn: Any = None,
) -> tuple[DraftOutput, AuditOutcome]:
    """Run the audit, act on the verdict, persist, return (final_draft, outcome).

    ``save_fn`` is the module-level ``save_draft`` from ``agent_d2.main``;
    passed in to avoid an import cycle. If ``None``, the caller is
    responsible for persisting the returned draft.

    ``skeleton_sections`` is required for the ``patch`` path (it needs
    Section objects to re-call fill_section). Pass the list from
    ``skeleton.section_outline``. If ``None`` and verdict is patch, the
    patch path is downgraded to ``pass`` (no-op + telemetry note).
    """
    outcome = await audit_draft(
        draft,
        hotspot=hotspot,
        style_profile=style_profile,
        intent=intent,
    )

    final_draft = draft
    if outcome.verdict == "patch":
        if skeleton_sections is None:
            _log.warning(
                "audit verdict=patch but skeleton not supplied; treating as pass "
                "(article_id=%s)",
                draft.article_id,
            )
            outcome.verdict = "pass"
        else:
            target_indices = _section_indices_from_issues(
                outcome.issues, len(draft.sections)
            )
            max_rounds = _max_patch_rounds()
            if max_rounds <= 0:
                outcome.verdict = "pass"
            else:
                final_draft, refilled = await patch_draft(
                    draft,
                    hotspot=hotspot,
                    style_profile=style_profile,
                    skeleton_sections=skeleton_sections,
                    target_indices=target_indices,
                    audit_issues=outcome.issues,
                    article_id=draft.article_id,
                )
                outcome.patched_section_indices = refilled
                if save_fn:
                    save_fn(final_draft)
    elif outcome.verdict == "rewrite":
        try:
            rewritten = await rewrite_draft(
                draft,
                hotspot=hotspot,
                style_profile=style_profile,
                audit_issues=outcome.issues,
                intent=intent,
            )
        except Exception as err:  # pragma: no cover
            _log.warning(
                "rewrite path failed (article_id=%s): %s — falling back to original",
                draft.article_id,
                err,
            )
            outcome.error = f"rewrite_failed: {err}"
            outcome.verdict = "error"
        else:
            final_draft = rewritten
            outcome.rewritten_draft = rewritten
            if save_fn:
                save_fn(final_draft)

    try:
        append_memory_event(
            "d2_structure_audit",
            article_id=draft.article_id,
            payload=outcome.to_dict(),
        )
    except Exception as err:  # pragma: no cover
        _log.debug("audit memory event failed: %s", err)

    return final_draft, outcome
