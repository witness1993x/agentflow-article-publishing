# Install AgentFlow (外部测试版)

> Snapshot date: 2026-04-28
> Target: Claude Code + Python 3.11+

## ⚡ 5-Minute Mock Quickstart

For a fast end-to-end mock run (no real keys needed):

```bash
# 1. install backend
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# 2. install skills
af skill-install

# 3. mock setup (no api keys needed)
echo 'MOCK_LLM=true' > .env

# 4. mock smoke
af hotspots --json | tee /tmp/h.json
HID=$(python -c 'import json; print(json.load(open("/tmp/h.json"))["hotspots"][0]["id"])')
af write "$HID" --auto-pick --json | tee /tmp/a.json
AID=$(python -c 'import json; print(json.load(open("/tmp/a.json"))["article_id"])')
af preview "$AID" --json >/dev/null
af publish "$AID" --force-strip-images --json
af memory-tail --limit 5 --json
```

完整 real-key 安装见下面。

## What's in this bundle

```
agentflow-article-publishing/
├── backend/                    # Python source + af CLI
│   ├── agentflow/              # 5 agents (D0-D4) + shared/cli/config
│   ├── prompts/                # 7 prompt templates
│   ├── requirements.txt
│   ├── pyproject.toml
│   └── .env.template           # copy to .env and fill keys
├── .claude/skills/             # 7 Claude Code skills
│   ├── agentflow/
│   ├── agentflow-style/
│   ├── agentflow-hotspots/
│   ├── agentflow-write/
│   ├── agentflow-publish/
│   ├── agentflow-tweet/
│   └── agentflow-newsletter/
├── config-examples/            # seed configs → ~/.agentflow/
├── docs/                       # PRD / SOLUTION / backlog MEMOs
├── samples/README.md           # (put your 3-5 past articles here)
├── scripts/
├── README.md
├── CCREVIEW_HANDOFF.md
└── _legacy/api + _legacy/tests # historical Next.js+FastAPI refs (no node_modules)
```

Excluded from this bundle (rebuild locally):
- `backend/.venv/` — platform-specific Python venv
- `backend/.env` — your real API keys
- `_legacy/frontend/node_modules/` — 868MB, not needed
- `__pycache__/`, `.DS_Store`
- `samples/*.docx`, `*.pdf` — previous author's private articles

## Install steps

### 1. Unpack

```bash
tar -xzf agentflow-bundle-2026-04-28.tar.gz
cd agentflow-article-publishing
```

### 2. Python venv + deps

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .          # reads pyproject.toml, registers `af` CLI
# or: pip install -r requirements.txt
```

Verify:

```bash
af --help
af --version    # 0.1.0
```

### 3. Install Claude Code skill harness

One command symlinks all 7 skills into your Claude Code skills dir:

```bash
af skill-install                # default target: ~/.claude/skills
# af skill-install --cursor     # also install into Cursor's skills dir
# af skill-install --target /custom/path
```

This replaces the old 7-line manual `ln -s` loop. The command is idempotent —
re-running it just refreshes the symlinks.

<details>
<summary>Fallback / advanced — manual symlink</summary>

If `af skill-install` is unavailable (older builds) or you want fine control,
you can still link skills by hand:

```bash
cd ..    # project root
for s in agentflow agentflow-style agentflow-hotspots agentflow-write agentflow-publish agentflow-tweet agentflow-newsletter; do
  ln -sf "$(pwd)/.claude/skills/$s" "$HOME/.claude/skills/$s"
done
```

Or just launch Claude Code with this project as cwd; skills under
`.claude/skills/` auto-register for the session.

</details>

### 4. Configure .env

```bash
cp .env.template .env
# Edit .env — at minimum:
#   MOCK_LLM=true           (default; leave true for dry runs)
# For real-key runs you'll also want:
#   MOONSHOT_API_KEY=...    (generation; Kimi K2.6)
#   JINA_API_KEY=...        (embeddings for D1 clustering)
#   TWITTER_BEARER_TOKEN=...(optional; D1 falls back to RSS+HN)
#   GHOST_ADMIN_API_URL=... (optional; only if publishing to Ghost)
#   GHOST_ADMIN_API_KEY=... (format: 24hex:hex_secret)
#   AGENTFLOW_IMAGE_LIBRARY=~/Pictures/agentflow   (optional; for af image-auto-resolve)
```

The CLI auto-loads `backend/.env` on startup (doesn't override existing env vars).

**或者**直接跑 `af onboard` 一站式凭据向导 —— 它会交互式问每一个 key、把结果写到
`backend/.env`，并在每步给出"为什么需要这个 key / 怎么申请"的提示。如果你只是
做 mock 跑、什么都不填，让它跳过就好。

If you want to attach an external automation agent, also see
`docs/integrations/AGENT_BRIDGE.md`. The relevant env vars are:

- `REVIEW_DASHBOARD_TOKEN`
- `AGENTFLOW_AGENT_BRIDGE_TOKEN`
- `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL`
- `AGENTFLOW_AGENT_EVENT_AUTH_HEADER`
- `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS`

### 5. Bootstrap a topic profile

Topic profiles (`F1` series) scope hotspots / write / publish to a single
content vertical (e.g. "ai-coding", "ml-infra"). Pick one of:

```bash
# Interactive: CLI walks you through name / keywords / sources / tone
af topic-profile init -i --profile <id>

# Or from a YAML patch file:
af topic-profile init --profile <id> --from-file path/to/patch.yaml
```

Optional — let the LLM reverse-engineer profile fields from a seed description:

```bash
af topic-profile derive --profile <id>
```

Profiles land at `~/.agentflow/profiles/<id>.yaml` and become the default
scope for subsequent `af hotspots` / `af write` calls when you pass
`--profile <id>`.

### 6. Learn style + keyword candidates from a public handle

`af learn-from-handle` (F2) pulls a target handle's recent posts, derives a
voice/tone fingerprint plus keyword candidates, and writes both back into
the profile:

```bash
af learn-from-handle <handle> --profile <id>
# e.g. af learn-from-handle simonwillison --profile ai-coding
```

This complements (and on most setups replaces) the older
`af learn-style --dir ../samples/` flow. If you'd rather seed from local
samples, drop 3-5 articles into `samples/` and run:

```bash
PYTHONPATH=. af learn-style --dir ../samples/
PYTHONPATH=. af learn-style --show
```

### 7. Launch the review daemon

The review daemon watches drafts / publish queue and keeps the dashboard
fresh. Pick the mode that fits your env:

```bash
# Foreground (good for first-time verification, Ctrl+C to stop)
af review-daemon

# Background via systemd (Linux); template:
#   ExecStart=/usr/bin/env af review-daemon
#   Restart=on-failure
# launchd plist on macOS works the same way.
```

Logs tail at `~/.agentflow/logs/agentflow.log`; events at
`~/.agentflow/memory/events.jsonl`.

### 8. Verify (mock smoke)

```bash
cd backend && source .venv/bin/activate

MOCK_LLM=true PYTHONPATH=. af hotspots --json 2>/dev/null > /tmp/h.json
HID=$(python -c 'import json; print(json.load(open("/tmp/h.json"))["hotspots"][0]["id"])')

MOCK_LLM=true PYTHONPATH=. af write "$HID" --auto-pick --json 2>/dev/null > /tmp/a.json
AID=$(python -c 'import json; print(json.load(open("/tmp/a.json"))["article_id"])')

MOCK_LLM=true PYTHONPATH=. af preview "$AID" --json 2>/dev/null >/dev/null
MOCK_LLM=true PYTHONPATH=. af publish "$AID" --force-strip-images --json 2>/dev/null
MOCK_LLM=true PYTHONPATH=. af memory-tail --limit 5 --json
```

Expected: all commands exit 0; `~/.agentflow/` gets created with `hotspots/`, `drafts/`, `memory/`, `publish_history.jsonl`.

After filling `.env` with real keys, drop `MOCK_LLM=true` from commands above. Expect:

- `af hotspots` takes ~45-90s (Twitter + RSS + HN real fetch, then Jina clustering + Kimi angle mining)
- `af write --auto-pick` takes ~60-90s (Kimi skeleton + 4-section fill)
- `af publish` against Ghost: use `GHOST_STATUS=draft` in env to publish as draft for safety
- `af newsletter-preview-send <newsletter_id> --to self` is the safest first Resend check; use it before `newsletter-send`
- `af tweet-publish <tweet_id> --dry-run` is the safest first Twitter check; the read-only `TWITTER_BEARER_TOKEN` is not enough to post

## Daily flow (in Claude Code)

```
/agentflow-style       # weekly — refresh voice from your past articles
/agentflow-hotspots    # daily — pick a topic (add "只关心 X" for topic-targeted)
/agentflow-write <hotspot_id>
/agentflow-publish <article_id>
```

Each skill wraps `af` subcommands; see `.claude/skills/<skill>/SKILL.md` for the exact contract.

## Where state lives

All runtime state is local at `~/.agentflow/`:

- `style_profile.yaml` — D0 output
- `sources.yaml` — your KOL / RSS / HN config (copy from `config-examples/sources.example.yaml` on first run)
- `hotspots/<YYYY-MM-DD>.json` — D1 daily scans
- `drafts/<article_id>/` — skeleton / draft.md / metadata / platform_versions
- `medium/<article_id>/` — Medium export / package / ops checklist artifacts
- `publish_history.jsonl` — one row per publish + rollback
- `memory/events.jsonl` — append-only cross-article event log
- `logs/agentflow.log` + `llm_calls.jsonl` — debugging tail

## Key docs

- `README.md` — CLI reference, platform setup
- `docs/PRD_OVERVIEW.md` — product vision, roadmap, user stories
- `docs/SOLUTION_OVERVIEW.md` — architecture, modules, data models
- `docs/CC_ONE_PAGE_SUMMARY.md` — 1-page status (含本轮实 Key 验证结果)
- `docs/backlog/MEMORY_TO_DEFAULTS.md` — Memory → Default Strategy 设计
- `docs/backlog/IMAGE_INSERTION_STRATEGY.md` — 图片插入策略
- `docs/backlog/TOPIC_INTENT_FRAMEWORK.md` — 话题意图跨 flow 框架
- `CCREVIEW_HANDOFF.md` — Claude Code review handoff

## Known limitations (as of 2026-04-28 snapshot)

1. LinkedIn publish 与 Twitter / Resend 真 key 仍需你自己的最后一轮实网验证
2. `af image-auto-resolve` 当前只做本地图库匹配；reference 抓图 / 网络图源 / strict LLM 二次确认还没做
3. `af publish` 只覆盖长文平台；Twitter 与 newsletter 仍是独立 `af tweet-*` / `af newsletter-*` 流程
4. `af publish-rollback` 只覆盖 Ghost；LinkedIn/Medium API 不支持程序化 delete，email 也无法 unsend
5. Medium 推荐走新的半自动 browser-ops 流程：先 `af medium-export` / `af medium-package` / `af medium-ops-checklist`，再由人或 browser operator 在 Medium UI 内完成导入与发布
6. Kimi 生成段落偶尔超 `max_length_words`（D3 adapter 会做平台级二次拆分兜底）
7. Ghost Admin API 偶发 SSL handshake 失败；已加兜底，重跑即可
