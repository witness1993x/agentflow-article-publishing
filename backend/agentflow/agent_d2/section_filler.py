"""Stage 2 of D2 — fill a single section of the outline.

``fill_section`` takes a ``Section`` + a ``context`` dict containing title /
opening / closing / outline / previous_sections, renders the
``d2_paragraph_filling.md`` prompt, and fires ``chat_text``. The returned
markdown is measured, compliance-checked, and wrapped in a ``FilledSection``.

If the first attempt produces any compliance violations, we retry *once* with
a warning suffix appended to the prompt listing the specific offenders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentflow.agent_d2.compliance_checker import check as compliance_check
from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger
from agentflow.shared.markdown_utils import count_words
from agentflow.shared.memory import (
    load_current_intent,
    render_topic_intent_block,
)
from agentflow.shared.models import FilledSection, Section
from agentflow.shared.topic_profiles import (
    render_publisher_account_block,
    resolve_publisher_account_from_intent,
)

_log = get_logger("agent_d2.filler")

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d2_paragraph_filling.md"
)


def _load_prompt() -> str:
    raw = _PROMPT_PATH.read_text(encoding="utf-8")
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


def _format_outline(outline: list[dict[str, Any] | Section]) -> str:
    lines: list[str] = []
    for idx, item in enumerate(outline, start=1):
        if isinstance(item, Section):
            heading = item.heading
            args = item.key_arguments
            words = item.estimated_words
        else:
            heading = item.get("heading", "")
            args = item.get("key_arguments") or []
            words = item.get("estimated_words", 0)
        lines.append(f"{idx}. {heading} (~{words} 字)")
        for arg in args:
            lines.append(f"   - {arg}")
    return "\n".join(lines) if lines else "(no outline)"


def _format_previous_sections(previous: list[dict[str, Any]]) -> str:
    if not previous:
        return "(none — 这是第一节)"
    chunks: list[str] = []
    for sec in previous:
        heading = sec.get("heading", "")
        body = sec.get("content_markdown", "")
        chunks.append(f"### {heading}\n\n{body}")
    return "\n\n".join(chunks)


async def fill_section(
    section: Section,
    context: dict[str, Any],
    style_profile: dict[str, Any],
) -> FilledSection:
    """Generate markdown for a single section given the chosen skeleton context.

    ``context`` keys expected:
        - ``title`` (str)
        - ``opening`` (str)
        - ``closing`` (str)
        - ``full_outline`` (list of Section | dict)
        - ``previous_sections`` (list of dicts with ``heading`` + ``content_markdown``)
    """
    para_prefs = style_profile.get("paragraph_preferences") or {}
    avg_para = para_prefs.get("average_length_words", 60)
    max_para = para_prefs.get("max_length_words", 100)

    intent = load_current_intent()
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
        "title": context.get("title", ""),
        "opening": context.get("opening", ""),
        "closing": context.get("closing", ""),
        "full_outline": _format_outline(context.get("full_outline") or []),
        "previous_sections": _format_previous_sections(
            context.get("previous_sections") or []
        ),
        "current_heading": section.heading,
        "key_arguments": "\n".join(f"- {arg}" for arg in section.key_arguments),
        "target_words": section.estimated_words,
        "section_purpose": section.section_purpose,
        "avg_para_words": avg_para,
        "max_para_words": max_para,
    }

    prompt = _render(_PROMPT_TEMPLATE, substitutions)

    client = LLMClient()
    content = await client.chat_text(
        prompt_family="d2-fill",
        prompt=prompt,
        max_tokens=2000,
    )
    content = content.strip()

    score, violations = compliance_check(content, style_profile)

    # Retry once if any violations — append warning suffix.
    if violations:
        _log.info(
            "section %r had %d violations on first try, retrying",
            section.heading,
            len(violations),
        )
        suffix = (
            "\n\n## 上一次生成的修改要求\n\n"
            "你刚才的输出违反了以下规则,请**重新写一版本**,必须修掉这些问题:\n\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n\n只输出新的段落正文,不要解释你做了什么改动.\n"
        )
        retry_prompt = prompt + suffix
        content = await client.chat_text(
            prompt_family="d2-fill",
            prompt=retry_prompt,
            max_tokens=2000,
        )
        content = content.strip()
        score, violations = compliance_check(content, style_profile)

    word_count = count_words(content)
    _log.info(
        "filled section %r: %d words, compliance=%.2f (%d violations)",
        section.heading,
        word_count,
        score,
        len(violations),
    )

    return FilledSection(
        heading=section.heading,
        content_markdown=content,
        word_count=word_count,
        compliance_score=score,
        taboo_violations=violations,
    )


def _render(template: str, values: dict[str, Any]) -> str:
    out = template
    out = out.replace("{{", "\x00LB\x00").replace("}}", "\x00RB\x00")
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    out = out.replace("\x00LB\x00", "{").replace("\x00RB\x00", "}")
    return out
