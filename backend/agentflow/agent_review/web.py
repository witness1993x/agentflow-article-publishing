"""HTTP agent bridge over review/preflight state and selected CLI commands.

Read endpoints expose the current review state for polling agents and local
dashboards. A separate command endpoint can be enabled for trusted external
automation so a framework like OpenClaw can query state, issue whitelisted
commands, and observe the resulting event stream via outbound webhooks.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from agentflow.agent_review import preflight, state
from agentflow.agent_review.triggers import _af_argv
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.agent_bridge import emit_agent_event


_LOCALHOST_ORIGINS = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:7860",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:7860",
    "http://127.0.0.1:8000",
]


def _read_metadata(article_id: str) -> dict[str, Any] | None:
    p = agentflow_home() / "drafts" / article_id / "metadata.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return None


def _last_transition_at(meta: dict[str, Any]) -> str | None:
    history = meta.get("gate_history") or []
    if not history:
        return meta.get("updated_at") or meta.get("saved_at") or meta.get("created_at")
    return history[-1].get("timestamp")


def _summarise(article_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    history = meta.get("gate_history") or []
    cur = str(history[-1].get("to_state")) if history else state.STATE_TOPIC_POOL
    publisher = (meta.get("publisher_account") or {}).get("brand") or ""
    return {
        "article_id": article_id,
        "title": meta.get("title") or "(no title)",
        "current_state": cur,
        "publisher": publisher,
        "published_url": meta.get("published_url"),
        "last_transition_at": _last_transition_at(meta),
    }


def _require_auth(request: Request) -> None:
    """Bearer-token gate. No-op if ``REVIEW_DASHBOARD_TOKEN`` is unset."""
    token = (os.environ.get("REVIEW_DASHBOARD_TOKEN") or "").strip()
    if not token:
        return
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    supplied = auth.split(" ", 1)[1].strip()
    if supplied != token:
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _require_command_auth(request: Request) -> None:
    """Bearer-token gate for mutating agent commands. Always requires a token."""
    token = (os.environ.get("AGENTFLOW_AGENT_BRIDGE_TOKEN") or "").strip()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="agent command bridge disabled (AGENTFLOW_AGENT_BRIDGE_TOKEN unset)",
        )
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    supplied = auth.split(" ", 1)[1].strip()
    if supplied != token:
        raise HTTPException(status_code=401, detail="invalid bearer token")


class CommandRequest(BaseModel):
    request_id: str | None = None
    command: str
    params: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


def _bool_opt(options: dict[str, Any], key: str) -> bool:
    return bool(options.get(key))


def _str_param(params: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    raw = params.get(key)
    if raw is None or str(raw).strip() == "":
        if required:
            raise HTTPException(status_code=400, detail=f"missing required param: {key}")
        return None
    return str(raw).strip()


def _int_param(params: dict[str, Any], key: str, *, required: bool = False) -> int | None:
    raw = params.get(key)
    if raw is None or str(raw).strip() == "":
        if required:
            raise HTTPException(status_code=400, detail=f"missing required param: {key}")
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as err:
        raise HTTPException(status_code=400, detail=f"invalid int param {key!r}: {err}") from err


_COMMAND_SPECS: dict[str, dict[str, Any]] = {
    "doctor": {
        "scope": "read",
        "description": "Run preflight health checks.",
        "timeout_seconds": 30,
        "dangerous": False,
    },
    "review_status": {
        "scope": "read",
        "description": "Read one article's gate history and current state.",
        "timeout_seconds": 15,
        "dangerous": False,
    },
    "review_list": {
        "scope": "read",
        "description": "List articles grouped by review state.",
        "timeout_seconds": 15,
        "dangerous": False,
    },
    "draft_show": {
        "scope": "read",
        "description": "Read a draft artifact by article id.",
        "timeout_seconds": 15,
        "dangerous": False,
    },
    "memory_tail": {
        "scope": "read",
        "description": "Read recent memory events.",
        "timeout_seconds": 15,
        "dangerous": False,
    },
    "intent_show": {
        "scope": "read",
        "description": "Read the current topic intent.",
        "timeout_seconds": 15,
        "dangerous": False,
    },
    "hotspots": {
        "scope": "pipeline",
        "description": "Run D1 hotspots scan.",
        "timeout_seconds": 180,
        "dangerous": False,
    },
    "write": {
        "scope": "pipeline",
        "description": "Create a draft from a hotspot.",
        "timeout_seconds": 240,
        "dangerous": False,
    },
    "fill": {
        "scope": "pipeline",
        "description": "Fill a skeleton-only draft.",
        "timeout_seconds": 240,
        "dangerous": False,
    },
    "image_gate": {
        "scope": "pipeline",
        "description": "Run the explicit image generation gate.",
        "timeout_seconds": 240,
        "dangerous": False,
    },
    "preview": {
        "scope": "pipeline",
        "description": "Generate D3 platform versions.",
        "timeout_seconds": 180,
        "dangerous": False,
    },
    "medium_package": {
        "scope": "pipeline",
        "description": "Build Medium browser-ops package.",
        "timeout_seconds": 60,
        "dangerous": False,
    },
    "review_post_d": {
        "scope": "pipeline",
        "description": "Post a Gate D channel-selection card.",
        "timeout_seconds": 30,
        "dangerous": False,
    },
    "review_publish_mark": {
        "scope": "publish",
        "description": "Mark an article as published after a manual post.",
        "timeout_seconds": 30,
        "dangerous": True,
    },
    "publish": {
        "scope": "publish",
        "description": "Run D4 publisher for one or more platforms.",
        "timeout_seconds": 600,
        "dangerous": True,
    },
}

_BRIDGE_SPEC_VERSION = "1.0"
_BRIDGE_STABILITY = {
    "versioning": "semantic-ish minor additions within v1; breaking changes require v2",
    "guarantees": [
        "read endpoints remain additive within v1",
        "command names are stable within v1 once listed in /api/bridge",
        "event envelope top-level fields are stable within v1",
    ],
    "non_goals": [
        "no websocket session contract in v1",
        "no durable job queue semantics in v1",
    ],
}


def _bridge_commands_descriptor() -> dict[str, dict[str, Any]]:
    return {
        name: {
            "scope": spec["scope"],
            "description": spec["description"],
            "dangerous": bool(spec.get("dangerous")),
            "timeout_seconds": int(spec.get("timeout_seconds") or 60),
        }
        for name, spec in _COMMAND_SPECS.items()
    }


def _bridge_schema() -> dict[str, Any]:
    command_names = sorted(_COMMAND_SPECS)
    return {
        "bridge_version": _BRIDGE_SPEC_VERSION,
        "descriptor_schema": {
            "type": "object",
            "required": [
                "bridge_version",
                "event_webhook_enabled",
                "command_endpoint_enabled",
                "dangerous_commands_enabled",
                "read_auth_env",
                "command_auth_env",
                "commands",
            ],
        },
        "command_request_schema": {
            "type": "object",
            "required": ["command"],
            "properties": {
                "request_id": {"type": "string"},
                "command": {"type": "string", "enum": command_names},
                "params": {"type": "object"},
                "options": {"type": "object"},
            },
        },
        "event_envelope_schema": {
            "type": "object",
            "required": [
                "schema_version",
                "event_id",
                "occurred_at",
                "ingested_at",
                "source",
                "event_type",
                "payload",
            ],
            "properties": {
                "schema_version": {"type": "integer"},
                "event_id": {"type": "string"},
                "occurred_at": {"type": "string"},
                "ingested_at": {"type": "string"},
                "source": {"type": "string", "enum": ["memory", "gate", "publish", "api"]},
                "event_type": {"type": "string"},
                "article_id": {"type": ["string", "null"]},
                "hotspot_id": {"type": ["string", "null"]},
                "payload": {"type": "object"},
            },
        },
    }


def _dangerous_commands_enabled() -> bool:
    return (os.environ.get("AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _build_command_argv(req: CommandRequest) -> list[str]:
    params = dict(req.params or {})
    options = dict(req.options or {})
    cmd = req.command

    if cmd == "doctor":
        argv = ["doctor", "--json"]
        if _bool_opt(options, "strict"):
            argv.append("--strict")
        if _bool_opt(options, "fresh"):
            argv.append("--fresh")
        return argv
    if cmd == "review_status":
        return ["review-status", _str_param(params, "article_id", required=True) or "", "--json"]
    if cmd == "review_list":
        argv = ["review-list", "--json"]
        if (state_filter := _str_param(params, "state")):
            argv.extend(["--state", state_filter])
        if (since := _str_param(params, "since")):
            argv.extend(["--since", since])
        return argv
    if cmd == "draft_show":
        return ["draft-show", _str_param(params, "article_id", required=True) or "", "--json"]
    if cmd == "memory_tail":
        argv = ["memory-tail", "--json"]
        if (limit := _int_param(params, "limit")) is not None:
            argv.extend(["--limit", str(limit)])
        if (article_id := _str_param(params, "article_id")):
            argv.extend(["--article-id", article_id])
        return argv
    if cmd == "intent_show":
        return ["intent-show", "--json"]
    if cmd == "hotspots":
        argv = ["hotspots", "--json"]
        if (scan_window_hours := _int_param(params, "scan_window_hours")) is not None:
            argv.extend(["--scan-window-hours", str(scan_window_hours)])
        if (target_candidates := _int_param(params, "target_candidates")) is not None:
            argv.extend(["--target-candidates", str(target_candidates)])
        if (filter_pattern := _str_param(params, "filter")):
            argv.extend(["--filter", filter_pattern])
        if (profile_id := _str_param(params, "profile")):
            argv.extend(["--profile", profile_id])
        if (top_k := _int_param(params, "gate_a_top_k")) is not None:
            argv.extend(["--gate-a-top-k", str(top_k)])
        return argv
    if cmd == "write":
        argv = ["write", _str_param(params, "hotspot_id", required=True) or "", "--json"]
        if (angle := _int_param(params, "angle")) is not None:
            argv.extend(["--angle", str(angle)])
        if (series := _str_param(params, "series")):
            argv.extend(["--series", series])
        if _bool_opt(options, "auto_pick"):
            argv.append("--auto-pick")
        if _bool_opt(options, "ignore_prefs"):
            argv.append("--ignore-prefs")
        if (title := _int_param(params, "title")) is not None:
            argv.extend(["--title", str(title)])
        if (opening := _int_param(params, "opening")) is not None:
            argv.extend(["--opening", str(opening)])
        if (closing := _int_param(params, "closing")) is not None:
            argv.extend(["--closing", str(closing)])
        return argv
    if cmd == "fill":
        return [
            "fill",
            _str_param(params, "article_id", required=True) or "",
            "--title",
            str(_int_param(params, "title", required=True)),
            "--opening",
            str(_int_param(params, "opening", required=True)),
            "--closing",
            str(_int_param(params, "closing", required=True)),
            "--json",
        ]
    if cmd == "image_gate":
        argv = ["image-gate", _str_param(params, "article_id", required=True) or "", "--json"]
        if (mode := _str_param(params, "mode")):
            argv.extend(["--mode", mode])
        if (cover_style := _str_param(params, "cover_style")):
            argv.extend(["--cover-style", cover_style])
        if (cover_size := _str_param(params, "cover_size")):
            argv.extend(["--cover-size", cover_size])
        if (cover_resolution := _str_param(params, "cover_resolution")):
            argv.extend(["--cover-resolution", cover_resolution])
        return argv
    if cmd == "preview":
        argv = ["preview", _str_param(params, "article_id", required=True) or "", "--json"]
        if (platforms := _str_param(params, "platforms")):
            argv.extend(["--platforms", platforms])
        if _bool_opt(options, "ignore_prefs"):
            argv.append("--ignore-prefs")
        if _bool_opt(options, "force_strip_images"):
            argv.append("--force-strip-images")
        if _bool_opt(options, "skip_images"):
            argv.append("--skip-images")
        return argv
    if cmd == "medium_package":
        argv = ["medium-package", _str_param(params, "article_id", required=True) or "", "--json"]
        if (distribution_mode := _str_param(params, "distribution_mode")):
            argv.extend(["--distribution-mode", distribution_mode])
        if (canonical_url := _str_param(params, "canonical_url")):
            argv.extend(["--canonical-url", canonical_url])
        return argv
    if cmd == "review_post_d":
        return ["review-post-d", _str_param(params, "article_id", required=True) or "", "--json"]
    if cmd == "review_publish_mark":
        argv = [
            "review-publish-mark",
            _str_param(params, "article_id", required=True) or "",
            _str_param(params, "published_url", required=True) or "",
            "--json",
        ]
        if (platform := _str_param(params, "platform")):
            argv.extend(["--platform", platform])
        if (notes := _str_param(params, "notes")):
            argv.extend(["--notes", notes])
        return argv
    if cmd == "publish":
        argv = ["publish", _str_param(params, "article_id", required=True) or "", "--json"]
        if (platforms := _str_param(params, "platforms")):
            argv.extend(["--platforms", platforms])
        if _bool_opt(options, "force_strip_images"):
            argv.append("--force-strip-images")
        return argv

    raise HTTPException(status_code=400, detail=f"unsupported command: {cmd}")


def _run_command(req: CommandRequest) -> dict[str, Any]:
    spec = _COMMAND_SPECS.get(req.command)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"unsupported command: {req.command}")
    if spec.get("dangerous") and not _dangerous_commands_enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                f"dangerous command {req.command!r} disabled; "
                "set AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true to allow it"
            ),
        )

    request_id = req.request_id or str(uuid.uuid4())
    emit_agent_event(
        source="api",
        event_type="agent.command.requested",
        article_id=_str_param(req.params, "article_id"),
        hotspot_id=_str_param(req.params, "hotspot_id"),
        payload={
            "request_id": request_id,
            "command": req.command,
            "scope": spec["scope"],
            "params": req.params,
            "options": req.options,
        },
        actor={"type": "agent_bridge"},
    )

    argv = _af_argv(*_build_command_argv(req))
    env = os.environ.copy()
    timeout_seconds = int(spec.get("timeout_seconds") or 60)
    try:
        res = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        emit_agent_event(
            source="api",
            event_type="agent.command.failed",
            article_id=_str_param(req.params, "article_id"),
            hotspot_id=_str_param(req.params, "hotspot_id"),
            payload={
                "request_id": request_id,
                "command": req.command,
                "error": f"timeout after {timeout_seconds}s",
            },
            actor={"type": "agent_bridge"},
        )
        raise HTTPException(status_code=504, detail=f"command timed out after {timeout_seconds}s") from err

    stdout = (res.stdout or "").strip()
    stderr = (res.stderr or "").strip()
    data: Any = None
    if stdout:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = {"raw_stdout": stdout}

    payload = {
        "ok": res.returncode == 0,
        "request_id": request_id,
        "command": req.command,
        "scope": spec["scope"],
        "returncode": res.returncode,
        "data": data,
        "stderr": stderr or None,
    }
    emit_agent_event(
        source="api",
        event_type="agent.command.completed" if res.returncode == 0 else "agent.command.failed",
        article_id=_str_param(req.params, "article_id"),
        hotspot_id=_str_param(req.params, "hotspot_id"),
        payload={
            "request_id": request_id,
            "command": req.command,
            "returncode": res.returncode,
        },
        actor={"type": "agent_bridge"},
    )
    if res.returncode != 0:
        raise HTTPException(status_code=400, detail=payload)
    return payload


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>AgentFlow agent bridge API</title></head>
<body>
<h1>AgentFlow agent bridge API</h1>
<p>Read-first JSON endpoints plus an optional command endpoint for trusted
automation. See
<code>agentflow/agent_review/web.py</code>.</p>
<ul>
  <li><a href="/api/health">/api/health</a> &mdash; preflight checks + readiness gates</li>
  <li><a href="/api/articles">/api/articles</a> &mdash; all articles (summary)</li>
  <li><a href="/api/articles?state=published">/api/articles?state=published</a> &mdash; filter by gate state</li>
  <li><code>/api/article/{article_id}</code> &mdash; full metadata + gate_history for one article</li>
  <li><a href="/api/bridge">/api/bridge</a> &mdash; machine-readable bridge capabilities</li>
  <li><a href="/api/bridge/schema">/api/bridge/schema</a> &mdash; machine-readable v1 schemas</li>
  <li><code>POST /api/commands</code> &mdash; whitelisted agent commands (requires AGENTFLOW_AGENT_BRIDGE_TOKEN)</li>
</ul>
</body>
</html>
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="AgentFlow agent bridge",
        description="Review state API plus optional command endpoint for external agents.",
        version="1.1",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_LOCALHOST_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML

    @app.get("/api/health")
    def health(_: None = Depends(_require_auth)) -> JSONResponse:
        checks = [c.to_dict() for c in preflight.all_checks(fresh=False)]
        ready: dict[str, bool] = {}
        for name, fn in (
            ("review_daemon", preflight.assert_ready_for_review_daemon),
            ("hotspots", preflight.assert_ready_for_hotspots),
            ("image_gate", preflight.assert_ready_for_image_gate),
        ):
            try:
                fn()
                ready[name] = True
            except preflight.PreflightError:
                ready[name] = False
        return JSONResponse({"checks": checks, "ready": ready})

    @app.get("/api/articles")
    def list_articles(
        state_filter: str | None = Query(default=None, alias="state"),
        _: None = Depends(_require_auth),
    ) -> JSONResponse:
        drafts = agentflow_home() / "drafts"
        out: list[dict[str, Any]] = []
        if drafts.exists():
            for sub in sorted(drafts.iterdir()):
                if not sub.is_dir():
                    continue
                meta = _read_metadata(sub.name)
                if meta is None:
                    continue
                summary = _summarise(sub.name, meta)
                if state_filter and summary["current_state"] != state_filter:
                    continue
                out.append(summary)
        return JSONResponse(out)

    @app.get("/api/article/{article_id}")
    def get_article(
        article_id: str,
        _: None = Depends(_require_auth),
    ) -> JSONResponse:
        meta = _read_metadata(article_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"article {article_id!r} not found")
        return JSONResponse({
            "metadata": meta,
            "gate_history": list(meta.get("gate_history") or []),
        })

    @app.get("/api/bridge")
    def bridge(_: None = Depends(_require_auth)) -> JSONResponse:
        commands = _bridge_commands_descriptor()
        return JSONResponse(
            {
                "bridge_version": _BRIDGE_SPEC_VERSION,
                "stability": _BRIDGE_STABILITY,
                "event_webhook_enabled": bool(os.environ.get("AGENTFLOW_AGENT_EVENT_WEBHOOK_URL")),
                "command_endpoint_enabled": bool(os.environ.get("AGENTFLOW_AGENT_BRIDGE_TOKEN")),
                "dangerous_commands_enabled": _dangerous_commands_enabled(),
                "read_auth_env": "REVIEW_DASHBOARD_TOKEN",
                "command_auth_env": "AGENTFLOW_AGENT_BRIDGE_TOKEN",
                "commands": commands,
            }
        )

    @app.get("/api/bridge/schema")
    def bridge_schema(_: None = Depends(_require_auth)) -> JSONResponse:
        return JSONResponse(_bridge_schema())

    @app.post("/api/commands")
    def run_command(
        body: CommandRequest,
        _: None = Depends(_require_command_auth),
    ) -> JSONResponse:
        return JSONResponse(_run_command(body))

    return app


app = create_app()
