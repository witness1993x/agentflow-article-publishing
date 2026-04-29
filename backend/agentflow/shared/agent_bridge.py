"""Shared agent bridge helpers: outbound event fan-out for external agents."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

import requests

from agentflow.shared.logger import get_logger

_log = get_logger("shared.agent_bridge")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_webhook_url() -> str:
    return (os.environ.get("AGENTFLOW_AGENT_EVENT_WEBHOOK_URL") or "").strip()


def _event_auth_header() -> str:
    return (os.environ.get("AGENTFLOW_AGENT_EVENT_AUTH_HEADER") or "").strip()


def _build_event_id(envelope: dict[str, Any]) -> str:
    stable = {
        "source": envelope.get("source"),
        "event_type": envelope.get("event_type"),
        "occurred_at": envelope.get("occurred_at"),
        "article_id": envelope.get("article_id"),
        "hotspot_id": envelope.get("hotspot_id"),
        "payload": envelope.get("payload"),
    }
    digest = hashlib.sha1(
        json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"evt_{digest[:16]}"


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    auth = _event_auth_header()
    if not auth:
        return headers
    if ":" in auth and not auth.lower().startswith(("bearer ", "basic ")):
        key, _, value = auth.partition(":")
        headers[key.strip()] = value.strip()
        return headers
    headers["Authorization"] = auth
    return headers


def emit_agent_event(
    *,
    source: str,
    event_type: str,
    article_id: str | None = None,
    hotspot_id: str | None = None,
    payload: dict[str, Any] | None = None,
    occurred_at: str | None = None,
    source_ref: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    session_id: str | None = None,
    actor: dict[str, Any] | None = None,
) -> None:
    """Best-effort POST of an event envelope to an external agent webhook."""
    url = _event_webhook_url()
    if not url:
        return

    envelope: dict[str, Any] = {
        "schema_version": 1,
        "occurred_at": occurred_at or _now_iso(),
        "ingested_at": _now_iso(),
        "source": source,
        "event_type": event_type,
        "article_id": article_id,
        "hotspot_id": hotspot_id,
        "payload": payload or {},
    }
    if source_ref:
        envelope["source_ref"] = source_ref
    if correlation_id:
        envelope["correlation_id"] = correlation_id
    if session_id:
        envelope["session_id"] = session_id
    if actor:
        envelope["actor"] = actor
    envelope["event_id"] = _build_event_id(envelope)

    try:
        resp = requests.post(
            url,
            headers=_headers(),
            data=json.dumps(envelope, ensure_ascii=False),
            timeout=3,
        )
        if resp.status_code >= 400:
            _log.warning(
                "agent event webhook returned %s for %s",
                resp.status_code,
                envelope["event_id"],
            )
    except Exception as err:  # pragma: no cover - best effort only
        _log.warning("agent event webhook failed for %s: %s", envelope["event_id"], err)
