"""Language-consistency lint for D2 drafts.

When a profile declares ``output_language=zh-Hans`` but the produced
draft contains long stretches of unrelated English (or vice versa for
``output_language=en``), surface a warning before Gate B so the
operator can request a rewrite instead of shipping mixed-language
content. Used by ``agent_review.triggers.post_gate_b``.

The detector is intentionally simple — token-class ratio over the body,
with brand whitelisting (acronyms, product names) ignored. False
positives on technical content with code/identifiers are tolerable; the
warning is informational, not a hard block.
"""

from __future__ import annotations

import re
from typing import Iterable


# Threshold: when output_language=zh-Hans, more than this fraction of
# alphabetic non-whitespace characters being ASCII letters is flagged.
_ZH_FOREIGN_RATIO_THRESHOLD = 0.15

# Symmetric threshold for output_language=en: more than this fraction of
# CJK characters in the body is flagged. (CJK in an English draft is
# typically a translator artifact / leak.)
_EN_FOREIGN_RATIO_THRESHOLD = 0.05

# Tokens (case-insensitive) ignored when computing the foreign-letter
# ratio for zh-Hans — common product names / acronyms that legitimately
# stay in Latin script even in Chinese content.
_DEFAULT_BRAND_WHITELIST: tuple[str, ...] = (
    "AgentFlow", "Telegram", "Claude", "GPT", "OpenAI", "Anthropic",
    "Moonshot", "Kimi", "Jina", "Atlas", "Ghost", "WordPress", "Medium",
    "LinkedIn", "Substack", "Twitter", "X", "Mirror", "API", "URL",
    "JSON", "YAML", "MD", "PR", "CRM", "SaaS", "B2B", "MVP", "ROI",
    "AI", "ML", "LLM", "RAG", "MCP", "ETL", "SQL", "NoSQL", "K8s",
    "Kafka", "Spark", "Flink", "Redis", "PostgreSQL", "MySQL",
    "Web3", "Web2", "DeFi", "GDPR", "CCPA",
)


_CJK_RE = re.compile(r"[一-鿿]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


def _strip_whitelisted(text: str, whitelist: Iterable[str]) -> str:
    out = text
    for term in whitelist:
        if not term:
            continue
        out = re.sub(re.escape(term), " ", out, flags=re.IGNORECASE)
    return out


def detect_mixed_language(
    text: str,
    output_language: str | None,
    *,
    brand_whitelist: tuple[str, ...] = _DEFAULT_BRAND_WHITELIST,
) -> str | None:
    """Return a warning string if the body's character mix violates the
    declared ``output_language``, else ``None``.

    Inputs are lenient: empty / whitespace text returns None, unknown
    language tags return None.
    """
    body = (text or "").strip()
    if not body:
        return None
    lang = (output_language or "").strip().lower()
    if not lang:
        return None

    if lang in {"zh-hans", "zh", "zh-hant", "cn", "zh-cn"}:
        stripped = _strip_whitelisted(body, brand_whitelist)
        ascii_letters = len(_ASCII_LETTER_RE.findall(stripped))
        cjk_chars = len(_CJK_RE.findall(stripped))
        denom = ascii_letters + cjk_chars
        if denom < 50:
            return None
        ratio = ascii_letters / denom
        if ratio > _ZH_FOREIGN_RATIO_THRESHOLD:
            return (
                f"⚠ language drift: output_language={output_language} "
                f"但正文 ASCII 英文比例 {ratio:.0%} 超过阈值 "
                f"{_ZH_FOREIGN_RATIO_THRESHOLD:.0%} — 检查 D2 fill 是否漏接 "
                f"profile.publisher_account.output_language"
            )
        return None

    if lang in {"en", "english"}:
        cjk_chars = len(_CJK_RE.findall(body))
        ascii_letters = len(_ASCII_LETTER_RE.findall(body))
        denom = cjk_chars + ascii_letters
        if denom < 50:
            return None
        ratio = cjk_chars / denom
        if ratio > _EN_FOREIGN_RATIO_THRESHOLD:
            return (
                f"⚠ language drift: output_language={output_language} "
                f"but body has {ratio:.0%} CJK chars (threshold "
                f"{_EN_FOREIGN_RATIO_THRESHOLD:.0%}) — likely a translation "
                f"leak; rerun af edit"
            )
        return None

    return None
