# AgentFlow Open Claw Reference (v2)

## 1. Current Product Stage

The repo is past the v0.1 MVP skeleton. The review pipeline is now Lark-first through OpenClaw, with Telegram kept as mobile / fallback. The state machine remains the single source of truth, and most main-loop sprints have closed:

- S0.1 skill harness: review complete
- S0.2 env / config bootstrap: review complete
- S1 hotspot intake: review complete
- S2 draft review with round-limit: review complete
- S3 image gate: review complete
- S4 channel gate with preview: review complete
- S5 dispatch timeout/retry: review complete
- S6 Medium manual mark loop: review complete
- S7 `/list` extensions: partially implemented

The Lark bridge exposes 34 `lark_*` commands through the daemon-owned
`/api/commands`; write / spawn commands require
`AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`. In Lark-first mode,
`blogflow review-daemon` embeds the bridge on `127.0.0.1:7860` by default.
The TG fallback bot still exposes callback prefixes `A` / `B` / `C` / `D`,
plus `PD`, `I`, `L`, `PR`, `P`, and `S`.

**v1.1.8 — Lark @-mention parity with TG**: free-text @-bot messages now
route through the daemon via `lark_message`. The router classifies intent
deterministically (no LLM) and dispatches to the same handler the
corresponding button would fire. Pending edit slots take priority. Lark
gains per-action auth (env `LARK_OPERATOR_OPEN_ID` + allowlist file
`~/.agentflow/review/lark_auth.json`) parallel to TG's `_ACTION_REQ`.
Closure regressions fixed: `lark_gate_b_approve`, `lark_gate_c_approve`,
`lark_gate_c_skip` now spawn the next-gate card on a daemon thread
(parity with `daemon._route` lines 3414/3453/3473 — previously the Lark
loop stalled at draft_approved with no follow-up).

Do not describe this repo as "early skeleton" or use the old 5-state model unless the current code regresses to that.

## 2. Canonical Workflow

```text
cron `blogflow article-hotspots`
  -> Gate A (Lark card + TG fallback)
  -> lark_gate_a_write / A:write
  -> `blogflow write` + `blogflow fill`
  -> Gate B (Lark input box / @bot edit follow-up supported)
  -> lark_gate_b_approve / B:approve
  -> Lark image picker or CLI `blogflow image-gate`
  -> Gate C (image-review prompt can feed `--cover-description`)
  -> lark_gate_c_approve / C:approve or skip
  -> Gate D channel selection (Lark + TG fallback)
  -> Lark confirm runs full dispatch chain / TG fallback PD:dispatch
  -> ready_to_publish
  -> PR:mark or `blogflow review-publish-mark`
  -> published
```

Legitimate fallback edges:

- `D:cancel` or Gate D timeout returns the article to `image_approved`.
- `C:skip`, `blogflow image-gate --mode none`, and Gate C auto-skip route to `image_skipped`.
- `STATE_PUBLISHED -> STATE_CHANNEL_PENDING_REVIEW` is the incremental republish edge.

## 3. Runtime Artifacts

Use this source-of-truth map:

| Artifact | Path | Purpose |
|---|---|---|
| style profile | `~/.agentflow/style_profile.yaml` | D0 style baseline |
| style corpus | `~/.agentflow/style_corpus/` | style-learning inputs |
| hotspots | `~/.agentflow/hotspots/YYYY-MM-DD.json` | D1 hotspot output |
| draft markdown | `~/.agentflow/drafts/<article_id>/draft.md` | article body |
| draft metadata | `~/.agentflow/drafts/<article_id>/metadata.json` | single-article metadata |
| platform versions | `~/.agentflow/drafts/<article_id>/platform_versions/*.md` | D3 outputs |
| publish history | `~/.agentflow/publish_history.jsonl` | D4 publish log |
| memory events | `~/.agentflow/memory/events.jsonl` | cross-article behavior |
| daemon heartbeat | `~/.agentflow/review/last_heartbeat.json` | daemon liveness probe |
| short-id index | `~/.agentflow/review/short_id_index.json` | sid to article map and callback extras |
| timeout state | `~/.agentflow/review/timeout_state.json` | per-gate clocks |
| audit log | `~/.agentflow/review/audit.jsonl` | append-only audit |
| pending edits | `~/.agentflow/review/pending_edits.json` | active TG edit sessions |
| Lark pending edits | `~/.agentflow/memory/events.jsonl` (`lark_edit_pending`, `lark_locked_edit_pending`, `lark_pending_edit_consumed`) | Lark @bot follow-up slots; consumed once |
| auth grants | `~/.agentflow/review/auth.json` | per-uid action grants |
| review config | `~/.agentflow/review/config.json` | TG chat id and knobs |

## 4. State Boundary Rules

- `metadata.json` is for one article's state.
- `events.jsonl` is for cross-article behavior and preference learning.
- `draft.md` is rendered content, not a state ledger.
- `publish_history.jsonl` is a publish log, not decision memory.
- `~/.agentflow/review/*.json` is daemon-owned; do not mirror its fields into `metadata.json`.

## 5. Current State Machine

Current `STATE_*` set in `backend/agentflow/agent_review/state.py`:

1. `topic_pool`
2. `topic_approved`
3. `topic_rejected`
4. `drafting`
5. `draft_pending_review`
6. `draft_approved`
7. `draft_rejected`
8. `drafting_locked_human`
9. `image_pending_review`
10. `image_approved`
11. `image_skipped`
12. `channel_pending_review`
13. `ready_to_publish`
14. `published`

## 6. Image Gate Expectations

The current image path must preserve these edges:

- `B:approve` sends an image picker prompt and leaves state at `draft_approved`.
- `lark_gate_b_edit` can apply inline input (`comment` / `prompt` / `text`) immediately via `blogflow edit --post-review`; without input it registers a pending Lark edit slot.
- `lark_apply_pending_edit` consumes the latest pending Lark edit slot once, then runs `blogflow edit --post-review`.
- `blogflow image-gate <aid> --mode cover-only|cover-plus-body` generates image assets and posts Gate C.
- `lark_gate_c_regen` can pass review text into `blogflow image-gate --cover-description`.
- `blogflow image-gate <aid> --mode none` transitions to `image_skipped` and immediately calls `triggers.post_gate_d(aid)`.
- `C:approve` transitions to `image_approved` and spawns Gate D.
- `C:skip` transitions to `image_skipped` and spawns Gate D.

The test `test_image_gate_none_transitions_to_image_skipped_and_posts_gate_d` is the narrow regression guard for the CLI none path.

## 7. API-Key Matrix

Required in real-key mode:

- `MOONSHOT_API_KEY`
- `JINA_API_KEY`
- `ATLASCLOUD_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_REVIEW_CHAT_ID`
- `AGENTFLOW_AGENT_BRIDGE_TOKEN` for Lark/OpenClaw command bridge
- `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` for Lark/OpenClaw event rendering

Optional or on-demand:

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GHOST_ADMIN_API_URL` + `GHOST_ADMIN_API_KEY`
- `LINKEDIN_ACCESS_TOKEN` + `LINKEDIN_PERSON_URN`
- `TWITTER_*`
- `WEBHOOK_PUBLISH_URL` + `WEBHOOK_AUTH_HEADER` + `WEBHOOK_FORMAT`
- `RESEND_API_KEY` + `NEWSLETTER_*`

Medium is manual-mark only through `PR:mark` or `blogflow review-publish-mark`.

## 8. Verification Ladder

Use the lightest sufficient verification, but keep the loop closed.

```bash
cd backend && .venv/bin/python -m pytest tests/test_v02_workflows.py -q
.venv/bin/blogflow doctor
```

Do not run frontend build as the default ladder; the frontend is legacy.

## 9. Review Priorities

Bias reviews toward:

- auto-draft flow regressing into skeleton-first
- state transitions overwriting each other across Gate B/C/D
- memory logging leaking into per-article storage
- API contracts changing without daemon or review-store updates
- mock success overstated as production readiness
- `published_url` schema compatibility

## 10. Finish Condition

"Open claw complete" means:

- current state identified
- decision made against repo constraints
- action taken or a precise no-change conclusion given
- verification reported
- next step or blocker named
