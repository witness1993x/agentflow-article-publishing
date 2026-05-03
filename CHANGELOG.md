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

## [1.0.20] — 2026-05-03

Lark card polish — actionable cards now have a TG-bot deep-link button,
brand prefix in titles, optional dashboard URL template, and tunable
trim caps. Same v1.0.19 push-only fan-out semantics.

### Added (env-driven, all optional)

- `LARK_WEBHOOK_TG_BOT_URL` — `https://t.me/<bot_username>` deep link.
  When set, actionable cards get a button linking back to TG so the
  operator jumps straight to where the real action happens. Cards that
  add the button: `notify_publish_ready` (📌 去 TG 标记), `notify_dispatch_result`
  with failures (🔁 去 TG 重试 / 处理), `notify_spawn_failure`
  (🔧 去 TG 看详情), `notify_hotspots_digest` with hotspots
  (📝 去 TG 选题). Full-success dispatch and zero-hotspot digest stay
  button-free (informational only).
- `LARK_WEBHOOK_BRAND_PREFIX` — prepended to every card title (e.g.
  `[ChainStream] 🔎 AgentFlow · 今日热点扫描`). Useful when one Lark
  group hosts multiple AgentFlow instances. Brackets normalized.
- `LARK_WEBHOOK_DASHBOARD_URL_TEMPLATE` — `https://dash.example.com/article/{article_id}`.
  When set, every actionable card gets a `📊 查看 draft` button.
- `LARK_WEBHOOK_REASON_MAXLEN` — failure-reason cap on dispatch cards
  (default 80, min 20). Truncated values get a `…` suffix.
- `LARK_WEBHOOK_STDERR_MAXLEN` — stderr-tail cap on spawn-failure cards
  (default 500, min 80).

### Changed

- `lark_webhook.send_card` — `url_actions` now silently drops entries
  with empty/missing label or URL so callers don't have to gate
  per-button. The dashboard / TG buttons exploit this: pass them
  unconditionally; absent env config = no button rendered.

### Tests

- 4 new in `LarkWebhookTests`: brand prefix prepends correctly;
  actionable cards show TG button while informational cards do not;
  dashboard URL template renders with article_id; REASON_MAXLEN caps
  long reasons with ellipsis. 10 total Lark cases.

## [1.0.19] — 2026-05-03

Lark Custom Bot fan-out (path A in the Lark integration plan). HITL
review stays on Telegram — Lark Custom Bot is push-only webhook with
no callback support per
[Lark docs](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/bot-v3/custom-bot-guide),
so Gate A/B/C/D approvals can only happen on TG. Lark gets summary
notifications. Path B (full-feature Lark "self-built application"
migration) deferred — needs design pass.

### Added

- `agentflow/shared/lark_webhook.py` — push-only outbound module:
  * `send_text(text)` / `send_card(title, body_md, url_actions=...)`
    are the public primitives.
  * `notify_hotspots_digest`, `notify_dispatch_result`,
    `notify_publish_ready`, `notify_spawn_failure` are the four
    AgentFlow-specific builders that triggers / daemon call into.
  * HMAC-SHA256 sign per Lark spec (`timestamp + "\n" + secret` is the
    HMAC KEY, body is empty bytes, then base64). Optional —
    only emitted when `LARK_WEBHOOK_SECRET` is set.
  * Keyword guard: when `LARK_WEBHOOK_KEYWORDS` is set, text bodies
    missing all keywords get the first keyword auto-appended so the
    bot's "自定义关键词" security setting doesn't reject (code 19024).
  * Rate-limit defer: posts within ±60s of any HH:00 / HH:30 are
    deferred 90s (Lark docs warn 11232 系统压力 spikes at integer
    half-hours like 10:00 / 17:30). Override with
    `LARK_WEBHOOK_NO_DEFER=true`.
  * Per-process 5/sec floor (`_MIN_INTERVAL_SECONDS=0.22`) under a
    threading lock.
  * Body cap: truncates the longest text field when JSON encoded
    body exceeds 19 KB (Lark 20 KB cap with headroom for sign).
  * Best-effort: every send is exception-isolated; never raises into
    the caller's TG-primary path.
- `.env.template` — `LARK_WEBHOOK_URL` (empty = off),
  `LARK_WEBHOOK_SECRET`, `LARK_WEBHOOK_KEYWORDS`,
  `LARK_WEBHOOK_NO_DEFER` with inline doc.

### Wired (fan-out points)

- `triggers.post_gate_a` — emits `notify_hotspots_digest` after the
  TG card is sent (covers both manual `af hotspots` and the v1.0.17
  scheduled scan path).
- `triggers.post_publish_dispatch` — emits `notify_dispatch_result`
  after dispatch summary, separating succeeded / failed platforms
  for the on-call channel.
- `triggers.post_publish_ready` — emits `notify_publish_ready` so the
  Lark channel knows an article is waiting on the operator's manual
  Medium paste step.
- `daemon._notify_spawn_failure` — mirrors subprocess crash messages
  to Lark for on-call visibility.

### Tests

- `LarkWebhookTests` × 6: no-op when URL unset; sign matches Lark
  reference algorithm; sign included in body when secret set;
  keyword auto-append when missing; truncate caps oversized payload;
  rate-limit zone detection at HH:00 / HH:30.

### Deferred

- **Path B**: full Lark "self-built application" with event subscription
  + interactive card 回传 to enable Gate approval on Lark. Requires
  public webhook endpoint, OAuth flow, full re-port of 28 slash
  commands + 38 callback buttons. Needs separate design doc; current
  TG-primary HITL is unaffected.

## [1.0.18] — 2026-05-02

Drafts coming out of D2 read as generic AI/Web3 commentary instead of
publisher-specific content (autopost: "没有很贴合具体的产品相关的文章
出来,都是泛谈"). Three-layer fix — prompt anchoring + Gate B
specificity lint + upstream profile-thinness probe.

### Changed (D2 prompts — anchoring as a hard rule)

- `prompts/d2_skeleton_generation.md` — added rule #7 "品牌锚定": every
  `key_argument` must map back to `publisher_account_block` (`product_facts`
  / `perspectives` / `default_description`); ≥2 of the title/opening/closing
  candidates must reference brand assets; outline must include ≥1
  section explicitly grounding the topic in the publisher's own
  scenario. Self-check: tag each argument's anchor source internally.
- `prompts/d2_paragraph_filling.md` — added rule #7 mirroring the
  skeleton constraint at section level: each paragraph must show
  ≥1 explicit anchor; "if this sentence works for any other company,
  rewrite it" heuristic. Quality-comparison block extended with a
  "specific-but-floating vs specific-and-anchored" example pair.

### Added (Gate B specificity lint)

- `agent_d2/specificity_lint.py::detect_specificity_drift` — counts
  publisher-anchor token hits (drawn from `brand` / `product_facts` /
  `perspectives` / `default_description`) per body section; section
  density below 0.5 → warning appended to Gate B card's self-check.
  Tokens shorter than 4 chars ignored to avoid matching "AI" / "API"
  noise. Profile too thin (< 5 anchor tokens) skips this lint and
  defers to the doctor probe below.
- `agent_review/triggers.post_gate_b` — wired specificity lint right
  after the v1.0.16 language lint.

### Added (af doctor — upstream root-cause probe)

- `agent_review/preflight.check_active_profile_thinness` — returns
  WARN when the active profile has < 3 `product_facts` or < 2
  `perspectives`. Drafts produced from a thin profile have no anchor
  vocabulary to ground in, so this surfaces the root cause one level
  upstream of the Gate B lint. Wired into `all_checks` right after
  `check_mock_mode`.

### Tests

- `SpecificityLintTests` × 4: anchored draft passes, generic draft
  flagged, missing publisher returns None, thin profile skips the lint.
- `ActiveProfileThinnessTests` × 2: thin profile warns with
  actionable message; rich profile passes with count summary.

### Deferred (not in this batch — different problem class)

Hard fact-grounding linter (numbers / compliance / certifications /
GDPR / partnerships) — needs schema + ruleset + LLM verifier.
Specificity lint catches "wrong subject"; fact linter catches "wrong
claim about subject". Different design, different release.

## [1.0.17] — 2026-05-02

Twice-daily hotspots scan never fired on non-macOS deployments because
`af review-cron-install` only knew how to write a launchd plist + run
`launchctl`. Linux / Docker / sandbox installs (including the autopost
OpenClaw box) silently had no scheduling at all. Replaced with a
daemon-internal scheduler that requires no OS-level cron.

### Added

- `agent_review/schedule.py` — pure-function time-slot scheduler.
  `due_slots(...)` is side-effect-free for testability;
  `fire_due(spawn)` is the daemon hook; state persisted at
  `~/.agentflow/review/scheduled_state.json` per slot. Misses by more
  than 90s on a given day are skipped (no backfill after long
  downtime).
- `daemon.run()` housekeeping tick now calls
  `schedule.fire_due(_spawn_hotspots)` alongside `_scan_timeouts` /
  `_drain_deferred_reposts`. Cross-OS, no systemd / launchd / cron
  required.
- `.env.template` — `AGENTFLOW_HOTSPOTS_SCHEDULE="09:00,18:00"` (empty
  by default = scheduler off) and `AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K=3`.
- `af review-schedule-status` CLI — shows the parsed schedule, last
  fire per slot, next fire per slot. JSON mode for agents.

### Fixed

- `af review-cron-install` now refuses on non-Darwin with a clear
  pointer to `AGENTFLOW_HOTSPOTS_SCHEDULE` instead of crashing with a
  `launchctl: command not found` traceback the operator can't act on.

### Tests

- `HotspotsScheduleTests` × 6: `_parse_schedule` drops bad slots;
  `_slot_due` honours the 90s window + already-fired-today guard +
  fired-yesterday-still-due; `due_slots` returns only unfired;
  `fire_due` calls spawn once, stamps state, and is idempotent on
  re-run within the same window; `status` reports disabled when env
  is empty; `review-cron-install` refuses on non-Darwin with a
  pointer to the env-driven path.

## [1.0.16] — 2026-05-01

Five fixes from a real production-experience report on the autopost
side: two P0 production blockers + three P1 reliability/UX wins.
Bigger architectural items from the same report (full draft-refresh
command, fact-grounded hallucination linter, single-source-of-truth
refactor) deferred to a separate design pass.

### Fixed (P0 — production blockers)

- **`agent_review/tg_client.py::send_photo`** (#5) — large cover
  PNGs (typical Atlas output ~2.5MB) now go through
  `_optimize_photo_for_telegram` first: when source > 1MB, transcode
  to JPEG, downscale long edge to 1600px, quality=85. Pillow already
  in deps; original file untouched (`*_tgopt.jpg` sibling). On
  `requests.Timeout` / `ConnectionError`, falls back to
  `send_document` with the original so the operator still gets the
  asset. Previously a 2.5MB PNG would timeout the Gate C card and
  leave the article stuck in `image_pending_review` with no
  recovery path.
- **`agent_review/preflight.py::_probe`** (#1a) — cache entries now
  bind to a `token_fp` (8-hex sha256 of the relevant API token);
  rotating the token invalidates its cached probe result. Pre-v1.0.16
  cache entries (no `token_fp` key) are also treated as miss to
  force a re-probe under the new schema. Previously, rotating a
  Telegram bot token left `af doctor` returning yesterday's "401" for
  ~1h (cache TTL) even though the new token was healthy.

### Added (P1)

- **`agent_review/triggers.py::_revoke_prior_card_keyboard`** (#4a) —
  before posting a fresh Gate B/C/D card, find any active short_id
  for the same `(gate, article_id)`, edit its inline keyboard to `{}`
  on the old TG message, and revoke the old sid. Operator's TG
  history now has at most one interactable card per gate; old cards
  show no buttons (visibly stale) and clicks against them surface
  the existing soft-revoke "✓ 已处理" branch.
- **`agent_review/short_id.py::attach_message_id`** — supporting
  setter so #4a can find the old message_id to edit. Wired into
  `post_gate_a/b/c/d` and `post_image_gate_picker` after each
  `tg_client.send_message` / `send_photo`. Closes a latent gap:
  `entry.get("tg_message_id")` was being read by Gate A's idempotent
  duplicate-check (and now #4a) but never written.
- **`agent_d2/main.py::save_draft`** + **`triggers.post_gate_b`**
  (#2a) — drafts now stamp `metadata.profile_snapshot = {profile_id,
  last_updated_at}` at save time. `topic_profile_lifecycle.upsert_profile`
  stamps a per-profile `last_updated_at` on every write. Gate B
  re-checks at post time: when current profile is newer than the
  stamped snapshot, sets `metadata.draft_outdated_by_profile_change=true`
  and prepends a `⚠ profile {id} 已在 draft 之后被更新` warning to the
  card's self-check section. The operator sees that the active draft
  predates the latest profile rules before approving.
- **`agent_d2/language_lint.py::detect_mixed_language`** (#3b) — new
  module. For `output_language=zh-Hans` profiles, computes the
  ASCII-letter / CJK-char ratio in the body (after stripping a brand
  whitelist of API/JSON/AgentFlow/Telegram/etc.); >15% → warning
  appended to the Gate B self-check lines. Symmetric for `output_language=en`.
  Catches the "中文段落里突然冒出大段英文" content-quality issue
  flagged in the autopost report.

### Tests

- `V016BatchTests` × 9: cache invalidates on token rotation; pre-v1.0.16
  entry treated as miss; small photo passes through unmodified; large
  photo gets optimized to JPEG ≤1600px; send_photo Timeout falls
  back to send_document; stale-card keyboard cleanup edits the right
  message; language lint flags zh body with >15% English; passes
  clean zh; passes brand-whitelisted Chinese-with-English-acronyms.

## [1.0.15] — 2026-05-01

Two operator-facing message cleanups in the `/start` auto-dispatch
branches that were either redundant or self-contradictory.

### Fixed

- `/start` `missing_profile` / `incomplete_profile` branch
  (`daemon.py::_handle_message`) — dropped the
  `"⚙️ 检测到状态：{cs} / 原因：... / 正在自动开启 /onboard..."`
  preamble. `_handle_onboard` (called immediately after) sends its own
  `"⚙️ 开始引导式 onboard：将依次询问 brand / voice / sources / rules。"`
  intro and then the wizard sends Q1, so the operator no longer reads
  three near-identical "we're starting the wizard" lines before getting
  to the actual question.
- `/start` `daemon_not_running` branch — replaced the contradictory
  `"✅ daemon 已活着 (你看到这条消息说明它在跑). 心跳文件可能仅是 stale；其他 init check 已过."`
  message with a `_write_heartbeat()` refresh + plain `"✅ 已 ready..."`.
  The branch only fires when the heartbeat is stale at probe time;
  refreshing it before reply makes the next probe legitimate, and the
  operator sees the same `ready` UX as a normal install instead of a
  message that admits the heartbeat is wrong.

## [1.0.14] — 2026-05-01

`/start` auto-dispatch on a pure-TG-operator install told the operator
"run `af skill-install` in your terminal" instead of triggering the
onboard wizard, because the bootstrap detector was blocking on the
Claude Code / Cursor skill harness check before reaching the profile
check. Skill harness is irrelevant when interaction happens entirely
via Telegram (Mode B/C).

### Fixed

- `cli/bootstrap_commands.py::_detect_next_step` — skill-harness check
  (#2 in the detection ladder) now only fires when `mode == "harness"`
  (Claude Code / Cursor driven). In `mode == "tg_review"` the operator
  has no terminal-side skill consumer, so a missing `~/.claude/skills`
  / `~/.cursor/skills` is not a blocker. Detection ladder for tg_review
  becomes: `.env → real_keys → profile → chat_id → daemon → ready`,
  matching the actual Mode B/C dependency graph.

### Behaviour change

- `/start` auto-dispatch on a fresh tg_review install with no
  topic_profile now correctly returns `missing_profile` and kicks off
  the onboard wizard automatically, instead of returning
  `skills_not_installed` and asking the operator to run a CLI command
  they may not have access to (e.g. autopost agent in a sandbox).

### Tests

- `DetectNextStepModeAwarenessTests` × 2:
  * `tg_review` mode + missing skills + missing profile → result is
    NOT `skills_not_installed` (the regression);
  * `tg_review` mode reaches profile check when topic_profiles.yaml
    is absent.

## [1.0.13] — 2026-05-01

Five more `Bad Request: can't parse entities` MarkdownV2 escape leaks
caught from a real Telegram session. v1.0.6 introduced parens, v1.0.8
fixed `/audit` and one `/start` line, v1.0.13 finishes the sweep across
the operator-output formatters and Gate D resume card.

### Fixed

- `daemon.py::_send_status_summary` — `📊 *Pending* (N)` → `📊 *Pending* \\(N\\)`.
- `daemon.py::_send_queue_summary` — `📋 *Queue* (top N oldest)` →
  `📋 *Queue* \\(top N oldest\\)`.
- `daemon.py::_send_auth_debug` — `🔐 *Auth Debug* (uid ...)` →
  `🔐 *Auth Debug* \\(uid ...\\)`.
- `daemon.py::_handle_message` `/start` generic-fallback branch —
  `\\(mode={mode}\\)` had escaped parens but the literal `=` and the
  inline `{mode}` value were not escaped. Now `\\(mode\\={escape_md2(mode)}\\)`.
- `daemon.py::_route` Gate D `D:cancel` resume card —
  `· article=\`{...}\`` → `· article\\=\`{...}\``.

### Tests

- `MarkdownV2EscapeRegressionTests` × 3:
  * `_send_status_summary` body uses `\\(N\\)`, not bare parens.
  * `_send_queue_summary` body uses `\\(top N oldest\\)`, not bare parens.
  * `_send_auth_debug` body uses `\\(uid ...\\)`, not bare parens.

## [1.0.12] — 2026-05-01

Closes the last "real-mode silently emits placeholder data" gap found in
the third audit pass: when a real LLM call fails inside the viewpoint
miner, the cluster is now dropped instead of producing a stub Hotspot
with empty angles. Same theme as v1.0.8 / v1.0.10 / v1.0.11 — every
hotspot in `~/.agentflow/hotspots/*.json` must reflect real upstream
data.

### Fixed

- `agent_d1/viewpoint_miner.py::mine` — `LLMClient.chat_json` failure now
  re-raises in real mode (`MOCK_LLM != "true"`). Previously the exception
  was swallowed and a Hotspot with empty `mainstream_views`,
  `overlooked_angles`, and `suggested_angles` was returned — looked real
  in `af review-list`, evaded `check_hotspots_mock_leak` (no `mock=true`
  flag set). Mock mode keeps the old stub-on-fixture-miss behavior so
  smoke tests still close their loop.
- `agent_d1/main.py::run_d1_scan` — switched cluster mining from
  `asyncio.gather(...)` to `asyncio.gather(..., return_exceptions=True)`,
  drops failed clusters with `_log.error` audit. Net effect: a partial
  scan emits only the clusters whose viewpoint mining succeeded; total
  failure emits zero hotspots (which is honest) instead of N stubs.

### Hardened

- `agent_review/preflight.py::_twitter_mock_fingerprints` — derived live
  from `agent_d1.collectors.twitter._MOCK_TEMPLATES` instead of three
  hardcoded prefixes. If a future engineer adds a 4th mock template, the
  doctor probe catches it automatically. Closes the fragility flagged by
  the v1.0.11 audit pass.

### Tests

- `ViewpointMinerRealModeFailureTests` × 3:
  * `mine` raises `RuntimeError` in real mode on LLM failure;
  * `mine` returns a stub in mock mode on fixture miss (back-compat);
  * `run_d1_scan` drops the failed cluster, emits only the successful
    one, no stub hotspot in output.

## [1.0.11] — 2026-05-01

`af doctor` now surfaces historical mock-tagged hotspot files on disk.
v1.0.8 + v1.0.10 closed the production paths; this catches the static
state operators inherit from earlier installs so they can `rm` rather
than be misled by `af review-list` showing fake-source articles.

### Added

- `agent_review.preflight.check_hotspots_mock_leak` — scans
  `~/.agentflow/hotspots/*.json` for known `_MOCK_TEMPLATES` text
  fingerprints (the three twitter mock templates) and `"mock": true`
  raw_metadata tags. Reports as **WARN** (not FAIL) so it doesn't block
  startup; surfaces filenames so the operator knows what to delete.
- Wired into `preflight.all_checks()` right after `check_mock_mode` —
  shows up in `af doctor` between MOCK_LLM and Telegram rows.

### Tests

- `HotspotsMockLeakDoctorTests` × 4: clean dir, no-dir, template
  fingerprint hit, `"mock": true` raw_metadata hit.

## [1.0.10] — 2026-05-01

Hard guarantee that real-mode hotspots scans never emit mock-tagged signals
into clustering / persistence, even if a future collector regresses or the
runtime env is misconfigured. v1.0.8 fixed twitter's silent-mock fallback;
this is the structural guard that makes the same regression impossible
across all collectors going forward.

### Added

- `agent_d1.main._collect_all` — when `MOCK_LLM` is not explicitly `"true"`,
  signals carrying `raw_metadata.mock=True` are dropped before clustering
  with a visible `_log.error` audit. Previously a regressed collector could
  silently pollute a real scan with deterministic templates (this is exactly
  what produced the 04-29 hotspots batch where four KOL handles all "tweeted"
  the same `_MOCK_TEMPLATES[0]` text).
- `agent_d1.main._provenance_summary` — per-source `{real, mock}` counts
  emitted at INFO level after collection, so the operator can audit at a
  glance which collector contributed what.

### Tests

- `HotspotsMockGuardTests::test_real_mode_filters_mock_tagged_signals`
  — fakes a mixed batch (2 real + 2 mock-tagged) under `MOCK_LLM=false`,
  asserts only the 2 real survive `_collect_all`.
- `HotspotsMockGuardTests::test_mock_mode_preserves_mock_signals` — same
  fixture under `MOCK_LLM=true` keeps both, so the guard does not break
  legitimate mock smoke runs.

## [1.0.9] — 2026-05-01

`/start` → auto-onboard wizard was silently dead on slash-command entry,
plus stale doc rot mislabelling 8 wired callbacks as stubs.

### Fixed

- `daemon.py::_start_profile_setup_session` — sessions created via the
  slash-command path (`/start` auto-dispatch, `/onboard`,
  `/profile-init`) now write `status="collecting"` + `active_uid` +
  `active_chat_id`, matching the schema that `find_active_session_for_uid`
  filters on and `_send_profile_setup_question` reads. Previously the
  wizard printed its prelude ("⚙️ 开始引导式 onboard…") and then the
  first question was never sent (sender bailed on missing
  `active_chat_id`), and even if Q1 had been sent the user's reply
  would have been dropped (lookup filters on `active_uid` +
  `status=="collecting"`, neither set). Test
  `test_profile_session_reply_advances_and_applies` had been passing
  by handcrafting fixtures with the correct keys, so the broken real
  entry path didn't surface in CI.
- `daemon.py::_send_profile_setup_question` — defensively falls back
  to `session.get("chat_id")` when `active_chat_id` is absent, so
  legacy sessions on disk (mid-flight when the daemon restarts) don't
  hang.

### Docs

- `daemon.py::_ACTION_REQ` header comment — removed the stale "KNOWN
  STUB CALLBACKS" block listing `A:expand` / `A:defer` / `B:diff` /
  `B:defer` / `C:regen` / `C:relogo` / `C:full` / `C:defer`. All eight
  have real handlers in `_route` (verified via grep + read of each
  branch) and matching `_ACTION_REQ` permission entries.
- `agent_review/templates/state_machine.md::"Known stubs"` table →
  renamed `"Auxiliary gate actions"` and rewritten with the actual
  effect of each button (replies with batch JSON / unified-diff /
  2k cover; schedules deferred-repost; cycles brand_overlay anchor;
  re-spawns image-gate). Emoji labels in the table now match what
  `render.render_gate_*` actually emits.

## [1.0.8] — 2026-04-30

Three operator-visible bugs from a real Telegram-bot session:

1. **`/audit` failed with `Bad Request: can't parse entities`.** The
   audit-tail message header `📋 *Audit* (last N)` had unescaped `(`
   and `)`, which Telegram MarkdownV2 reserves. Same root cause for
   the `/start` "✅ 已 ready (mode=harness)" line that I introduced in
   v1.0.6 — also unescaped parens.
2. **Twitter collector silently mocked when no token was supplied.**
   On a real-key install where `TWITTER_BEARER_TOKEN` was simply not
   set, the collector emitted deterministic-mock tweets into the
   hotspots scan, polluting clustering / angle-mining / publish
   decisions with synthetic signals. The operator never opted into
   mock mode.
3. (Diagnostic only — not fixed) `/scan` occasionally takes long
   enough that the operator re-issues it before the first one
   completes. The 300s subprocess timeout + `_notify_spawn_failure`
   path is in place; the most common cause is a slow Twitter API
   round-trip, which fix #2 partially mitigates by skipping Twitter
   when no token is set.

### Fixed

- `daemon.py::_send_audit_tail` — `\(last N\)` MarkdownV2-escaped.
- `daemon.py::_handle_message` `/start` "ready" branch — the
  `\(mode=...\)` parens are escaped, and `mode` flows through
  `escape_md2` to handle future tokens.
- `agent_d1/collectors/twitter.py::collect` — three-way behaviour
  matrix:
  * `MOCK_LLM=true` → deterministic fixtures (mock).
  * No `TWITTER_BEARER_TOKEN` AND `MOCK_LLM` not set → SKIP (return
    empty, log warning). Refuses to fabricate signals into a real
    scan.
  * Bearer token present → real Twitter API.

## [1.0.7] — 2026-04-30

Brand-leak follow-up. Fresh operators bootstrapping from the deploy bundle
were ending up with a `chainstream` topic profile baked in via
`config-examples/topic_profiles.example.yaml`, which the framework loader
falls back to when `~/.agentflow/topic_profiles.yaml` doesn't exist yet.
The result: a brand-new install would scan hotspots under "Publisher
default → ChainStream" without the operator ever choosing that brand.
Same root cause as v1.0.2's daemon prompts, different file.

### Fixed

- **`config-examples/topic_profiles.example.yaml` rewritten** to a
  brand-neutral schema demo. Replaced the `chainstream` profile (full
  brand, keyword_groups, hotspot_terms, search_queries, publisher_account)
  with a placeholder `your-brand` profile that demonstrates the schema
  without injecting any real brand identity. The second `ai_infra`
  profile is kept as a vertical-specific schema example.
- **`docs/integrations/AGENT_BRIDGE.md`** example payload uses
  `"publisher": "your-brand"` instead of `"publisher": "ChainStream"`.

### Notes

- `docs/PHASE_REPORT_2026-04-27.md` (historical phase report) still
  mentions ChainStream — left as-is since it's a closed retrospective,
  not framework code.
- `backend/tests/` test fixtures continue to use `chainstream` as a test
  string. Per the project's brand-neutrality contract, framework code +
  configs + integration docs must be neutral; test fixtures (clearly
  scoped to test data) are exempt.

## [1.0.6] — 2026-04-30

Two operator-visible fixes from a real Telegram-bot session:

1. **`/start` did not auto-flow into onboarding.** The operator typed
   `/start`, captured chat_id, got `review bot 在线`, and was then on
   their own — no hint that profile setup was needed, no auto-dispatch
   to `/onboard`.
2. **`af onboard` asked for "media" before identity.** The first section
   was `telegram`, then `llm`, then `embeddings`, then `atlas`, then
   `twitter` / `ghost` / `linkedin`. Operators reasonably objected:
   "why are you asking which platform I publish on before asking who I
   am?" Identity (brand / voice / sources / rules) wasn't anywhere in
   the CLI onboard at all — it was siloed in the TG `/onboard` flow.

### Fixed

- **`/start` now smart-dispatches** based on `_detect_next_step`. The
  handler runs the same state machine `af bootstrap --next-step --json`
  drives, and reacts:
  - `ready` → "✅ 已 ready" + hint to run `/scan`
  - `missing_profile` / `incomplete_profile` → auto-starts the
    `/onboard` profile-setup session (operator just answers prompts)
  - `missing_real_keys` → tells operator the exact `af onboard
    --section <provider>` command to run in their terminal
  - `daemon_not_running` → reassures (the daemon IS alive if `/start`
    got delivered) and points at `/scan`
  - other states → echoes the canonical `next_command` + reason

  First-`/start`-ever no longer needs a follow-up hint. Operators in
  fresh installs get walked through automatically.

- **`onboard` `_SECTIONS` reordered** to put identity FIRST. New order:
  1. **`profile`** — brand, voice, sources, rules (NEW; routes to
     `af topic-profile init -i` or `--from-file`). Required.
  2. `llm` — Moonshot / Anthropic. Required.
  3. `embeddings` — Jina / OpenAI. Required.
  4. `atlas` — AtlasCloud image generation. Required.
  5. `telegram` — Mode B/C only. **Optional** (was Required).
  6–11. per-platform / channel — twitter, ghost, linkedin, webhook,
     resend, style. All Optional.

  The Telegram section being Optional aligns with v1.0.4's
  daemon-opt-in posture (Mode A operators never need the bot at all).
  The `profile` section is a pseudo-section: it doesn't write env
  vars; it dispatches to `af topic-profile init` (interactive) so
  identity setup lives in one canonical place across CLI and TG.

## [1.0.5] — 2026-04-30

A documentation + bootstrap-detector release. The install path is now
**agent-driven**: a Claude Code / Cursor / OpenClaw harness can self-deploy
AgentFlow end-to-end by looping on `af bootstrap --next-step --json` until
the detector reports `stage: "ready"`. The detector previously forced every
operator into Mode B/C (Telegram review daemon required); v1.0.5 fixes that
to honour the architectural rule that the daemon is opt-in.

### Fixed

- **`_detect_next_step` no longer treats the Telegram daemon as a universal
  blocker.** The detector now auto-resolves the operator's mode from
  `TELEGRAM_BOT_TOKEN` presence and skips TG-only checks
  (`missing_chat_id`, `daemon_not_running`) for Mode A (harness-only)
  operators. Mode B/C operators (TG token set) still see those states as
  blocking, exactly as before. New `mode` field added to every
  `bootstrap --next-step --json` payload (`harness` / `tg_review` /
  `unknown`).
- **`ready` payload for Mode A surfaces an `optional_next` field** describing
  how to upgrade to Mode B/C later, so an agent that hits `ready` knows
  the daemon path exists without the previous false signal that the
  daemon was required.
- **`_resolved_env_var` helper** reads the requested var from both the
  `.env` file and `os.environ`, so an operator who pre-exports
  `TELEGRAM_BOT_TOKEN` from the shell (or has systemd inject it) is
  correctly classified as Mode B/C.

### Changed

- **`INSTALL.md` rewritten as agent-driven** install doc. Top-level TL;DR
  is the bootstrap loop; full state table maps each `current_state` to
  its canonical `next_command`; secrets locations + `af onboard` /
  `af keys-*` commands documented; Mode A vs B/C contract spelled out.
  Removes the stale `echo 'MOCK_LLM=true' > .env` step from v1.0.0 era
  (writes to the wrong file post-v1.0.4).

### Pairs with sibling repo

- `witness1993x/agentflow-skills v1.0.3` adds
  `agentflow/references/install.md` — harness-side companion to the
  runtime install doc, structured as the single source of truth on
  what the agent should do for each `current_state`. The top-level
  `agentflow/SKILL.md` "Default entry" block is rewritten to point at
  it.

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
