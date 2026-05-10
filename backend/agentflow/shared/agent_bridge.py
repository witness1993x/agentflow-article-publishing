"""Shared agent bridge helpers: outbound event fan-out for external agents.

Two delivery modes are supported, picked at runtime per event:

1. **Webhook mode** (default when ``AGENTFLOW_AGENT_EVENT_WEBHOOK_URL`` is set):
   POST the envelope to that URL. This is the original integration shape
   used when the OpenClaw / agent bridge runs as a separate service.

2. **File-queue mode** (default when the URL is unset): append each envelope
   as one JSON line to ``~/.agentflow/agent_events/queue.jsonl``. An agent
   harness running on the same machine (e.g. OpenClaw on a managed cloud
   computer with a mounted Lark window) tails the file and pushes each
   event to Lark directly — no inbound HTTP listener required. See
   ``.cursor/skills/agentflow-open-claw-v2/SKILL.md`` §"Agent-Lark window
   mode" for the operator-side recipe.

Either mode can be forced via ``AGENTFLOW_AGENT_EVENT_MODE`` ∈ {``webhook``,
``file``, ``both``}. Default is ``webhook`` if URL set else ``file``.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.logger import get_logger

_log = get_logger("shared.agent_bridge")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_webhook_url() -> str:
    return (os.environ.get("AGENTFLOW_AGENT_EVENT_WEBHOOK_URL") or "").strip()


def _event_auth_header() -> str:
    return (os.environ.get("AGENTFLOW_AGENT_EVENT_AUTH_HEADER") or "").strip()


def _event_mode() -> str:
    """Resolve the delivery mode per env / URL state. Returns one of
    ``webhook``, ``file``, ``both``."""
    raw = (os.environ.get("AGENTFLOW_AGENT_EVENT_MODE") or "").strip().lower()
    if raw in {"webhook", "file", "both"}:
        return raw
    return "webhook" if _event_webhook_url() else "file"


def _queue_path() -> Path:
    p = agentflow_home() / "agent_events" / "queue.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


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


def _post_webhook(envelope: dict[str, Any]) -> None:
    url = _event_webhook_url()
    if not url:
        return
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


def _append_queue(envelope: dict[str, Any]) -> None:
    """Append the envelope as a single JSON line to the on-disk event queue.

    Format: one event per line, UTF-8 encoded. The OpenClaw skill agent
    tails this file and forwards each event to Lark via the mounted Lark
    window — see SKILL.md §"Agent-Lark window mode".
    """
    try:
        path = _queue_path()
        line = json.dumps(envelope, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as err:  # pragma: no cover - best effort only
        _log.warning(
            "agent event queue append failed for %s: %s",
            envelope.get("event_id", "?"),
            err,
        )


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
    """Best-effort fan-out of an event envelope to an external agent.

    Delivery mode is resolved by ``_event_mode()``:
    - ``webhook``: POST to ``AGENTFLOW_AGENT_EVENT_WEBHOOK_URL``.
    - ``file``: append to ``~/.agentflow/agent_events/queue.jsonl`` for an
      in-process agent harness (OpenClaw / Cursor / Claude Code) to tail.
    - ``both``: do both. Useful while migrating between modes.

    With no env config and Lark primary mode, file is the default — no HTTP
    listener is required, the agent reads the queue and pushes Lark cards
    directly via its mounted Lark window.
    """
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

    mode = _event_mode()
    if mode in {"webhook", "both"}:
        _post_webhook(envelope)
    if mode in {"file", "both"}:
        _append_queue(envelope)
