"""Lifecycle helpers for user-managed topic profile constraints."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from agentflow.config.topic_profiles_loader import (
    EXAMPLE_TOPIC_PROFILES_PATH,
    user_topic_profiles_path,
)
from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.topic_profiles import (
    _dedupe_keep_order,
    _flatten_terms,
    get_topic_profile,
    topic_profile_label,
)

SCHEMA_VERSION = 1
DEFAULT_OUTPUT_LANGUAGE = "zh-Hans"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def constraint_suggestions_dir() -> Path:
    ensure_user_dirs()
    path = agentflow_home() / "constraint_suggestions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def constraint_sessions_dir() -> Path:
    ensure_user_dirs()
    path = agentflow_home() / "constraint_sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _write_yaml_file(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def load_user_topic_profiles() -> dict[str, Any]:
    return _read_yaml_file(user_topic_profiles_path())


def materialize_user_topic_profiles() -> tuple[dict[str, Any], Path]:
    path = user_topic_profiles_path()
    if path.exists():
        return load_user_topic_profiles(), path
    if EXAMPLE_TOPIC_PROFILES_PATH.exists():
        data = _read_yaml_file(EXAMPLE_TOPIC_PROFILES_PATH)
    else:
        data = {"version": "1.0", "profiles": {}}
    if not isinstance(data.get("profiles"), dict):
        data["profiles"] = {}
    _write_yaml_file(path, data)
    return data, path


def _minimal_profile(profile_id: str) -> dict[str, Any]:
    label = profile_id.replace("_", " ").strip() or profile_id
    title = label[:1].upper() + label[1:] if label else profile_id
    return {
        "label": title,
        "summary": "",
        "intent": "",
        "publisher_account": {
            "brand": title,
            "voice": "first_party_brand",
            "pronoun": "我们",
            "output_language": DEFAULT_OUTPUT_LANGUAGE,
            "do": [],
            "dont": [],
            "product_facts": [],
            "default_tags": [],
        },
        "keyword_groups": {"core": []},
        "hotspot_terms": [],
        "search_queries": [],
        "default_search_query": "",
        "avoid_terms": [],
    }


def seed_profile(profile_id: str) -> dict[str, Any]:
    try:
        return deepcopy(get_topic_profile(profile_id))
    except Exception:
        return _minimal_profile(profile_id)


def deep_merge(base: Any, patch: Any, *, replace_lists: bool) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = deepcopy(base)
        for key, value in patch.items():
            if key in merged:
                merged[key] = deep_merge(merged[key], value, replace_lists=replace_lists)
            else:
                merged[key] = deepcopy(value)
        return merged
    if isinstance(base, list) and isinstance(patch, list):
        if replace_lists:
            return deepcopy(patch)
        merged = [*base, *patch]
        return _dedupe_keep_order([str(item) for item in merged if str(item).strip()])
    return deepcopy(patch)


def _profiles_mapping(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("profiles")
    if not isinstance(raw, dict):
        raw = {}
        data["profiles"] = raw
    return raw


def upsert_profile(
    profile_id: str,
    patch: dict[str, Any],
    *,
    replace_lists: bool,
    source: str,
) -> dict[str, Any]:
    data, path = materialize_user_topic_profiles()
    profiles = _profiles_mapping(data)
    current = deepcopy(profiles.get(profile_id) or seed_profile(profile_id))
    merged = deep_merge(current, patch, replace_lists=replace_lists)
    # v1.0.16: per-profile last_updated_at stamp so consumers (D2 draft
    # creation, post_gate_b's outdated check) can detect when a draft was
    # written against an older snapshot of the profile and warn the
    # operator before they ship content under stale rules.
    merged["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    profiles[profile_id] = merged
    data.setdefault("version", "1.0")
    data["last_updated"] = datetime.now().date().isoformat()
    _write_yaml_file(path, data)
    return {
        "profile_id": profile_id,
        "path": str(path),
        "source": source,
        "profile": merged,
    }


def profile_missing_fields(profile: dict[str, Any] | None) -> list[str]:
    if not profile:
        return [
            "publisher_account.brand",
            "publisher_account.voice",
            "publisher_account.output_language",
            "publisher_account.product_facts",
            "keyword_groups.core",
            "search_queries",
        ]
    missing: list[str] = []
    pub = profile.get("publisher_account") or {}
    if not str(pub.get("brand") or "").strip():
        missing.append("publisher_account.brand")
    if not str(pub.get("voice") or "").strip():
        missing.append("publisher_account.voice")
    if not str(pub.get("output_language") or "").strip():
        missing.append("publisher_account.output_language")
    if not _flatten_terms(pub.get("product_facts") or []):
        missing.append("publisher_account.product_facts")
    groups = profile.get("keyword_groups") or {}
    if not _flatten_terms((groups.get("core") or []) if isinstance(groups, dict) else []):
        missing.append("keyword_groups.core")
    if not _flatten_terms(profile.get("search_queries") or []):
        missing.append("search_queries")
    return missing


def normalize_output_language(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    mapping = {
        "zh-hans": "zh-Hans",
        "zh_hans": "zh-Hans",
        "zh": "zh-Hans",
        "cn": "zh-Hans",
        "zh-cn": "zh-Hans",
        "zh_cn": "zh-Hans",
        "简体": "zh-Hans",
        "简体中文": "zh-Hans",
        "中文": "zh-Hans",
        "普通话": "zh-Hans",
        "mandarin": "zh-Hans",
        "zh-hant": "zh-Hant",
        "zh_hant": "zh-Hant",
        "zh-tw": "zh-Hant",
        "zh_tw": "zh-Hant",
        "繁体": "zh-Hant",
        "繁體": "zh-Hant",
        "繁体中文": "zh-Hant",
        "繁體中文": "zh-Hant",
        "en": "en",
        "english": "en",
        "英文": "en",
        "英语": "en",
        "英語": "en",
        "bilingual": "bilingual",
        "双语": "bilingual",
        "雙語": "bilingual",
    }
    return mapping.get(raw)


def user_profile_bootstrap_state(profile_id: str) -> dict[str, Any]:
    user_data = load_user_topic_profiles()
    profiles = user_data.get("profiles") if isinstance(user_data.get("profiles"), dict) else {}
    user_profile = profiles.get(profile_id) if isinstance(profiles, dict) else None
    seeded = seed_profile(profile_id)
    return {
        "user_file_exists": user_topic_profiles_path().exists(),
        "user_profile_exists": isinstance(user_profile, dict),
        "missing_fields": profile_missing_fields(user_profile if isinstance(user_profile, dict) else None),
        "current_profile": deepcopy(user_profile) if isinstance(user_profile, dict) else None,
        "seed_profile": seeded,
    }


def build_patch_from_answers(
    profile_id: str,
    answers: dict[str, Any],
    *,
    existing_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = deepcopy(existing_profile or seed_profile(profile_id))
    current_pub = current.get("publisher_account") or {}
    brand = str(answers.get("brand") or current_pub.get("brand") or topic_profile_label(current, profile_id)).strip()
    voice = str(answers.get("voice") or current_pub.get("voice") or "first_party_brand").strip()
    output_language = (
        normalize_output_language(answers.get("output_language"))
        or normalize_output_language(current_pub.get("output_language"))
        or DEFAULT_OUTPUT_LANGUAGE
    )
    do = _dedupe_keep_order(_flatten_terms(answers.get("do") or current_pub.get("do") or []))
    dont = _dedupe_keep_order(_flatten_terms(answers.get("dont") or current_pub.get("dont") or []))
    facts = _dedupe_keep_order(
        _flatten_terms(answers.get("product_facts") or current_pub.get("product_facts") or [])
    )
    core_terms = _dedupe_keep_order(_flatten_terms(answers.get("core_terms") or []))
    search_queries = _dedupe_keep_order(
        _flatten_terms(answers.get("search_queries") or current.get("search_queries") or [])
    )
    avoid_terms = _dedupe_keep_order(
        _flatten_terms(answers.get("avoid_terms") or current.get("avoid_terms") or [])
    )
    pronoun = str(current_pub.get("pronoun") or "").strip()
    if not pronoun:
        pronoun = "我们" if voice == "first_party_brand" else "我"
    summary = str(current.get("summary") or "").strip()
    if not summary and brand:
        summary = f"{brand} topic profile"
    intent = str(current.get("intent") or "").strip()
    if not intent and core_terms:
        intent = f"围绕 {'、'.join(core_terms[:6])} 展开。"

    patch: dict[str, Any] = {
        "label": str(current.get("label") or brand or profile_id),
        "summary": summary,
        "intent": intent,
        "publisher_account": {
            "brand": brand,
            "voice": voice,
            "pronoun": pronoun,
            "output_language": output_language,
            "do": do,
            "dont": dont,
            "product_facts": facts,
        },
        "keyword_groups": {"core": core_terms},
        "hotspot_terms": core_terms,
        "search_queries": search_queries,
        "default_search_query": search_queries[0] if search_queries else str(current.get("default_search_query") or ""),
        "avoid_terms": avoid_terms,
    }
    return patch


def _new_record_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"


def session_path(session_id: str) -> Path:
    return constraint_sessions_dir() / f"{session_id}.json"


def save_session(payload: dict[str, Any]) -> Path:
    session_id = str(payload.get("id") or _new_record_id("session"))
    payload["id"] = session_id
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload["updated_at"] = _now_iso()
    path = session_path(session_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_session(session_id: str) -> dict[str, Any]:
    path = session_path(session_id)
    if not path.exists():
        raise FileNotFoundError(f"constraint session not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"invalid session payload at {path}")
    return data


def find_active_session_for_uid(uid: int) -> dict[str, Any] | None:
    for path in sorted(constraint_sessions_dir().glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "collecting":
            continue
        if int(data.get("active_uid") or 0) != int(uid):
            continue
        return data
    return None


def suggestion_path(suggestion_id: str) -> Path:
    return constraint_suggestions_dir() / f"{suggestion_id}.json"


def save_suggestion(payload: dict[str, Any]) -> Path:
    suggestion_id = str(payload.get("id") or _new_record_id("suggestion"))
    payload["id"] = suggestion_id
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("status", "pending")
    payload.setdefault("created_at", _now_iso())
    path = suggestion_path(suggestion_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_suggestion(suggestion_id: str) -> dict[str, Any]:
    path = suggestion_path(suggestion_id)
    if not path.exists():
        raise FileNotFoundError(f"constraint suggestion not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"invalid suggestion payload at {path}")
    return data


def list_suggestions(
    *,
    profile_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(constraint_suggestions_dir().glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if profile_id and str(data.get("profile_id") or "").strip() != profile_id:
            continue
        if status and str(data.get("status") or "").strip() != status:
            continue
        data["path"] = str(path)
        entries.append(data)
    return entries


def apply_suggestion(suggestion_id: str) -> dict[str, Any]:
    suggestion = load_suggestion(suggestion_id)
    profile_id = str(suggestion.get("profile_id") or "").strip()
    if not profile_id:
        raise ValueError("suggestion is missing profile_id")
    patch = suggestion.get("proposed_patch") or {}
    if not isinstance(patch, dict) or not patch:
        raise ValueError("suggestion has no proposed_patch to apply")
    applied = upsert_profile(
        profile_id,
        patch,
        replace_lists=False,
        source=f"suggestion:{suggestion_id}",
    )
    suggestion["status"] = "applied"
    suggestion["applied_at"] = _now_iso()
    save_suggestion(suggestion)
    return {
        "suggestion": suggestion,
        "applied": applied,
    }


def update_suggestion_status(suggestion_id: str, status: str) -> dict[str, Any]:
    suggestion = load_suggestion(suggestion_id)
    suggestion["status"] = status
    suggestion["updated_at"] = _now_iso()
    save_suggestion(suggestion)
    return suggestion


def review_suggestion(suggestion_id: str) -> dict[str, Any]:
    suggestion = load_suggestion(suggestion_id)
    profile_id = str(suggestion.get("profile_id") or "").strip()
    current_state = user_profile_bootstrap_state(profile_id)
    current = current_state.get("current_profile") or seed_profile(profile_id)
    patch = suggestion.get("proposed_patch") or {}
    preview = deep_merge(current, patch, replace_lists=False) if isinstance(patch, dict) else current
    return {
        "suggestion": suggestion,
        "current_profile": current,
        "preview_profile": preview,
        "missing_fields": current_state.get("missing_fields") or [],
    }


def make_learning_suggestion(
    *,
    profile_id: str,
    stage: str,
    title: str,
    summary: str,
    proposed_patch: dict[str, Any] | None,
    evidence: list[dict[str, Any]] | None = None,
    risk_level: str = "low",
) -> dict[str, Any]:
    payload = {
        "profile_id": profile_id,
        "stage": stage,
        "title": title,
        "summary": summary,
        "proposed_patch": proposed_patch or {},
        "evidence": evidence or [],
        "risk_level": risk_level,
    }
    path = save_suggestion(payload)
    payload["path"] = str(path)
    return payload
