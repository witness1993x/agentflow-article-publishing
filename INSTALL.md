# Install AgentFlow

> Snapshot: 2026-04-30
> Target: Python 3.11+, Claude Code or Cursor as the harness (optional but recommended)
> Runtime version: see [`backend/pyproject.toml`](backend/pyproject.toml); release notes in [`CHANGELOG.md`](CHANGELOG.md).

This document is **agent-driven**. It tells a Claude Code / Cursor / OpenClaw harness how to walk the install path on your behalf without asking you to type anything beyond credentials. A human can read it top-to-bottom too — every step is also doable manually.

## TL;DR — the agent loop

After cloning the runtime repo and creating the venv, the agent calls one command in a loop until it returns `stage: "ready"`:

```bash
af bootstrap --next-step --json
```

Each call returns JSON like:

```json
{
  "current_state": "missing_profile",
  "next_command": "af topic-profile init -i --profile <name>",
  "reason": "~/.agentflow/topic_profiles.yaml does not exist",
  "stage": "init",
  "mode": "harness"
}
```

The agent executes `next_command`, then re-runs `af bootstrap --next-step --json`. Repeat until `current_state == "ready"`. That's the whole self-deploy contract.

## Mode A vs Mode B/C — auto-detected

| Mode | When | Daemon required? |
|---|---|---|
| **A — harness-only** | `TELEGRAM_BOT_TOKEN` not set anywhere | No — Claude Code / Cursor drives `af` directly. Approve/reject via reply messages in the chat session. |
| **B — TG-review only** | `TELEGRAM_BOT_TOKEN` set | Yes — `af review-daemon` long-polls Telegram so cards land on your phone. |
| **C — hybrid** | Same as B | Yes — daemon mirrors gates to Telegram while harness still drives the pipeline. |

The bootstrap detector reads `TELEGRAM_BOT_TOKEN` from `~/.agentflow/secrets/.env` (or `~/.agentflow/secrets/telegram.env`, or process env, or `backend/.env`) and infers the mode. **You do not pick the mode explicitly** — your credentials choice picks it.

## Step 0 — clone + venv (one-time)

```bash
git clone https://github.com/witness1993x/agentflow-article-publishing.git
cd agentflow-article-publishing/backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Verify the CLI is on `$PATH`:

```bash
af --version          # should print "af, version <pyproject version>"
```

## Step 1 — start the agent loop

From the harness session (Claude Code, Cursor, etc.) or from a plain shell:

```bash
af bootstrap --next-step --json
```

The agent dispatches on `current_state`. Reference table for all states:

| `current_state` | Meaning | `next_command` (canonical) | Stage |
|---|---|---|---|
| `no_env` | No `~/.agentflow/secrets/.env` and no legacy `backend/.env` | `af bootstrap` (seeds from `backend/.env.template`) | `init` |
| `unknown` | I/O error reading the env file | `af doctor` (then re-run bootstrap) | `init` |
| `skills_not_installed` | Neither `~/.claude/skills/` nor `~/.cursor/skills/` populated | `af skill-install` | `init` |
| `missing_real_keys` | `LLM_PROVIDER` set + `MOCK_LLM != true` + provider key empty | `af onboard --section <provider>` | `init` |
| `missing_profile` | No `~/.agentflow/topic_profiles.yaml`, or no profile inside | `af topic-profile init -i --profile <name>` (or `--from-file`) | `init` |
| `incomplete_profile` | Profile lacks brand/voice/do/dont/product_facts/keyword_groups | `af topic-profile init -i --profile <id>` or `af topic-profile derive --profile <id>` | `init` |
| `missing_chat_id` (B/C only) | `TELEGRAM_BOT_TOKEN` set but bot hasn't seen a `/start` yet | Send `/start` to your bot in Telegram (auto-captures `chat_id`) | `init` |
| `daemon_not_running` (B/C only) | Heartbeat missing or > 5min stale | `af review-daemon &` (or systemd / launchd) | `init` |
| `ready` | All checks pass | `af hotspots --gate-a-top-k 3` | `ready` |

Mode A operators (no `TELEGRAM_BOT_TOKEN`) skip the `missing_chat_id` and `daemon_not_running` states entirely. They land on `ready` straight from `incomplete_profile` resolution. The `ready` payload still surfaces `optional_next` describing how to upgrade to Mode B later.

### Credentials — `af onboard` writes here

Secrets live at `~/.agentflow/secrets/.env` (catch-all) and per-service files at `~/.agentflow/secrets/<service>.env`:

```
~/.agentflow/secrets/
├── .env                  # catch-all (operator-friendly single-file)
├── telegram.env          # only Mode B/C operators need this
├── atlascloud.env        # image generation
├── ghost.env             # publishing
├── moonshot.env          # primary LLM
├── jina.env              # embeddings (D1 clustering)
└── <service>.env         # see `af keys-where` for full list
```

Use the wizard to write them — never hand-edit unless you know what you're doing:

```bash
af onboard                      # full guided wizard (interactive)
af onboard --section moonshot   # just one section
af onboard --section telegram   # for Mode B/C operators
```

To inspect what's loaded and where:

```bash
af keys-where        # precedence + per-var source map
af keys-show         # masked values + source paths
af keys-edit ghost   # opens $EDITOR on ~/.agentflow/secrets/ghost.env
```

`backend/.env` still works as a back-compat fallback for installs predating v1.0.4 — but new installs should use `~/.agentflow/secrets/`.

### Mock-mode quickstart

Want to drive a full pipeline without any real API keys? One command:

```bash
af bootstrap --mock --first-run    # writes MOCK_LLM=true, runs the agent loop
```

After that, `af hotspots --json | af write … --auto-pick --json | af preview … | af publish --force-strip-images` all run end-to-end against deterministic fixtures. Nothing leaves your machine.

## Step 2 — operate

Once `current_state == "ready"`:

```bash
af hotspots --gate-a-top-k 3        # D1: today's topics
af write <hotspot_id> --auto-pick   # D2: skeleton + fill
af preview <article_id>             # D3: platform-adapted versions
af publish <article_id>             # D4: real publish (or mock)
```

The harness usually orchestrates this via the public skill distribution (`witness1993x/agentflow-skills`). See `.claude/skills/agentflow*/SKILL.md` once installed via `af skill-install`.

## Linux VM deploy (Mode B / C / production)

If you want the daemon running 24/7 (Mode B/C), use the deploy bundle:

```bash
# On your laptop
bash scripts/build_deploy_bundle.sh ~/Desktop/agentflow-deploy.tar.gz
scp ~/Desktop/agentflow-deploy.tar.gz user@vm:/tmp/

# On the VM
ssh user@vm
sudo tar -xzf /tmp/agentflow-deploy.tar.gz -C /opt/
sudo bash /opt/agentflow-deploy/deploy.sh
sudo -u agentflow /opt/agentflow/backend/.venv/bin/af onboard
sudo systemctl restart agentflow-review
```

The deploy bundle excludes any local `.env` / `env_config*` / `*.key` / `*.pem` (sanity-guarded since v1.0.4); you fill `.env` on the VM via `af onboard`, not by SCP'ing it.

See [`agentflow-deploy/INSTALL_LINUX.md`](agentflow-deploy/INSTALL_LINUX.md) for the systemd unit details.

## Where things live

| Path | Owner | Purpose |
|---|---|---|
| `~/.agentflow/secrets/.env` | operator | API keys, tokens, env-var-style config |
| `~/.agentflow/secrets/<service>.env` | operator | Per-service slice (overrides catch-all) |
| `~/.agentflow/topic_profiles.yaml` | operator | Brand / voice / sources / keywords |
| `~/.agentflow/style_profile.yaml` | `af learn-style` | Voice fingerprint from your past articles |
| `~/.agentflow/hotspots/<date>.json` | `af hotspots` | Daily D1 scan output |
| `~/.agentflow/drafts/<article_id>/` | `af write/fill/edit` | Skeleton, draft.md, metadata, platform_versions |
| `~/.agentflow/memory/events.jsonl` | every CLI mutation | Append-only event stream |
| `~/.agentflow/review/` | review daemon | Heartbeat, pending-edit state, short-id index |
| `backend/.env.template` | repo | Schema reference for the secrets file |
| `backend/.env` | (legacy) | Fallback if `~/.agentflow/secrets/.env` doesn't exist yet |

## Troubleshooting

- **`af` not found** → `source backend/.venv/bin/activate` (or add the venv's bin to `$PATH`).
- **`agentflow.*` import errors** → run from `backend/` with `PYTHONPATH=.` set, or just `pip install -e .` again.
- **`af bootstrap --next-step --json` returns `current_state: unknown`** → the env file exists but can't be read; check `ls -la ~/.agentflow/secrets/.env` for permissions.
- **Bootstrap loop never reaches `ready`** → run `af doctor` to see which credential the loop is stuck on.
- **`af doctor` shows source `backend/.env` instead of `~/.agentflow/secrets/.env`** → run `af bootstrap` (or `af onboard`); they migrate the legacy file on first invocation.
- **Anything else** → tail `~/.agentflow/logs/agentflow.log`.
