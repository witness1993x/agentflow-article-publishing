"""Stage 1 of D2 — generate skeleton candidates (titles / openings / outline / closings).

Inputs come from D1 (the chosen Hotspot + a SuggestedAngle) plus the user's
style profile and content matrix. We render the ``d2_skeleton_generation.md``
prompt, fire one ``chat_json`` call, and map the response into a
``SkeletonOutput``.

In ``MOCK_LLM=true`` mode this reads ``shared/mocks/d2-skeleton.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger
from agentflow.shared.memory import (
    append_memory_event,
    intent_query_text,
    load_current_intent,
    render_topic_intent_block,
)
from agentflow.shared.models import (
    Hotspot,
    SkeletonOutput,
    SuggestedAngle,
)
from agentflow.shared.topic_profiles import (
    render_publisher_account_block,
    resolve_publisher_account_from_intent,
)

_log = get_logger("agent_d2.skeleton")

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d2_skeleton_generation.md"
)


def _load_prompt() -> str:
    """Strip the markdown preamble and ```text fence so we're left with the real prompt body."""
    raw = _PROMPT_PATH.read_text(encoding="utf-8")
    # The prompt file wraps the actual template in a ```text ... ``` fence.
    # Pull out everything between the first ```text and the last ``` .
    marker = "```text"
    start = raw.find(marker)
    if start == -1:
        return raw
    start += len(marker)
    end = raw.rfind("```")
    if end == -1 or end <= start:
        return raw[start:].strip()
    return raw[start:end].strip()


_PROMPT_TEMPLATE = _load_prompt()


def _series_config(content_matrix: dict[str, Any], target_series: str) -> dict[str, Any]:
    """Find the series config by key `series_<X>`, falling back to a loose lookup."""
    key = f"series_{target_series}"
    if key in content_matrix and isinstance(content_matrix[key], dict):
        return content_matrix[key]
    # Some files store it under content_matrix.series_A
    cm = content_matrix.get("content_matrix")
    if isinstance(cm, dict) and key in cm:
        return cm[key]
    # Case-insensitive fallback
    for k, v in content_matrix.items():
        if isinstance(v, dict) and k.lower() == key.lower():
            return v
    return {}


def _fmt_length(series: dict[str, Any]) -> str:
    length = series.get("typical_length_words")
    if isinstance(length, (list, tuple)) and len(length) == 2:
        return f"{length[0]}-{length[1]} words"
    if isinstance(length, int):
        return str(length)
    return "1500 words"


def _fmt_list(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value)
    return str(value or "")


async def generate_skeleton(
    hotspot: Hotspot,
    chosen_angle: SuggestedAngle,
    style_profile: dict[str, Any],
    content_matrix: dict[str, Any],
    target_series: str = "A",
    target_length_words: int = 1500,
    article_id: str | None = None,
) -> SkeletonOutput:
    """Render the D2 skeleton prompt and parse the JSON response → SkeletonOutput."""

    series = _series_config(content_matrix, target_series)

    # Pull any active TopicIntent and render it as a prompt block. Absent
    # intent → empty string, so the placeholder collapses cleanly.
    intent = load_current_intent()
    intent_text = intent_query_text(intent)
    intent_block = render_topic_intent_block(intent)
    publisher_block = render_publisher_account_block(
        resolve_publisher_account_from_intent(intent)
    )

    substitutions = {
        "style_profile_yaml": yaml.safe_dump(
            style_profile, allow_unicode=True, sort_keys=False
        ),
        "topic_intent_block": intent_block,
        "publisher_account_block": publisher_block,
        "target_series": target_series,
        "series_description": series.get("theme") or series.get("description") or "",
        "typical_length": _fmt_length(series),
        "primary_language": series.get("primary_language", "bilingual"),
        "opening_style": series.get("opening_style", "scenario_driven"),
        "closing_style": series.get("closing_style", "open_question"),
        "typical_sections": _fmt_list(series.get("typical_sections")),
        "topic": hotspot.topic_one_liner,
        "chosen_angle": chosen_angle.angle,
        "angle_fit_explanation": chosen_angle.fit_explanation,
        "target_length": target_length_words,
    }

    prompt = _render(_PROMPT_TEMPLATE, substitutions)

    client = LLMClient()
    data = await client.chat_json(
        prompt_family="d2-skeleton",
        prompt=prompt,
        max_tokens=3000,
    )

    skeleton = SkeletonOutput.from_dict(data)
    _log.info(
        "skeleton generated: %d titles / %d openings / %d sections / %d closings",
        len(skeleton.title_candidates),
        len(skeleton.opening_candidates),
        len(skeleton.section_outline),
        len(skeleton.closing_candidates),
    )

    if intent_text:
        intent_profile = (intent.get("profile") or {}) if intent else {}
        append_memory_event(
            "intent_used_in_write",
            article_id=article_id,
            payload={
                "query": intent_text,
                "stage": "skeleton",
                "ttl": ((intent.get("metadata") or {}) if intent else {}).get("ttl"),
                "profile_id": intent_profile.get("id"),
                "profile_label": intent_profile.get("label"),
            },
        )

    return skeleton


def build_skeleton_prompt(
    hotspot: Hotspot,
    chosen_angle: SuggestedAngle,
    style_profile: dict[str, Any],
    content_matrix: dict[str, Any],
    target_series: str = "A",
    target_length_words: int = 1500,
) -> str:
    """Render the skeleton prompt without firing an LLM call.

    Used by tests / smoke assertions that want to confirm the intent block
    gets injected. Safe to call with or without a current intent.
    """
    series = _series_config(content_matrix, target_series)
    intent = load_current_intent()
    substitutions = {
        "style_profile_yaml": yaml.safe_dump(
            style_profile, allow_unicode=True, sort_keys=False
        ),
        "topic_intent_block": render_topic_intent_block(intent),
        "target_series": target_series,
        "series_description": series.get("theme") or series.get("description") or "",
        "typical_length": _fmt_length(series),
        "primary_language": series.get("primary_language", "bilingual"),
        "opening_style": series.get("opening_style", "scenario_driven"),
        "closing_style": series.get("closing_style", "open_question"),
        "typical_sections": _fmt_list(series.get("typical_sections")),
        "topic": hotspot.topic_one_liner,
        "chosen_angle": chosen_angle.angle,
        "angle_fit_explanation": chosen_angle.fit_explanation,
        "target_length": target_length_words,
    }
    return _render(_PROMPT_TEMPLATE, substitutions)


def _render(template: str, values: dict[str, Any]) -> str:
    """Simple ``{key}`` substitution with brace-escape awareness.

    The prompt file uses doubled braces (``{{``/``}}``) inside its JSON example
    blocks so that ``.format`` sees literal braces. We honor the same
    convention here.
    """
    # Replace placeholders in a two-step fashion to dodge `{{` / `}}`.
    out = template
    out = out.replace("{{", "\x00LB\x00").replace("}}", "\x00RB\x00")
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    out = out.replace("\x00LB\x00", "{").replace("\x00RB\x00", "}")
    return out
