---
name: agentflow-style
description: |
  Teach or refresh the AgentFlow voice profile from sample past articles.

  TRIGGER: "/agentflow-style", "learn-style", "learn-from-handle", "style profile", "voice profile", "samples", "学风格".

  SKIP for: inspecting / editing an existing style_profile.yaml directly (raw yaml editing is out of skill scope); non-D0 topics.
---

# agentflow-style — learn or refresh the voice profile

Wraps the `af learn-style` command. Use when the user wants to (re)build the style profile at `~/.agentflow/style_profile.yaml` from their own past articles.

## Decide the mode from the user's message

1. If the user provided file paths, URLs, or a directory in their invocation — use them directly.
2. If the user said "show the current profile" — go to `--show`.
3. If the user said "recompute from corpus" — go to `--recompute`.
4. If the user said "re-ingest my published drafts" — go to `--from-published`.
5. Otherwise ask: "Paste paths or URLs of 3–5 past articles (one per line), or say 'show' / 'recompute' / 'from-published'."

## How to invoke

From the project root (the venv is at `backend/.venv/`):

```bash
cd /Users/witness/Desktop/experimental/medium\&blog_posting_agent/agentflow-article-publishing/backend
source .venv/bin/activate
PYTHONPATH=. af learn-style --file /path/to/a.md --file /path/to/b.docx --url https://example.com/post
```

Other modes:

```bash
PYTHONPATH=. af learn-style --dir /path/to/drafts_folder
PYTHONPATH=. af learn-style --show
PYTHONPATH=. af learn-style --recompute
PYTHONPATH=. af learn-style --from-published
```

Tip: `--file` and `--url` can repeat. Mix freely in one call.

## After the command succeeds

Run `PYTHONPATH=. af learn-style --show` and summarise in 6–10 lines, pulling from the YAML output:

- `voice_principles` — 3 bullets.
- `taboos` — the 3–5 words/phrases the user most wants to avoid.
- `paragraph_preferences` — typical min/max paragraph length.
- Number of corpus entries.
- Path to the saved profile (`~/.agentflow/style_profile.yaml`).

Don't dump the raw YAML — distil.

## Advanced options to mention (once)

- `--recompute` — re-aggregate the profile from the full `~/.agentflow/style_corpus/` without re-ingesting new sources.
- `--from-published` — ingest every draft marked as published (useful once you've shipped a few articles via AgentFlow).

## Error handling

If `af learn-style` exits non-zero:

```bash
tail -n 20 ~/.agentflow/logs/agentflow.log
```

Show the tail to the user and stop — do not retry automatically; the user usually needs to adjust paths.

## Hand-off

After the profile is updated, suggest: "Run `/agentflow-hotspots` to scan for today's topics, or `/agentflow-write <hotspot_id>` if you already have one."

## MOCK_LLM note

`MOCK_LLM=true af learn-style --file ...` ingests real files but routes LLM calls (per-article analyses + aggregation) through fixtures. Without `MOCK_LLM=true`, each new source triggers a real Moonshot/Claude analysis, then a final aggregation pass. Real-key run on 2 samples ≈ 20-30s.

## NEVER

- Never import `agentflow.agent_d0` directly. Use `af learn-style`.
- Never write to `~/.agentflow/style_profile.yaml` by hand.
