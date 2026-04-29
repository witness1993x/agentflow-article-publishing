"""AI-assisted helpers for D3 adapters.

The sentence-split fallback in ``BasePlatformAdapter._adjust_paragraphs`` is
adequate most of the time. When a single sentence (or the sentence-split
result) is still longer than ``max_words * 1.2`` we optionally fall back to
semantic splitting via Claude using prompt family ``d3-split``.

All calls route through ``LLMClient.chat_text`` which tolerates ``MOCK_LLM=true``.
"""

from __future__ import annotations

from pathlib import Path

from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger
from agentflow.shared.markdown_utils import count_words

_log = get_logger("agent_d3.ai_helpers")

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "d3_platform_adapt.md"
)


def _load_prompt_template() -> str:
    if _PROMPT_PATH.exists():
        return _PROMPT_PATH.read_text(encoding="utf-8")
    # Minimal fallback used only if the prompts dir is missing.
    return (
        "将以下长段落拆成多个短段,每段不超过 {max_words} 词.\n\n"
        "按语义边界切分,保留全部原始信息.\n\n"
        "原段落:\n{paragraph}\n\n"
        "直接输出拆分后的段落,段落之间用一个空行分隔.\n"
    )


async def semantic_split(paragraph: str, max_words: int) -> str:
    """Split an over-long paragraph into shorter paragraphs joined by blank lines.

    Returns the new multi-paragraph markdown (paragraphs separated by ``\\n\\n``).

    Called ONLY by ``BasePlatformAdapter._adjust_paragraphs`` when sentence
    splitting leaves a paragraph still > max_words * 1.2.

    In ``MOCK_LLM=true`` mode, the fixture at
    ``agentflow/shared/mocks/d3-split.txt`` is returned verbatim.
    """
    if count_words(paragraph) <= max_words:
        return paragraph

    template = _load_prompt_template()
    # Inject the concrete paragraph + max_words. The prompt file uses
    # ``{max_words}`` and ``{paragraph}`` placeholders; we guard against
    # KeyError by falling back to a naive substitution if the template has
    # unexpected braces.
    try:
        prompt = template.format(paragraph=paragraph, max_words=max_words)
    except (KeyError, IndexError, ValueError):
        prompt = (
            template.replace("{paragraph}", paragraph).replace(
                "{max_words}", str(max_words)
            )
        )

    client = LLMClient()
    try:
        text = await client.chat_text(
            prompt_family="d3-split",
            prompt=prompt,
            max_tokens=1500,
        )
    except Exception as err:  # pragma: no cover - network
        _log.warning("semantic_split failed, returning original paragraph: %s", err)
        return paragraph

    text = (text or "").strip()
    if not text:
        return paragraph
    return text
