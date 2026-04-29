---
name: agentflow-hotspots
description: |
  Scan trending topics, present them, and help the user pick one hotspot + angle.

  TRIGGER: "/agentflow-hotspots", "af hotspots", "热点", "topic discovery", "Gate A", "选题", "today's topic".

  SKIP for: modifying the hotspots scoring algorithm (D1 internals are not skill orchestration); editing publisher_account.keyword_groups (topic_profile domain).
---

# agentflow-hotspots — pick today's topic

Wraps `af hotspots` and `af hotspot-show`. Goal: leave the user with one `<hotspot_id>` + `<angle_index>` chosen, then hand off to `agentflow-write`.

## Step 0 — detect a topic intent in the user's message

Before running a scan, inspect how the user invoked the skill:

- **Broad scan**: "跑一遍热点 / 今天有什么 / hotspots / scan" → no topic intent. Skip to Step 1 with no filter.
- **Topic-targeted**: "帮我找 X / 有没有关于 X 的 / 最近 Y 怎么说 / `--filter X`" → extract the topic phrase. Use it as the `--filter` regex in Step 1.

If the topic is multi-word or has OR alternatives, build a regex: `MCP server` → `"MCP|server|agent"`; `量子计算和 AI` → `"量子|AI"`. Err on the side of **broader** regex — too narrow returns 0 hotspots.

## Step 0b — choose the topic profile id

Before profile-aware scans, resolve the profile id:

- If the user passed `--profile <id>`, use it.
- Else if the active TopicIntent in `~/.agentflow/intents/current.yaml` contains a profile id, use that.
- Else if `AGENTFLOW_DEFAULT_TOPIC_PROFILE` is configured, use that.
- Else ask the user which profile id to use. Do not silently reuse an old profile from another bot or project.

## Step 1 — scan

Run the scan. It takes ~30s with the real LLM, <1s under `MOCK_LLM=true`.
Before the scan, make sure the active topic profile is at least minimally usable. If `af topic-profile show --profile <id> --json` reports missing `publisher_account.brand`, `publisher_account.voice`, `publisher_account.output_language`, `publisher_account.product_facts`, `keyword_groups.core`, or `search_queries`, start the profile setup/update path before treating the scan as production-quality.

**Broad scan**:

```bash
cd /Users/witness/Desktop/experimental/medium\&blog_posting_agent/agentflow-article-publishing/backend
source .venv/bin/activate
PYTHONPATH=. af hotspots --profile <id> --json
```

**Topic-targeted scan** (adds `--filter`):

```bash
PYTHONPATH=. af hotspots --profile <id> --filter "MCP|agent orchestration" --json
```

The filter is a case-insensitive regex matched against each hotspot's `topic_one_liner`, suggested angle titles, and source reference snippets. Result's `filter.matched / filter.total` tells you how narrow it got. `filter.filtered_out_preview` gives a bounded preview of what was excluded, and `filter.boundary.level` tells you whether the filter is `too_narrow`, `narrow`, `balanced`, or `broad`.

If the scan or TG panel surfaces `Config Suggestions`, review them through the CLI contract:

```bash
af topic-profile review <suggestion_id> --json
af topic-profile apply <suggestion_id> --json
```

If `matched == 0`, tell the user: "0 matched out of N. Widen the query?" — don't silently fall back to unfiltered results.

Capture stdout. If the JSON is large, redirect to a temp file and `Read` it:

```bash
PYTHONPATH=. af hotspots --json > /tmp/af_hotspots.json
```

Then `Read /tmp/af_hotspots.json`.

## Step 2 — present a numbered list

From `hotspots[]` in the JSON, print:

```
[1] <topic_one_liner> — series:<recommended_series> freshness:<freshness_score>
    overlooked: <overlooked_angles[0]>
[2] ...
```

Keep it to the top 5–8 entries. Don't dump every field.

If `filter` is present, also surface a compact filter panel:

```text
Filter: <pattern>
Boundary: <level> (<matched>/<total>)
Filtered out preview:
  - <topic_one_liner>
  - <topic_one_liner>
```

Only show `filtered_out_preview` when the user asks, or when `boundary.level` is
`too_narrow` / `narrow`.

## Step 3 — prompt for a pick

Ask literally: "Pick a number (1–N), 'write all', or 'skip' to stop."

- Number → proceed to Step 4.
- "write all" → loop Step 4 for each, producing a queue of `(hotspot_id, angle_index)` pairs. Advise this only makes sense with MOCK_LLM.
- "skip" → stop cleanly, do not mutate state.

## Step 4 — show angles for the chosen hotspot

Pull the hotspot id from the list. Run:

```bash
PYTHONPATH=. af hotspot-show <hotspot_id> --json
```

From `suggested_angles[]`, enumerate:

```
Angle [0] <angle> — depth:<depth> difficulty:<difficulty>
         fit: <fit_explanation>
Angle [1] ...
```

Ask: "Which angle index? (default 0)"

## Step 5 — hand off

Once you have `<hotspot_id>` + `<angle_index>`, say:

> "Run `/agentflow-write <hotspot_id> --angle <N>` — or say 'yes' and I'll kick it off now."

If the user says "yes" / "go" / "do it", internally transition into the `agentflow-write` flow without asking again. Do NOT actually invoke another skill via the Skill tool mid-chain — just follow the write workflow from memory starting at the "auto or manual?" prompt.

## Error handling

- If no hotspots file exists yet (first run), `af hotspots` will create one. If it returns an empty list, tell the user to check `~/.agentflow/sources.yaml` and `logs/agentflow.log`.
- On non-zero exit: `tail -n 20 ~/.agentflow/logs/agentflow.log` and surface to user.

## State side-effects

`af hotspots` writes `~/.agentflow/hotspots/<YYYY-MM-DD>.json`. Running it twice in the same day overwrites the daily batch. Search-result archives live separately under `~/.agentflow/search_results/` so later AI runs can trace historical recall inputs. Hotspot scans may also surface profile config suggestions; review/apply those through `af topic-profile` or `/suggestions` rather than editing YAML directly.

## Downstream contract: source_references

Each hotspot record carries a `source_references[]` array (author / URL / text_snippet / engagement). These are the raw signals that led to the cluster. `agentflow-publish` Step 1b will surface the top 3-5 of them at publish time so the user can check whether the generated article actually reflects what the sources said — don't strip them from the hotspot file. If you see a hotspot with 0 references, the downstream hallucination check won't work; warn the user.

## NEVER

- Never import agentflow modules directly. Use the CLI.
- Never pick an angle for the user silently — always surface the options.
- Never proceed if `suggested_angles` is empty for the picked hotspot; re-scan or pick another hotspot.

## MOCK_LLM

`MOCK_LLM=true af hotspots --json` returns 5 deterministic fixture hotspots. Good for skill demos.
