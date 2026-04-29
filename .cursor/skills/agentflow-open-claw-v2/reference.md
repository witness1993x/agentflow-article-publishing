# AgentFlow Open Claw Reference (v2)

## 1. Current Product Stage

The repo is past the v0.1 MVP skeleton. The TG-driven review pipeline is the canonical path and most main-loop sprints have closed:

- S0.1 (skill harness) — review complete
- S0.2 (env / config bootstrap) — review complete
- S1 (起稿 / hotspot intake) — review complete
- S2 (草稿审，round-limit) — review complete
- S3 (封面，4-button image gate) — review complete
- S4 (渠道，6-row keyboard + Preview) — review complete
- S5 (dispatch 防爆 / adaptive timeout) — review complete
- S6 (Medium 闭环) — review complete
- S7 (`/list` 扩展) — partially implemented

The TG bot now exposes a full callback prefix family: `A` / `B` / `C` / `D` (the four main gates), `PD` (preview confirm), `I` (image-gate picker), `L` (locked takeover), `PR` (publish-mark), and `P` / `S` (profile / suggestions).

Cross-cutting behaviors that already shipped:

- Soft-revoke with a 60s grace window so the operator stops seeing "已失效" misfires on legitimate clicks.
- Per-action authorization: the `_ACTION_REQ` table in `daemon.py` gates 5 action families — `review`, `edit`, `image`, `publish`, `write` (plus the catch-all `*`) — against `~/.agentflow/review/auth.json`.

Do not describe this repo as "early skeleton" or "v0.1 MVP" anymore unless current code regresses to that.

## 2. Canonical Workflow

Treat this as the current default flow:

```
cron `af hotspots`
   ↓
Gate A  (TG: ✅ write / 🚫 reject_all / ⏭ skip)
   ↓ A:write
spawn `af write` + `af fill`
   ↓
Gate B  (4 buttons; round-limit max 2; round 3 → Manual Takeover / drafting_locked_human)
   ↓ B:approve
operator runs `af image-gate` manually
   ↓
Gate C  (4 buttons: ✅ approve / 🔁 regenerate / 🎨 restyle / 🚫 skip)
   ↓ C:approve
spawn Gate D
   ↓
Gate D  channel keyboard (6 rows + ✅全选 / 🚫全清 / 💾保存默认 / 👁 Preview)
   ↓ Preview → PD:confirm
PD:dispatch
   ↓ adaptive timeout / ETA echo / 全成功标语
Medium leg → post_publish_ready
   ↓ PR:mark (TG button) OR `af mark-published` (CLI)
STATE_PUBLISHED
```

Fallback edges (legitimate but non-default):

- D:cancel or D 12h timeout returns the article to `image_approved`.
- C:skip / `--mode none` / 12h auto-skip routes to `image_skipped`.
- B reject is terminal; A reject_all is terminal at the batch level.

## 3. Runtime Artifacts

Use this as the source-of-truth map. The new review-state stores in `~/.agentflow/review/` are first-class — do not assume `metadata.json` alone.

| Artifact | Path | Purpose |
|---|---|---|
| style profile | `~/.agentflow/style_profile.yaml` | D0 style baseline |
| style corpus | `~/.agentflow/style_corpus/` | style-learning inputs |
| hotspots | `~/.agentflow/hotspots/YYYY-MM-DD.json` | D1 hotspot output (batch unit) |
| hotspot reviews | `~/.agentflow/hotspots/reviews/*.json` | save / skip / approve actions |
| draft markdown | `~/.agentflow/drafts/<article_id>/draft.md` | article body |
| draft metadata | `~/.agentflow/drafts/<article_id>/metadata.json` | single-article state |
| platform versions | `~/.agentflow/drafts/<article_id>/platform_versions/*.md` | D3 outputs |
| publish history | `~/.agentflow/publish_history.jsonl` | D4 publish log |
| memory events | `~/.agentflow/memory/events.jsonl` | cross-article behavior |
| daemon heartbeat | `~/.agentflow/review/last_heartbeat.json` | NEW; daemon liveness probe |
| short-id index | `~/.agentflow/review/short_id_index.json` | sid → article map; carries `selected` / `failed` extras and `revoked_at` for soft-revoke |
| timeout state | `~/.agentflow/review/timeout_state.json` | per-gate clocks plus `__digest__` 24h cooldown |
| audit log | `~/.agentflow/review/audit.jsonl` | append-only audit (kind, sid, gate, payload) |
| pending edits | `~/.agentflow/review/pending_edits.json` | active TG edit sessions; `gate` field is `B`, `L`, or `PR` |
| auth grants | `~/.agentflow/review/auth.json` | per-uid action grants |
| review config | `~/.agentflow/review/config.json` | TG `chat_id` and review knobs |

## 4. State Boundary Rules

Keep these boundaries intact:

- `metadata.json` is for one article's state.
- `events.jsonl` is for cross-article behavior and preference learning.
- `draft.md` is the rendered body, not the state ledger.
- `publish_history.jsonl` is a publish log, not a decision-memory store.
- `~/.agentflow/review/*.json` is daemon-owned; do not mirror its fields into `metadata.json`.

Two newer transitions that look like boundary violations but are intentional:

- `STATE_PUBLISHED → STATE_CHANNEL_PENDING_REVIEW` is the legitimate incremental-republish edge (operator adds a channel post-publish).
- `drafting_locked_human → {draft_pending_review, draft_rejected}` is the manual-takeover exit. The locked state is an escape hatch, not a sink.

If a proposed change crosses these boundaries elsewhere, call it out explicitly.

## 5. Current State Machine

Current `STATE_*` set in `backend/agentflow/agent_review/state.py` (14 states):

1. `topic_pool` — default entry state.
2. `topic_approved` — after `A:write`.
3. `topic_rejected` — after `A:reject_all` or A 24h timeout. Terminal.
4. `drafting` — `af fill` is running, or `B:rewrite` round 1 is in flight.
5. `draft_pending_review` — Gate B is open and waiting.
6. `draft_approved` — after `B:approve`.
7. `draft_rejected` — after `B:reject`. Terminal.
8. `drafting_locked_human` — Gate B rewrite hit round 3+, manual takeover required.
9. `image_pending_review` — Gate C is open.
10. `image_approved` — after `C:approve`, or `D:cancel` / D timeout rollback.
11. `image_skipped` — after `C:skip`, 12h auto-skip, or `--mode none`.
12. `channel_pending_review` — Gate D is open.
13. `ready_to_publish` — dispatch finished; awaiting Medium paste mark.
14. `published` — terminal, but carries the incremental-republish out-edge.

## 6. Memory Layer Scope

Real event types in use (grep `append_memory_event` under `backend/agentflow/`):

`article_created` / `fill_choices` / `section_edit` / `hotspot_review` / `preview` / `publish` / `learn_style` / `image_resolved` / `image_gate` / `topic_profile_updated` / `topic_profile_suggestion_created` / `learning_review` / `newsletter_drafted` / `newsletter_edited` / `newsletter_preview_sent` / `newsletter_sent` / `newsletter_correction_sent` / `system_notified` / `publish_rolled_back`

Do not say "the system records but does not consume memory" anymore. `af learning-review` and the topic-profile pipeline are real consumers of these events.

## 7. API-Key Matrix

When the user asks "what keys are missing", answer using these buckets.

### Required (real-key mode)

- `MOONSHOT_API_KEY` — primary LLM
- `JINA_API_KEY` — primary embeddings
- `ATLASCLOUD_API_KEY` — image generation
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_REVIEW_CHAT_ID`

### Optional fallback LLMs

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

### D4 publishers (load on demand)

- Ghost: `GHOST_ADMIN_API_URL` + `GHOST_ADMIN_API_KEY` (must be `<24hex>:<hex>`)
- LinkedIn: `LINKEDIN_ACCESS_TOKEN` + `LINKEDIN_PERSON_URN`
- Twitter: `TWITTER_*` (4 OAuth values + bearer)
- Webhook: `WEBHOOK_PUBLISH_URL` + `WEBHOOK_AUTH_HEADER` + `WEBHOOK_FORMAT`
- Medium: deprecated — manual paste only via `PR:mark` or `af mark-published`.
- Resend (newsletter): `RESEND_API_KEY` + 4 `NEWSLETTER_*` envs

### Control envs

- `REVIEW_GATE_*_HOURS` (B 12/24, C 6/12, D 12, A 12/24)
- `AGENTFLOW_FIT_WEIGHT=0.6`
- `MOCK_LLM=true|false`
- `GHOST_STATUS=draft|published|scheduled`

## 8. Review Priorities

When reviewing, bias toward these failure modes:

1. Auto-draft flow regresses into skeleton-first.
2. Status transitions overwrite each other across Gate B / C / D.
3. Memory logging leaks into per-article storage.
4. API contracts change without daemon / review-store updates.
5. Mock success is overstated as production readiness.
6. Latent `cb` out-of-scope reference in `daemon._route` `B:edit` / `L:edit` branches (known `NameError` pending fix).
7. `published_url` schema migration (str → dict) — check both readers and writers stay compatible.

## 9. Default Verification Ladder

Use the lightest sufficient verification, but keep the loop closed.

```bash
# Primary test suite (replaces the legacy unittest test_p1_api)
cd backend && .venv/bin/python -m pytest tests/test_v02_workflows.py -q
# Expect 42+ passed.

# Health probe
.venv/bin/af doctor

# Daemon online smoke (12s short-run)
.venv/bin/af review-daemon &
DAEMON_PID=$!
sleep 12
kill -TERM $DAEMON_PID
# Verify ~/.agentflow/review/last_heartbeat.json mtime is fresh.
```

Do not run the frontend `npm run build` ladder. The frontend has been moved to `_legacy/` and is not part of the current verification path.

## 10. Open Claw Finish Condition

For this repository, "open claw complete" means:

- current state was identified
- a decision was made against repo constraints
- action was taken or a precise no-change conclusion was given
- verification was reported
- next step or blocker was named

If one of these is missing, the claw is still open.
