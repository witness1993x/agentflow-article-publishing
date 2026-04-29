---
name: agentflow-write
description: |
  Turn a chosen hotspot into a finished draft via skeleton, fill, and an interactive edit loop.

  TRIGGER: "/agentflow-write", "af write", "af fill", "af edit", "write draft", "Gate B", "写稿", "draft", "section edit".

  SKIP for: modifying D2 prompt templates (agentflow/prompts/); modifying image-gate (belongs to agentflow-publish or a standalone flow).
---

# agentflow-write — from hotspot to finished draft

Wraps `af write`, `af fill`, `af edit`, `af draft-show`, and `af image-resolve`. Goal: produce a draft the user is happy with, then hand off to `agentflow-publish`.

## Inputs

Expect `<hotspot_id>` (and optional `--angle N`) from the user invocation. If missing, tell the user to run `/agentflow-hotspots` first.
Hotspots can now be resolved from both daily hotspot batches and archived search-result bundles. Do not assume the id only lives under `~/.agentflow/hotspots/`.

## Setup (every invocation)

Ensure the venv is active:

```bash
cd /Users/witness/Desktop/experimental/medium\&blog_posting_agent/agentflow-article-publishing/backend
source .venv/bin/activate
```

Prefix every `af` call with `PYTHONPATH=.` unless the package is globally installed.

## Step 1 — auto vs manual skeleton

Ask: "auto-write the full draft (recommended) or pick the skeleton yourself?"

### Auto path (fast)

```bash
PYTHONPATH=. af write <hotspot_id> --angle <N> --auto-pick --json
```

- **Preferences-aware:** `--auto-pick` now consults `~/.agentflow/preferences.yaml`
  first. If the aggregator has enough signal (N≥3 past `fill_choices` events and
  confidence ≥ 0.5), it uses your historical majority-vote title / opening /
  closing indices; otherwise it falls back to `0/0/0`. In non-JSON mode it
  prints a stderr line like `using historical default: title=2 opening=0
  closing=1 (based on 7 past runs, confidence 0.58)`. In `--json` mode the
  response includes `defaults_source` (`"preferences"` | `"hardcoded"`) and
  `defaults_source_events`.
- **Escape hatch:** add `--ignore-prefs` to force `0/0/0`, or pass explicit
  `--title N --opening N --closing N` to override preferences for this run.
- Parse the JSON; the top-level `article_id` is what you need for later steps.
- Then `Read ~/.agentflow/drafts/<article_id>/draft.md` and walk the user through it section by section (print heading + first ~200 chars of each section, plus word count from the `draft.sections[*]` payload).
- Jump to Step 3 (edit loop).

### Manual path

```bash
PYTHONPATH=. af write <hotspot_id> --angle <N> --json
```

- Returns a `skeleton` with 3 `title_candidates`, 3 `opening_candidates`, 4–6 `section_outline`, and 3 `closing_candidates`.
- Print all three titles, all three openings, the outline headings, and all three closings. Number them all.
- Ask: "title index? opening index? closing index? (e.g. 'title 2, opening 0, closing 1')"
- Parse and call:

```bash
PYTHONPATH=. af fill <article_id> --title 2 --opening 0 --closing 1 --json
```

- Then continue to Step 3.

## Step 2 — show the draft (with compliance signals)

Before entering the edit loop, surface the draft:

```bash
PYTHONPATH=. af draft-show <article_id> --json
```

Summarise for the user:

- Title, total word count, section count.
- **Per-section compliance** — each section has `compliance_score` (1.0 = clean, lower = rule violations). Scoring is per-violation at 0.15 penalty. If any section < 0.70, call it out so the user knows which ones to inspect.
- Image placeholder count (unresolved).

Then optionally `Read ~/.agentflow/drafts/<article_id>/draft.md` for the full text.

**Known limitation**: Kimi tends to produce 140-180 char paragraphs even though the style profile asks for ≤150. Most too-long violations are in the 10-40 char overshoot band. D3 adapters will mechanically split paragraphs at publish time (Ghost max 70w, LinkedIn max 40w), so compliance < 1.00 on raw D2 draft is usually not a blocker — just a signal.

## Step 3 — interactive edit loop

Prompt (copy this literally):

> Draft ready. You can say things like:
> - `section 2 改短`
> - `第 3 段加例子`
> - `section 0 paragraph 1 改锋利`
> - `done` to finish → proceed to publish
> - `show` to re-display the full draft
> - `regen` to re-fill with different title/opening/closing

### Matching rules (apply in order)

1. `第 N 段 <cmd>` or `paragraph N <cmd>` with no section mentioned → treat as section=0, paragraph=N-1 (if Chinese 第 N 段 is 1-indexed) or paragraph=N (if English). Confirm with user if ambiguous.
2. `section N paragraph M <cmd>` → `af edit <id> --section N --paragraph M --command "<cmd>"`.
3. `section N <cmd>` → `af edit <id> --section N --command "<cmd>"` (paragraph_index null).
4. `全文 <cmd>` → advanced: iterate `af edit <id> --section N --command "<cmd>"` for every N in the draft. Warn the user this is a destructive bulk edit before running.
5. `regen` → ask for new title/opening/closing indices, then call `af fill <id> --title X --opening Y --closing Z --json`.
6. `show` → `Read ~/.agentflow/drafts/<article_id>/draft.md` and render.
7. `done` → exit loop → go to Step 4.

### Preset commands

The CLI recognises these 5 preset Chinese commands. Anything else is free-form:

- `改短` — shrink to ~60% length, keep core argument.
- `展开` — grow to ~1.5x, add details / examples.
- `改锋利` — sharpen tone, drop hedging.
- `加例子` — add a concrete, preferably real-world example.
- `去AI味` — strip AI slop phrases ("让我们", "值得注意的是", "综上所述").

Free-form example: `section 2 --command "把那段关于 subagent 的论点改成问答体"` — just pass the string.

### After each edit

Re-`Read ~/.agentflow/drafts/<article_id>/draft.md` (or parse the JSON returned by `af edit --json`) and show the user **only the edited section**, with old word count → new word count. Do not re-dump the whole draft.

## Step 3.5 — propose images (new, optional but default-on)

After the edit loop ends (`done`), ask:

> "Run image proposal? (yes — recommended / skip)"

If yes:

```bash
PYTHONPATH=. af propose-images <article_id> --json
```

This reads the full draft + hotspot `source_references` and has the LLM
propose image placements. It:

- Inserts `[IMAGE: <zh description>]` placeholders into `draft.md` at
  anchors like `after_opening` / `before_section:N` / `middle_of_section:N` /
  `before_closing`.
- Saves the raw LLM output to `~/.agentflow/drafts/<article_id>/image_proposals.json`
  (including `rationale`, `role`, `style_hint`, `priority` — data that isn't
  needed at publish time but is useful for review / auto-resolve).
- Writes an `images_proposed` memory event.
- Re-syncs `metadata.json::image_placeholders` so `af draft-show` picks up the
  new ones.

Show the user something like:

```
[1] after_opening    cover      "subagent 架构示意图"  (priority: required)
[2] before_section:1 inline     "上下文切换代价对比图" (priority: recommended)
[3] before_closing   inline     "subagent 边界清单"    (priority: optional)
```

(Read these from `image_proposals.json::raw.images` — it has position, role,
description.zh, and priority.)

Then ask: "Accept all / reject specific indices / regenerate all?"

- **Accept all** → proceed to Step 4 (image-resolve) with the freshly-inserted
  placeholders.
- **Reject [N]** → manually edit `draft.md` to remove that `[IMAGE: ...]` line,
  then `Read` it again.
- **Regenerate** → re-run `af propose-images <article_id> --json`. It
  overwrites both `draft.md` and `image_proposals.json`.

If the user says `skip`, proceed to Step 4 with whatever placeholders D2
already inserted during fill (usually 0 in v0.5).

## Step 4 — resolve images

When the user says `done` (or after Step 3.5):

```bash
PYTHONPATH=. af draft-show <article_id> --json
```

From `image_placeholders[]`, filter where `resolved_path` is null. If any:

```
Unresolved image placeholders:
  1. <id> — <description> (section: <section_heading>)
  2. ...
Paste a local file path for each, or say 'skip' to force-strip at publish time.
```

For each path provided:

```bash
PYTHONPATH=. af image-resolve <article_id> <placeholder_id> <absolute_path>
```

The command echoes `{"ok": true, "remaining_unresolved": N}`.

## Step 5 — hand off

Once images are handled (or the user chose 'skip'), say:

> "Draft `<article_id>` is ready. Run `/agentflow-publish <article_id>` (add `--force-strip-images` if you skipped image resolution)."

## Error handling

- `af write` fails with "Hotspot not found" → user probably typo'd, or the relevant archive was not created in this `AGENTFLOW_HOME`; re-run `af hotspots` / `af search`, then check both `~/.agentflow/hotspots/` and `~/.agentflow/search_results/`.
- `af edit` fails with IndexError → section or paragraph is out of range; `af draft-show` to see real indices.
- Any non-zero exit: `tail -n 20 ~/.agentflow/logs/agentflow.log` and show.

## State side-effects

Every run of `af write` creates a new `article_id`. If the user aborts mid-flow, old drafts stay under `~/.agentflow/drafts/<id>/` — safe but clutters the queue. You may mention this once.

Memory events written by this flow: `article_created`, `fill_choices`, `section_edit`, `images_proposed`, `image_resolved`.

## NEVER

- Never call `agentflow.agent_d2.*` Python modules directly. Only via `af`.
- Never modify `draft.md` or `metadata.json` by hand — always go through `af edit` / `af fill`.
- Never invoke another skill via the Skill tool mid-loop; just continue the conversation.

## MOCK_LLM

`MOCK_LLM=true` makes `af write --auto-pick` deterministic (same 5 sections, same 5 image placeholders) — useful for testing skills. Drop it for real writing.
