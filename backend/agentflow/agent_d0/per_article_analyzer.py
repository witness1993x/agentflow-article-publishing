"""Per-article style analysis.

Loads the ``d0_style_learn_per_article.md`` prompt and calls the LLM. In mock
mode the fixture is returned verbatim.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger

_log = get_logger("agent_d0.per_article")

# prompts live at repo/backend/prompts
_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d0_style_learn_per_article.md"
)


@lru_cache(maxsize=1)
def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


_DEFAULT_ANALYSIS: dict[str, Any] = {
    "language": "bilingual",
    "zh_ratio": 0.5,
    "avg_para_words": 60,
    "max_para_words": 100,
    "min_para_words": 20,
    "avg_sentence_words": 15,
    "voice_principles": [],
    "signature_phrases": [],
    "taboo_candidates": [],
    "tone_intensity": "medium",
    "structural_pattern": "unknown",
    "emoji_density": "low",
}

_REQUIRED_KEYS = set(_DEFAULT_ANALYSIS.keys())


def _coerce_analysis(raw: Any) -> dict[str, Any]:
    """Make the LLM output conform to the expected schema; fill missing keys."""
    if not isinstance(raw, dict):
        _log.warning("Per-article analysis was not a dict; using defaults")
        return dict(_DEFAULT_ANALYSIS)

    out: dict[str, Any] = dict(_DEFAULT_ANALYSIS)
    out.update(raw)

    missing = _REQUIRED_KEYS - set(out.keys())
    if missing:
        _log.warning("Per-article analysis missing keys: %s", sorted(missing))
        for k in missing:
            out[k] = _DEFAULT_ANALYSIS[k]

    # Guard list-valued fields against None.
    for list_key in ("voice_principles", "signature_phrases", "taboo_candidates"):
        if out.get(list_key) is None:
            out[list_key] = []

    return out


async def analyze_article(article: dict[str, Any]) -> dict[str, Any]:
    """Run per-article style analysis against the LLM and return a dict."""
    text = article.get("text") or ""
    title_hint = article.get("title") or ""

    prompt_template = _load_prompt()
    prompt = prompt_template.replace("{article_text}", text).replace(
        "{article_title_hint}", title_hint
    )

    client = LLMClient()
    try:
        raw = await client.chat_json(
            prompt_family="d0-per-article",
            prompt=prompt,
            max_tokens=2000,
        )
    except Exception as err:
        _log.warning(
            "Per-article LLM call failed for source_id=%s: %s; using defaults",
            article.get("source_id"),
            err,
        )
        return dict(_DEFAULT_ANALYSIS)

    return _coerce_analysis(raw)
