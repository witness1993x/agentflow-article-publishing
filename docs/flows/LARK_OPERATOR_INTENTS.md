# Lark Operator Intents (Chrome Slash Equivalents)

This file documents the operator-facing intents the BlogFlow daemon
recognizes from `lark_message` free-text @-bot input — the Lark counterpart
to TG slash commands. AgentFlow daemon parses each free-text @-bot turn
through `lark_callback._route_message_intent`, which dispatches into one of
12 chrome handlers documented below. OpenClaw renders the daemon's response
card as-is.

Implemented by `backend/agentflow/agent_review/lark_callback.py` (handlers
+ `_classify_chrome_intent`) and `backend/agentflow/agent_review/triggers.py`
(card emit helpers).

Plan ref: `docs/BLOGFLOW_TG_TO_LARK_PARITY.md` §3.4 GAP-CHROME.

## Quick reference

| @-bot text (中) | @-bot text (en) | TG slash | Action | Mutates? |
|---|---|---|---|---|
| `状态` | `status` | `/status` | Daemon health card | No |
| `列表` / `在审` | `list` / `in review` | `/list` | List articles in any pending_review | No |
| `已发` | `published` | `/published` | List last 20 published | No |
| `扫一下` / `扫扫` / `找选题` / `热点` | `scan` | `/scan` | Trigger article-hotspots scan | Yes (spawns) |
| `任务` | `jobs` / `in-flight` | `/jobs` | List in-flight subprocesses / cron | No |
| `审计列表` | `audit list` | `/audit` (no-id) | Tail recent audit.jsonl entries | No |
| `鉴权` | `auth` / `auth-debug` | `/auth-debug` | Show operator auth + action table | No |
| `建议` / `改进建议` | `suggestions` | `/suggestions` | Re-emit suggestion list card | No |
| `跳过 <id>` | `skip <id>` | `/skip <id>` | Skip article's image gate | Yes |
| `推迟 <id> <h>` | `defer <id> <h>` | `/defer <id> <h>` | Defer article gate by N hours | Yes |
| `标记已发 <id>` | `publish-mark <id>` | `/publish-mark <id>` | Mark article published | Yes |
| `取消 <id>` | `cancel <id>` | `/cancel <id>` | Set article to draft_rejected | Yes |

## Determinism contract

Intent classification is **keyword-driven**, not LLM-driven. The
`_classify_chrome_intent` matcher uses two distinct contracts:

1. **Read-only intents** (status / list / published / scan / jobs /
   audit_list / auth_debug / suggestions): whole-text exact equality
   match against the chrome keyword set, after `_normalize_text` has
   stripped @-mentions and URLs. Substring CJK matches are **explicitly
   rejected** so a phrase like `推进到状态 X` cannot trip `状态`. See
   CHANGELOG v1.1.8 for the false-positive lock that triggered this rule.
2. **Verb intents** (skip / defer / publish_mark / cancel): anchored
   regex match (`^...$`) on the normalized text, so `跳过 abc` works
   but `请帮我跳过 abc` does not.

The chrome dispatch runs **before** the legacy `_classify_intent`
matcher, but its whole-text contract guarantees that any phrase that
isn't an exact chrome keyword falls through to the legacy classifier
unchanged. In particular:

- The existing `推进 / advance` intent (legacy `_advance` branch) still
  beats every chrome keyword, because the chrome whole-text contract
  doesn't match `推进到下个 gate` against any chrome keyword.
- Bare `审计` still routes to the existing `gate_b_diff` intent (per-article
  audit) — only `审计列表` triggers the no-id chrome `audit_list` mode.

## Authorization (fail-closed)

Every chrome handler runs through `_authorize_or_deny_v2`, which uses
`auth.is_authorized_open_id` (the **fail-closed** v2 path):

- An empty / missing `lark_operators` section in
  `~/.agentflow/review/auth.json` denies every chrome action — even for
  read-only intents — so a fresh deployment that hasn't onboarded any
  operator cannot fire arbitrary commands.
- The required action verb is taken from
  `_LARK_ACTION_REQ[chrome_<intent>]`. Read-only intents need `review`;
  the four mutating intents (skip / defer / publish_mark / cancel) need
  `edit`.

The deny response is the standard `_deny_card` (`template="red"`) the
operator sees on any other unauthorized Lark callback.

## Per-intent

### `chrome_status` / `状态`

Reads `~/.agentflow/review/last_heartbeat.json` for daemon liveness, plus
the tail of `~/.agentflow/review/audit.jsonl` for recent events.
Pending-review count comes from `review_state.articles_in_state` over
the four pending-review states (`draft_pending_review`,
`image_pending_review`, `channel_pending_review`,
`drafting_locked_human`). Emits `review.status_card`.

### `chrome_list` / `列表` / `在审`

Walks `~/.agentflow/drafts/*/metadata.json` once, filters by
`current_state contains "_pending_review"`, sorts by most-recent
gate_history timestamp. Emits `review.article_list_card` with `article_id`,
`title`, `current_state`, `last_ts` per row.

### `chrome_published` / `已发`

Same drafts walk, filters by `current_state == "published"`, sorts by
`published_at`, caps at 20 rows. Emits `review.published_list_card` with
`article_id`, `title`, `published_at`, `published_url`, `platforms`.

### `chrome_scan` / `扫一下` / `找选题` / `热点`

Spawns `article-hotspots` (top_k=5). Prefers `daemon._spawn_hotspots`
(threaded subprocess + spawn-failure notification). Falls back to
`_spawn_async` direct subprocess. Emits `review.scan_kicked_card`
ack — actual Gate A cards arrive separately when the hotspots run completes
(existing behaviour). Failure path: red error card.

### `chrome_jobs` / `任务`

Mirrors TG's `/jobs`: shells out to `blogflow review-cron-status` (launchd
on macOS) and surfaces installed schedule entries. Falls back to "no
in-flight jobs" when the CLI is unavailable. Emits `review.jobs_card`.

### `chrome_audit_list` / `审计列表`

Stub form here; full implementation will be unified by **Wave 2 step 4
(GAP-AUDIT-LIST)**. Reads the last 20 entries from
`~/.agentflow/review/audit.jsonl` and emits `review.audit_list_card` with
the raw JSON entries. The card render contract is owned by GAP-AUDIT-LIST;
chrome only feeds the data.

### `chrome_auth_debug` / `鉴权` / `auth`

Reports the operator's `open_id`, whether they're in the
`lark_operators` whitelist, what actions they can perform, and a
summary of the `_LARK_ACTION_REQ` table. Emits
`review.auth_debug_card`. Useful when an operator gets unexpected
deny cards — they can `@bot 鉴权` and see exactly what they're authorized
to do.

### `chrome_suggestions` / `建议` / `改进建议`

Re-emits the GAP-S suggestions list card. Reuses
`triggers._emit_lark_suggestion_list_card` so OpenClaw's renderer doesn't
need to handle two list shapes — chrome simply re-fires the same event
the GAP-S deep-links use.

### `chrome_skip <id>` / `跳过 <id>` / `skip <id>`

Resolves `<id>` either as a full article_id (matching
`drafts/<id>/metadata.json`) or as a `short_id` via
`agentflow.agent_review.short_id.resolve`. Validates that the article
is currently in `image_pending_review`, then calls
`state.transition(force=True, decision="chrome_skip_via_lark")` to move
it to `image_skipped`. The next gate (Gate D channel-review) is
**not** auto-spawned from chrome — operators are expected to push the
flow forward via the standard Gate D card or another chrome intent.

### `chrome_defer <id> <h>` / `推迟 <id> <h>` / `defer <id> <h>`

Re-uses the daemon's `_handle_defer` ack contract: emits the
`gate_X 已延后` card and writes a `lark_chrome_defer` audit memory
event. The actual repost scheduling is **not** wired here in v1
(unlike TG's `_schedule_deferred_repost`); operators still get the ack,
and the card surface knows nothing was lost. Re-firing the gate card
later remains a manual operation. Tracked separately for Wave 3.

### `chrome_publish_mark <id>` / `标记已发 <id>` / `publish-mark <id>`

Calls `state.transition(force=True, to_state="published",
decision="chrome_publish_mark_via_lark")`. Does NOT itself record a
URL — the URL-recording flow is the per-platform
`mark_published(article_id, url=...)` invoked from Gate D buttons.
Chrome `publish_mark` is a **terminal-state hammer** for cases where the
operator already published manually and just wants to clear the gate.

### `chrome_cancel <id>` / `取消 <id>` / `cancel <id>`

Calls `state.transition(force=True, to_state="draft_rejected",
decision="chrome_cancel_via_lark")`. The choice of `draft_rejected` is
deliberate — it matches the TG sad-path table entry for "operator gives
up on the article entirely" and is one of the two terminal states in the
state machine. Cancelling from a published article is permitted (force=True
overrides the state-machine check), but operators rarely want to
self-rollback published articles via chrome.

## Wire-up

1. **Free-text @-bot path**: `lark_message` →
   `lark_callback._route_message_intent` → `_classify_chrome_intent` →
   `_CHROME_HANDLERS[intent]`.
2. **Programmatic command path** (slash menus, tests, OpenClaw native UI):
   `POST /api/commands` with `command="lark_chrome_<intent>"` → web.py
   `_run_lark_command_in_process` chrome dispatch → same
   `_CHROME_HANDLERS` table.

The 12 `lark_chrome_*` commands are registered in
`agent_review/web.py::_COMMAND_SPECS` with `in_process=True` and the
mutating four flagged `dangerous=True` (so `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`
is required to invoke them programmatically).

## Authorization table summary

| Chrome action token | Required verb |
|---|---|
| `chrome_status` | `review` |
| `chrome_list` | `review` |
| `chrome_published` | `review` |
| `chrome_scan` | `review` |
| `chrome_jobs` | `review` |
| `chrome_audit_list` | `review` |
| `chrome_auth_debug` | `review` |
| `chrome_suggestions` | `review` |
| `chrome_skip` | `edit` |
| `chrome_defer` | `edit` |
| `chrome_publish_mark` | `edit` |
| `chrome_cancel` | `edit` |

## Tests

`backend/tests/test_lark_chrome_intents.py` covers each of the 12 intents
with:
- A happy-path test (text match → handler called → card emitted).
- A false-positive guard (similar text → NOT triggered).

Plus four cross-cutting tests:
- `ChromeAuthFailClosedTests` — fail-closed deny when no operator entry.
- `ChromeDoesNotShadowAdvanceTests` — `推进到下个 gate` still beats chrome.
- `ChromeMutateAuditTests` — skip / publish_mark / cancel each write a
  `gate_history` entry tagged with the chrome decision label.
- `ChromeWebCommandsTests` — all 12 `lark_chrome_*` commands registered;
  mutating ones flagged `dangerous=True`; read-only ones flagged
  `dangerous=False`.
