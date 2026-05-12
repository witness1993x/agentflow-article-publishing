"""Helpers for reusable topic / keyword profiles."""

from __future__ import annotations

import os
import re
from typing import Any

from agentflow.config.topic_profiles_loader import load_topic_profiles


class TopicProfileNotFoundError(KeyError):
    """Raised when a requested topic profile id doesn't exist."""


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _flatten_terms(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_terms(item))
        return out
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten_terms(item))
        return out
    return []


def list_topic_profiles() -> dict[str, dict[str, Any]]:
    raw = load_topic_profiles().get("profiles") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for profile_id, profile in raw.items():
        if isinstance(profile, dict):
            out[str(profile_id)] = profile
    return out


def get_topic_profile(profile_id: str) -> dict[str, Any]:
    profiles = list_topic_profiles()
    profile = profiles.get(profile_id)
    if profile is None:
        known = ", ".join(sorted(profiles)) or "(none)"
        raise TopicProfileNotFoundError(
            f"unknown topic profile {profile_id!r}; available: {known}"
        )
    return profile


def topic_profile_label(profile: dict[str, Any], profile_id: str) -> str:
    label = str(profile.get("label") or "").strip()
    return label or profile_id


def topic_profile_all_terms(profile: dict[str, Any]) -> list[str]:
    terms = []
    terms.extend(_flatten_terms(profile.get("keyword_groups") or {}))
    terms.extend(_flatten_terms(profile.get("hotspot_terms") or []))
    terms.extend(_flatten_terms(profile.get("search_queries") or []))
    return _dedupe_keep_order(terms)


def topic_profile_hotspot_terms(profile: dict[str, Any]) -> list[str]:
    terms = _flatten_terms(profile.get("hotspot_terms") or [])
    if not terms:
        terms = topic_profile_all_terms(profile)
    return _dedupe_keep_order(terms)


def topic_profile_avoid_terms(profile: dict[str, Any]) -> list[str]:
    return _dedupe_keep_order(_flatten_terms(profile.get("avoid_terms") or []))


def topic_profile_regex(profile: dict[str, Any]) -> str:
    terms = sorted(topic_profile_hotspot_terms(profile), key=len, reverse=True)
    escaped = [re.escape(term) for term in terms if term]
    return "|".join(escaped)


def topic_profile_default_search_query(profile: dict[str, Any], profile_id: str) -> str:
    explicit = str(profile.get("default_search_query") or "").strip()
    if explicit:
        return explicit
    search_queries = _dedupe_keep_order(_flatten_terms(profile.get("search_queries") or []))
    if search_queries:
        return search_queries[0]
    return topic_profile_intent_text(profile, profile_id)


def topic_profile_search_queries(profile: dict[str, Any], profile_id: str) -> list[str]:
    search_queries = _dedupe_keep_order(_flatten_terms(profile.get("search_queries") or []))
    if search_queries:
        return search_queries

    groups = profile.get("keyword_groups") or {}
    if isinstance(groups, dict):
        core_terms = _dedupe_keep_order(_flatten_terms(groups.get("core") or []))
        if core_terms:
            return core_terms[:8]

    hotspot_terms = topic_profile_hotspot_terms(profile)
    if hotspot_terms:
        return hotspot_terms[:8]

    fallback = topic_profile_default_search_query(profile, profile_id)
    return [fallback] if fallback else []


def topic_profile_intent_text(profile: dict[str, Any], profile_id: str) -> str:
    explicit = str(profile.get("intent") or "").strip()
    if explicit:
        return explicit
    label = topic_profile_label(profile, profile_id)
    terms = topic_profile_hotspot_terms(profile)[:8]
    if not terms:
        return label
    return f"{label}: " + " / ".join(terms)


def topic_profile_publisher_account(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the ``publisher_account`` block, or an empty dict if absent.

    Shape (all keys optional; absence == not constrained):
      brand:           str — display name of the publishing entity
      voice:           "first_party_brand" | "observer" | "personal"
      pronoun:         str — canonical first-person pronoun ("我们", "I", ...)
      output_language: str — output language constraint ("zh-Hans", "en", ...)
      do:              list[str] — voice rules to follow
      dont:            list[str] — voice rules to avoid
      product_facts:   list[str] — declarative facts the LLM may ground in
      default_tags:    list[str] — fallback Medium tags when not overridden
      image_prompt_hints: list[str] | str — visual vocabulary for D2 image prompts
      canonical_domain: str | None — base URL for canonical_url construction

    Newly-added optional fields (all optional; consumed by F2/F3 wizards and
    per-profile brand overlay):
      default_description: str — freeform paragraph describing the publisher;
                                 fed to LLM in `af topic-profile derive` to
                                 reverse-engineer keyword_groups / do / dont /
                                 perspectives / product_facts as suggestions.
      perspectives:        list[str] — signature article angles ("总把 X 框成 Y"
                                       style framing patterns); freeform list,
                                       complementary to the more rigid `voice`
                                       enum above.
      platform_handles:    dict[str, dict] — per-platform business identity.
                                  e.g. {"medium":  {"handle": "@xxx",
                                                    "url": "https://medium.com/@xxx"},
                                        "twitter": {"handle": "@yyy"},
                                        "ghost":   {"site_url":
                                                    "https://blog.example.com"}}
      brand_overlay:       dict — per-profile override for the global
                                  `preferences.yaml::image_generation.brand_overlay`
                                  block. Same keys as the global config:
                                    enabled / logo_path / anchor / width_ratio /
                                    padding_ratio_x / padding_ratio_y /
                                    recolor_dark_to_light / dark_threshold
                                  Caller is responsible for merging with the
                                  global preferences (profile takes precedence).
    """
    block = profile.get("publisher_account") if profile else None
    return block if isinstance(block, dict) else {}


def topic_profile_default_description(profile: dict[str, Any]) -> str:
    """Return publisher_account.default_description, or '' when absent."""
    block = topic_profile_publisher_account(profile)
    value = block.get("default_description")
    if isinstance(value, str):
        return value.strip()
    return ""


def topic_profile_perspectives(profile: dict[str, Any]) -> list[str]:
    """Return publisher_account.perspectives, or []. Always returns list."""
    block = topic_profile_publisher_account(profile)
    raw = block.get("perspectives")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def topic_profile_platform_handles(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return publisher_account.platform_handles, or {}."""
    block = topic_profile_publisher_account(profile)
    raw = block.get("platform_handles")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[str(key)] = value
    return out


def topic_profile_brand_overlay(profile: dict[str, Any]) -> dict[str, Any]:
    """Return publisher_account.brand_overlay, or {}. Caller merges with global
    preferences.image_generation.brand_overlay (profile takes precedence)."""
    block = topic_profile_publisher_account(profile)
    raw = block.get("brand_overlay")
    return raw if isinstance(raw, dict) else {}


def resolve_publisher_account_from_intent(
    intent: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve the active intent's profile.id → its publisher_account block.

    Returns ``{}`` when no intent is active, the intent has no profile id, the
    profile doesn't exist, or the profile has no publisher_account section.
    """
    if not intent:
        return {}
    profile_ref = intent.get("profile") or {}
    profile_id = str(profile_ref.get("id") or "").strip()
    if not profile_id:
        return {}
    try:
        profile = get_topic_profile(profile_id)
    except TopicProfileNotFoundError:
        return {}
    return topic_profile_publisher_account(profile)


def topic_profile_effective_voice(
    publisher: dict[str, Any] | None,
    fit_score: float | None,
) -> tuple[str, str]:
    """v1.1.9 — resolve the effective writing voice from configured voice + fit.

    Returns ``(effective_voice, reason)``.

    Logic:
      * fit_score is None → keep configured voice (backward compat;
        callers without a fit signal don't get downgraded).
      * fit_score >= AGENTFLOW_VOICE_FIRST_PARTY_MIN_FIT (default 0.20)
        → keep configured voice. Topic is in our wheelhouse, anchoring back
        to product_facts is appropriate.
      * fit_score < that threshold (but the article still made it past the
        D1 hard gate) → force "observer". Topic is too distant for a
        first-party-brand voice to be honest; writing must be industry
        observation, not product story.

    The thresholds are env-tunable; conservative defaults match v1.0.21's
    hard-gate intuition (0.025 hard floor, 0.10 observer floor, 0.20
    first-party floor)."""
    configured = (publisher or {}).get("voice") or "first_party_brand"
    if fit_score is None:
        return str(configured), "no_fit_signal"
    try:
        first_party_min = float(
            os.environ.get("AGENTFLOW_VOICE_FIRST_PARTY_MIN_FIT", "0.20")
            or "0.20"
        )
    except (TypeError, ValueError):
        first_party_min = 0.20
    if fit_score >= first_party_min:
        return str(configured), f"fit_score={fit_score:.3f}>=first_party_min({first_party_min:.3f})"
    return "observer", f"fit_score={fit_score:.3f}<first_party_min({first_party_min:.3f})"


def render_publisher_account_block(
    publisher: dict[str, Any] | None,
    *,
    fit_score: float | None = None,
    hotspot: dict[str, Any] | None = None,
) -> str:
    """Render publisher_account as a prompt-injectable markdown block.

    Returns an empty string when publisher is None / empty so the placeholder
    collapses cleanly in templates.

    v1.1.9: ``fit_score`` (or ``hotspot``, computed inline) determines the
    effective voice. Distant topics (low fit) flip the prompt to observer
    mode — different pronoun, no product_facts anchor block, explicit
    "do not graft this onto our product story" rule. This kills the
    forced-analogy failure where every off-topic hotspot got rewritten
    into "我们 ChainStream 做的是…".
    """
    if not publisher:
        return ""
    if fit_score is None and hotspot is not None:
        try:
            from agentflow.agent_d1.topic_fit import score_fit  # local import — avoid cycle
            fit_score = float(score_fit(hotspot, publisher))
        except Exception:  # pragma: no cover — defensive
            fit_score = None
    effective_voice, voice_reason = topic_profile_effective_voice(
        publisher, fit_score,
    )
    is_observer = effective_voice == "observer"
    lines: list[str] = ["## Publisher 账号身份（写作视角硬约束）", ""]
    brand = str(publisher.get("brand") or "").strip()
    voice = str(publisher.get("voice") or "").strip()
    pronoun = str(publisher.get("pronoun") or "").strip()
    if is_observer:
        # Observer mode: swap pronoun to first-person personal, never
        # corporate "我们". The previous configured pronoun is recorded
        # in the reason line so a reader of the prompt knows what's
        # being overridden.
        pronoun = "我（个人观察）"
    output_language = str(publisher.get("output_language") or "zh-Hans").strip()
    if brand:
        lines.append(f"- **品牌**: {brand}")
    if voice:
        if is_observer:
            lines.append(
                f"- **口吻 voice**: observer "
                f"（配置为 `{voice}`，但根据当前话题与品牌距离自动降档；"
                f"原因：{voice_reason}）"
            )
        else:
            lines.append(f"- **口吻 voice**: {voice}")
    if pronoun:
        lines.append(f"- **第一人称代词**: {pronoun}（写作时优先使用）")
    if output_language:
        label = {
            "zh-Hans": "简体中文",
            "zh-Hant": "繁体中文",
            "en": "English",
            "bilingual": "双语",
        }.get(output_language, output_language)
        lines.append(f"- **输出语言**: {label}（{output_language}）")
        if output_language == "zh-Hans":
            lines.append(
                "- **语言硬约束**: 标题、摘要、正文、改写结果、图片描述和平台适配文案均必须使用简体中文；"
                "保留专有名词、协议名、产品名和必要英文缩写。"
            )
    default_description = str(publisher.get("default_description") or "").strip()
    if default_description:
        lines.append("")
        lines.append("## 自我描述")
        lines.append("")
        lines.append(default_description)
    perspectives = publisher.get("perspectives") or []
    if isinstance(perspectives, list):
        cleaned_perspectives = [str(p).strip() for p in perspectives if str(p or "").strip()]
    else:
        cleaned_perspectives = []
    if cleaned_perspectives:
        lines.append("")
        lines.append("## 文章视角 (signature angles)")
        for item in cleaned_perspectives:
            lines.append(f"- {item}")
    do = publisher.get("do") or []
    dont = publisher.get("dont") or []
    if do:
        lines.append("")
        lines.append("**必须 (do)**:")
        for item in do:
            lines.append(f"- {item}")
    if dont:
        lines.append("")
        lines.append("**禁止 (dont)**:")
        for item in dont:
            lines.append(f"- {item}")
    facts = publisher.get("product_facts") or []
    if facts and not is_observer:
        # Observer mode INTENTIONALLY drops the product_facts anchor list.
        # Listing them here is a temptation for the LLM to paste-and-pivot:
        # "this article is about X, and X reminds me of our product fact
        # Y, which is why ChainStream…". When the topic is genuinely close
        # to our domain, anchoring is the right thing. When it's not, it
        # produces the v1.1.7 forced-analogy failure mode (硬套预言机).
        lines.append("")
        lines.append("**可引用的产品事实** (这些都是 publisher 自家的事，可以直接陈述):")
        for fact in facts:
            lines.append(f"- {fact}")
    if is_observer:
        lines.append("")
        lines.append("## ⚠ 当前为 observer 模式（行业观察口吻）")
        lines.append("")
        lines.append(
            "本条话题与本品牌产品域的相似度较低，**不要把它强行嫁接回我们的"
            " product_facts**。请按下面的规则写作："
        )
        lines.append("")
        lines.append("- 用「我」（第一人称个人观察）开口，**不要**用「我们」/「ChainStream 做的是」")
        lines.append("- 写成行业观察、判断或评论，可以引用公开资料和现象")
        lines.append("- **禁止**做出代表 publisher 的承诺式陈述（"
                     "如「我们已经支持」「我们的方案是」），即便正文里有任何"
                     " product_facts 候选，也不要把它当作必引锚点")
        lines.append("- **禁止**把当前话题硬转成 publisher 自家产品的一面来讲；"
                     "即便能找到 0.5 步之外的类比，也保持观察口吻")
        lines.append("- 文末可以收一句「这件事和我们做的也有点关系」式的轻锚，"
                     "但全文重心放在话题本身、不是放在自家产品上")
    lines.append("")
    return "\n".join(lines)


def topic_profile_keywords_payload(profile: dict[str, Any]) -> dict[str, Any]:
    groups = profile.get("keyword_groups") or {}
    primary: list[str] = []
    if isinstance(groups, dict):
        primary = _flatten_terms(groups.get("core") or [])
        if not primary:
            for value in groups.values():
                primary.extend(_flatten_terms(value))
                if primary:
                    break
    expanded = topic_profile_all_terms(profile)
    avoid = topic_profile_avoid_terms(profile)
    payload: dict[str, Any] = {"expanded": expanded}
    if primary:
        payload["primary"] = _dedupe_keep_order(primary)
    if avoid:
        payload["avoid"] = avoid
    return payload
