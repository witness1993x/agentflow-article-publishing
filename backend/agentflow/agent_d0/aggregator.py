"""Aggregator: merge per-article analyses into a final style_profile dict."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger

_log = get_logger("agent_d0.aggregator")

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d0_style_aggregate.md"
)

_REQUIRED_TOP_KEYS: tuple[str, ...] = (
    "identity",
    "content_matrix",
    "voice_principles",
    "taboos",
    "tone",
    "paragraph_preferences",
    "emoji_preferences",
    "citation_preferences",
    "reference_samples",
    "_meta",
)


@lru_cache(maxsize=1)
def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Default skeletons
# --------------------------------------------------------------------------- #


def _default_identity(hint: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "name": "Mobius",
        "handle": "(待作者填写)",
        "positioning": "(待作者填写)",
        "persona": "",
    }
    if hint:
        for k, v in hint.items():
            if v is not None:
                base[k] = v
    return base


def _default_series_a() -> dict[str, Any]:
    return {
        "name": "Series A",
        "theme": "(待作者填写)",
        "typical_length_words": [1500, 2500],
        "primary_language": "zh",
        "language_mix_ratio": 0.6,
        "opening_style": "scenario_driven",
        "closing_style": "open_question",
        "typical_sections": [],
        "target_platforms_primary": ["ghost_wordpress"],
        "target_platforms_secondary": ["medium", "linkedin_article"],
        "target_platforms_skip": [],
    }


def _default_taboos() -> dict[str, Any]:
    return {
        "vocabulary": [],
        "sentence_patterns": [
            "综上所述",
            "总的来说",
            "值得注意的是",
            "毋庸置疑",
            "首先...其次...最后",
            "一方面...另一方面",
        ],
        "contexts": [],
    }


def _default_tone() -> dict[str, Any]:
    return {
        "default_intensity": "medium",
        "intensity_by_series": {"series_A": "medium"},
    }


def _default_paragraph_prefs() -> dict[str, Any]:
    return {
        "average_length_words": 60,
        "max_length_words": 100,
        "min_length_words": 15,
        "prefer_short_sentences": True,
        "use_bullet_lists": "moderate",
        "use_numbered_lists": "low",
    }


def _default_emoji_prefs() -> dict[str, Any]:
    return {
        "default_density": "low",
        "density_by_platform": {
            "medium": "low",
            "linkedin_article": "medium",
            "ghost_wordpress": "low",
        },
    }


def _default_citation_prefs() -> dict[str, Any]:
    return {
        "external_sources_frequency": "medium",
        "prefer_primary_sources": True,
        "quote_style": "direct_quote_with_link",
    }


# --------------------------------------------------------------------------- #
# Merge helpers
# --------------------------------------------------------------------------- #


def _merge_dict_over_defaults(
    defaults: dict[str, Any], incoming: Any
) -> dict[str, Any]:
    """Overlay ``incoming`` onto ``defaults`` (shallow per key, recursive for dicts)."""
    if not isinstance(incoming, dict):
        return dict(defaults)

    merged: dict[str, Any] = dict(defaults)
    for k, v in incoming.items():
        if (
            k in merged
            and isinstance(merged[k], dict)
            and isinstance(v, dict)
        ):
            merged[k] = _merge_dict_over_defaults(merged[k], v)
        else:
            merged[k] = v
    return merged


def _normalize_content_matrix(raw: Any) -> dict[str, Any]:
    """Ensure ``content_matrix`` has a complete series_A block."""
    default_series = _default_series_a()

    if not isinstance(raw, dict):
        return {"series_A": default_series}

    series_a_raw = raw.get("series_A")
    series_a = _merge_dict_over_defaults(default_series, series_a_raw)

    out: dict[str, Any] = dict(raw)
    out["series_A"] = series_a
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def aggregate(
    per_article_analyses: list[dict[str, Any]],
    identity_hint: dict[str, Any] | None = None,
    *,
    source_article_hashes: list[str] | None = None,
    recompute_generation: int = 0,
) -> dict[str, Any]:
    """Call the aggregate LLM prompt, then post-process to enforce schema."""
    article_count = len(per_article_analyses)
    prompt_template = _load_prompt()
    analyses_json = json.dumps(per_article_analyses, ensure_ascii=False, indent=2)
    prompt = prompt_template.replace(
        "{per_article_analyses}", analyses_json
    ).replace("{article_count}", str(article_count))

    client = LLMClient()
    try:
        raw = await client.chat_json(
            prompt_family="d0-aggregate",
            prompt=prompt,
            max_tokens=3000,
        )
    except Exception as err:
        _log.warning("Aggregate LLM call failed: %s; using pure defaults", err)
        raw = {}

    if not isinstance(raw, dict):
        _log.warning("Aggregate output not a dict; using pure defaults")
        raw = {}

    return _post_process(
        raw,
        identity_hint=identity_hint,
        article_count=article_count,
        source_article_hashes=source_article_hashes or [],
        recompute_generation=recompute_generation,
    )


def _post_process(
    raw: dict[str, Any],
    *,
    identity_hint: dict[str, Any] | None,
    article_count: int,
    source_article_hashes: list[str],
    recompute_generation: int,
) -> dict[str, Any]:
    today_iso = date.today().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    profile: dict[str, Any] = {}

    # ---- top-level scalars ------------------------------------------------
    profile["version"] = raw.get("version") or "1.0"
    profile["last_updated"] = today_iso
    profile["author_id"] = raw.get("author_id") or "author"

    # ---- identity ---------------------------------------------------------
    profile["identity"] = _merge_dict_over_defaults(
        _default_identity(identity_hint), raw.get("identity")
    )

    # ---- content_matrix ---------------------------------------------------
    profile["content_matrix"] = _normalize_content_matrix(raw.get("content_matrix"))

    # ---- voice_principles -------------------------------------------------
    vp = raw.get("voice_principles")
    if not isinstance(vp, list) or not vp:
        vp = []
    cleaned_vp: list[dict[str, Any]] = []
    for item in vp:
        if isinstance(item, dict) and "key" in item:
            cleaned_vp.append(
                {
                    "key": str(item.get("key") or "").strip() or "unknown",
                    "description": str(item.get("description") or "").strip(),
                }
            )
    profile["voice_principles"] = cleaned_vp

    # ---- taboos -----------------------------------------------------------
    profile["taboos"] = _merge_dict_over_defaults(_default_taboos(), raw.get("taboos"))

    # ---- tone -------------------------------------------------------------
    profile["tone"] = _merge_dict_over_defaults(_default_tone(), raw.get("tone"))

    # ---- paragraph_preferences -------------------------------------------
    profile["paragraph_preferences"] = _merge_dict_over_defaults(
        _default_paragraph_prefs(), raw.get("paragraph_preferences")
    )

    # ---- emoji_preferences ------------------------------------------------
    profile["emoji_preferences"] = _merge_dict_over_defaults(
        _default_emoji_prefs(), raw.get("emoji_preferences")
    )

    # ---- citation_preferences --------------------------------------------
    profile["citation_preferences"] = _merge_dict_over_defaults(
        _default_citation_prefs(), raw.get("citation_preferences")
    )

    # ---- reference_samples -----------------------------------------------
    rs = raw.get("reference_samples")
    profile["reference_samples"] = rs if isinstance(rs, list) else []

    # ---- _meta ------------------------------------------------------------
    incoming_meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
    profile["_meta"] = {
        "version": incoming_meta.get("version") or "1.0",
        "source_article_count": article_count,
        "source_article_hashes": list(source_article_hashes),
        "generated_at": now_iso,
        "recompute_generation": int(recompute_generation),
    }

    # ---- validate top-level ----------------------------------------------
    missing = [k for k in _REQUIRED_TOP_KEYS if k not in profile]
    if missing:
        _log.warning("Aggregated profile missing top-level keys: %s", missing)

    return profile
