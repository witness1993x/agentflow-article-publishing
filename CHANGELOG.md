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

## [1.0.4] — 2026-04-29

A four-front release closing a security leak, relocating secrets to the
operator's key folder, completing the YAML-as-CLI-args affordance across
the writer pipeline, and finishing the Telegram operator-completeness work
started in v1.0.3 (8 review-ops commands → 21 ops/bootstrap/profile/
intent/prefs/system commands).

### Security

- **Plugged a deploy-bundle secret leak.** `scripts/build_deploy_bundle.sh`
  excluded `.env` (exact match) but NOT `env_config*` / `.env_config*` /
  `*.key` / `*.pem` / `id_rsa`. Confirmed: `agentflow-deploy-v1.0.2.zip`
  and `agentflow-deploy-v1.0.3.zip` (and matching `.tar.gz`) on the local
  build host carried operator API keys (TELEGRAM_BOT_TOKEN, GHOST_ADMIN_API_KEY,
  ATLASCLOUD_API_KEY, AGENTFLOW_AGENT_BRIDGE_TOKEN). Bundles never reached
  GitHub Releases or third parties (no rotation needed). The contaminated
  bundles were deleted; rsync exclude set extended; new post-build sanity
  guard rejects any bundle containing `env_config*`, `.env_config*`,
  `*.key`, `*.pem`, `id_rsa`, or any non-`.template` `.env`-shaped file.
- **`.gitignore` extended** with `env_config*`, `.env_config*`,
  `*env_config copy*`, `secrets/`, `*.key`, `*.pem`, `id_rsa`,
  `id_rsa.pub`, plus `agentflow-deploy-*.zip`. The dotted-name pattern
  catches macOS-rename variants the operator used to bypass `.env*` hits.

### Migration — secrets relocate to `~/.agentflow/secrets/`

- **CLI loader rewritten** (`backend/agentflow/cli/commands.py`). New
  precedence: `~/.agentflow/secrets/.env` (catch-all primary) →
  `~/.agentflow/secrets/<service>.env` (per-service: `telegram.env`,
  `atlascloud.env`, `ghost.env`, `moonshot.env`, `anthropic.env`,
  `jina.env`, `openai.env`, `twitter.env`, `linkedin.env`,
  `agent_bridge.env`, `review_dashboard.env`) → `backend/.env` (back-compat
  fallback for installs predating v1.0.4). All loads use `override=False`
  so a value set by an earlier source (process env or higher-precedence
  file) wins.
- **`af onboard` writes to `~/.agentflow/secrets/.env`** by default; dir
  is created at mode 0700, files at mode 0600.
- **`af bootstrap` env-seed step** migrates an existing `backend/.env` to
  the new location on first run after upgrade (preserving operator-set
  values), or seeds from `backend/.env.template` if no legacy file exists.
- **`af doctor` shows the source file** for each credential check, so an
  operator can see at a glance whether keys came from the secrets folder
  or the legacy fallback.
- **3 new CLI subcommands** in `agentflow/cli/keys_commands.py`:
  - `af keys-where` — prints precedence list + per-var resolution map.
  - `af keys-show [--service NAME]` — masked values + source paths.
  - `af keys-edit [SERVICE]` — opens `$EDITOR` on per-service file
    (creates 0600 if missing).
- **`backend/.env.template` header rewritten** to clarify it is the
  upstream schema doc, not the operator's destination.

### Added — YAML-as-CLI-args (`--from-file`)

`af learn-style`, `af hotspots`, `af edit`, `af preview`, `af tweet-draft`,
and `af newsletter-draft` now accept `--from-file <yaml>`. Each command
maps known YAML keys to its click options (CLI-explicit values override
YAML); unrecognized keys are surfaced via env to the agent's downstream
config so adapters / drafters / scoring can opt in to the same tunings.
The matching template YAMLs ship in the sibling skill repo
(`agentflow-skills v1.0.2`) under each skill's `assets/`.

### Added — Telegram operator-completeness (21 commands)

Built on the v1.0.3 `_COMMAND_REGISTRY` foundation. Curated subset of 12
appears in Telegram's global `/` menu via `setMyCommands`; remaining 9 are
accepted by the text dispatcher (and listed in `/help`). All commands
backed by direct Python imports of the matching CLI subcommand — no
subprocess shells.

| Group | Commands |
|---|---|
| Bootstrap | `/onboard`, `/doctor`, `/scan` |
| Profile | `/profile`, `/profiles`, `/profile-init`, `/profile-update`, `/profile-switch` |
| Sources | `/keyword-add`, `/keyword-rm` |
| Style | `/style`, `/style-learn` |
| Intent | `/intent`, `/intent-set`, `/intent-clear` |
| Prefs | `/prefs`, `/prefs-rebuild`, `/prefs-explain`, `/prefs-reset` |
| Review-ops | (existing v1.0.3) `/status`, `/queue`, `/help`, `/skip`, `/defer`, `/publish-mark`, `/audit`, `/auth-debug` |
| System | `/report`, `/restart-daemon` |

Each has both hyphen and underscore aliases (Telegram's `setMyCommands`
rejects hyphens; the text dispatcher accepts both for muscle-memory
continuity with the CLI).

### Added — `system` auth bucket

New `_ACTION_REQ` value covering high-impact ops (onboard, profile init/
update/switch, intent set/clear, scan trigger, prefs rebuild/reset,
restart-daemon). Default-grant only to the implicit operator UID;
explicit `af review-auth-set-actions` to extend. (`agent_review/auth.py`)

### Added — 2 new `topic-profile` subcommands

- `af topic-profile list [--json]` — markdown table or JSON of all
  profiles in `~/.agentflow/profiles/<id>.yaml`, with active marker
  resolved from `~/.agentflow/intents/current.yaml`.
- `af topic-profile set-active <id>` — switches the active profile,
  validates the file exists, appends a `profile_switched` memory event.

### Changed

- `backend/agentflow/agent_review/daemon.py` grew ~1300 lines for the
  v1.0.4 command registry, dispatcher, and 21 command handlers. Helper
  functions `_handle_profile_init / _handle_profile_update /
  _handle_profile_switch / _handle_keyword_add / _handle_keyword_rm /
  _handle_style / _handle_style_learn / _handle_intent_* / _handle_prefs_* /
  _handle_report / _handle_restart_daemon` follow the v1.0.3 pattern of
  acking the callback, doing work, sending a follow-up, and appending an
  audit-visible memory event.
- `tests/test_v02_workflows.py::TopicProfileIntentTests::test_configure_bot_menu_registers_basic_commands`
  updated to assert the v1.0.4 curated set (the prior version asserted
  `start/help/list/suggestions/cancel`, which were removed in v1.0.3).

### Pairs with sibling repo

- `witness1993x/agentflow-skills v1.0.2` — public skill distribution
  restructured to standard layout (`SKILL.md` + `references/` + `assets/`)
  across all 7 skills; daemon explicitly marked TG-only opt-in;
  `assets/<topic>.yaml` templates consumed by the new `--from-file` flags.

## [1.0.3] — 2026-04-29

Telegram bot menu enrichment. The 4 review gates (A topic / B draft /
C image / D channel-select) now expose every action defined in the
auth table, share a unified label vocabulary, and a global `/`-command
menu lets the operator drive the bot without remembering article ids.

### Added

- **6 wired callbacks** previously defined in `_ACTION_REQ` but never
  rendered as buttons:
  - `A:expand` — render the full hotspot record (suggested_angles, raw
    signals, refs) as a reply.
  - `A:defer:hours=4` / `B:defer:hours=2` / `C:defer:hours=2` — re-post
    the gate card after the configured delay; backed by a new
    deferred-repost store + drainer in `daemon.py`.
  - `B:diff` — unified diff between current draft and last reviewed
    snapshot (falls back to a friendly "no prior version" message when
    no snapshot exists; a real versioned-draft store is queued for
    v1.0.4).
  - `C:full` — reply with the original 2k cover PNG as a Telegram
    document so the operator gets the unsampled image.
- **8 global slash commands** registered via `setMyCommands` on daemon
  startup (failure to register is logged but does not crash):
  - `/status` — articles in any `*_pending_review` state + age.
  - `/queue` — next 5 articles waiting for action, sorted by age.
  - `/help` — gate definitions + button legend + role matrix
    (regenerated at runtime from `_ACTION_REQ` so it stays in sync).
  - `/skip <id>` — skip image-gate for an article.
  - `/defer <id> <hours>` — defer an article's current gate card.
  - `/publish-mark <id> <url>` — record a manual Medium paste.
  - `/audit` — last 20 callback decisions.
  - `/auth-debug` — show the calling user's roles + per-action grants.
  - Telegram rejects hyphens in `setMyCommands` names, so the registered
    commands use underscores (`publish_mark`, `auth_debug`); the text
    dispatcher accepts both forms for back-compat in operator muscle
    memory.
- **9 new tests** (`TgMenuV103Tests` in `tests/test_v02_workflows.py`)
  covering: zero-orphans static check on `_ACTION_REQ`, unified-label
  assertions per gate, defer scheduling, all 8 slash-command handlers
  registered, role matrix non-empty, `A:expand` end-to-end render. Full
  suite: 53 passed.

### Changed

- **Unified approve/reject vocabulary across all gates.** Approve is
  `✅ 通过` (Gates B/C/D — Gate A keeps slot-pick semantics with
  `✅ 选中 #N`). Reject is `🚫 拒绝` / gate-appropriate (`🚫 全拒绝`,
  `🚫 跳过`, `🚫 取消`). Retry is `🔁`, edit is `✏️`, defer is `⏰`,
  view-actions are `📋 / 📊 / 🖼`. Operators now build muscle memory
  faster across gates.
- **Predictable button row order** for Gates A/B/C: Row 1 primary
  forward, Row 2 edit/regenerate, Row 3 reject/skip/defer. Gate D
  retains its multi-select layout (semantically different).

### Known follow-up (v1.0.4 candidate)

- A proper versioned-draft snapshot store (`drafts/<aid>/.history/`)
  so `B:diff` can compare against the actual last reviewed version
  rather than a best-effort fallback.

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
