"""Append-only user memory event log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.logger import get_logger

MEMORY_DIR = agentflow_home() / "memory"
EVENTS_PATH = MEMORY_DIR / "events.jsonl"
SCHEMA_VERSION = 1

_log = get_logger("shared.memory")

_INTENTS_DIR = agentflow_home() / "intents"
_INTENTS_CURRENT = _INTENTS_DIR / "current.yaml"

# Per-process cache for a single_use intent. After we delete the file on
# first read, subsequent reads inside the same CLI invocation still get the
# intent back — but a brand new process will correctly see None.
_SINGLE_USE_CACHE: dict[str, Any] | None = None


def memory_events_path() -> Path:
    ensure_user_dirs()
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return EVENTS_PATH


def append_memory_event(
    event_type: str,
    *,
    article_id: str | None = None,
    hotspot_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Path:
    path = memory_events_path()
    record = {
        "schema_version": SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "article_id": article_id,
        "hotspot_id": hotspot_id,
        "payload": payload or {},
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        from agentflow.shared.agent_bridge import emit_agent_event

        emit_agent_event(
            source="memory",
            event_type=event_type,
            article_id=article_id,
            hotspot_id=hotspot_id,
            payload=record.get("payload") or {},
            occurred_at=str(record.get("ts") or ""),
            source_ref={"store": "memory/events.jsonl"},
        )
    except Exception:
        pass
    return path


def read_memory_events(
    *,
    article_id: str | None = None,
    event_type: str | None = None,
    hotspot_id: str | None = None,
) -> list[dict[str, Any]]:
    path = memory_events_path()
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if article_id and record.get("article_id") != article_id:
                continue
            if hotspot_id and record.get("hotspot_id") != hotspot_id:
                continue
            if event_type and record.get("event_type") != event_type:
                continue
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# TopicIntent loader — shared by D2 skeleton/fill and publish Step 1b.
# ---------------------------------------------------------------------------


def _current_intent_path() -> Path:
    return _INTENTS_CURRENT


def load_current_intent() -> dict[str, Any] | None:
    """Read ``~/.agentflow/intents/current.yaml``.

    Returns ``None`` if the file is missing, empty, or malformed.

    TTL semantics (from ``metadata.ttl``):

    - ``single_use`` — returned once per CLI invocation, then the file is
      deleted. Subsequent reads inside the **same Python process** still
      return the cached intent (so skeleton + fill within one ``af write``
      both see it). A fresh process will correctly see ``None``.
    - ``session``   — returned on every call until the user runs ``af intent-clear``
    - ``persistent``— returned on every call; ``af prefs-rebuild`` also
      remembers it in ``preferences.intent``

    The intent file is the state. Callers do not need to clean up after
    ``single_use`` reads — this helper handles it.
    """
    global _SINGLE_USE_CACHE

    path = _current_intent_path()
    if not path.exists():
        # File is gone. If we already consumed a single_use intent in this
        # process, keep returning it to callers who still need it (e.g. the
        # fill stage after the skeleton stage triggered the delete).
        return _SINGLE_USE_CACHE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return _SINGLE_USE_CACHE
    if not raw.strip():
        return _SINGLE_USE_CACHE
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        _log.warning("intent file at %s is not valid YAML; ignoring", path)
        return _SINGLE_USE_CACHE
    if not isinstance(data, dict) or not data:
        return _SINGLE_USE_CACHE

    ttl = ""
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        ttl = str(metadata.get("ttl") or "")

    if ttl == "single_use":
        # The file IS the state — delete so the next *process* sees None.
        try:
            path.unlink()
        except OSError:
            _log.warning("failed to delete single_use intent file at %s", path)
        _SINGLE_USE_CACHE = data

    return data


def _reset_intent_cache() -> None:
    """Test helper — clear the in-process single_use cache."""
    global _SINGLE_USE_CACHE
    _SINGLE_USE_CACHE = None


def intent_query_text(intent: dict[str, Any] | None) -> str:
    """Extract ``intent.query.text``, returning an empty string if absent."""
    if not intent:
        return ""
    query = intent.get("query")
    if not isinstance(query, dict):
        return ""
    text = query.get("text")
    if not isinstance(text, str):
        return ""
    return text.strip()


def intent_keyword_terms(intent: dict[str, Any] | None) -> list[str]:
    """Extract expanded keyword terms from ``intent.keywords.expanded``."""
    if not intent:
        return []
    keywords = intent.get("keywords")
    if not isinstance(keywords, dict):
        return []
    expanded = keywords.get("expanded")
    if not isinstance(expanded, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in expanded:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def intent_avoid_terms(intent: dict[str, Any] | None) -> list[str]:
    """Extract avoid-terms from ``intent.keywords.avoid``."""
    if not intent:
        return []
    keywords = intent.get("keywords")
    if not isinstance(keywords, dict):
        return []
    avoid = keywords.get("avoid")
    if not isinstance(avoid, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in avoid:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def render_topic_intent_block(intent: dict[str, Any] | None) -> str:
    """Render the Chinese "话题意图" prompt block.

    Returns an empty string when the intent is missing / empty so callers can
    unconditionally substitute into prompt templates without stray braces.
    """
    text = intent_query_text(intent)
    if not text:
        return ""
    keywords = intent_keyword_terms(intent)
    avoid_terms = intent_avoid_terms(intent)
    # Guard against accidental brace injection from user-supplied query text
    # (the prompt renderer treats {foo} as a substitution placeholder).
    safe_text = text.replace("{", "").replace("}", "")
    block = (
        "## 话题意图 (TopicIntent — 必须遵守)\n\n"
        f"用户当前的创作意图是: \"{safe_text}\"\n\n"
        "要求:\n"
        "- 文章的中心论点必须紧扣这个意图,不要发明新的话题包装\n"
        "- 如果 hotspot/references 里不支持这个意图,你应该在结构上反映"
        "这种紧张,不要硬编造\n"
        "- 不要引入和意图无关的术语作为修辞包装\n"
    )
    if keywords:
        block += "\n优先围绕这些关键词/概念展开:\n"
        block += "\n".join(f"- {term}" for term in keywords[:12])
        block += "\n"
    if avoid_terms:
        block += "\n尽量避免被这些泛词带偏:\n"
        block += "\n".join(f"- {term}" for term in avoid_terms[:8])
        block += "\n"
    return block
