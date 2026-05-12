# CC Handoff: Lark-First Review Loop v1.1.7

Updated: 2026-05-07

This handoff summarizes the latest Cursor-side iteration for
`agentflow-article-publishing`, focused on closing the Lark-first review loop,
daemon-owned bridge behavior, command naming, docs/reference sync, and package
artifacts.

## 1. Current Version And Artifacts

Current runtime version: `v1.1.7`

Canonical version source:

- `backend/pyproject.toml`

Latest local artifacts on Desktop:

- `/Users/witness/Desktop/blogflow-article-publishing-v1.1.7.zip`
- `/Users/witness/Desktop/blogflow-deploy-v1.1.7.tar.gz`

Artifact verification:

- source zip sha256:
  `ae27779f4297bd96a44605b84bc885d642a8ea30160e7397254de47c5bf0ee10`
- deploy tar sha256:
  `f13c52fbf3216660e8e36332cc52fdb2fb7af890c87a765cdc7650cc3921c80a`
- forbidden entries: `0`
- verified included:
  - `version = "1.1.7"`
  - `docs/flows/LARK_FIRST_REVIEW_FLOWS.md`
  - OpenClaw skill reference `v2.8`
  - daemon embedded bridge implementation
  - Lark review card template

Verification run:

```bash
"backend/.venv/bin/python" -m pytest backend/tests -q
```

Result:

```text
197 passed, 1 warning
```

The warning is the existing mock-publisher compatibility deprecation warning.

## 2. High-Level Outcome

The local runtime is now designed as Lark-first:

```text
AgentFlow review-daemon
  -> AGENTFLOW_AGENT_EVENT_WEBHOOK_URL (OpenClaw event listener)
  -> OpenClaw renders review.*_card
  -> Lark interactive card
  -> OpenClaw card callback
  -> blogflow review-daemon embedded bridge /api/commands
  -> lark_* command handler
```

Important directionality:

- `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` is outbound from AgentFlow to OpenClaw.
- Lark button callbacks are inbound from OpenClaw to AgentFlow's daemon-owned
  `/api/commands`.
- `blogflow review-dashboard` is now only a standalone debug/read API runner,
  not the primary callback process.

## 3. CLI And Naming Changes

Primary CLI:

- `blogflow`

Equivalent alias:

- `mediaflow`

Legacy only:

- `af`

Article hotspot terminology:

- recommended: `blogflow article-hotspots`
- recommended: `blogflow article-hotspot-show`
- old `hotspots` / `hotspot-show` remain hidden legacy aliases

Deployment naming:

- systemd service: `blogflow-review`
- service file: `agentflow-deploy/blogflow-review.service`
- install prefix default: `/opt/blogflow`
- schedule envs: `BLOGFLOW_ARTICLE_HOTSPOTS_SCHEDULE*`

## 4. Main Runtime Changes

### Daemon owns the bridge

File:

- `backend/agentflow/agent_review/daemon.py`

Key behavior:

- `blogflow review-daemon` starts the embedded agent bridge API when
  `AGENTFLOW_LARK_APP_PRIMARY=true`.
- Default bridge URL:
  `http://127.0.0.1:7860/api/commands`
- Host/port envs:
  - `AGENTFLOW_REVIEW_BRIDGE_HOST`
  - `AGENTFLOW_REVIEW_BRIDGE_PORT`
- Lark-first daemon can run without `TELEGRAM_BOT_TOKEN`.
- Without Telegram, daemon still:
  - writes heartbeat
  - runs timeout sweeps
  - drains deferred reposts
  - fires scheduled article-hotspots scans

### Bridge command surface

File:

- `backend/agentflow/agent_review/web.py`

Current Lark command count:

- 33 `lark_*` commands

Important behavior:

- `POST /api/commands` remains protected by
  `AGENTFLOW_AGENT_BRIDGE_TOKEN`.
- dangerous/spawn commands require:
  `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`
- `event_envelope_schema.source` includes `agentflow.review`.

### Lark callback handlers

File:

- `backend/agentflow/agent_review/lark_callback.py`

Important covered actions:

- Gate A write / expand / reject all
- Gate B approve / edit / rewrite / refill / reject / diff / meta
- Lark pending edit one-shot consumption
- image picker cover-only / cover-plus-body / skip
- Gate C approve / skip / regen / relogo / full
- Gate D toggle / select all / save default / confirm / cancel / extend
- locked takeover critique / edit / give up

Gate D confirm now calls the full dispatch chain through
`review_triggers.post_publish_dispatch(...)`; it does not spawn a bare
`blogflow publish`.

## 5. Lark Review Cards And Events

Primary rendering contract:

- `backend/agentflow/agent_review/templates/lark_review_cards.md`

OpenClaw must treat:

- `review.*_card` as interactive review cards
- `notify.*` as broadcast/status cards only

Do not treat `notify.hotspots_digest` as Gate A.

Review events covered:

- `review.gate_a_card`
- `review.profile_setup_card`
- `review.gate_b_card`
- `review.image_gate_picker_card`
- `review.gate_c_card`
- `review.gate_d_card`
- `review.locked_takeover_card`

Input aliases documented and expected:

- `payload.comment`
- `payload.edit_text`
- `payload.prompt`
- `payload.cover_description`
- `payload.feedback`
- `payload.text`
- `payload.answer`

## 6. Scenario Closure Matrix

Canonical flow doc:

- `docs/flows/LARK_FIRST_REVIEW_FLOWS.md`

This is now the main topology and closure reference.

Summary:

| Scenario | Status |
|---|---|
| Gate A topic review | Closed through `review.gate_a_card` and `lark_gate_a_write` |
| Profile setup | Closed through `review.profile_setup_card` and input payload |
| Gate B draft review | Closed with approve/edit/rewrite/refill/reject/diff/meta |
| Gate B input box | Closed with inline payload and one-shot pending edit fallback |
| Image picker | Closed with cover-only / cover-plus-body / skip |
| Gate C image review | Closed with approve/skip/regen/relogo/full and prompt aliases |
| Gate D channel selection | Closed with toggle/select all/save default/confirm/cancel/extend |
| Gate D publish dispatch | Closed through full dispatch chain |
| Locked takeover | Closed with critique/edit/give up |
| Legacy digest confusion | Closed in docs and legacy Custom Bot copy |

Telegram is now fallback/mobile:

- `docs/flows/TG_BOT_FLOWS.md` was downgraded to TG fallback and daemon `_route`
  internals reference.

## 7. Docs And References Updated

Main docs:

- `README.md`
- `CHANGELOG.md`
- `INSTALL.md`
- `agentflow-deploy/INSTALL_LINUX.md`
- `docs/integrations/AGENT_BRIDGE.md`
- `docs/openclaw_plugin_integration.md`
- `docs/flows/LARK_FIRST_REVIEW_FLOWS.md`
- `docs/flows/TG_BOT_FLOWS.md`
- `docs/flows/USER_SCENARIOS.md`

OpenClaw skill reference:

- `.cursor/skills/agentflow-open-claw-v2/SKILL.md`
- `.cursor/skills/agentflow-open-claw-v2/references/reference.md`
- `.cursor/skills/agentflow-open-claw-v2/references/package.md`
- `.cursor/skills/agentflow-open-claw-v2/references/template.md`

OpenClaw skill version marker:

- `AgentFlow Open Claw v2.8`

Claude skills were partially synced to the `blogflow` command surface:

- `.claude/skills/README.md`
- `.claude/skills/agentflow/SKILL.md`
- `.claude/skills/agentflow-style/SKILL.md`
- `.claude/skills/agentflow-write/SKILL.md`
- `.claude/skills/agentflow-publish/SKILL.md`
- `.claude/skills/agentflow-tweet/SKILL.md`
- `.claude/skills/agentflow-newsletter/SKILL.md`
- `.claude/skills/agentflow-hotspots/SKILL.md`

Some skill trigger metadata intentionally still includes old `af ...` phrases as
legacy trigger aliases, but examples and instructions should prefer `blogflow`.

## 8. Configuration Required For Deployment

AgentFlow side:

```dotenv
AGENTFLOW_LARK_APP_PRIMARY=true
AGENTFLOW_AGENT_EVENT_WEBHOOK_URL=<OpenClaw event listener URL>
AGENTFLOW_AGENT_EVENT_AUTH_HEADER=<optional auth header>
AGENTFLOW_AGENT_BRIDGE_TOKEN=<shared write token>
AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true
AGENTFLOW_REVIEW_BRIDGE_HOST=127.0.0.1
AGENTFLOW_REVIEW_BRIDGE_PORT=7860
```

OpenClaw side:

```text
Event listener:
  receives AgentFlow review.*_card / notify.* events

Card callback target:
  http://127.0.0.1:7860/api/commands

Auth:
  Authorization: Bearer <AGENTFLOW_AGENT_BRIDGE_TOKEN>
```

If OpenClaw and AgentFlow are not on the same machine, replace `127.0.0.1` with
a reachable daemon address or put a tunnel/proxy in front of the daemon bridge.

## 9. How To Detect Old Or Broken Deployment

If Lark still shows any of these, the live deployment is probably on a stale
package, stale OpenClaw renderer, or legacy Custom Bot path:

- "Gate A 卡已推送到 Telegram"
- "去 TG 审核"
- "legacy Custom Bot 扫描摘要"
- "需要启动 review-dashboard 才能收按钮"
- OpenClaw routes callbacks to `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL`

Correct behavior:

- `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` points to OpenClaw.
- OpenClaw callback points to daemon `/api/commands`.
- `blogflow review-daemon` is the single review runtime process in Lark-first
  mode.

## 10. Tests Added Or Updated

Main test files:

- `backend/tests/test_v02_workflows.py`
- `backend/tests/test_lark_callback.py`

Coverage added/confirmed:

- Lark review card template includes all `review.*_card` events, buttons, and
  input aliases.
- Lark-first preflight detects legacy path and missing event webhook.
- daemon embedded bridge is enabled by Lark primary mode.
- daemon embedded bridge can be enabled explicitly.
- daemon starts bridge thread.
- daemon can run in Lark-primary mode without Telegram token.
- daemon fails clearly when neither Telegram nor Lark review surface is set.
- docs and OpenClaw skill reference point to daemon-owned bridge and
  Lark-first flow.
- image picker cover-only / cover-plus-body / skip.
- Gate D confirm triggers full dispatch chain.

## 11. Suggested CC Next Steps

1. Deploy `/Users/witness/Desktop/blogflow-deploy-v1.1.7.tar.gz`.
2. Restart `blogflow-review`.
3. Confirm runtime version:

```bash
blogflow --version
```

4. Run:

```bash
blogflow doctor --fresh
```

5. Confirm bridge:

```bash
curl -H "Authorization: Bearer $REVIEW_DASHBOARD_TOKEN" \
  http://127.0.0.1:7860/api/bridge
```

6. Trigger a Gate A or Gate B card and verify:

- AgentFlow emits `review.*_card`.
- OpenClaw renders an interactive Lark card.
- Button callback reaches `/api/commands`.
- dangerous commands are not blocked by `403`.
- the next Gate is posted after the command completes.

7. If still seeing old card copy, inspect:

- deployed package version
- active systemd service binary path
- daemon env
- OpenClaw callback target
- stale Custom Bot `LARK_WEBHOOK_URL`

## 12. Residual Risk

The code path is locally closed and tested. Remaining risk is deployment and
OpenClaw runtime configuration:

- wrong package deployed
- old service still running
- OpenClaw event listener and callback target reversed
- dangerous bridge commands disabled
- legacy Custom Bot still posting digest cards
- cross-machine callback using `127.0.0.1` incorrectly

No further local code blocker is currently known for the Lark-first review loop.
