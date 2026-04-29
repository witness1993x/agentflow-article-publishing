# AgentFlow Open Claw Reference

## 1. Current Product Stage

Current repo stage:

- v0.1 MVP core flow is implemented
- default path has already shifted to auto-draft-first
- workflow queue, review buckets, publish history, retry, and style console are already present
- unified memory layer exists, but memory is not yet consumed to change defaults

Do not describe this repo as an early skeleton-only prototype unless current code proves regression.

## 2. Canonical Workflow

Treat this as the current default flow:

1. D1 produces hotspots
2. user enters from `Hotspots`
3. `POST /api/articles` creates skeleton and auto-fills full draft
4. user edits locally in `Write`
5. user previews and publishes in `Publish`
6. key behavior is appended to `~/.agentflow/memory/events.jsonl`

Fallback flow exists, but is not the preferred path:

- if draft is missing, write page may still expose skeleton/fill recovery behavior

## 3. Runtime Artifacts

Use these as the main source-of-truth map:

| Artifact | Path | Purpose |
|---|---|---|
| style profile | `~/.agentflow/style_profile.yaml` | D0 style baseline |
| style corpus | `~/.agentflow/style_corpus/` | style-learning inputs |
| hotspots | `~/.agentflow/hotspots/YYYY-MM-DD.json` | D1 hotspot output |
| hotspot reviews | `~/.agentflow/hotspots/reviews/*.json` | save/skip/approve actions |
| draft markdown | `~/.agentflow/drafts/<article_id>/draft.md` | article body |
| draft metadata | `~/.agentflow/drafts/<article_id>/metadata.json` | single-article state |
| platform versions | `~/.agentflow/drafts/<article_id>/platform_versions/*.md` | D3 outputs |
| publish history | `~/.agentflow/publish_history.jsonl` | D4 publish log |
| memory events | `~/.agentflow/memory/events.jsonl` | cross-article behavior |

## 4. State Boundary Rules

Keep these boundaries intact:

- `metadata.json` is for one article's state
- `events.jsonl` is for cross-article behavior and future preference learning
- `draft.md` is the rendered article body, not the main state ledger
- publish history is a publish log, not a decision-memory store

If a proposed change crosses these boundaries, call it out explicitly.

## 5. Current Workflow Statuses

Known status set:

- `approved`
- `skeleton_ready`
- `draft_ready`
- `preview_ready`
- `published`

When touching write/preview/publish flows, re-check whether status transitions remain coherent.

## 6. Memory Layer Scope

Current recorded event types include:

- `article_created`
- `fill_choices`
- `section_edit`
- `hotspot_review`
- `preview`
- `publish`
- `learn_style`
- `image_resolved`

Current limitation:

- the system records events
- it does not yet derive reusable preference defaults from them

Do not claim “personalized defaults” already exist unless a new implementation adds that consumption layer.

## 7. API-Key and Credential Scope

When the user asks “what keys are missing” or “what keys are needed”, answer using these buckets:

### Core real-mode keys

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

These are required when `MOCK_LLM` is not `true`.

### Real D1 collection key

- `TWITTER_BEARER_TOKEN`

Without it, Twitter collection falls back to mock/empty-safe behavior.

### Real D4 publishing credentials

- `MEDIUM_INTEGRATION_TOKEN`
- `LINKEDIN_ACCESS_TOKEN`
- `LINKEDIN_PERSON_URN`
- `GHOST_ADMIN_API_URL`
- `GHOST_ADMIN_API_KEY`

Important:

- `LINKEDIN_PERSON_URN` and `GHOST_ADMIN_API_URL` are required credentials, but they are not API keys in the strict sense
- `GHOST_ADMIN_API_KEY` must be in `<id>:<secret>` format

### Optional / future credentials already reserved in code

- `SUBSTACK_EMAIL`
- `SUBSTACK_PASSWORD`
- `WECHAT_APP_ID`
- `WECHAT_APP_SECRET`
- `X_API_KEY`
- `X_API_SECRET`
- `NEWSLETTER_EMAIL`
- `NEWSLETTER_APP_PASSWORD`

Current repo truth:

- Substack / WeChat / X long-form are loaded in config as optional future credentials
- newsletter credentials appear in `.env.template`, but are not part of the current v0.1 core path

## 8. Review Priorities

When reviewing, bias toward these failure modes:

1. auto-draft flow regresses into skeleton-first flow
2. write / preview / publish status transitions overwrite each other
3. memory logging leaks into per-article storage
4. API contracts change without corresponding frontend/store changes
5. mock success is overstated as production readiness

## 9. Default Verification Ladder

Use the lightest sufficient verification, but keep the loop closed:

1. read impacted code and contracts
2. run targeted test if present
3. run build/typecheck if UI is touched
4. run mock manual path when behavior spans multiple stages
5. state clearly what was not verified

Common current checks:

```bash
cd backend
source .venv/bin/activate
python -m unittest tests.test_p1_api

cd ../frontend
npm run build
```

## 10. Open Claw Finish Condition

For this repository, “open claw complete” means:

- current state was identified
- a decision was made against repo constraints
- action was taken or a precise no-change conclusion was given
- verification was reported
- next step or blocker was named

If one of these is missing, the claw is still open.
