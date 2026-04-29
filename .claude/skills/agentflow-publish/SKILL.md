---
name: agentflow-publish
description: |
  Preview, resolve images, publish configured platforms, or prepare Medium manual fallback.

  TRIGGER: "/agentflow-publish", "af preview", "af publish", "Gate C", "Gate D", "image-gate", "publish-rollback", "medium-package", "review-publish-mark", "PR:mark", "I:cover_only", "PD:dispatch".

  SKIP for: modifying D3 platform adapter internals (agent_d3/adapters/); modifying D4 publisher internals (agent_d4/publishers/); editing platform-specific token / OAuth flows.
---

# agentflow-publish — preview, fan out, or prepare Medium manual publish

Wraps `af draft-show`, `af image-resolve`, `af image-auto-resolve`, `af preview`, `af publish`, and `af publish-rollback`. Goal: ship `<article_id>` to every configured long-form platform and report status — or take a post back down if the user changes their mind.

**Platform status (v0.1):**
- `medium` — default. Manual package/browser paste is always available; close the loop with `af review-publish-mark <article_id> <url>`.
- `ghost_wordpress` — optional configured channel. Requires `GHOST_ADMIN_API_URL` + `GHOST_ADMIN_API_KEY`.
- `linkedin_article` — optional configured channel. Requires `LINKEDIN_ACCESS_TOKEN` + `LINKEDIN_PERSON_URN`.
- Twitter and newsletter are **not** part of `af publish`; use `af tweet-*` and `af newsletter-*` instead.

## Input

Expect `<article_id>` from the user invocation. If missing: "Give me an article id, or run `/agentflow-write <hotspot_id>` first."

## Setup

```bash
cd /Users/witness/Desktop/experimental/medium\&blog_posting_agent/agentflow-article-publishing/backend
source .venv/bin/activate
```

Prefix `af` with `PYTHONPATH=.`.

## Step 1 — inspect the draft

```bash
PYTHONPATH=. af draft-show <article_id> --json
```

Count:

- Total sections, total words.
- Unresolved image placeholders = entries in `image_placeholders[]` where `resolved_path` is null.

Report to user: "Draft has X sections, Y words, Z unresolved images."

## Step 1b — pre-publish overview (lineage + references + readiness)

Before showing platform previews, produce a one-screen overview so the user can see what they're about to ship, where it came from, and what might go wrong. Skip this step only if the user explicitly says "just publish".

Gather:

1. **Lineage** — read `~/.agentflow/drafts/<article_id>/metadata.json` for `hotspot_id`, `chosen_angle_index`, `target_series`.
2. **References** — resolve the `hotspot_id` from either `~/.agentflow/hotspots/` or `~/.agentflow/search_results/`; pull `topic_one_liner`, `suggested_angles[chosen_angle_index]`, and the top 3-5 entries from `source_references[]` (each has `source`, `author`, `url`, `text_snippet`).
3. **Compliance** — from `draft-show --json`, scan `sections[].compliance_score` and `sections[].taboo_violations[]`. Count sections with `compliance_score < 0.85` and the worst offender.
4. **Tag quality** — glance at the tags produced in `d3_output.json` or `platform_versions/<platform>.md` front-matter. Flag if any tag is a bare particle fragment (starts with 的/在/对/和 or is shorter than 2 chars).
5. **Platform readiness** — Medium manual is available even without credentials. Check env for optional API channels: `GHOST_ADMIN_API_URL`+`GHOST_ADMIN_API_KEY`, `LINKEDIN_ACCESS_TOKEN`+`LINKEDIN_PERSON_URN`, and any legacy `MEDIUM_INTEGRATION_TOKEN` only if the user explicitly asks for the deprecated API path. Missing optional credentials mean that channel is not ready.

Present as:

```
Article: <title>
  Lineage:     <hotspot_id> → angle #<N> (<angle_title>) → series <X>
  Topic:       <topic_one_liner>
  Content:     <N> sections, <W> words
  Compliance:  avg <S>; <K> section(s) < 0.85 (worst: "<heading>" at <score>)
  Tags:        <first 5 tags, comma-separated>  [flag if any look broken]
  Images:      <R> resolved / <U> unresolved
  Platforms:   ready: <list>   will skip: <list with reason>
  Preferences: <one-line summary of what preferences.yaml is steering — see below>

References (top 3-5 that fed this hotspot):
  1. [<source> <author>]  <url>
       "<text_snippet truncated to ~120 chars>"
  2. ...
```

For the **Preferences** line, run `PYTHONPATH=. af prefs-show --json` and
summarise as:

- `preview.default_platforms` (if confident) → "default platforms <csv>
  (from N past publishes)"
- `publish.ghost_status_override = "draft"` with `override_remaining_runs > 0`
  → "Ghost will be forced to `draft` this run (recent rollback detected,
  <N> runs remaining)"
- Nothing relevant → "none"

Example: `Preferences: default platforms medium
(from 10 past publishes); Ghost forced to draft if selected (2 runs remaining)`.

If the user asks why an angle or claim exists, trace it back to one of these references. If none of the references seem to actually support the article's main claim, flag it: "The article's central claim X doesn't appear in any of the N references — this may be hallucinated."

### Intent alignment check

This runs **after** the reference check above. The reference check asks "do the sources support the article?"; this check asks "does the article reflect what the user said they wanted to write about?" Both can fire independently — surface both.

1. Read `~/.agentflow/intents/current.yaml`. If the file is missing, empty, or `query.text` is blank, skip this section entirely (no output).
2. Otherwise load `intent.query.text` and tokenize it into lowercase words (split on whitespace/punctuation; drop tokens shorter than 2 chars). For CJK strings, also keep 2-char sliding substrings as tokens.
3. Build the "article signal" string from:
   - `metadata.title`
   - the first section heading (`metadata.sections[0].heading`)
   - the article's central claim — prefer `metadata.opening`; fall back to the first 500 chars of `sections[0].content_markdown`.
4. Lowercase the signal. Count how many intent tokens appear as substrings of the signal.
5. If **zero** intent tokens appear, flag:

   ```
   ⚠ intent_drift: you set intent '<query.text>' but the article doesn't
   obviously reflect it (0 / <N> intent tokens found in title + first
   heading + central claim). Proceed? (yes / re-write via
   /agentflow-write <article_id> / clear intent via `af intent-clear`)
   ```

6. If between 1 and half the tokens match, surface a softer note: "intent partial match: <k>/<N> tokens from '<query.text>' appear in the article — double-check alignment before publishing."
7. If at least half match, stay silent — alignment is fine.

Shortcut: if `af intent-check` is wired, run `PYTHONPATH=. af intent-check <article_id> --json` and use its `alignment_score` (< 0.1 → drift flag; 0.1–0.5 → partial note; ≥ 0.5 → quiet).

Then pause: "Review this overview. Proceed to platform previews? (yes / edit first via `/agentflow-write <article_id>` / cancel)"

## Step 2 — resolve images (or force-strip)

If Z > 0:

```
Unresolved images:
  1. <id> — <description>
  2. ...
Options:
  (a) paste a local file path for each,
  (a2) say 'auto' to try `af image-auto-resolve`,
  (b) say 'strip' to force-strip all placeholders at publish time,
  (c) say 'cancel' to abort.
```

If the user says `auto`, run:

```bash
PYTHONPATH=. af image-auto-resolve <article_id> --json
```

If they have a known local library path, include it:

```bash
PYTHONPATH=. af image-auto-resolve <article_id> --library <absolute_dir> --json
```

Show the matched files and remaining unresolved count, then continue with manual `af image-resolve` only for anything still unresolved.

For each path the user gives:

```bash
PYTHONPATH=. af image-resolve <article_id> <placeholder_id> <absolute_path>
```

If they say `strip`, remember this and later pass `--force-strip-images` to `af preview` and `af publish`.

## Step 3 — preview (D3 adapters)

Default platform: `medium` manual package unless preferences or the user explicitly select other ready channels. Ghost/LinkedIn are optional configured channels, not the baseline.

```bash
PYTHONPATH=. af preview <article_id> --json
```

For the explicit Medium manual path: `PYTHONPATH=. af preview <article_id> --platforms medium --json`, then `PYTHONPATH=. af medium-package <article_id>`.

Add `--force-strip-images` if the user chose strip in Step 2.

This writes `~/.agentflow/drafts/<article_id>/d3_output.json` and `~/.agentflow/drafts/<article_id>/platform_versions/<platform>.md`.

## Step 4 — present previews

For each platform actually produced (default: `medium`):

```bash
Read ~/.agentflow/drafts/<article_id>/platform_versions/<platform>.md
```

Show the YAML front-matter summary (title, tags, canonical_url if present) and the first ~30 lines of the body, then "... (N more lines)".

## Step 5 — confirm

Ask: "Dispatch selected platforms? (yes / list only the platforms you want, comma-separated / cancel)"

- "yes" → dispatch all previewed platforms. Medium produces a manual package; optional API channels publish directly.
- Comma list (e.g. `ghost_wordpress`) → publish only those.
- "cancel" → stop. Do not run `af publish`.

## Step 6 — publish

```bash
PYTHONPATH=. af publish <article_id> --platforms <csv> [--force-strip-images] --json
```

Use `--platforms medium` for the default manual flow. Include `ghost_wordpress` or `linkedin_article` only when the user selected those ready channels.

If Step 2 ended with unresolved images NOT resolved, the CLI will abort unless `--force-strip-images` was passed. Respect that — don't paper over it.

For Medium default/manual, expect a `manual` result/package rather than a direct URL. Tell the user to paste the package in Medium, then run:

```bash
PYTHONPATH=. af review-publish-mark <article_id> <published_url>
```

## Step 7 — report

Parse `results[]` and print a table:

```
PLATFORM              STATUS   URL / REASON
--------------------  -------  -----------------------------
medium                manual   paste package.md, then review-publish-mark
ghost_wordpress       success  https://yoursite.ghost.io/...
linkedin_article      success  https://linkedin.com/feed/...
```

If any `status != "success"`, ask: "Retry failed platforms? (yes / no)". On yes, re-run `af publish <id> --platforms <failed_csv>`.

## Step 8 — close

End with one line: "Article `<article_id>` published to M/N platforms. Memory event written to `~/.agentflow/memory/events.jsonl`."

## Rollback — taking a post back down

Trigger when the user says any of: "undo", "撤下", "删掉刚才那篇", "takedown", "rollback", "回滚", "取消发布". Only Ghost is supported in v0.1 (LinkedIn's API does not allow programmatic delete; Medium is deprecated).

**Happy path** (the publish record has `platform_post_id`):

```bash
PYTHONPATH=. af publish-rollback <article_id> --json
```

Optionally pass `--platform ghost_wordpress` (this is already the default).

**Historical-record fallback** (records written before `platform_post_id` was stored): ask the user for the Ghost post ID. They can find it in the Ghost admin UI URL (`/ghost/#/editor/post/<id>`), or you can find it in `/tmp/` if the original publish JSON is still there.

```bash
PYTHONPATH=. af publish-rollback <article_id> --post-id <platform_post_id> --json
```

**Confirmation** — always confirm before running: "This will permanently delete the Ghost post for `<article_id>`. Proceed? (yes / cancel)"

**Report** — on success, say: "Rolled back Ghost post `<platform_post_id>` for `<article_id>`. `publish_rolled_back` event written to memory."

**Side-effects**:
- `publish_history.jsonl` — appends a new line with `status=rolled_back`.
- `memory/events.jsonl` — one `publish_rolled_back` event.
- `metadata.json` — `published_platforms` loses this platform; if empty, `status` reverts to `preview_ready`.

**Failure modes**:
- `"no successful <platform> record found"` → the article was never successfully published to that platform. Tell the user to check `af memory-tail`.
- `"matching record has no platform_post_id"` → pre-fix history record. Use `--post-id` override.
- `"Ghost DELETE network error"` / `"Ghost DELETE 404"` → the post may already be gone. Suggest checking Ghost admin.

## Error handling

- `af publish` fails with "Unresolved image placeholders present." → means you skipped Step 2 or the user didn't commit to strip. Re-prompt.
- `af publish` fails with "no platform versions found" → you skipped Step 3 (preview). Run it now.
- Any other non-zero: `tail -n 20 ~/.agentflow/logs/agentflow.log` and show.
- `~/.agentflow/publish_history.jsonl` is append-only — each publish leaves a record regardless of success; you don't need to write to it.

## State side-effects

- `drafts/<id>/d3_output.json` — persisted by `af preview`.
- `drafts/<id>/platform_versions/*.md` — persisted by `af preview`.
- `drafts/<id>/metadata.json` — updated with `status: published`, `published_platforms`, `published_at` after successful API publish or `review-publish-mark`.
- `medium/<id>/package.md` — generated for Medium manual browser paste.
- `memory/events.jsonl` — one `preview` event and one `publish` event per run.
- `publish_history.jsonl` — one line per platform per attempt.

## NEVER

- Never call `agentflow.agent_d3.*` or `agentflow.agent_d4.*` directly — always via `af`.
- Never force-strip images silently. Always confirm with user first.
- Never retry a `success` — only retry failed platforms.
- Never publish with no preview present — the CLI will reject it.

## MOCK_LLM

`MOCK_LLM=true af publish <id> --force-strip-images --json` uses mock publishers that return fake URLs (`https://blog.mock/...` for Ghost, `https://www.linkedin.com/feed/update/mock_...` for LinkedIn). Safe for dry runs.
