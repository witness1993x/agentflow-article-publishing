# AgentFlow skills for Claude Code

Self-contained skills that let a user drive the AgentFlow workflow from inside a Claude Code session.

## Contents

- `agentflow/` — top-level guide. Explains profile/bootstrap, hotspots, writing, and publishing.
- `agentflow-style/` — wraps `af learn-style` to teach/refresh the voice profile.
- `agentflow-hotspots/` — wraps `af hotspots` + `af hotspot-show` to pick today's topic.
- `agentflow-write/` — wraps `af write` / `af fill` / `af edit` / `af image-resolve` for the full writing loop.
- `agentflow-publish/` — wraps `af preview` + `af publish`, including Medium manual package fallback.
- `agentflow-tweet/` — Twitter/X short-form distribution helpers.
- `agentflow-newsletter/` — newsletter distribution helpers.

## Install

Option A (symlink — follows repo updates):

```bash
ln -s "$(pwd)/.claude/skills/agentflow"           ~/.claude/skills/agentflow
ln -s "$(pwd)/.claude/skills/agentflow-style"     ~/.claude/skills/agentflow-style
ln -s "$(pwd)/.claude/skills/agentflow-hotspots"  ~/.claude/skills/agentflow-hotspots
ln -s "$(pwd)/.claude/skills/agentflow-write"     ~/.claude/skills/agentflow-write
ln -s "$(pwd)/.claude/skills/agentflow-publish"   ~/.claude/skills/agentflow-publish
ln -s "$(pwd)/.claude/skills/agentflow-tweet"     ~/.claude/skills/agentflow-tweet
ln -s "$(pwd)/.claude/skills/agentflow-newsletter" ~/.claude/skills/agentflow-newsletter
```

Option B (copy — snapshots the current version):

```bash
cp -R .claude/skills/agentflow*         ~/.claude/skills/
```

Option C (run from project root) — Claude Code auto-registers skills under `.claude/skills/` when its cwd is at or below the project root. No install step; just launch Claude Code here.

## Invoke

From a Claude Code session, use either:

- The Skill tool, with `skill: "agentflow"` / `"agentflow-style"` / etc.
- Slash commands — `/agentflow`, `/agentflow-style`, `/agentflow-hotspots`, `/agentflow-write <hotspot_id>`, `/agentflow-publish <article_id>` — if auto-registration is enabled.

## Daily flow

```
    first run / drift check
            │
            ▼
  account config + topic profile
  (doctor, topic-profile show/init/update,
   suggestions review/apply)
            │
            ▼
          once a week                        daily
            │                                  │
            ▼                                  ▼
    /agentflow-style            /agentflow-hotspots
            │                                  │
            │                                  ▼
            │                 (user picks hotspot_id + angle)
            │                                  │
            │                                  ▼
            │                /agentflow-write <hotspot_id>
            │                                  │
            │                   (auto or manual skeleton,
            │                    then edit loop, then images)
            │                                  │
            │                                  ▼
            │                /agentflow-publish <article_id>
            │                                  │
            │                                  ▼
            │                           (report URLs)
            ▼
   updated style_profile.yaml
```

## Prerequisites

- Python venv at `backend/.venv/` with the `af` launcher installed (`.venv/bin/af`).
- All skills assume the project lives at `<repo_root>/` and the venv at `<repo_root>/backend/.venv/`.
- For mock-friendly demos: `export MOCK_LLM=true` before running.
- `topic_profiles.yaml` is the source of truth for publisher/topic constraints; use `af topic-profile` commands rather than editing it directly.

## Shared rules (enforced by every skill)

- Use the `af` CLI. Never import `agentflow.*` Python modules directly.
- Prefer `--json` output; read JSON inline for small payloads, via the `Read` tool for large ones.
- On any non-zero `af` exit, tail `~/.agentflow/logs/agentflow.log` and surface the last 20 lines.
