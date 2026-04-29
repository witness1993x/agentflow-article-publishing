# Changelog

All notable changes to **agentflow-article-publishing** (the canonical
runtime for the AgentFlow content pipeline) are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version number is the single value in
[`backend/pyproject.toml::project.version`](backend/pyproject.toml); the
sibling skill-distribution repo
([`agentflow-skills`](https://github.com/witness1993x/agentflow-skills))
is versioned independently and tracks the **`af` CLI surface** rather than
runtime code parity.

## [Unreleased]

- _no changes yet_

## [1.0.2] — 2026-04-29

Bug-fix release driven by real-Telegram-bot operator feedback on v1.0.1.
Three operator-visible bugs closed.

### Fixed

- **Gate A card pushed repeatedly to TG.** `triggers.post_gate_a()` had no
  idempotency check; running `af hotspots` twice in the same review window
  produced multiple identical cards. Fixed by checking
  `short_id.find_active(gate="A", batch_path=...)` before rendering and
  returning `{"duplicate": True}` if an active card exists for the same batch.
- **"ChainStream" / "Uniswap" brand names leaked into framework prompts.**
  `agent_review/daemon.py` `_PROFILE_SETUP_STEPS` used real brand names
  (`Uniswap`, `ChainStream`, `AMM`, `DEX liquidity`, `Uniswap v4 hooks`) as
  example values in the brand and source-materials onboarding prompts.
  Replaced with brand-neutral placeholders so the framework never names a
  user's brand for them. Reaffirms the project's brand-neutrality contract:
  user brand data lives in `~/.agentflow/topic_profiles.yaml`, never in
  framework code.
- **"Regenerate image" button appeared to hang.** `_spawn_image_gate()`
  shelled out to `af image-gate` with a hardcoded 600s timeout and no
  in-flight progress message; the operator saw an instant ack toast then
  silence for minutes. Now sends an interim "🔁 已开始重新生成封面…
  完成后会自动推送新的 Gate C；超时或失败会发错误通知" message before
  spawning, and the subprocess timeout is configurable via
  `AGENTFLOW_IMAGE_GATE_SUBPROCESS_TIMEOUT_SECONDS` (default 240s).
- **Stale session-intent shadowing new profile selection.** Switching topic
  profiles didn't clear the previous session intent, so old defaults could
  bleed into the new flow. `memory.load_current_intent()` now checks
  `AGENTFLOW_SESSION_INTENT_MAX_HOURS` (default 12h) and deletes intents
  older than the threshold.

### Added

- `short_id.find_active(gate, article_id, batch_path)` — public helper
  returning the newest non-expired, non-revoked short_id matching a gate +
  optional selector. Foundation for idempotency checks across all gates.
- `.cursor/skills/agentflow-open-claw-v2/` — restructured to standard
  skill layout: `SKILL.md` (entry) + `references/` (deep-read, optional)
  + `assets/` (YAML templates consumed as CLI args, e.g.
  `topic_profile.yaml`, `style_tuning.yaml`). No source code in the skill;
  CLI is the only execution surface.
- New env knobs in `backend/.env.template`:
  - `AGENTFLOW_IMAGE_GATE_SUBPROCESS_TIMEOUT_SECONDS=240`
  - `AGENTFLOW_SESSION_INTENT_MAX_HOURS=12`
- New tests in `tests/test_v02_workflows.py`:
  `test_stale_session_intent_expires_before_shadowing_default_profile`,
  `test_gate_a_post_is_idempotent_for_active_batch_card`.

### Changed

- `cli/commands.py`: `intent_show` now reads via
  `memory.load_current_intent()` (decouples CLI from yaml internals).
- `docs/flows/USER_SCENARIOS.md`: S0.1 sequence diagram rewritten to
  emphasize "skill harness loads SKILL.md, then agent invokes `af`" —
  no source code in the skill repo.

## [1.0.1] — 2026-04-29

### Fixed

- `af --version` was hardcoded to `"0.1.0"` in `agentflow/cli/commands.py`
  via `@click.version_option`, so it kept reporting `0.1.0` even after the
  package version bumped to `1.0.0`. Now reads from `importlib.metadata`,
  so the CLI version tracks `pyproject.toml::project.version` automatically.
  Discovered when smoking the v1.0.0 deploy bundle in a fresh venv.

## [1.0.0] — 2026-04-29

First tagged release. The codebase had been carrying `pyproject.toml`
version `0.1.0` during private development; this is the first time the
runtime is being cut as a named, taggable release.

### Pipeline (D0–D4)

- **D0** voice profile from local samples (`af learn-style`) and from a
  public handle (`af learn-from-handle`).
- **D1** hotspot discovery across Twitter / RSS / HackerNews with Jina
  embeddings + Kimi angle mining; topic-targeted post-filtering;
  HN Algolia search (`af search`); cross-flow `TopicIntent` (`af intent-*`).
- **D2** writer with skeleton + per-section fill, natural-language edit loop
  (`改短` / `加例子` / `改锋利` / `去AI味` / `展开`), `--auto-pick` from
  preferences, and an explicit image gate (`af propose-images` /
  `af image-resolve` / `af image-auto-resolve`).
- **D3** platform adapters (Medium / Ghost / WordPress / LinkedIn-article /
  Twitter-longform), preview JSON, per-platform paragraph/emoji/heading
  shaping.
- **D4** publishers with rollback (`af publish-rollback` for Ghost),
  `--force-strip-images` escape hatch, Ghost image upload to CDN at
  publish time, draft-mode override after a rollback.

### Distribution surfaces

- `af` CLI (entry point in `backend/pyproject.toml`).
- 7-skill Claude Code / Cursor distribution (sibling repo,
  `agentflow-skills` v1.0.0).
- `af skill-install` for one-shot symlinking into Claude Code / Cursor
  config dirs.
- Twitter/X distribution (`af tweet-*`).
- Resend email newsletter (`af newsletter-*`) with `preview-send`,
  `send`, `correction`.
- Medium semi-automatic browser-ops flow (`af medium-export` /
  `af medium-package` / `af medium-ops-checklist`).

### Memory & preferences

- Append-only event stream at `~/.agentflow/memory/events.jsonl`.
- `af prefs-rebuild` aggregates events into
  `~/.agentflow/preferences.yaml`; consumed by `af write --auto-pick`,
  `af preview`, `af publish` (rollback-aware draft override), and
  TopicIntent recall.
- `af report` cross-channel status digest (IDEAS / IN FLIGHT / SHIPPED /
  ROLLBACKS / ATTENTION).

### Agent Bridge (v1)

- Local HTTP API at `127.0.0.1:7860` started via `af review-dashboard`.
- Read endpoints (`/api/health`, `/api/articles`, `/api/article/<id>`,
  `/api/bridge`, `/api/bridge/schema`).
- Whitelisted `POST /api/commands` runner for read- / pipeline- /
  publish-scoped commands; publish scope blocked unless
  `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`.
- Outbound event webhook (best-effort fan-out) controlled by
  `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` + `AGENTFLOW_AGENT_EVENT_AUTH_HEADER`.
- Self-describing capability descriptor at `/api/bridge` so a fresh
  external orchestrator (e.g. OpenClaw) can negotiate without prior
  knowledge.
- v1 schemas at `docs/integrations/AGENT_BRIDGE_V1.schema.json`;
  example clients at `docs/integrations/examples/`.

### Telegram review daemon

- 4 review gates (A topic / B draft / C image / D channel-select).
- Per-action auth model (`review/write/edit/image/publish/*`) keyed on
  operator UID.
- `af review-daemon` foreground; `agentflow-review.service` systemd unit
  for Linux deploy.

### Topic profiles

- `af topic-profile init / show / update / derive` for per-vertical
  scoping (e.g. `ai-coding`, `ml-infra`).
- `--profile <id>` flag honored across `af hotspots`, `af write`,
  `af publish`.
- `~/.agentflow/profiles/<id>.yaml` storage; current selection at
  `~/.agentflow/intents/current.yaml`.

### Deployment

- `scripts/build_deploy_bundle.sh` produces a clean tarball excluding
  `.venv/`, `tests/`, `.env`, `*.bak.*`, local audit data; default
  output `~/Desktop/agentflow-deploy.tar.gz`.
- `agentflow-deploy/deploy.sh` provisions venv + systemd + chmod 600 in
  one shot on a Linux VM.
- `agentflow-deploy/INSTALL_LINUX.md` + `SECURITY.md`.

### MOCK mode

- `MOCK_LLM=true` short-circuits all LLM and publisher calls to
  deterministic fixtures under `backend/agentflow/shared/mocks/`,
  enabling end-to-end CI smokes with no API keys.

[Unreleased]: https://github.com/witness1993x/agentflow-article-publishing/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/witness1993x/agentflow-article-publishing/releases/tag/v1.0.0
