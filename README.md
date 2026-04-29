# AgentFlow Article Publishing

**Version:** `1.0.0` (see [`backend/pyproject.toml`](backend/pyproject.toml) for the canonical string; release notes in [`CHANGELOG.md`](CHANGELOG.md)).

Single-user writing/publishing tool that compresses per-article workflow from 5-7h down to ~90min.

**Form factor**: skill-first. The daily flow lives inside Claude Code as a small set of skills at `.claude/skills/` (`agentflow-*`). No standalone Web UI or HTTP server is needed. A full-featured `af` CLI sits underneath — the skills just orchestrate it.

> **Sibling repo (skills only)**: [`witness1993x/agentflow-skills`](https://github.com/witness1993x/agentflow-skills) — a thin ~90 KB distribution of just the SKILL.md files. Install it into any workspace where you want the orchestration prompts available, while this repo provides the `af` runtime that those skills shell out to.

The earlier Next.js + FastAPI implementation is preserved under `_legacy/` for reference (not bundled in this public repo).

## What it does

- **D0** auto-learns your writing style from 3-5 past articles (MD / DOCX / TXT / URL).
- **D1** scans external sources (Twitter / RSS / HackerNews) for hotspots and mines independent angles.
- **D2** helps you co-write — skeleton-first, then section-by-section, with natural-language edit commands (`改短` / `加例子` / `改锋利` / `去AI味` / `展开`).
- **D3** adapts the final draft for each target platform (paragraph length, emoji density, heading style, metadata).
- **D4** publishes long-form drafts to selected platforms and records URLs; Twitter and newsletter fan-out use dedicated `af tweet-*` / `af newsletter-*` flows.

Runtime data lives at `~/.agentflow/` (style profile, hotspots, drafts, memory events, publish history, logs).

## Requirements

- Python 3.11+ (the venv bundled under `backend/.venv/` uses 3.14).
- Claude Code (for the skill-first UX). Skills also work from a plain shell via the `af` CLI.

## Install

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.template .env         # leave MOCK_LLM=true for dry runs
```

External agent orchestration:

- see `docs/integrations/AGENT_BRIDGE.md` for the local HTTP bridge, outbound
  event webhook, and authenticated command API

Skill install (once, so Claude Code can invoke them anywhere):

```bash
# Option A: symlink (follows repo updates)
for s in agentflow agentflow-style agentflow-hotspots agentflow-write agentflow-publish agentflow-tweet agentflow-newsletter; do
  ln -sf "$(pwd)/../.claude/skills/$s" "$HOME/.claude/skills/$s"
done

# Option B: just launch Claude Code with cwd at the project root — skills under
# .claude/skills/ are auto-registered.
```

## Daily flow (skills)

```
once a week                       daily
    │                               │
    ▼                               ▼
/agentflow-style          /agentflow-hotspots
    │                               │
    │                               ▼
    │           (pick hotspot_id + angle)
    │                               │
    │                               ▼
    │           /agentflow-write <hotspot_id>
    │                               │
    │           (auto-fill or skeleton→fill,
    │            then interactive edit loop)
    │                               │
    │                               ▼
    │           /agentflow-publish <article_id>
    │                               │
    │                               ▼
    │                        (report URLs)
    ▼
style_profile.yaml updated
```

## `af` CLI reference

```bash
af learn-style --dir ./my_articles/      # D0: learn voice from 3-5 articles
af learn-style --show                    # inspect current profile
af learn-style --recompute               # re-aggregate from full corpus

af hotspots --json                       # D1: scan → prints D1Output JSON
af hotspots --filter "MCP|agent" --json  # D1 with topic-targeted post-filter
af hotspot-show <hotspot_id> --json      # single hotspot with all angles

af search "multi-agent orchestration" --days 14 --min-points 20 --json  # D1 via HN Algolia
af intent-set "MCP server" --ttl session  # stash a topic intent for this session
af intent-show                            # inspect current intent
af intent-clear                           # drop current intent

af write <hotspot_id> --auto-pick --json        # auto-fill using preferences.yaml (or 0/0/0 fallback)
af write <hotspot_id> --auto-pick --ignore-prefs  # force 0/0/0, bypass preferences
af write <hotspot_id> --json                     # skeleton only (manual flow)
af fill <article_id> --title N --opening N --closing N --json
af edit <article_id> --section N [--paragraph M] --command "改短"
af propose-images <article_id> --json            # D2.5: LLM proposes [IMAGE:] placements
af image-auto-resolve <article_id> [--library ~/Pictures/agentflow] [--min-score 0.55] --json
af image-resolve <article_id> <placeholder_id> /abs/path/to.png
af draft-show <article_id> --json
af intent-check <article_id> --json              # score how well article reflects current TopicIntent

af preview <article_id> --json                   # D3: defaults to Medium manual unless preferences override
af preview <article_id> --ignore-prefs --json    # force all platforms from d3_output
af publish <article_id> --platforms medium --json  # D4 default: Medium manual package
af publish <article_id> --platforms ghost_wordpress --force-strip-images --json
af publish-rollback <article_id> --json          # DELETE the Ghost post for this article
af publish-rollback <article_id> --post-id <id> --json  # override when history lacks platform_post_id
af medium-export <article_id> --json             # export Medium browser-ops source bundle to ~/.agentflow/medium/<article_id>/
af medium-package <article_id> [--distribution-mode draft_only|cross_post] [--canonical-url URL] --json
af medium-ops-checklist <article_id> --json      # human/browser-operator checklist for draft import/review

# Twitter / X
af tweet-draft <hotspot_id> --form thread --json          # draft single or thread
af tweet-draft <article_id> --form thread --from-article --json
af tweet-show <tweet_id> --json
af tweet-edit <tweet_id> --index N --command "改短"       # or --split N / --merge i,j / --reorder 0,3,1,2
af tweet-publish <tweet_id> [--dry-run] --json            # needs TWITTER_CONSUMER_* + USER_ACCESS_* in .env
af tweet-rollback <tweet_id> --json
af tweet-list [--status draft|published] --json

# Email newsletter (Resend)
af newsletter-draft <article_id> --json                   # derive from blog
af newsletter-draft --from-scratch "title" --json
af newsletter-show <newsletter_id>
af newsletter-edit <newsletter_id> --section subject|intro|body|closing --command "..."
af newsletter-preview-send <newsletter_id> --to self --json
af newsletter-send <newsletter_id> [--dry-run] --json     # needs RESEND_API_KEY + NEWSLETTER_* in .env
af newsletter-correction <newsletter_id> [--dry-run] --json  # follow-up correction email; does NOT unsend
af newsletter-list-show --json
af notify "hotspots scan done" --json                     # system self-notification

# Preferences (Memory → Default Strategy)
af prefs-rebuild [--dry-run] --json                       # aggregate events.jsonl → preferences.yaml
af prefs-show [--key write.default_title_index] --json
af prefs-explain <dotted.key>                             # 10 evidence events
af prefs-reset [--key <dotted.key>] --json

# Cross-channel status
af report [--window 7d|30d|all] [--json]                  # IDEAS / IN FLIGHT / SHIPPED / ROLLBACKS / ATTENTION

af memory-tail --limit 20 --json         # inspect recent events
af run-once                              # D1 then hand off (legacy flow)
```

**`--json` output contract**: stdout is pure JSON; all logs (collector progress, LLM
calls, compliance warnings) go to **stderr**. If you pipe or `tee` the output,
remember to redirect stderr: `af hotspots --json 2>/dev/null | jq .`.

**Ghost publish status**: by default Ghost posts go live (`published`). Set
`GHOST_STATUS=draft` to create a hidden draft instead — useful for real-key
smoke tests. `af publish-rollback` works on both. `af publish` also **auto-downgrades to draft** for N runs after a rollback is detected in memory events (read from `preferences.publish.ghost_status_override`).

**Preferences (Memory → Default Strategy)**: `af prefs-rebuild` now aggregates `fill_choices`, publish history, rollback signals, and TopicIntent usage into `~/.agentflow/preferences.yaml`. `af write --auto-pick` reads `write.default_*_index`; `af preview` reads `preview.default_platforms`; `af publish` honors `publish.ghost_status_override`; `preferences.intent.*` tracks recent / persistent TopicIntent recall. Each command's `--ignore-prefs` flag bypasses. See `docs/backlog/MEMORY_TO_DEFAULTS.md`.

**TopicIntent**: `af intent-set "MCP server"` stashes a cross-flow intent. D2 skeleton + fill prompts receive it automatically (reduces hallucination drift). `af intent-check <aid>` scores article-vs-intent alignment. `ttl=single_use` clears after first use; `session` persists until `af intent-clear`. See `docs/backlog/TOPIC_INTENT_FRAMEWORK.md`.

**Ghost images**: local `[IMAGE:]` paths resolved via `af image-resolve` or `af image-auto-resolve` are **automatically uploaded to Ghost Storage** at `af publish` time — both feature_image and inline `<img src>` get swapped for CDN URLs. Mock mode short-circuits to `https://blog.mock/cdn/...` URLs.

**Profile id selection**: commands that need a topic profile should use explicit `--profile <id>` when provided, then the profile stored in `~/.agentflow/intents/current.yaml`, then `AGENTFLOW_DEFAULT_TOPIC_PROFILE` if configured. If none exists, prompt for a profile id before continuing; do not silently reuse an old profile from another bot/project.

**Channel split**: `af publish` only fans out long-form platforms. Gate D defaults to `medium` because Medium manual package/browser paste is always available. `ghost_wordpress` and `linkedin_article` are optional configured channels, selected only when the user/preferences/env make them ready. Twitter and email stay on their own flows: `af tweet-*` and `af newsletter-*`. For the Medium semi-automatic browser flow, use `af medium-export` / `af medium-package` / `af medium-ops-checklist` instead of relying on the deprecated API path.

Every mutation appends an event to `~/.agentflow/memory/events.jsonl`, including `article_created`, `fill_choices`, `preview`, `publish`, `publish_rolled_back`, `image_resolved`, `images_auto_resolved`, `newsletter_sent`, `newsletter_correction_sent`, `medium_exported`, `medium_packaged`, `topic_intent_set`, and `intent_used_in_write`.

## MOCK_LLM toggle

`.env.template` sets `MOCK_LLM=true`. In that mode all Claude + OpenAI calls return deterministic fixtures from `backend/agentflow/shared/mocks/`, and every publisher short-circuits to a fake URL with no network call. Great for dry runs and CI.

Unset (or set to `false`) to use real keys.

## Platforms (v0.1)

| Platform | Status | Credentials |
|---|---|---|
| `medium` | Default manual/browser-ops | No credential required for package generation; `MEDIUM_INTEGRATION_TOKEN` is legacy only (Medium closed public API on 2025-01-01). Prefer `af medium-*` for the semi-automatic draft workflow. |
| `ghost_wordpress` | Optional configured channel | `GHOST_ADMIN_API_URL` + `GHOST_ADMIN_API_KEY` (format `<24hex>:<hex_secret>`) |
| `linkedin_article` | Optional configured channel | `LINKEDIN_ACCESS_TOKEN` + `LINKEDIN_PERSON_URN` |
| `substack` / `wechat_official` / `x_longform` | v0.5 | — |

To get a Ghost Admin key: in the Ghost admin UI → Settings → Integrations → Add custom integration → copy the **Admin API Key** (the one with the colon), not the Content API Key.

## Mock end-to-end verification

With `MOCK_LLM=true` and no real keys:

```bash
cd backend && source .venv/bin/activate
MOCK_LLM=true PYTHONPATH=. af hotspots --json > /tmp/out.json
HID=$(python -c 'import json; print(json.load(open("/tmp/out.json"))["hotspots"][0]["id"])')
MOCK_LLM=true PYTHONPATH=. af write "$HID" --auto-pick --json > /tmp/art.json
AID=$(python -c 'import json; print(json.load(open("/tmp/art.json"))["article_id"])')
MOCK_LLM=true PYTHONPATH=. af preview "$AID" --json >/dev/null
MOCK_LLM=true PYTHONPATH=. af publish "$AID" --force-strip-images --json
MOCK_LLM=true PYTHONPATH=. af memory-tail --limit 5 --json
```

Expected artifacts:

- `~/.agentflow/hotspots/<YYYY-MM-DD>.json`
- `~/.agentflow/drafts/<article_id>/{skeleton.json,draft.md,metadata.json,d3_output.json}`
- `~/.agentflow/drafts/<article_id>/platform_versions/*.md`
- `~/.agentflow/publish_history.jsonl`
- `~/.agentflow/memory/events.jsonl`

## External Agent Bridge

If you want an OpenClaw-style orchestrator or any generic LLM agent framework
to supervise AgentFlow and issue commands, start:

```bash
cd backend && source .venv/bin/activate
af review-dashboard
```

Then read `docs/integrations/AGENT_BRIDGE.md`. The bridge exposes:

- `GET /api/health`
- `GET /api/articles`
- `GET /api/article/{article_id}`
- `GET /api/bridge`
- `GET /api/bridge/schema`
- `POST /api/commands` (requires `AGENTFLOW_AGENT_BRIDGE_TOKEN`)

Outbound event fan-out is optional and controlled by
`AGENTFLOW_AGENT_EVENT_WEBHOOK_URL`.

## Switching to real keys

1. Unset `MOCK_LLM` (or set `MOCK_LLM=false`).
2. Generation (D0/D1/D2/D3): `MOONSHOT_API_KEY` recommended for Kimi K2.6 (`GENERATION_PROVIDER=kimi`, OpenAI-compatible, Chinese-friendly). Alternative: `ANTHROPIC_API_KEY` for Claude (`GENERATION_PROVIDER=claude`).
3. Embeddings (D1 clustering): `JINA_API_KEY` recommended (10M tokens one-time free, then ~$0.02/1M, multilingual, `EMBEDDING_PROVIDER=jina`). Alternative: `OPENAI_API_KEY` (`EMBEDDING_PROVIDER=openai`). Kimi/Moonshot does **not** offer embeddings, so can't be used here.
4. Medium manual publishing works without platform credentials. Optionally add configured publishing targets:
   - `GHOST_ADMIN_API_URL` + `GHOST_ADMIN_API_KEY`.
   - `LINKEDIN_ACCESS_TOKEN` + `LINKEDIN_PERSON_URN`.
5. (Optional for D1) `TWITTER_BEARER_TOKEN` — else D1 falls back to RSS + HackerNews only.

Provider summary:

| Layer | Primary | Fallback | Notes |
|---|---|---|---|
| Chat generation | Kimi K2.6 (Moonshot) | Claude Opus 4.7 | Moonshot API is OpenAI-compatible |
| Embeddings | Jina v3 | OpenAI text-embedding-3-small | Kimi has no embedding endpoint |
| Publishing | Medium manual | Ghost / LinkedIn | Medium public API deprecated 2025-01-01 |

## Layout

```
agentflow-article-publishing/
├── .claude/skills/              # skill-first UX
│   ├── agentflow/
│   ├── agentflow-style/
│   ├── agentflow-hotspots/
│   ├── agentflow-write/
│   └── agentflow-publish/
├── backend/
│   ├── agentflow/
│   │   ├── agent_d0/            # style learner
│   │   ├── agent_d1/            # hotspot discovery
│   │   ├── agent_d2/            # writer
│   │   ├── agent_d3/            # platform adapters
│   │   ├── agent_d4/            # publishers
│   │   ├── cli/commands.py      # `af` entry point
│   │   ├── config/              # yaml loaders (with user override + example fallback)
│   │   └── shared/              # models, llm_client (+ MOCK fixtures), memory, markdown utils
│   └── prompts/                 # 7 prompt templates
├── config-examples/             # seed configs copied to ~/.agentflow/
├── _legacy/                     # previous Next.js + FastAPI form factor (not active)
└── docs/                        # PRD / solution / backlog (informational)
```

## State files at `~/.agentflow/`

| Path | Written by | Purpose |
|---|---|---|
| `style_profile.yaml` | D0 | Voice profile (all agents read this) |
| `style_corpus/` | D0 | Per-article analyses + raw text |
| `sources.yaml` | you | KOL / RSS / HN configuration |
| `hotspots/<date>.json` | D1 | Daily scan output |
| `drafts/<id>/` | D2, D3 | Skeleton, draft.md, metadata, platform_versions |
| `medium/<id>/` | Medium browser ops | Export, package, and checklist artifacts for semi-automatic Medium publishing |
| `intents/current.yaml` | intent commands | Current TopicIntent and selected profile id |
| `publish_history.jsonl` | D4 | One row per publish attempt |
| `memory/events.jsonl` | all CLI mutations | Append-only event stream |
| `logs/agentflow.log` | everyone | Tail this on any failure |
| `logs/llm_calls.jsonl` | llm_client | One row per LLM call (input/output tokens, latency, mocked=bool) |

## Troubleshooting

- **`af` not found** → `source backend/.venv/bin/activate`.
- **`agentflow.*` import errors** → prepend `PYTHONPATH=.` from `backend/`.
- **`af publish` 409 "Unresolved image placeholders"** → expected; either resolve each placeholder via `af image-resolve` or retry with `--force-strip-images`.
- **Any other CLI failure** → `tail -n 20 ~/.agentflow/logs/agentflow.log`.
