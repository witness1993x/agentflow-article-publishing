"""Stage 3 of D2 — apply a natural-language edit command to one paragraph.

Five preset commands (``改短`` / ``展开`` / ``改锋利`` / ``加例子`` / ``去AI味``)
expand into longer prompts; anything else is passed through verbatim as the
``{user_command}`` substitution. The d2_interactive_edit.md prompt handles the
heavy lifting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger
from agentflow.shared.markdown_utils import count_words, split_paragraphs
from agentflow.shared.memory import (
    load_current_intent,
    render_topic_intent_block,
)
from agentflow.shared.models import DraftOutput, FilledSection
from agentflow.shared.topic_profiles import (
    render_publisher_account_block,
    resolve_publisher_account_from_intent,
)

_log = get_logger("agent_d2.editor")

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d2_interactive_edit.md"
)
_META_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d2_edit_meta.md"
)


# Five canonical preset commands surfaced in the Web UI.
PRESET_COMMANDS: dict[str, str] = {
    "改短": "把当前段落压缩到 60% 长度,保留核心论点",
    "展开": "展开当前段落到 1.5 倍长度,补充细节和例子",
    "改锋利": "把当前段落改得更锋利直接,减弱缓冲词和套话",
    "加例子": "在当前段落补充一个具体例子(最好是业界真实案例)",
    "去AI味": "去掉 AI 味:删除'让我们''值得注意的是''综上所述'等 AI 套话",
}


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


def _condense_style_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Shrink the profile down to the fields the edit prompt actually needs."""
    return {
        "taboos": profile.get("taboos") or {},
        "voice_principles": profile.get("voice_principles") or [],
        "tone": profile.get("tone") or {},
        "paragraph_preferences": profile.get("paragraph_preferences") or {},
    }


def _resolve_command(command: str) -> str:
    # Normalize "去 AI 味" / "去AI味" / "去ai味" variants toward the preset key.
    normalized = command.strip().replace(" ", "")
    if normalized.lower() in {"去ai味", "别像chatgpt", "别像 chatgpt"}:
        return PRESET_COMMANDS["去AI味"]
    return PRESET_COMMANDS.get(normalized, command)


async def apply_edit(
    article: DraftOutput,
    section_index: int,
    paragraph_index: int | None,
    command: str,
    style_profile: dict[str, Any],
) -> FilledSection:
    """Apply an edit command to a single paragraph inside a draft section.

    If ``paragraph_index`` is ``None``, the entire section content is treated
    as one "paragraph" for the edit. Otherwise we target the Nth (0-indexed)
    paragraph as split by blank lines.

    Returns a new FilledSection with the edit applied, recomputed word_count,
    and refreshed compliance (compliance is not re-checked here — the caller
    can run ``compliance_checker.check`` if they want).
    """
    if section_index < 0 or section_index >= len(article.sections):
        raise IndexError(
            f"section_index {section_index} out of range (have {len(article.sections)} sections)"
        )

    section = article.sections[section_index]
    paragraphs = split_paragraphs(section.content_markdown)

    # Build (prev, current, next) window.
    if paragraph_index is None or not paragraphs:
        target_paragraph = section.content_markdown
        prev_paragraph = _section_last_paragraph(article, section_index - 1)
        next_paragraph = _section_first_paragraph(article, section_index + 1)
        target_idx = -1  # signal: replace whole section
    else:
        if paragraph_index < 0 or paragraph_index >= len(paragraphs):
            raise IndexError(
                f"paragraph_index {paragraph_index} out of range "
                f"(section has {len(paragraphs)} paragraphs)"
            )
        target_paragraph = paragraphs[paragraph_index]
        if paragraph_index > 0:
            prev_paragraph = paragraphs[paragraph_index - 1]
        else:
            prev_paragraph = _section_last_paragraph(article, section_index - 1)
        if paragraph_index < len(paragraphs) - 1:
            next_paragraph = paragraphs[paragraph_index + 1]
        else:
            next_paragraph = _section_first_paragraph(article, section_index + 1)
        target_idx = paragraph_index

    resolved_command = _resolve_command(command)

    substitutions = {
        "style_profile_condensed": yaml.safe_dump(
            _condense_style_profile(style_profile),
            allow_unicode=True,
            sort_keys=False,
        ),
        "article_title": article.title,
        "section_heading": section.heading,
        "prev_paragraph": prev_paragraph or "(无)",
        "next_paragraph": next_paragraph or "(无)",
        "current_paragraph": target_paragraph,
        "user_command": resolved_command,
    }

    prompt = _render(_PROMPT_TEMPLATE, substitutions)

    client = LLMClient()
    edited = await client.chat_text(
        prompt_family="d2-edit",
        prompt=prompt,
        max_tokens=1500,
    )
    edited = edited.strip()

    # Replace the target paragraph in the section.
    if target_idx < 0:
        new_content = edited
    else:
        paragraphs[target_idx] = edited
        new_content = "\n\n".join(p for p in paragraphs if p)

    new_section = FilledSection(
        heading=section.heading,
        content_markdown=new_content,
        word_count=count_words(new_content),
        compliance_score=section.compliance_score,  # caller can re-run check
        taboo_violations=list(section.taboo_violations),
    )
    _log.info(
        "edited section %r (paragraph=%s, cmd=%r)",
        section.heading,
        paragraph_index,
        command,
    )
    return new_section


def _section_last_paragraph(article: DraftOutput, index: int) -> str:
    if index < 0 or index >= len(article.sections):
        return ""
    paragraphs = split_paragraphs(article.sections[index].content_markdown)
    return paragraphs[-1] if paragraphs else ""


def _section_first_paragraph(article: DraftOutput, index: int) -> str:
    if index < 0 or index >= len(article.sections):
        return ""
    paragraphs = split_paragraphs(article.sections[index].content_markdown)
    return paragraphs[0] if paragraphs else ""


def _render(template: str, values: dict[str, Any]) -> str:
    out = template
    out = out.replace("{{", "\x00LB\x00").replace("}}", "\x00RB\x00")
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    out = out.replace("\x00LB\x00", "{").replace("\x00RB\x00", "}")
    return out


# ---------------------------------------------------------------------------
# Stage 4 — title / opening / closing rewrites
# ---------------------------------------------------------------------------


def _load_meta_prompt() -> str:
    raw = _META_PROMPT_PATH.read_text(encoding="utf-8")
    marker = "```text"
    start = raw.find(marker)
    if start == -1:
        return raw
    start += len(marker)
    end = raw.rfind("```")
    if end == -1 or end <= start:
        return raw[start:].strip()
    return raw[start:end].strip()


_META_PROMPT_TEMPLATE = _load_meta_prompt()


# (target_kind label, length_hint, char_min, char_max)
_META_TARGETS: dict[str, tuple[str, str, int, int]] = {
    "title": ("标题", "20-40 字符", 8, 60),
    "opening": ("开头", "50-80 字", 30, 200),
    "closing": ("结尾", "50-80 字", 30, 200),
}


def _strip_meta_output(text: str) -> str:
    """Strip leading/trailing whitespace, surrounding quotes, and stray prefix."""
    out = (text or "").strip()
    # Drop common preamble like "标题:" / "重写后的标题:"
    for prefix in ("标题:", "标题：", "开头:", "开头：", "结尾:", "结尾："):
        if out.startswith(prefix):
            out = out[len(prefix):].lstrip()
    # Strip leading markdown heading markers.
    while out.startswith("#"):
        out = out.lstrip("#").lstrip()
    # Drop matched outer quotes (Chinese + ASCII).
    pairs = [("“", "”"), ("「", "」"), ("\"", "\""), ("'", "'")]
    for lo, hi in pairs:
        if out.startswith(lo) and out.endswith(hi) and len(out) >= 2:
            out = out[1:-1].strip()
            break
    return out.strip()


async def _edit_meta(
    article_id: str,
    target: str,
    command: str,
    style_profile: dict[str, Any],
    current_text: str,
) -> str:
    if target not in _META_TARGETS:
        raise ValueError(f"unknown meta target {target!r}")
    target_kind, length_hint, _char_min, char_max = _META_TARGETS[target]

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
        "target_kind": target_kind,
        "current_text": current_text or "(空)",
        "command": command,
        "length_hint": length_hint,
    }

    prompt = _render(_META_PROMPT_TEMPLATE, substitutions)
    client = LLMClient()
    raw = await client.chat_text(
        prompt_family="d2-edit-meta",
        prompt=prompt,
        max_tokens=600,
    )
    new_text = _strip_meta_output(raw)
    if not new_text:
        raise ValueError(f"{target} edit produced empty output")
    # Soft length validation: if the model blew way past the cap, retry once
    # with a tighter reminder. (We don't enforce char_min — short is fine.)
    if len(new_text) > char_max * 1.5:
        _log.info(
            "%s edit overshot length (%d chars > %d soft cap), retrying",
            target,
            len(new_text),
            char_max,
        )
        retry_prompt = (
            prompt
            + "\n\n## 上一次输出太长\n\n"
            f"上次写了 {len(new_text)} 字符, 超出 {length_hint} 上限. "
            "重新写一版, 必须压到长度内, 只输出新的{target_kind}正文.\n"
        )
        raw = await client.chat_text(
            prompt_family="d2-edit-meta",
            prompt=retry_prompt,
            max_tokens=600,
        )
        new_text = _strip_meta_output(raw) or new_text
    _log.info("edited %s (article=%s, len=%d)", target, article_id, len(new_text))
    return new_text


async def edit_title(
    article_id: str, command: str, style_profile: dict[str, Any]
) -> str:
    """Rewrite the article title via natural-language command. Persists draft."""
    from agentflow.agent_d2.main import load_draft, save_draft

    draft = load_draft(article_id)
    new_text = await _edit_meta(
        article_id, "title", command, style_profile, draft.title
    )
    draft.title = new_text
    save_draft(draft)
    return new_text


async def edit_opening(
    article_id: str, command: str, style_profile: dict[str, Any]
) -> str:
    """Rewrite the article opening paragraph. Persists draft."""
    from agentflow.agent_d2.main import load_draft, save_draft

    draft = load_draft(article_id)
    # opening is stashed in metadata, not on DraftOutput — read it via metadata.json.
    current = _read_meta_field(article_id, "opening")
    new_text = await _edit_meta(
        article_id, "opening", command, style_profile, current
    )
    save_draft(draft, extra_metadata={"opening": new_text})
    return new_text


async def edit_closing(
    article_id: str, command: str, style_profile: dict[str, Any]
) -> str:
    """Rewrite the article closing paragraph. Persists draft."""
    from agentflow.agent_d2.main import load_draft, save_draft

    draft = load_draft(article_id)
    current = _read_meta_field(article_id, "closing")
    new_text = await _edit_meta(
        article_id, "closing", command, style_profile, current
    )
    save_draft(draft, extra_metadata={"closing": new_text})
    return new_text


def _read_meta_field(article_id: str, field: str) -> str:
    """Tiny helper: read a single string field from the draft's metadata.json."""
    import json as _json

    from agentflow.shared.bootstrap import agentflow_home

    meta_path = agentflow_home() / "drafts" / article_id / "metadata.json"
    if not meta_path.exists():
        return ""
    try:
        data = _json.loads(meta_path.read_text(encoding="utf-8")) or {}
    except _json.JSONDecodeError:
        return ""
    val = data.get(field) or ""
    return str(val)
