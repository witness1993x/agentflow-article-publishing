# Lark Bot Adapter Service

A standalone FastAPI service that terminates Feishu/Lark open-platform webhooks
(events + interactive-card actions), verifies their HMAC-SHA256 signature,
optionally decrypts AES-CBC payloads, and dispatches the decoded event into
AgentFlow's `agent_review.lark_callback.handle_event(...)`. Treat it as the
Lark counterpart to the existing Telegram review pipeline — same review
state machine, different transport.

## Required environment variables

| Variable                  | Required | Purpose                                                        |
| ------------------------- | :------: | -------------------------------------------------------------- |
| `LARK_APP_ID`             |    yes   | Lark/Feishu custom app identifier (`cli_xxx`).                 |
| `LARK_APP_SECRET`         |    yes   | Custom-app secret used to mint `tenant_access_token`.          |
| `LARK_VERIFICATION_TOKEN` |    yes   | Token configured in Lark dev console; used for HMAC verify.    |
| `LARK_ENCRYPT_KEY`        |    no    | If set, payloads arrive AES-CBC encrypted and we decrypt them. |
| `LARK_ADAPTER_BIND`       |    no    | `host:port`, default `0.0.0.0:8765`.                           |
| `LARK_ADAPTER_LOG_LEVEL`  |    no    | `DEBUG`/`INFO`/`WARNING`, default `INFO`.                      |

Place these in `~/.agentflow/secrets/.env.lark` (file mode `0600`) and source
the file before starting the service. Never commit them.

## Local development

```bash
# from the lark-adapter/ directory
python -m uvicorn lark_adapter.app:create_app --factory --port 8765
```

The `--factory` flag is required so uvicorn calls `create_app()` rather than
treating `app` as a module-level instance.

Run the test suite:

```bash
python -m pytest tests -q
```

## Production deployment

- Run uvicorn (or gunicorn with uvicorn workers) behind a reverse proxy
  (nginx, Caddy, or Cloudflare) that terminates TLS. Lark only delivers
  events to HTTPS endpoints.
- If the deploy host has no public IP, expose the adapter through a
  Cloudflare Tunnel or FRP tunnel rather than poking holes in your firewall.
- The service depends on `agent_review.lark_callback` being importable from
  the same Python environment in production. That module ships in the
  v1.1.0 PR (the parallel "C agent" deliverable) — link it from this repo
  once merged. Until then `callback_bridge` returns the
  `agentflow_not_installed` stub response so the adapter is observable but
  inert.

## curl recipes

### 1. Challenge handshake

Lark issues an unencrypted `url_verification` POST when you save the webhook
URL. Replace `VERIFICATION_TOKEN` with your real token.

```bash
TOKEN=VERIFICATION_TOKEN
TS=$(date +%s)
NONCE=test-nonce
BODY='{"type":"url_verification","challenge":"abc123","token":"'$TOKEN'"}'
SIG=$(printf '%s%s%s' "$TS" "$NONCE" "$BODY" | \
        openssl dgst -sha256 -hmac "$TOKEN" -hex | awk '{print $2}')

curl -sS -X POST http://127.0.0.1:8765/lark/event \
  -H "Content-Type: application/json" \
  -H "X-Lark-Signature: $SIG" \
  -H "X-Lark-Request-Timestamp: $TS" \
  -H "X-Lark-Request-Nonce: $NONCE" \
  --data "$BODY"
# -> {"challenge":"abc123"}
```

### 2. Mocked card-action POST

```bash
TOKEN=VERIFICATION_TOKEN
TS=$(date +%s)
NONCE=card-nonce
BODY='{"open_id":"ou_demo","user_name":"Demo","chat_id":"oc_demo",
       "action":{"tag":"button",
                 "value":{"article_id":"a-42","action":"approve_b","v":1}}}'
SIG=$(printf '%s%s%s' "$TS" "$NONCE" "$BODY" | \
        openssl dgst -sha256 -hmac "$TOKEN" -hex | awk '{print $2}')

curl -sS -X POST http://127.0.0.1:8765/lark/card \
  -H "Content-Type: application/json" \
  -H "X-Lark-Signature: $SIG" \
  -H "X-Lark-Request-Timestamp: $TS" \
  -H "X-Lark-Request-Nonce: $NONCE" \
  --data "$BODY"
# Without agent_review installed -> {} (stub ack with no reply payload)
```

### 3. Health probe

```bash
curl -sS http://127.0.0.1:8765/healthz
# -> {"ok":true,"lark_app_id_present":true,"callback_bridge_loaded":false}
```

## Card action vocabulary

These strings are the only values the bridge will accept in a card-action
payload's `action` field. They must stay in lockstep with whatever the
`agent_review.lark_callback` implementation accepts on the other side.

| Action       | Meaning                                                     |
| ------------ | ----------------------------------------------------------- |
| `approve_b`  | Gate B approve.                                             |
| `reject_b`   | Gate B reject (returns the article to drafting).            |
| `refill`     | Trigger D2 refill subprocess (mirrors Telegram `I:refill`). |
| `takeover`   | Manual takeover by reviewer.                                |
| `view_audit` | Fetch this article's `d2_structure_audit` memory event.     |
| `view_meta`  | Return the article metadata snapshot.                       |

## Layout

```
lark-adapter/
  pyproject.toml
  lark_adapter/
    app.py             # FastAPI factory
    config.py          # env + Settings dataclass
    security.py        # HMAC verify + AES-CBC decrypt
    routes_event.py    # POST /lark/event
    routes_card.py     # POST /lark/card
    routes_health.py   # GET /healthz
    lark_client.py     # tenant_access_token cache + send_card / send_text
    card_meta.py       # encode/decode article_id+action in card payloads
    callback_bridge.py # imports agent_review.lark_callback when available
  tests/
    test_security.py
    test_routes.py
```
