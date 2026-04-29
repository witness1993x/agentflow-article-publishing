# Agent Bridge v1

AgentFlow ships a local-first agent bridge for external automation frameworks.
It is designed for OpenClaw-style orchestrators, but the contract is generic
enough for any LLM agent that can:

- read JSON over HTTP
- tail webhook events
- send authenticated HTTP POST requests

The bridge has two surfaces:

1. outbound events: AgentFlow best-effort POSTs normalized envelopes to your
   webhook
2. inbound commands: trusted agents call `POST /api/commands` to run a
   whitelisted subset of `af` commands

This is not a standalone cloud control plane. The default deployment model is
still single-user and localhost-bound.

## Stability Contract

This document defines the **v1** bridge contract.

Within v1:

- read endpoints are additive
- listed command names are stable
- event envelope top-level fields are stable
- breaking changes require a new bridge version

Non-goals for v1:

- no WebSocket session contract
- no durable job queue semantics
- no multi-tenant session model

## Start

```bash
cd backend
source .venv/bin/activate
af review-dashboard
```

Default base URL:

```text
http://127.0.0.1:7860
```

## Required Env

Add these to `backend/.env` when you want the full bridge:

```dotenv
REVIEW_DASHBOARD_TOKEN=read-token
AGENTFLOW_AGENT_BRIDGE_TOKEN=write-token
AGENTFLOW_AGENT_EVENT_WEBHOOK_URL=https://your-agent.example.com/events
AGENTFLOW_AGENT_EVENT_AUTH_HEADER=Bearer your-secret
AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=false
```

Notes:

- `REVIEW_DASHBOARD_TOKEN` protects read endpoints.
- `AGENTFLOW_AGENT_BRIDGE_TOKEN` protects `POST /api/commands`.
- if `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` is unset, outbound events are disabled.
- dangerous commands remain blocked unless
  `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`.

## Runtime Truth

These remain the authoritative stores:

- `~/.agentflow/drafts/<article_id>/metadata.json`: single-article state
- `~/.agentflow/memory/events.jsonl`: cross-article memory events
- `~/.agentflow/publish_history.jsonl`: publish audit log

The bridge exposes or relays these stores. It does not replace them.

## Read Endpoints

### `GET /api/health`

Returns preflight checks and readiness gates:

```json
{
  "checks": [],
  "ready": {
    "review_daemon": true,
    "hotspots": true,
    "image_gate": true
  }
}
```

### `GET /api/articles`

Returns article summaries:

```json
[
  {
    "article_id": "hs_20260427_001-...",
    "title": "AI-native architecture ...",
    "current_state": "channel_pending_review",
    "publisher": "ChainStream",
    "published_url": null,
    "last_transition_at": "2026-04-27T10:00:00+00:00"
  }
]
```

Supports `?state=published`.

### `GET /api/article/{article_id}`

Returns full `metadata` and `gate_history`.

### `GET /api/bridge`

Machine-readable capability descriptor for agents. This is the best first call
for a framework that has never seen AgentFlow before.

Example:

```json
{
  "bridge_version": "1.0",
  "event_webhook_enabled": true,
  "command_endpoint_enabled": true,
  "dangerous_commands_enabled": false,
  "read_auth_env": "REVIEW_DASHBOARD_TOKEN",
  "command_auth_env": "AGENTFLOW_AGENT_BRIDGE_TOKEN",
  "commands": {
    "doctor": {
      "scope": "read",
      "description": "Run preflight health checks.",
      "dangerous": false,
      "timeout_seconds": 30
    }
  }
}
```

### `GET /api/bridge/schema`

Returns machine-readable v1 schemas for:

- bridge descriptor
- command request body
- outbound event envelope

Repository copy:

- `docs/integrations/AGENT_BRIDGE_V1.schema.json`

## Outbound Events

When `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` is set, AgentFlow emits normalized
events for:

- memory events (`source=memory`)
- gate transitions (`source=gate`)
- publish records (`source=publish`)
- command lifecycle events from the bridge (`source=api`)

Envelope:

```json
{
  "schema_version": 1,
  "event_id": "evt_1234abcd5678efgh",
  "occurred_at": "2026-04-27T10:00:00+00:00",
  "ingested_at": "2026-04-27T10:00:00+00:00",
  "source": "memory",
  "event_type": "article_created",
  "article_id": "hs_20260427_001-...",
  "hotspot_id": "hs_20260427_001",
  "payload": {
    "hotspot_id": "hs_20260427_001",
    "angle_index": 0,
    "target_series": "A",
    "auto_filled": true
  },
  "source_ref": {
    "store": "memory/events.jsonl"
  }
}
```

Important:

- delivery is best-effort
- the webhook should treat `event_id` as the idempotency key
- AgentFlow does not currently retry outbound bridge events

## Command Endpoint

`POST /api/commands` runs a whitelisted subset of `af` commands.

Auth:

```http
Authorization: Bearer <AGENTFLOW_AGENT_BRIDGE_TOKEN>
```

Request:

```json
{
  "request_id": "run-001",
  "command": "preview",
  "params": {
    "article_id": "hs_20260427_001-...",
    "platforms": "medium,ghost_wordpress"
  },
  "options": {
    "skip_images": true
  }
}
```

Success response:

```json
{
  "ok": true,
  "request_id": "run-001",
  "command": "preview",
  "scope": "pipeline",
  "returncode": 0,
  "data": {
    "article_id": "hs_20260427_001-..."
  },
  "stderr": null
}
```

Failure response:

```json
{
  "detail": {
    "ok": false,
    "request_id": "run-001",
    "command": "preview",
    "scope": "pipeline",
    "returncode": 1,
    "data": null,
    "stderr": "..."
  }
}
```

## Command Sets

Capability matrix:

| Command | Scope | Side effects | Default availability |
|---|---|---|---|
| `doctor` | `read` | none | enabled |
| `review_status` / `review_list` / `draft_show` / `memory_tail` / `intent_show` | `read` | none | enabled |
| `hotspots` / `write` / `fill` / `image_gate` / `preview` / `medium_package` / `review_post_d` | `pipeline` | local files, memory events, possible TG side effects | enabled |
| `publish` / `review_publish_mark` | `publish` | real external side effects possible | disabled by default |

Read scope:

- `doctor`
- `review_status`
- `review_list`
- `draft_show`
- `memory_tail`
- `intent_show`

Pipeline scope:

- `hotspots`
- `write`
- `fill`
- `image_gate`
- `preview`
- `medium_package`
- `review_post_d`

Publish scope:

- `publish`
- `review_publish_mark`

By default, publish-scope commands are blocked until
`AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`.

## Recommended Agent Loop

1. call `GET /api/bridge`
2. call `GET /api/health`
3. subscribe to outbound event webhook
4. when an event arrives, decide whether to:
   - fetch `GET /api/article/{article_id}`
   - call a pipeline command
   - wait for human review
5. watch for `agent.command.completed` or `agent.command.failed`

## Example Client

A minimal Python client lives at:

- `docs/integrations/examples/python_bridge_client.py`
- `docs/integrations/examples/bridge_event_listener.py`

It demonstrates:

- discovering bridge capabilities
- checking health
- calling a safe command (`doctor`)
- receiving outbound bridge events on a local HTTP listener

Run it with:

```bash
export AGENTFLOW_BASE_URL=http://127.0.0.1:7860
export AGENTFLOW_READ_TOKEN=read-token
export AGENTFLOW_WRITE_TOKEN=write-token
python docs/integrations/examples/python_bridge_client.py
```

For a local event sink:

```bash
export BRIDGE_LISTENER_OUTPUT=/tmp/agentflow-bridge-events.jsonl
python docs/integrations/examples/bridge_event_listener.py
```

## Safety Notes

- `publish` can trigger real external side effects.
- `review_publish_mark` marks a document as published and advances state.
- if you expose the bridge beyond localhost, set both tokens and terminate TLS
  at a trusted proxy.
- Telegram auth and bridge auth are separate systems.

## Compatibility Notes

- v1 assumes the project remains `skill-first + af CLI + local files`.
- v1 does **not** assume a database, async job runner, or multi-user host.
- external agents should treat `event_id` as the idempotency key.
- outbound events are best-effort; if you need guaranteed replay, read the local
  runtime files (`metadata.json`, `events.jsonl`, `publish_history.jsonl`) as
  the source of truth.

## Non-Goals

- no WebSocket or SSE event bus yet
- no multi-tenant agent session model yet
- no durable command queue yet

Use the bridge as a local automation layer, not a distributed control plane.
