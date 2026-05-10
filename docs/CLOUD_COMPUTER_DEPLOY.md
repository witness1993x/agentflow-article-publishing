# Cloud-Computer Deploy Runbook (Agent-Lark Window Mode)

**Target audience**: operator on a managed cloud computer (e.g. 华为云电脑 / 阿里无影 / 移动云电脑) where:

- Python 3.11+ binary exists, but `apt install` requires sudo you don't have
- No HTTP listener can be exposed to the public internet
- An agent harness (OpenClaw / Cursor / Claude Code) is already running with a mounted Lark window — i.e. the agent can already send Lark messages directly via its Lark MCP / `openclaw-lark` plugin

**Result after this runbook**: AgentFlow review-daemon running locally; review cards flow into the Lark group via the agent's existing Lark capability; no inbound HTTP, no `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL`.

---

## 0. Prerequisites

Confirm on the cloud computer:

```bash
python3.11 --version          # ≥ 3.11 (3.12 also fine)
which python3.11              # /usr/bin/python3.11 or similar
curl https://pypi.org/        # outbound HTTPS to PyPI works
```

If Python is missing entirely, see ["Portable Python"](#a-portable-python-fallback) at the bottom.

## 1. Install pip without sudo

```bash
curl -LO https://bootstrap.pypa.io/get-pip.py
python3.11 get-pip.py --user --no-warn-script-location
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
pip --version    # → pip X.Y from /home/<you>/.local/...
```

## 2. Install AgentFlow

```bash
# Drop the deploy tarball anywhere user-writable; here we use ~/blogflow.
mkdir -p ~/blogflow && cd ~/blogflow
# (scp / curl / wget / etc. — drop the file from your laptop or release artifact storage)
tar xzf blogflow-lark-deploy-v1.3.x.tar.gz
cd blogflow-deploy/backend

# --user install — no venv needed; bypasses ensurepip / python3.11-venv requirements.
python3.11 -m pip install --user -e .

blogflow --version       # confirm CLI on PATH
```

If `blogflow` isn't on PATH after install, `~/.local/bin` may not be exported — see step 1 again.

## 3. Configure `.env`

```bash
cp ~/blogflow/blogflow-deploy/.env.template ~/blogflow/blogflow-deploy/backend/.env
nano ~/blogflow/blogflow-deploy/backend/.env
```

Minimum required keys for Agent-Lark Window mode:

```ini
# === Required ===
AGENTFLOW_LARK_APP_PRIMARY=true

# === Optional but recommended ===
# Anthropic / OpenAI / Moonshot — at least one LLM provider must be set
ANTHROPIC_API_KEY=...
# AtlasCloud for image-gate
ATLASCLOUD_API_KEY=...
# Jina or OpenAI for embeddings (hotspots)
JINA_API_KEY=...
# Ghost / Medium / Twitter / LinkedIn — only for the channels you publish to

# === Explicitly NOT set (Phase 3 removed TG; v1.3.2 added file-queue mode) ===
# TELEGRAM_BOT_TOKEN=
# AGENTFLOW_AGENT_EVENT_WEBHOOK_URL=    ← leave empty; daemon defaults to file queue
```

## 4. Verify with `blogflow doctor`

```bash
blogflow doctor
```

Look for:

```
✓ Lark App primary       review.*_card events enabled (file queue → ~/.agentflow/agent_events/queue.jsonl); ...
✓ Moonshot Kimi          (or Anthropic — at least one LLM)
✓ AtlasCloud (image)
✓ Jina embeddings        (or OpenAI)

Readiness gates
✓ review-daemon
✓ hotspots / write / fill
✓ image-gate
```

If `Lark App primary` is ✗, re-check `AGENTFLOW_LARK_APP_PRIMARY=true` in `.env` and `blogflow keys-where AGENTFLOW_LARK_APP_PRIMARY` to confirm which file the value loaded from.

## 5. Smoke-test the file queue

```bash
# Emit a synthetic Gate A card into the queue.
blogflow agent-events-emit-test
```

Expected output:

```
emit mode:           file
queue file:          /home/<you>/.agentflow/agent_events/queue.jsonl
queue grew by:       686 bytes (one envelope appended)
✓ file-queue path verified
```

Inspect the envelope:

```bash
blogflow agent-events-tail --from-start | head -1 | python3.11 -m json.tool
```

You should see a JSON envelope with `event_type: review.gate_a_card`, `article_id: hs_smoke_test_001`, and a `payload.smoke_test: true` marker.

## 6. Start the daemon

Foreground (for first-run / debugging):

```bash
blogflow review-daemon
# Ctrl+C to stop.
```

Background (with persistence across SSH disconnects):

```bash
nohup blogflow review-daemon > ~/blogflow/daemon.log 2>&1 &
echo $! > ~/blogflow/daemon.pid
tail -f ~/blogflow/daemon.log
```

Or with systemd-user (no root needed):

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/blogflow-review.service <<'EOF'
[Unit]
Description=AgentFlow review daemon (Lark-only)
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/blogflow review-daemon
WorkingDirectory=%h/blogflow/blogflow-deploy/backend
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now blogflow-review
systemctl --user status blogflow-review
journalctl --user -u blogflow-review -f
```

## 7. Wire up the skill-agent side

The OpenClaw / Cursor / Claude Code agent already loaded with the `agentflow-open-claw-v2` skill needs to do **two things on a loop**:

1. **Tail the queue** — read each new line from `~/.agentflow/agent_events/queue.jsonl`, render per `references/lark_review_cards.md`, push to Lark via the mounted Lark window.
2. **Forward Lark button clicks** — when an operator clicks a card button, shell out to `blogflow lark-cli-emit --command lark_<action> --article-id <aid> --operator-open-id <ou_xxx> --payload '<json>'`.

The full recipe (with Python pseudocode tail loop + cursor handling) is in `SKILL.md` §"模式 A: Agent-Lark Window". Skill agents that follow that section will do this automatically.

**Verify end-to-end**:

1. Already ran `blogflow agent-events-emit-test` in step 5 → queue has one envelope.
2. The skill agent's tail should pick it up within ~2 seconds.
3. A Gate A card should appear in your Lark group with `Smoke test candidate — please ignore`.
4. If you click any button, the agent should run `blogflow lark-cli-emit ...` and return a JSON result; daemon log should record the action.

If a card doesn't appear:

- Check `blogflow agent-events-tail --from-start | wc -l` — if 0, daemon isn't writing (start it).
- Check the agent's session log — is it tailing the queue at all? Did it lose the cursor?
- Check the Lark MCP / mounted window — can the agent send any Lark message right now? (`@bot ping` is the cheapest probe.)

## 8. Day-2 operations

```bash
blogflow doctor                           # any time: full env / readiness matrix
blogflow review-list                      # active review queue
blogflow agent-events-tail -f             # see daemon → agent fan-out live
journalctl --user -u blogflow-review -f   # daemon logs
```

Upgrade: `tar xzf blogflow-lark-deploy-v1.3.<n>.tar.gz` over the same dir, then `pip install --user -e .` again, then `systemctl --user restart blogflow-review`.

---

## Appendix

### A. Portable Python fallback

If even Python 3.11 is missing, drop in a `python-build-standalone` tarball:

```bash
cd ~ && mkdir agentflow-runtime && cd agentflow-runtime
curl -LO https://github.com/indygreg/python-build-standalone/releases/download/20240415/cpython-3.12.3+20240415-x86_64-unknown-linux-gnu-install_only.tar.gz
tar xzf cpython-3.12.3+20240415-*.tar.gz
export PATH="$HOME/agentflow-runtime/python/bin:$PATH"
python3 --version    # 3.12.3
```

Add the export line to `~/.bashrc`. Then resume from step 1, replacing `python3.11` with `python3`.

### B. Air-gapped install (no PyPI access)

On a connected machine with the same Python version:

```bash
cd /path/to/blogflow-deploy/backend
python3.11 -m pip download . -d offline-wheels/
tar czf offline-deploy.tar.gz offline-wheels/ get-pip.py
```

On the cloud computer:

```bash
tar xzf offline-deploy.tar.gz
python3.11 get-pip.py --user --no-index --find-links offline-wheels
~/.local/bin/pip install --user --no-index --find-links offline-wheels -e blogflow-deploy/backend
```

### C. Forced webhook mode (legacy / dual-write)

To run BOTH file queue and a webhook (e.g. while migrating an existing OpenClaw HTTP listener):

```ini
AGENTFLOW_AGENT_EVENT_MODE=both
AGENTFLOW_AGENT_EVENT_WEBHOOK_URL=https://your-openclaw.example.com/agentflow/events
AGENTFLOW_AGENT_EVENT_AUTH_HEADER=Bearer <token>
```

`blogflow doctor` will show `agent_event_mode: both` in the extras.
