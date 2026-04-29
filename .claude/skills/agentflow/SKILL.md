---
name: agentflow
description: |
  Top-level guide for the AgentFlow daily writing workflow (profile, hotspots, write, publish).

  TRIGGER: "agentflow", "daily workflow", "/agentflow", "what's the flow", "hotspot to publish".

  SKIP for: single sub-tasks (use a more specific sibling skill); pure bash / grep; editing ~/.agentflow/topic_profiles.yaml user data.
---

# agentflow — daily workflow guide

AgentFlow is a single-user article pipeline. This skill is the entry point; it explains the flow and dispatches to the task-specific sibling skills.

When the user invokes this skill without a clear subtask, print a short overview and ask what they want to do.

## The sibling skills

- `agentflow-style` — teach or refresh the voice profile from past articles (`af learn-style`).
- `agentflow-hotspots` — scan trending topics and pick one (`af hotspots`, `af hotspot-show`).
- `agentflow-write` — turn a hotspot into a finished draft (`af write`, `af fill`, `af edit`, `af image-resolve`).
- `agentflow-publish` — preview on each platform, fix images, publish, and roll back if needed (`af preview`, `af publish`, `af publish-rollback`).
- `agentflow-tweet` — Twitter/X 短形分发 helpers（包 `af tweet-*` 子命令）.
- `agentflow-newsletter` — newsletter 分发 helpers（包 `af newsletter-*` 子命令）.

## Main flow

0. First run / profile drift check:
   - `af doctor` — confirm runtime account config (`.env`, LLM, Telegram, webhook, Medium manual default, optional Ghost/LinkedIn channels).
   - `af topic-profile show --profile <id> --json` — inspect missing publisher/profile fields.
   - If missing fields exist, complete the Telegram profile setup session or run `af topic-profile init --profile <id> --from-file <patch.yaml>`.
   - Optional enrichment: `af topic-profile update --profile <id> --from-file <patch.yaml>` for search queries, avoid terms, tags, image hints, canonical domain.
   - Learning updates stay suggestion-first: review with `af topic-profile suggestion-list/review/apply` or `/suggestions`.
1. (Once a week) `/agentflow-style` — update the voice profile from past articles.
2. `/agentflow-hotspots` — pick today's topic.
3. `/agentflow-write <hotspot_id>` — auto or manual skeleton, then edit loop.
4. `/agentflow-publish <article_id>` — preview, resolve images, fan out or prepare Medium manual package.

Profile setup is part of the main flow, not a side quest. Resolve profile id in this order: explicit `--profile`, active TopicIntent profile, `AGENTFLOW_DEFAULT_TOPIC_PROFILE`, then ask the user. Do not silently reuse an old profile from another bot or project.

Treat the minimal profile path as:

`account_config_pending -> publisher_profile_pending -> learning_sources_pending -> extra_profile_pending -> profile_suggestions_pending -> profile_ready`

Only `publisher_profile_pending` is blocking for first use. The learning sources, extra profile, and suggestions states are enrichment layers and should not block a first article unless the user asks for strict brand governance.

## State lives at `~/.agentflow/`

- `hotspots/<YYYY-MM-DD>.json` — one file per scan day.
- `search_results/*.json` — archived search-result bundles for traceability and later recall.
- `drafts/<article_id>/metadata.json` — skeleton + filled sections + image placeholders.
- `drafts/<article_id>/draft.md` — the assembled article.
- `drafts/<article_id>/skeleton.json` — stashed so `af fill` can re-run with new indices.
- `drafts/<article_id>/d3_output.json` — per-platform adapted versions.
- `drafts/<article_id>/platform_versions/<platform>.md` — human-readable preview per platform.
- `memory/events.jsonl` — append-only log of every mutation (article_created, fill_choices, section_edit, hotspot_review, preview, publish, publish_rolled_back, image_resolved, learn_style, topic_profile_updated, topic_profile_suggestion_*).
- `intents/current.yaml` — current TopicIntent, including the selected profile when one was set.
- `publish_history.jsonl` — one row per publish attempt and per rollback, with `platform_post_id` so rollback can target the exact post.
- `style_profile.yaml` — the learned voice profile.
- `topic_profiles.yaml` — the confirmed topic/profile constraints.
- `constraint_suggestions/*.json` — suggestion-only learning artifacts waiting for review/apply.
- `constraint_sessions/*.json` — Telegram profile-setup sessions in progress.
- `logs/agentflow.log` — surface the tail on any CLI failure.

## Running `af`

The venv is at `backend/.venv/`. From the project root (or wherever cwd is):

```bash
cd /Users/witness/Desktop/experimental/medium\&blog_posting_agent/agentflow-article-publishing/backend
source .venv/bin/activate
PYTHONPATH=. af <subcommand>
```

If the shell session is already initialised, just `af <subcommand>` works.

The CLI auto-loads `backend/.env` on startup (without overriding already-set env vars), so real-key runs don't need an explicit `source .env`.

## MOCK_LLM toggle

Prefix any `af` call with `MOCK_LLM=true` to run the full flow against canned fixtures (deterministic, instant). Drop the env var for real LLM calls.

`.env` ships with `MOCK_LLM=false`; set `MOCK_LLM=true` inline to force mock for a single command.

## Platform defaults

Gate D defaults to Medium manual publishing because Medium package generation is always available without platform credentials. Ghost and LinkedIn are optional configured channels; include them only when the user selects them or project preferences/env make them ready.

When Ghost is selected, `af publish` publishes Ghost posts live (`status=published`) by default. Set `GHOST_STATUS=draft` in env to create a hidden draft instead — useful for smoke tests. `af publish-rollback` works on both.

## --json output contract

`af <cmd> --json` prints pure JSON on stdout. All logs (collector progress, LLM calls, SSL warnings, compliance messages) go to **stderr**. When piping, redirect stderr: `af hotspots --json 2>/dev/null | jq .`.

## Cross-cutting rules for ALL AgentFlow skills

- NEVER bypass the CLI by importing agentflow Python modules directly. The `af` CLI is the contract.
- Prefer `--json` output and parse it inline. Use the `Read` tool when the payload is too large to fit conversationally.
- On any `af` command failure, `tail -n 20 ~/.agentflow/logs/agentflow.log` and surface the output to the user.
- MOCK_LLM=true in env means deterministic mock — great for dry runs. Remove it for real.
- For topic/profile constraint changes, use `af topic-profile show/init/update/suggestion-list/review/apply` instead of editing `topic_profiles.yaml` manually.
- Keep generated article output in the profile's `publisher_account.output_language`; default is `zh-Hans` and should be treated as a hard prompt constraint.

### Topic intent — a cross-flow concept

When the user's message implies a specific topic (not a broad scan), treat that topic as a **TopicIntent** that threads through the whole session:

- `agentflow-hotspots` → run with `af hotspots --filter <regex>`.
- `agentflow-write` → mention the intent in the edit prompt so the article stays on-topic (and avoids the kind of hallucination where a hotspot about "consciousness" became an article about "quantum entanglement" because the LLM drifted).
- `agentflow-publish` → Step 1b should note whether the final article still reflects the original intent.

Full design: `docs/backlog/TOPIC_INTENT_FRAMEWORK.md`. Every intent use writes a `topic_intent_used` memory event so the Memory → Default Strategy layer can learn your topic patterns.

## When invoked standalone

If the user just says "agentflow" with no target, print the main flow above and ask: "Which step do you want to take? (profile / style / hotspots / write / publish)"
