"""Persist publish results to ``~/.agentflow/publish_history.jsonl``."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.memory import append_memory_event
from agentflow.shared.models import PublishResult

HISTORY_PATH: Path = agentflow_home() / "publish_history.jsonl"


def append_publish_record(article_id: str, result: PublishResult) -> Path:
    """Append a single JSONL record for ``result`` under ``article_id``.

    The record schema is fixed by Agent D4 spec:
    ``{article_id, platform, status, published_url, published_at, failure_reason}``.
    """
    ensure_user_dirs()

    published_at = result.published_at
    if isinstance(published_at, datetime):
        published_at_iso = published_at.isoformat()
    elif published_at is None:
        published_at_iso = datetime.now().isoformat()
    else:
        published_at_iso = str(published_at)

    record = {
        "article_id": article_id,
        "platform": result.platform,
        "status": result.status,
        "published_url": result.published_url,
        "platform_post_id": result.platform_post_id,
        "published_at": published_at_iso,
        "failure_reason": result.failure_reason,
    }
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        if result.status == "success":
            from agentflow.shared.topic_profile_learning import suggest_from_publish

            suggestion = suggest_from_publish(
                article_id=article_id,
                platform=result.platform,
                published_url=result.published_url,
            )
            if suggestion:
                append_memory_event(
                    "topic_profile_suggestion_created",
                    article_id=article_id,
                    payload={
                        "profile_id": suggestion.get("profile_id"),
                        "suggestion_id": suggestion.get("id"),
                        "stage": "publish",
                    },
                )
    except Exception:
        pass
    try:
        from agentflow.shared.agent_bridge import emit_agent_event

        emit_agent_event(
            source="publish",
            event_type="publish.record",
            article_id=article_id,
            payload=record,
            occurred_at=published_at_iso,
            source_ref={"store": "publish_history.jsonl"},
        )
    except Exception:
        pass
    return HISTORY_PATH


def append_rollback_record(
    article_id: str,
    platform: str,
    platform_post_id: str | None,
    published_url: str | None,
    failure_reason: str | None = None,
) -> Path:
    """Append a rollback record. Status is ``rolled_back`` on success or
    ``rollback_failed`` on error."""
    ensure_user_dirs()
    record = {
        "article_id": article_id,
        "platform": platform,
        "status": "rolled_back" if failure_reason is None else "rollback_failed",
        "published_url": published_url,
        "platform_post_id": platform_post_id,
        "published_at": datetime.now().isoformat(),
        "failure_reason": failure_reason,
    }
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        from agentflow.shared.agent_bridge import emit_agent_event

        emit_agent_event(
            source="publish",
            event_type="publish.record",
            article_id=article_id,
            payload=record,
            occurred_at=str(record.get("published_at") or ""),
            source_ref={"store": "publish_history.jsonl"},
        )
    except Exception:
        pass
    return HISTORY_PATH


def read_publish_history(article_id: str | None = None) -> list[dict[str, Any]]:
    """Read publish history JSONL, optionally filtered by article_id."""
    ensure_user_dirs()
    if not HISTORY_PATH.exists():
        return []

    records: list[dict[str, Any]] = []
    with HISTORY_PATH.open("r", encoding="utf-8") as fh:
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
            records.append(record)
    records.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
    return records
