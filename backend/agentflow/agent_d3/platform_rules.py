"""Platform rule loader with hard-coded defaults + YAML override.

Resolution order:
1. ``~/.agentflow/platform_rules.yaml`` (user override)
2. ``<project_root>/config-examples/platform_rules.example.yaml`` (repo default)
3. Hard-coded ``DEFAULT_RULES`` embedded below (ultimate fallback)

The returned dict is keyed by platform name (``medium``, ``linkedin_article``,
``ghost_wordpress`` are guaranteed present in v0.1). Each value is the per-
platform rule mapping consumed by the adapters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.logger import get_logger

_log = get_logger("agent_d3.platform_rules")

# Ultimate fallback baked into code so the pipeline runs even if both YAML
# files are missing. Matches the MVP spec + handoff plan.
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "medium": {
        "enabled": True,
        "paragraph_max_words": 60,
        "paragraph_min_words": 15,
        "heading_style": "sentence_case",
        "max_heading_levels": 3,
        "length_preference": [1500, 3000],
        "length_hard_limit": None,
        "emoji_density": "low",
        "quote_style": "blockquote",
        "emphasis_style": "bold",
        "image_placement": "between_sections",
        "max_tags": 5,
        "subtitle_supported": True,
        "canonical_url_supported": True,
        "image_policy": {
            "max_images": 10,
            "preferred_image_count": [3, 5],
            "feature_image": "first",
            "inline_image_mode": "native",
            "caption_from_alt": True,
            "drop_images_beyond_max": True,
        },
    },
    "linkedin_article": {
        "enabled": True,
        "paragraph_max_words": 40,
        "paragraph_min_words": 10,
        "heading_style": "sentence_case",
        "max_heading_levels": 2,
        "length_preference": [800, 1500],
        "length_hard_limit": 1500,  # per spec: hard upper bound in words
        "emoji_density": "medium",
        "quote_style": "bold_highlight",
        "emphasis_style": "bold",
        "image_placement": "top_only",
        "max_tags": 5,
        "subtitle_supported": False,
        "canonical_url_supported": True,
        "ending_should_have_question": True,
        "image_policy": {
            "max_images": 5,
            "preferred_image_count": [1, 2],
            "feature_image": "first",
            "inline_image_mode": "feature_only",
            "caption_from_alt": False,
            "drop_images_beyond_max": True,
        },
    },
    "ghost_wordpress": {
        "enabled": True,
        "paragraph_max_words": 70,
        "paragraph_min_words": 15,
        "heading_style": "title_case",
        "max_heading_levels": 4,
        "length_preference": [1000, 5000],
        "length_hard_limit": None,
        "emoji_density": "low",
        "quote_style": "blockquote",
        "emphasis_style": "bold",
        "image_placement": "as_needed",
        "max_tags": 10,
        "subtitle_supported": True,
        "canonical_url_supported": True,
        "meta_description_max_chars": 160,
        "image_policy": {
            "max_images": 10,
            "preferred_image_count": [3, 5],
            "feature_image": "first",
            "inline_image_mode": "native",
            "caption_from_alt": True,
        },
    },
    "twitter_thread": {
        "image_policy": {
            "max_images": 4,
            "preferred_image_count": [1, 2],
            "feature_image": "first",
            "per_tweet_max": 4,
            "inline_image_mode": "split_across_thread",
        },
    },
    "email_newsletter": {
        "image_policy": {
            "max_images": 5,
            "preferred_image_count": [1, 3],
            "feature_image": "first",
            "inline_image_mode": "cid_attachment_or_cdn",
            "force_max_width_px": 600,
        },
    },
}


USER_RULES_PATH = agentflow_home() / "platform_rules.yaml"

# config-examples lives at project_root/config-examples; this file sits at
# backend/agentflow/agent_d3/platform_rules.py — 4 parents up reaches the repo
# root where config-examples/ lives.
_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "config-examples"
EXAMPLE_RULES_PATH = _EXAMPLES_DIR / "platform_rules.example.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Platform rules at {path} is not a YAML mapping")
    return data


def load_rules() -> dict[str, dict[str, Any]]:
    """Load platform rules. Returns a dict keyed by platform name.

    Merges the YAML source (if any) *over* the hard-coded defaults so a
    user-supplied file can omit keys and still get sane values.
    """
    merged: dict[str, dict[str, Any]] = {
        k: dict(v) for k, v in DEFAULT_RULES.items()
    }

    source: Path | None = None
    if USER_RULES_PATH.exists():
        source = USER_RULES_PATH
    elif EXAMPLE_RULES_PATH.exists():
        source = EXAMPLE_RULES_PATH

    if source is None:
        _log.info("platform_rules: no YAML found, using hard-coded defaults")
        return merged

    try:
        loaded = _read_yaml(source)
    except Exception as err:  # pragma: no cover - defensive
        _log.warning("platform_rules: failed to parse %s: %s", source, err)
        return merged

    for platform, rules in loaded.items():
        if not isinstance(rules, dict):
            continue
        if platform in merged:
            merged[platform].update(rules)
        else:
            merged[platform] = dict(rules)

    _log.debug("platform_rules: loaded from %s", source)
    return merged


def rules_for(platform: str, style_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return effective rules for one platform, optionally overlaid with style profile overrides."""
    all_rules = load_rules()
    base = dict(all_rules.get(platform) or DEFAULT_RULES.get(platform) or {})

    if style_profile:
        emoji_prefs = style_profile.get("emoji_preferences") or {}
        density_by_platform = emoji_prefs.get("density_by_platform") or {}
        if platform in density_by_platform:
            base["emoji_density"] = density_by_platform[platform]

    return base
