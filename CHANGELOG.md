# Changelog

All notable changes to **agentflow-article-publishing** (the canonical
runtime for the AgentFlow content pipeline) are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version number is the single value in
[`backend/pyproject.toml::project.version`](backend/pyproject.toml); the
sibling skill-distribution repo
([`agentflow-skills`](https://github.com/witness1993x/agentflow-skills))
is versioned independently and tracks the **`blogflow` / `mediaflow` CLI
surface** rather than runtime code parity.

## [Unreleased]

- _no changes yet_

## [1.3.11] — 2026-05-11 — Recall-source observability + auto-prune dead handles

> Live `blogflow article-hotspots` against the real ChainStream profile
> exposed the operational reality: 65/65 high-weight Twitter KOL handles
> came back 402 Payment Required (Twitter free tier can't pull
> per-user timelines), profile_search returned 0 hits across all 5
> queries, no Brave queries ran (no API key), no RSS ran (default off).
> Final pool: 0 hotspots. Every downstream filter / threshold / fallback
> mechanism was being asked to work miracles on an empty input.
>
> This release adds the missing observability + cleanup tools so the
> operator can see and fix the source layer without spelunking logs.

### `~/.agentflow/review/source_health.json` (emitted automatically)

- Every `blogflow article-hotspots` run now writes this file at end of
  scan. Captures:
  - `hotspot_count` in the run
  - `per_source_signal_counts`: how many signals each source kind
    contributed (twitter, hn, brave, rss, etc.)
  - `twitter_handles.total_probed` + `by_status` (alive /
    payment_required / not_found / forbidden / unauthorized /
    rate_limited / other) + `dead` list with each handle + http code
- Source: new `_HANDLE_HEALTH` snapshot captured in
  `agent_d1/collectors/twitter.py::_fetch_real_sync`, drained at end
  of scan by `agent_d1/main.py::_emit_source_health`.
- Best-effort: failures here never block the scan.

### New CLI: `blogflow source-doctor`

- Reads `source_health.json`, surfaces per-source signal yield + Twitter
  handle health table.
- `--fix-block`: edits `sources.yaml` in place to set
  `weight: blocked` on dead handles. Writes a `.bak` of the original.
- Block policy:
  - **Always blocks**: `not_found` (404 / dead account), `forbidden`,
    `unauthorized` — these can't recover.
  - **Opt-in via `--include-rate-limited`**: 429s (may be transient).
  - **Opt-in via `--include-payment-required`**: 402s. Default OFF
    because a paid Twitter tier would restore them; if the operator
    doesn't have a paid tier, pass the flag to stop wasting scan
    time + log noise on these handles.
- Adds an inline note: `[auto-blocked by source-doctor; was <status>]`
  so the audit trail is visible in the yaml.
- Idempotent — re-running on already-blocked handles is a no-op.

### `.env.template` recommendations updated

- `AGENTFLOW_TWITTER_SEARCH_ENABLED` now defaults to `true` in the
  template (was `false`). Twitter keyword search bypasses the
  per-user-timeline 402 wall — much cheaper way to use Twitter than
  KOL pulls.
- `AGENTFLOW_BRAVE_SEARCH_ENABLED` now defaults to `true` in the
  template (was `false`). Brave Web Search is the recall-pool
  insurance policy for niche profiles where HN + Twitter both fail
  to surface enough domain content.
- Added a §"v1.3.11 — `blogflow source-doctor`" hint section calling
  out the post-scan diagnostics flow.

### `check_recall_sources_enabled` fix

- v1.3.9 introduced this check but referenced the wrong env var name
  (`BRAVE_SEARCH_API_KEY` — code actually reads `BRAVE_API_KEY`).
  Fixed; doctor extras now correctly reflect `brave_api_key_set`.

### Tests

- 297/297 still passing — all new code is additive.

### Real-world demo

- Live ran on `~/.agentflow` against the chainstream profile, captured
  in `docs/PRODUCTION_WALKTHROUGH.md` continuation. 65 dead handles
  identified + tested `source-doctor --fix-block` successfully (then
  reverted to leave the operator's actual config untouched).

## [1.3.10] — 2026-05-11 — Gate B/C silent-emit P0 + production walkthrough

> Caught while doing a live end-to-end dry-run of the v1.3.9 pipeline:
> Gate B and Gate C Lark review cards never emit in production. The
> `check_gate_b()` / `check_gate_c()` helpers return `(lines, int)`
> where `int` is a blocker count, but the corresponding
> `_emit_lark_gate_b_card` / `_emit_lark_gate_c_card` builders did
> `list(blockers)` → TypeError on the int. Worse: the entire emit
> + side fanout sat under a single outer `try/except` logging at
> INFO with the misleading message "Lark draft fan-out skipped".
> Result: every Gate B / C emit silently failed; operator never
> saw the cards; log line lied about the cause. This regressed in
> Phase 3 when TG was removed (TG previously surfaced the same data
> via its own emit path which didn't crash on int blockers).

### `_normalize_blockers` + `_blocker_count` helpers (triggers.py)

- Accept `int | str | list | None` and emit `list[str]`.
- Used by both Gate B and Gate C card builders.
- New `blocker_count: int` field in the payload so skill agents
  can render a numeric badge without parsing the list.

### Split main emit from side fanout (`post_gate_b`)

- Pre-fix: one `try/except` wrapped `_emit_lark_gate_b_card` AND
  `lark_webhook.notify_draft_ready`. Either failure logged at
  INFO with "Lark draft fan-out skipped".
- Post-fix: two separate `try/except` blocks. Main emit failure
  now logs at WARNING with the explicit message "Gate B Lark
  review-card emit FAILED for X — operators will not see this
  draft in Lark". Side fan-out (Custom Bot notify) stays at INFO.
- `post_gate_c` follows the same shape now.

### `docs/PRODUCTION_WALKTHROUGH.md` (new)

- End-to-end captured walkthrough of v1.3.10 in mock mode:
  doctor → smoke test → Gate A → Gate B → Gate C → Gate D → publish,
  with real queue event payloads + skill-agent `lark-cli-emit`
  commands captured live (not pseudocode).
- Includes the Profile setup multi-turn flow (v1.3.6 path).
- Closes with the "Phase 3 silent-emit regression" class summary
  and an audit grep recipe to find any remaining silent-broken
  sites in `triggers.py`.

### Tests

- 297/297 still passing.

## [1.3.9] — 2026-05-11 — Doctor catches the "sources configured but ENABLED off" footgun

> Real root cause uncovered while investigating the ChainStream
> too_narrow / 0-matched recall reports. `sources.yaml` had 9
> brave_search + 6 twitter_search ChainStream-aligned queries
> seeded (since v1.1.9). Both `AGENTFLOW_BRAVE_SEARCH_ENABLED` and
> `AGENTFLOW_TWITTER_SEARCH_ENABLED` were off (default), so the
> collectors silently returned empty lists at line 142-143 / 180-181
> of `agent_d1/main.py`. Recall pool collapsed to HN Algolia,
> couldn't surface ChainStream-domain content, triggered every
> downstream symptom: too_narrow boundary, v1.3.7 soft-floor
> fallback, v1.3.8 streak ticker. **All four symptoms had one root
> cause: a missed env flag the operator could not see**.

### New preflight check: `check_recall_sources_enabled`

- Parses `~/.agentflow/sources.yaml`, counts live (non-blocked)
  `brave_search` / `twitter_search` entries.
- Cross-references env flags `AGENTFLOW_BRAVE_SEARCH_ENABLED` /
  `AGENTFLOW_TWITTER_SEARCH_ENABLED`.
- Cross-references API keys `BRAVE_SEARCH_API_KEY` /
  `TWITTER_BEARER_TOKEN`.
- Emits row in `blogflow doctor`:
  - ✗ when queries configured but ENABLED flag off → "Recall pool
    will collapse to HN Algolia only — set the missing env flag(s)
    in .env to actually run these queries."
  - ✓ when at least one extra recall source is active → "extra
    recall sources active: brave=N, twitter=M"
  - · (info) when no brave/twitter entries are in sources.yaml at
    all (operator consciously skipped)
- Added between `check_hotspots_mock_leak` and `check_telegram` in
  `all_checks()` so it surfaces near the top of the doctor matrix.

### Why this matters

The whole "ChainStream 0-matched" thread (v1.3.6 → v1.3.7 → v1.3.8)
was treating symptoms because the diagnostic surface didn't show
this misconfig. With v1.3.9 the operator would have seen at first
`blogflow doctor` run:

```
✗ Recall sources enabled    brave_search has 9 live queries but
                             AGENTFLOW_BRAVE_SEARCH_ENABLED is not
                             true; twitter_search has 6 live queries
                             but AGENTFLOW_TWITTER_SEARCH_ENABLED is
                             not true. Recall pool will collapse to
                             HN Algolia only...
```

— and gone straight to the fix instead of debugging through
filter→profile→threshold layers.

### Tests

- 297/297 still passing. The new check is additive in `all_checks()`
  and doesn't gate any readiness assertion (doctor is non-strict).

## [1.3.8] — 2026-05-11 — Direction A: consecutive soft-floor-fallback detection

> Pairs with v1.3.7. Once Gate A starts emitting `gate_warning` daily,
> the operator could keep accepting low-fit candidates indefinitely
> without realizing the root cause is signal source ↔ profile
> misalignment (not threshold knobs). v1.3.8 adds a per-publisher
> streak counter; after 3 consecutive fallback days (env
> `AGENTFLOW_SIGNAL_MISALIGNMENT_DAYS`) the daemon emits a one-time-
> per-streak `notify.signal_misalignment` event prompting the
> operator to add seed sources via
> `blogflow learn-from-handle <h> --profile <id>` rather than
> chase regex / threshold widening.

### New helpers in `triggers.py`

- `_signal_misalignment_state_path()` → `~/.agentflow/review/signal_misalignment.json`.
- `_track_signal_misalignment(*, publisher_brand, in_fallback)` →
  reads state, increments streak on consecutive fallback days,
  resets on a successful (non-fallback) emit. Returns a notify
  payload when the streak crosses the threshold AND we haven't
  already pinged the operator today (per-day cooldown).
- `post_gate_a` calls it after the soft-floor branch; if a payload
  comes back, emits `notify.signal_misalignment` with
  `suggested_action: add_seed_sources` + a CLI hint.

### State file shape

```json
{
  "chainstream": {
    "last_fallback_date": "2026-05-11",
    "consecutive_days": 3,
    "last_notified_date": "2026-05-11"
  }
}
```

### Tests

- 297/297 still passing.

## [1.3.7] — 2026-05-11 — Hard topic-fit gate gains soft-floor fallback

> Operator on ChainStream-style narrow profiles reported "every scan
> ends in 0 matched / too_narrow / no Gate A card." Root cause: the
> v1.1.9 hard topic-fit gate (`AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD`,
> default 0.10) drops every hotspot below the threshold, and when it
> drops ALL of them `post_gate_a` silently returns `None` — no card,
> no visibility into why. This was supposed to prevent forced-analogy
> articles ("TCG customs failure ≈ ChainStream data infra"), but the
> medicine was killing the patient on niche-profile days when general
> tech news genuinely doesn't overlap with the publisher's domain.

### Behavior change in `triggers.post_gate_a`

- If the hard gate would drop EVERY hotspot, instead of returning
  `None` we now drop to a softer floor (env
  `AGENTFLOW_TOPIC_FIT_SOFT_FLOOR`, default 0.02) and emit Gate A
  with a `gate_warning` payload + every candidate auto-tagged with
  the existing `low_topic_fit` red flag.
- Set `AGENTFLOW_TOPIC_FIT_SOFT_FLOOR=0` to restore the old
  silent-skip behavior (for operators who'd rather have nothing than
  off-domain candidates).
- The `gate_warning` payload carries `kind=soft_floor_fit_fallback`,
  the actual threshold values, candidate counts, and a Chinese
  one-liner the skill agent can render verbatim in the Gate A card.
- `_emit_lark_gate_a_card` gained an optional `gate_warning` kwarg
  that flows the warning into the Lark card payload as `gate_warning`.

### Operator playbook

When the warning fires, two ways forward:

1. **Tighten sources**: today's `article-hotspots` upstream was off-
   domain. Add seed handles / RSS / Twitter lists that match the
   profile better — Direction A in the diagnosis.
2. **Accept the soft floor for now**: open Gate A as normal, but
   reject candidates that aren't writable. The `low_topic_fit` flag
   warns the LLM not to force analogies if the operator does pick
   one to write.

### Tests

- 297/297 still passing. The two soft-floor-fallback paths are
  covered by the existing topic-fit hard gate tests (which assert
  `return None` on empty was not the case for the `else` branch);
  new behavior is additive.

## [1.3.6] — 2026-05-11 — Profile flow Lark-event regressions fixed

> User reported: "现在好像没有触发和保存 profile" — Wave C left two
> seams in `daemon.py` as no-op TG-removal stubs that never got
> rewired to emit Lark events. The Lark-callback path
> (`lark_callback._handle_profile_advance`) was always intact, but
> CLI-driven profile applies + the secondary daemon-side advance
> path (preserved for backward-compat with tests) had no operator
> visibility under Mode A.

### `_send_profile_setup_question` now emits a Lark event card

- Wave C left it as `return None` with a comment "Lark profile-advance
  card flow in lark_callback.py is the live path; this helper is
  preserved only as a no-op". Correct that the lark_callback path is
  the live one for *button-driven* advance, but this daemon-side
  helper is what `_maybe_handle_profile_session_reply` calls when an
  answer arrives via a different surface (and what tests still hit).
  Now calls `triggers._emit_lark_profile_question_card(...)` with the
  current step's prompt, question_field, index/total — so the skill
  agent sees the next question card and can render it to Lark.

### `_spawn_apply_profile_session` now emits Lark notify events

- Success → `notify.profile_setup_done` with profile_id + session_id
  + mode (init/update). Same event shape lark_callback already emits
  on its own apply path, so the skill agent's downstream handling
  doesn't need to discriminate between sources.
- Failure → `notify.profile_setup_failed` with error tail.
- Crash → same failure event with `crash: <err>` prefix.
- Pre-fix behavior was log-only — operator on Lark saw nothing
  after triggering a CLI-driven profile setup, even though the apply
  ran in the background.
- Also dropped the `if chat_id is None: return` early-return guard:
  in Mode A chat_id is always None and the apply should still run +
  emit notify events into the queue.

### Tests

- 297/297 still passing — no test changes.

## [1.3.5] — 2026-05-11 — SKILL.md webhook framing rewrite

> Closes a UX bug observed in the wild: even after v1.3.4 made the
> skill self-contained, the cloud-computer agent was still asking
> users for `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL`, `AGENTFLOW_AGENT_EVENT_AUTH_HEADER`,
> `AGENTFLOW_AGENT_BRIDGE_TOKEN`, "review-dashboard 怎么起",
> "Lark callback 到 bridge 再到 daemon 的闭环" — i.e. all the Mode B
> webhook concepts that don't exist in the file-queue path. The skill
> framed Mode A and Mode B as equally valid choices, so the agent
> would happily walk users through Mode B prerequisites that have no
> meaning in Mode A.

### What changed in `SKILL.md`

- §"Lark Card Rendering" rewritten so Mode A (Agent-Lark Window /
  file queue) is THE default and described first; opening TL;DR
  bullet explicitly says "no webhook, no review-dashboard, no
  bridge token, no auth header — these are Mode A non-issues, do
  not ask the user about them."
- Mode B section retitled "Mode B (advanced):Webhook 部署 — 多数 skill
  agent 不该走这条" with a trigger condition: "OpenClaw runs on a
  *different* machine from daemon AND you can configure a real
  webhook URL pointing at it. If you're not sure, it's not." Default
  remains Mode A.
- Anti-pattern #11 added: "On cloud-computer / same-host deploy, do
  NOT ask the user for `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` /
  `AGENTFLOW_AGENT_EVENT_AUTH_HEADER` / `AGENTFLOW_AGENT_BRIDGE_TOKEN`
  / review-dashboard port. These are Mode B artifacts — Mode A is
  zero-config." Existing #11/#12/#13 shifted to #12/#13/#14.
- §"Required Runtime" `.env` row rewritten: only required vars are
  `AGENTFLOW_LARK_APP_PRIMARY=true` + at least one LLM/embedding/Atlas
  key. Webhook / bridge / dashboard mentions removed.
- §"Required Init Flows" "Lark App 主路径" row rewritten: drops the
  webhook URL / bridge token / dashboard port requirements; adds
  "Don't push these on the user" to the NEVER column.
- Package-contract paragraph rewritten: review-daemon writes to the
  file queue by default; Lark button callback flows back via
  `blogflow lark-cli-emit` CLI, no HTTP bridge required.
- `Repo facts` "入口" line: TG bot reference removed (Phase 3 deleted
  it); webhook reference replaced with Mode A/B language.

### Bundled artifacts unchanged

- Skill bundle file count (12) and contents (`lark_review_cards.md`,
  `CLOUD_COMPUTER_DEPLOY.md`) unchanged from v1.3.4 — only SKILL.md
  text edits.

### Tests

- 297/297 still passing — no test changes.

## [1.3.4] — 2026-05-11 — Skill self-contained for cloud-computer ops

> Closes a hole in v1.3.3: SKILL.md *referenced* `docs/CLOUD_COMPUTER_DEPLOY.md`
> but skill agents on a constrained cloud computer only see the skill
> bundle, not the deploy tarball's docs. Ops guidance is now bundled
> AND inlined in SKILL.md so the skill agent can guide a fresh install
> without external lookups.

### `references/CLOUD_COMPUTER_DEPLOY.md` bundled into skill

- The 8-step runbook is now copied into the skill bundle's references/
  alongside `lark_review_cards.md`. Skill agents read both directly from
  the package — no filesystem access to the deploy tarball needed.

### SKILL.md §"Cloud-Computer First-Time Deploy"

- New top-level section that inlines a condensed 8-step deploy flow
  (Python → pip → install → .env → doctor → smoke-test → daemon → tail
  loop → end-to-end verification). Plus a 5-row "stuck symptom →
  direct line" troubleshooting table.
- §"Default Entry: First Deployment" gains a Step-0 deployment-form
  detector: managed install (form A: backend/.venv/bin/blogflow exists)
  vs cloud computer (form B: ~/.local/bin/blogflow exists, no venv) vs
  empty machine (form C). Skill agent picks the right walkthrough.

### Tests

- 297/297 still passing (no test changes).

## [1.3.3] — 2026-05-11 — Deliverable mode complete (smoke test + runbook)

> Closes the cloud-computer deployment loop. The Agent-Lark Window
> architecture from v1.3.2 is now operator-runnable end-to-end without
> consulting source code: smoke-test command, single-page runbook, and
> the card schema reference is bundled inside the skill itself.

### `blogflow agent-events-emit-test`

- New CLI: emits a synthetic `review.gate_a_card` event into the queue
  / webhook for end-to-end smoke verification. Reports the active mode
  (file / webhook / both), the queue path, and the byte delta after the
  append. Used as the final verification step in
  `docs/CLOUD_COMPUTER_DEPLOY.md` step 5.

### Card schema bundled into skill

- `.cursor/skills/agentflow-open-claw-v2/references/lark_review_cards.md`
  is now a copy of `backend/agentflow/agent_review/templates/lark_review_cards.md`
  (308 lines). The skill agent no longer needs filesystem access to
  the deploy backend tree to render cards correctly. SKILL.md updated
  to reference the bundled path.

### `docs/CLOUD_COMPUTER_DEPLOY.md`

- Single-page operator runbook (≈250 lines) covering the full
  no-sudo / no-Docker / no-webhook deploy: get-pip.py user-install →
  unpack tarball → pip install --user -e . → .env config → doctor →
  smoke-test → daemon foreground / nohup / systemd-user → skill agent
  side-loop verification. Plus three appendices: portable Python
  fallback, air-gapped wheel install, and dual-write `MODE=both` mode.

### SKILL.md

- §"模式 A: Agent-Lark Window" prerequisites bullet now points at
  `docs/CLOUD_COMPUTER_DEPLOY.md` for the end-to-end install steps and
  at `blogflow agent-events-emit-test` for the one-line smoke probe.

### Tests

- 297/297 still passing (no test changes — pure delivery additions).

## [1.3.2] — 2026-05-11 — Agent-Lark Window mode (no-webhook deploy)

> Targets the constrained-cloud-computer scenario where the operator
> can install Python (after a `get-pip.py` --user dance) but cannot
> stand up an HTTP listener for the OpenClaw bridge. The daemon now has
> a "file queue" delivery mode for `review.*_card` / `notify.*` events
> — the OpenClaw skill agent tails the queue and pushes Lark cards via
> its already-mounted Lark window, no inbound webhook required.

### `agent_bridge.emit_agent_event` gains a file-queue fallback

- New env var `AGENTFLOW_AGENT_EVENT_MODE` ∈ {`webhook`, `file`, `both`}.
  Auto-resolves to `webhook` if `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` is set,
  else `file`.
- `file` mode appends each envelope as one JSON line to
  `~/.agentflow/agent_events/queue.jsonl`. Append-only, audit-friendly.
- Webhook mode unchanged. `both` is a migration aid.

### `preflight.check_lark_app_primary` no longer fails on missing webhook

- v1.3.1 returned `valid=False` if `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL`
  wasn't set. v1.3.2 detects file-queue fallback and reports
  `valid=True` with a message pointing operators at SKILL.md §"Agent-Lark
  Window mode". Forced `AGENTFLOW_AGENT_EVENT_MODE=webhook` without a
  URL still fails (real config error).

### New CLI: `blogflow lark-cli-emit` + `blogflow agent-events-tail`

- `lark-cli-emit` injects a Lark callback (`lark_gate_b_approve`,
  `lark_message`, etc.) directly into `lark_callback.handle_event` via
  CLI — no `/api/commands` HTTP server needed. Used by the Agent-Lark
  Window flow when an operator clicks a Lark button: the OpenClaw skill
  agent shells out to this command to forward the callback into the
  daemon.
- `agent-events-tail` streams the on-disk queue. Supports `--follow` for
  long-running tails and `--from-start` for replay.

### SKILL.md `agentflow-open-claw-v2`

- New §"Lark Card Rendering — 两种部署模式" section. Mode A
  (Agent-Lark Window, file queue) is documented as the recommended path
  for cloud-computer / OpenClaw-with-mounted-Lark-window deployments.
  Mode B (legacy webhook) is preserved for traditional HTTP server
  setups. Includes a Python pseudocode tail loop, cursor handling, and
  the new CLI helpers.
- Removed the stale "TG fallback" advice — Phase 3 (v1.3.0) deleted
  Telegram. Constrained environments must use Mode A.

### Tests

- `test_lark_primary_preflight_fails_without_event_webhook` replaced by
  two finer-grained tests: one for the file-queue fallback, one for
  `MODE=webhook` without a URL still failing.
- Test suite: 295 → 297 passing (+2).

## [1.3.1] — 2026-05-10 — render.py removed + timeout-sweeper regression fix

> v1.3.0 left `render.py` (802 lines, TG-Markdown + inline-keyboard
> renderer) on disk because triggers.py's `_sid.register()` calls were
> embedded inside each `render_X()`. This release refactors triggers.py
> to call `_sid.register()` directly and deletes the module. While there,
> a regression in `daemon.py`'s timeout sweeper (introduced by Phase 3
> Wave C) is also fixed.

### `render.py` removed (commit `87b1004`)

- 15 `render.X(...)` callsites in `triggers.py` refactored to direct
  `_sid.register(gate=..., article_id=..., ttl_hours=...)` calls per
  gate. The text/keyboard outputs of the deleted render functions had
  been unused locals since Phase 3 Wave B. Per-gate TTL helpers
  (`_gate_b_ttl_hours`, `_gate_c_ttl_hours`) inlined into triggers.py.
- `export_body_markdown(article_id)` inlined as `_export_body_markdown`
  in triggers.py (only consumer was the Lark draft fan-out).
- 7 tests + the `TgMenuV103Tests` class in `test_v02_workflows.py`
  deleted — they asserted on rendered Markdown content the runtime no
  longer produces. `from agentflow.agent_review import render as
  review_render` import removed.
- `render.py` deleted (-802 lines).

### `daemon._scan_timeouts` regression fix (same commit)

- `_safe_send` was reduced to a no-op (`return False`) in Phase 3 Wave
  C, but the surrounding `if _safe_send(text): timeout_state.mark_first_pinged(aid)`
  guards meant the timeout state was never marked, so the sweeper
  re-fired the same audit log every 60 seconds for every pending
  article. Fixed: the sweeper now records timeout state + audit
  unconditionally and runs the auto-skip / auto-cancel transitions
  where applicable. The active Lark gate card is already on the
  operator's screen — re-pinging via daemon-side text would just spam.
- `_safe_send` and `_title_of` shims deleted along with all 5
  `_render.escape_md2(...)` callsites in daemon.py.

### Net diff

`+142 / -1196` across 4 files. Test suite: 296/296 passing clean
(303 → 296 after deleting 7 review_render tests).

## [1.3.0] — 2026-05-10 — Phase 3 — Telegram surface removed

> The user's original 痛点 ("上下文太多导致 daemon 判定一直会走 tg bot")
> is now structurally impossible: the Telegram poll loop, dispatch chain,
> and SDK file are all deleted. AgentFlow runs Lark-first by construction.
> daemon.py shrinks from 5634 → 1748 lines (-69%). 30 TG-runtime tests
> retired in favor of the Lark-first suite.
> Test suite: 333 (1.2.2) → 303 passing clean (-30 deleted, +0 failures).

### Wave A — CLI surface (commit `d717afa`)

Removed CLI entry points that were TG-only with no Lark counterpart:

- `blogflow review-init` — printed bot info via `tg_client.get_me()`
  and the captured chat_id. Lark uses `open_id`; deployment health is
  covered by `blogflow doctor`.
- `blogflow review-publish-stats --tg` — posted stats card to TG.
- `blogflow learning-review --post-tg` and `_post_to_tg` helper —
  posted the markdown report to TG. Report stays CLI-only.

### Wave B — `triggers.py` (commit `4db98d4`)

29 `tg_client.*` callsites stripped. 7 TWINNED functions had `_emit_lark_*_card`
siblings already wired (post_gate_a / post_gate_b / post_gate_c / post_gate_d /
post_profile_setup_prompt / post_locked_takeover / post_image_gate_picker) —
the TG halves were deleted; Lark cards now fire unconditionally with
`telegram_message_id=None`. The 7 TG-ONLY publish/notification functions
(post_critique, mark_published, post_publish_ready, post_dispatch_preview,
post_publish_dispatch, post_publish_retry, post_publish_digest) had their
TG sends removed but the surrounding state writes / audit trail /
pending_edits registration / lark_webhook fan-outs preserved. Net diff:
-235 lines.

### Wave C — `daemon.py` poll loop + handlers (commit `24a6e9c`)

`daemon.py` shrinks from 5634 → 1748 lines (-69%). Removed:

- `_handle_message`, `_handle_callback`, `_route` + `_ACTION_REQ` map
  (the entire TG poll-loop dispatch chain — ~3300 lines).
- `_populate_command_registry`, `_register_command`, `_resolve_command`,
  `_dispatch_v104_command`, `_COMMAND_REGISTRY` global.
- All TG slash handlers: `_handle_onboard`, `_handle_doctor`, `_handle_scan`,
  `_handle_profile*`, `_handle_keyword*`, `_handle_style*`, `_handle_intent*`,
  `_handle_prefs*`, `_handle_report`, `_handle_v103_passthrough`,
  `_handle_restart_daemon`, `_handle_pending_confirm_reply`.
- Helpers: `_send_status_summary`, `_send_queue_summary`, `_send_audit_tail`,
  `_send_auth_debug`, `_slash_skip`, `_slash_publish_mark`, `_build_help_text`.
- TG poll loop branch in `run()` (`get_updates` polling).
- `tg_client` lazy import + sentinel inside `daemon.py`.
- Residual TG side-channel calls inside surviving helpers (timeout sweeper,
  downtime warning, mock-mode warning, deferred Gate A repost ping). They
  log only now; Lark fan-out happens via `lark_webhook` where applicable.

Behavior change: `run()` raises `SystemExit` if `AGENTFLOW_LARK_APP_PRIMARY`
is not truthy. Operators must opt into Lark-first mode explicitly.

Kept (Lark or shared callers): `get_review_chat_id`, `set_review_chat_id`,
`configure_bot_menu` (no-op stub for back-compat callers/tests),
`_slash_defer`, `_audit`, `_audit_slash`, all state / timeout / hotspot /
image-gate primitives, the embedded bridge wiring, and the Lark-only
bookkeeping main loop (heartbeat + GC + timeout sweep + deferred-repost
drain + scheduled hotspots).

### Wave D — file deletions + test cleanup (commits `3166ccd` + `6875963`)

- **Deleted** `backend/agentflow/agent_review/tg_client.py` (330 lines, the
  entire TG REST client wrapper).
- `triggers.py`: removed `_TgClientUnavailable` sentinel + the lazy
  `try/except ImportError` import scaffolding (62 lines). `_tg_configured()`
  preserved as a back-compat predicate that returns False in practice.
- `preflight.py`:
  - `check_telegram` no longer probes the SDK; reports env-var status only.
  - `critical_for_review_daemon` returns `[check_lark_app_primary()]`
    unconditionally; legacy TG fallback branch deleted.
- `tests/test_no_tg_runtime.py`: rewritten — Phase 2 L-1 sentinel/import-
  fallback regression test is moot. New file asserts that `tg_client` and
  `_TgClientUnavailable` symbols are absent at module level (guard against
  re-introduction) and that `_emit_lark_review_card` is still present.
- `tests/test_e2e_lark_pure.py`: Phase 2 canary that patched every
  `tg_client.*` with a detonating sentinel is now redundant. The
  `tg_violations` list is preserved (always empty) for back-compat; the
  `_TG_FN_NAMES` tuple is deleted.
- `tests/test_v02_workflows.py`: 30 TG-runtime tests deleted (1079-line
  diff), including all of `TgMenuV103Tests`, `MarkdownV2EscapeRegressionTests`,
  and `TopicFitHardGateTests` whole-class deletions, plus targeted method
  removals across `TopicProfileIntentTests`, `LarkReviewCardTemplateTests`,
  `V016BatchTests`, `LocalMockPipelineTests`, `MediumWorkflowTests`.

Note: `render.py` (802 lines, TG-format Markdown V2 rendering) is still
on disk. Its outputs (`sid` registration, `body_doc` Markdown export)
feed `triggers.py` paths that haven't been refactored to talk to the
`_sid` module directly. Refactor + deletion is deferred to the v1.3.x
maintenance line; v1.3.0 ships with `render.py` intact but its TG-Markdown
text/keyboard outputs become unused locals in `triggers.py`.

## [1.2.2] — 2026-05-08 — Phase 2 truly final (L-5 + skill v3.0 path fix)

> Seals Phase 2 by closing L-5 inside pytest **and** repairing the two
> repo-root-relative paths in `LarkReviewCardTemplateTests` so the full
> suite runs green from `backend/` as cwd. After this tag, every Phase 2
> ID (L-1…L-5) has automated coverage. Phase 3 (delete `tg_client.py`,
> `render.py`, ~1500 lines of TG handlers in `daemon.py`) remains the
> only outstanding item.
> Test suite: 330 (1.2.1) → 333 passing (+3 from L-5; 2 stale failures
> resolved without test-count delta).

### L-5 doctor --fresh no-TG validation (closes Phase 2 §6.2 acceptance)

- Phase 2's last open item closed inside pytest. New subprocess-isolated
  test `tests/test_l5_doctor_no_tg.py` (3 cases) installs a `meta_path`
  finder that blocks `tg_client` BEFORE click loads, then invokes
  `blogflow doctor --fresh` via `CliRunner` with `TELEGRAM_BOT_TOKEN`
  cleared (and force-emptied to defeat `_load_dotenv_once` re-fill from
  `backend/.env`). Asserts exit 0, "TELEGRAM_BOT_TOKEN not set" in the
  matrix, and no `ImportError`/`Traceback` leak — proves the doctor
  command is Phase 3 deletion-tolerant in both legacy and
  `AGENTFLOW_LARK_APP_PRIMARY=true` modes, and in `--json` mode.
- `docs/BLOGFLOW_TG_TO_LARK_PARITY.md §11.5` L-5 row → ✅.

### LarkReviewCardTemplateTests path fix

- `test_lark_first_flow_reference_documents_daemon_owned_bridge` and
  `test_openclaw_skill_reference_points_to_lark_first_flow` were reading
  `docs/flows/...` and `.cursor/skills/...` via bare relative paths,
  which only resolve when pytest is invoked from the repo root. Pytest
  in this repo runs with cwd = `backend/`, so they failed every run.
- Fixed by rooting the paths at `Path(__file__).resolve().parents[2]`
  (the repo root) — the same idiom the sibling `test_template_covers_all_review_cards_buttons_and_inputs`
  test in this class already uses for `parents[1]`. The skill content
  itself was already v3.0-shaped; only the test plumbing was wrong.

## [1.2.1] — 2026-05-08 — Phase 2 closure (L-2 / L-3 / L-4)

> Closes the remaining Phase 2 follow-ups identified during the v1.2.0
> e2e verification. After this release, **all of Phase 2's audit-trail,
> auth-hardening, and write-back gaps are resolved**. L-5 (`blogflow
> doctor --fresh` no-TG validation) is then sealed in `[Unreleased]` via
> an in-pytest subprocess test.
> Test suite: 308 → 330 passing (22 new across L-2/L-3/L-4).

### L-2 Profile yaml writeback (`_handle_profile_advance` completion)

- Completion branch of `_handle_profile_advance` now mutates
  `~/.agentflow/topic_profiles.yaml` via `build_patch_from_answers` +
  `upsert_profile(replace_lists=False, source="lark_profile_advance:<sid>")`.
  Previously answers stayed in `session["collected"]` and never reached
  the on-disk profile.
- Added `_PROFILE_FIELD_TO_SLOT` translation: dotted missing-field keys
  (`publisher_account.brand`, `keyword_groups.core`, etc.) → friendly
  slots that `build_patch_from_answers` understands. Non-trivial rename:
  `keyword_groups.core` → `core_terms`.
- Added `_split_profile_terms`-style separator policy for list-valued
  slots (`do`, `dont`, `product_facts`, `default_tags`, `core_terms`,
  `search_queries`, `avoid_terms`): splits on ASCII / 中文 comma,
  semicolon, 、, newline + bullet/dash strip.
- Best-effort: writeback failures log + surface warning in the success
  card body, but don't block `release_session_lark` or
  `notify.profile_setup_done` emission. D1 scan never gated on
  yaml-write hiccups.
- Tests: `test_l2_profile_yaml_writeback.py` (7 tests).

### L-3 chrome_defer + lark_defer button real scheduling

- `_handle_chrome_defer` ("推迟 <id> <h>") was ack-only — wrote audit
  memory but never called `_schedule_deferred_repost`. Now wires to the
  real store at `~/.agentflow/review/deferred_reposts.json`. The daemon
  poll loop drains via `_drain_deferred_reposts` → `triggers.post_gate_*`
  which already dual-emits on TG + Lark surfaces (no schema change
  required).
- Validates current state is `*_pending_review` (mirrors TG `/defer`
  semantics) and emits `wrong_state` error otherwise.
- **Latent bug fix (follow-up commit)**: `_handle_defer` (the per-card
  `lark_defer` BUTTON used by Gate A/B/C/D `推迟` buttons) was also
  ack-only — same bug. Same `_schedule_deferred_repost` wiring
  applied. This was masked by the v1.2.0 e2e test not exercising
  the defer path.
- Tests: `test_l3_chrome_defer_wiring.py` (10 tests covering both
  chrome path and button path).

### L-4 Legacy `_authorize_or_deny` migration to v2 fail-closed

- Migrated 5 inline callsites of legacy `_authorize_or_deny` (which
  used fail-OPEN `is_lark_authorized`) to `_authorize_or_deny_v2`
  (fail-CLOSED via `is_authorized_open_id`). Discovery: although
  ~30 handlers were initially scoped, all per-handler auth is
  centrally dispatched at `handle_event` line 4067 + 4 router
  helpers — migrating those 5 covers all Gate B/C/D/L/A handlers.
- **3-state `auth.json` semantics** in `is_authorized_open_id`:
  - File ABSENT → fail-OPEN (dev-friendly default; preserves existing
    tests that don't seed an auth.json)
  - File present, `lark_operators` empty/missing/malformed → fail-CLOSED
    (operator explicitly chose the closed model)
  - File present, populated → strict lookup; `"*"` wildcard supported
- Legacy `_authorize_or_deny` preserved as importable compat surface
  with deprecation docstring (Phase 3 will remove).
- 4 pre-existing fail-closed tests rewrote fixtures to seed `auth.json`
  (`test_lark_callback`, `test_lark_chrome_intents`, `test_lark_profile_advance`,
  `test_lark_suggestions`). Without those nudges they would have
  passed under the new file-absent fail-open default.
- Tests: `test_l4_auth_migration.py` (5 tests).

### Production deploy notes

- **`LARK_OPERATOR_OPEN_ID` env-var bypass** no longer works for
  in-module handlers post-L-4 migration. Operators must be migrated
  to the `auth.json::lark_operators` JSON entry. The legacy
  `is_lark_authorized` remains active for any external callers — but
  no in-module handler hits it now.
- Recommendation: `agentflow-deploy/deploy.sh` should auto-create
  `auth.json` with `{"lark_operators": []}` on fresh installs to flip
  the gate to fail-closed by default. Otherwise the file-absent
  fail-open semantics will accept ANY `open_id` from the bridge.
  Update `agentflow-deploy/SECURITY.md` to document the 3-state
  semantic.

### Build / artifacts

- `backend/pyproject.toml` 1.2.0 → 1.2.1
- New deploy bundle: `blogflow-lark-deploy-v1.2.1.tar.gz`
- OpenClaw skill bundle (no schema delta from v3.0):
  `dist/agentflow-open-claw-v3.0.zip`

## [1.2.0] — 2026-05-07 — TG → Lark parity (`lark-parity` branch)

> Phase 1 of the BlogFlow Lark-only roadmap. Brings the Lark review
> surface to functional parity with TG (every TG button has a Lark
> equivalent), adds operator chrome cards (status / list / scan / etc.),
> hardens auth with a fail-closed Lark operator whitelist, and verifies
> end-to-end independence: a full article D1 → published runs with
> `TELEGRAM_BOT_TOKEN` unset and zero TG calls. Test suite: 230 → 308
> passing (78 new). See `docs/BLOGFLOW_TG_TO_LARK_PARITY.md` for the
> full migration spec.

### Independence layer (Wave 1)

- **`gate_history` dual-track schema.** `state.transition()` accepts
  `lark_chat_id` / `lark_card_id` kwargs; both are optional and additive.
  Existing TG entries (read or written) are unaffected. Updated
  `templates/state_machine.md` and `templates/callback_data_schema.md`
  with the dual-emission note.
- **`short_id.attach_lark_card(sid, lark_card_id, lark_chat_id)`.**
  Mirrors `attach_message_id()`. Persists Lark card identity to
  `~/.agentflow/review/short_id_index.json` so daemon-side code can later
  edit / disable the rendered card.
- **`auth.is_authorized_open_id(open_id, action)`.** New fail-closed
  Lark operator whitelist. `auth.json` gains a `lark_operators` section
  (`{open_id, name, actions[]}`). When the file is absent → fail-open
  (dev-friendly default); when the file exists but `lark_operators` is
  empty/missing → fail-closed (explicit configuration). Four CLI commands
  added: `blogflow review-auth-{add,remove,list,set-actions}-lark`.
- **Profile-session schema for Lark.** `topic_profile_lifecycle` gains
  `claim_session_lark`, `release_session_lark`, `find_active_session_lark`
  + idempotent `migrate_session_schema_v2` helper. Sessions store
  `active_open_id` / `active_lark_chat_id` alongside the TG fields.
- **`docs/flows/LARK_NOTIFY_CARDS.md` (NEW, 255 lines).** Rendering
  contract for `notify.*` family: `dispatch_preview`, `dispatch_result`,
  `publish_ready`, `publish_digest`, `hotspots_digest`, `draft_ready`,
  `spawn_failure`, `profile_setup_done`. Cross-referenced from
  `lark_review_cards.md`. Codifies the "notify.* are NOT review cards"
  rule that the v1.1.7 footgun violated.

### Parity gaps closed (Wave 2)

- **GAP-S Suggestions parity.** TG had `S:review` / `S:apply` /
  `S:dismiss` callbacks via `render_suggestion_list` / `render_suggestion_review`;
  Lark had nothing. Added `review.suggestion_list_card` +
  `review.suggestion_review_card` templates, `_emit_lark_suggestion_*_card`
  helpers in `triggers.py`, four `lark_suggestion_*` commands
  (`list/review/apply/dismiss`), and `_authorize_or_deny_v2` (fail-closed
  via `is_authorized_open_id`) for new handlers. Suggestions are
  profile-scoped (not article-scoped) — `_SUGGESTION_HANDLERS` early-route
  bypasses the article_id guard. Test: `test_lark_suggestions.py` (8 tests).
- **GAP-P2 Profile multi-turn (daemon-driven).** TG had
  `render_profile_setup_question` for multi-turn follow-up; Lark had only
  the intro card. Extended `review.profile_setup_card` schema with
  `current_question` / `question_index` / `total_questions`, added
  `_emit_lark_profile_question_card`, `_handle_profile_advance` +
  `_PROFILE_HANDLERS` early-route, plus `notify.profile_setup_done` real
  emit site. Wired through `claim_session_lark` / `find_active_session_lark`
  per the schema-footgun memory (must set `status="collecting"` + active
  open_id). KNOWN GAP: profile yaml mutation deferred (answers stay in
  `session.collected[]`) — see L-2 in §11.5 of plan doc. Test:
  `test_lark_profile_advance.py` (9 tests).
- **GAP-CHROME 12 operator intents.** TG had 14 slash commands; Lark
  had only `lark_message` free-text with limited keyword coverage.
  Added `_CHROME_INTENTS` keyword table + `_CHROME_VERB_PATTERNS` regex
  for verb intents (skip / defer / publish-mark / cancel) — all
  deterministic, NO LLM-based inference (honors v1.1.8 false-positive
  lock). Twelve chrome handlers cover: 状态/list/已发/扫一下/任务/
  跳过/推迟/标记已发/取消/审计列表/鉴权/建议. Seven `_emit_lark_*_card`
  helpers in `triggers.py`. Twelve `lark_chrome_*` commands in `web.py`.
  Six false-positive guards (e.g. `审计` keeps routing to
  `gate_b_diff` so chrome reserves only `审计列表` / `audit list`;
  `已发` vs `已发现`; `任务` vs `新任务给你`; `扫一下` vs `扫地了`).
  New doc: `docs/flows/LARK_OPERATOR_INTENTS.md` (190 lines). Test:
  `test_lark_chrome_intents.py` (31 tests).
- **GAP-AUDIT-LIST.** `lark_view_audit` previously per-article only;
  TG `/audit` could also list recent events. Added unified
  `_handle_view_audit_recent` (clamp `n` ≤ 100, optional `kind` filter,
  fail-closed auth), `_AUDIT_HANDLERS` early-route, `lark_view_audit_recent`
  command. `_emit_lark_audit_list_card` payload locked to
  `{entries, total, since, filter}` plus `刷新` / `仅看失败` buttons.
  `_handle_chrome_audit_list` now thin DRY wrapper. Test:
  `test_lark_audit_list.py` (7 tests).

### Phase 2 closure (started 2026-05-07)

- **L-1 / IND-4 import-time independence.** `triggers.py` and `daemon.py`
  now wrap `from agentflow.agent_review import tg_client` in try/except
  with a `_TgClientUnavailable` sentinel that raises informatively on
  any method call (so missed chat_id guards are loud, not silent). The
  29 callsites in `triggers.py` and 100+ in `daemon.py` are unchanged —
  they remain gated by `chat_id is not None` upstream. Test:
  `test_no_tg_runtime.py` (5 tests, including a subprocess-isolated test
  with a `MetaPathFinder` blocking `tg_client` to verify Phase 3
  deletion-tolerance). Phase 2 deployment without `tg_client.py` now
  loads cleanly.

### End-to-end verification

- **`test_e2e_lark_pure.py` (NEW, 673 lines).** Drives a complete
  pipeline D1 hotspot → Gate A → Gate B → image picker → Gate C →
  Gate D → published, entirely through Lark cards (`_emit_lark_*`) and
  `lark_*` callbacks. Replaces all `tg_client.*` outbound functions
  (`send_message` / `send_photo` / `send_document` / `send_long_text` /
  `answer_callback_query` / `edit_message_reply_markup` /
  `edit_message_text` / `get_me` / `get_updates`) with raise-on-call
  sentinels. Test ends with `tg_violations == []` — **zero TG calls
  leaked into the Lark-only happy path**. Phase 2 happy-path
  independence: VERIFIED.

### Known follow-ups (Phase 2 remaining)

Documented in `docs/BLOGFLOW_TG_TO_LARK_PARITY.md` §11.5:

- **L-2** Profile yaml write-back (currently `session.collected[]` stays
  unwritten on completion — needs dotted-key → friendly-slot translation)
- **L-3** `chrome_defer` real scheduling (currently only ack + audit log,
  needs wiring to `_schedule_deferred_repost` store)
- **L-4** Migrate ~30 legacy `_authorize_or_deny` callsites in
  `lark_callback.py` to the v2 fail-closed path (current handlers still
  use `is_lark_authorized` which fail-opens on missing whitelist)
- **L-5** `blogflow doctor --fresh` validation without TG token
  (CLI/Linux box manual verify, outside pytest scope)

### Build / artifacts

- `backend/pyproject.toml` version bumped 1.1.9 → 1.2.0
- Deploy bundle: `blogflow-lark-deploy-v1.2.0.tar.gz` (built by
  `scripts/build_deploy_bundle.sh`)
- OpenClaw skill bundle: `dist/agentflow-open-claw-v3.0.zip` (skill
  v2.9 → v3.0 documenting all new cards: suggestion list/review,
  profile question-advance, 6 chrome cards, audit_list)

## [1.1.9] — 2026-05-07

- **OpenClaw-Lark skill hardening (skill v2.8 → v2.9).**
  `agentflow-open-claw-v2/SKILL.md` gains a "Lark Card Rendering" section
  documenting the only legal wiring when `@larksuite/openclaw-lark` is
  installed: AgentFlow daemon POSTs `review.*_card` event envelopes to
  an OpenClaw-side listener (e.g. `/agentflow/events`); listener renders
  per `agent_review/templates/lark_review_cards.md` using `sendCardFeishu`;
  button callbacks go through `dispatchFeishuPluginInteractiveHandler` to
  AgentFlow's `/api/commands` with `lark_*` command format. Two new
  anti-patterns added: (#11) `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` pointing
  to AgentFlow's own `/api/commands` (causes 422 because envelope ≠
  command format) and (#12) BEE falling back to plain-text scan dumps
  when openclaw-lark is installed (strips the user of all Gate buttons).
  No runtime / Python change — `agent_bridge.py:emit_agent_event` and
  `agent_review/web.py:/api/commands` were already correct; the bug
  surface was BEE's misconfiguration. Standalone skill zip:
  `dist/agentflow-open-claw-v2.9.zip`.
- **D1 third recall layer — Brave Web Search collector.** New
  `backend/agentflow/agent_d1/collectors/brave_search.py` mirrors the
  twitter_search.py contract: opt-in via `AGENTFLOW_BRAVE_SEARCH_ENABLED`,
  requires `BRAVE_API_KEY`, refuses to fabricate when key is missing and
  MOCK_LLM is not set. Self-paces at 1.1s/req to stay under the free
  tier's 1qps cap. Surfaces vendor blogs / GitHub READMEs / Substack
  drops that don't show up via KOL pulls or Twitter search. Tagged
  `source="rss"` so D1 clustering already knows how to score it.
  `sources.yaml::brave_search:` is the per-query config block.
- **Web3-infra vendor RSS + Twitter seed.** `~/.agentflow/sources.yaml`
  gains 9 peer/competitor RSS feeds (Dune, Glassnode, The Graph, Pyth,
  Chainalysis, Goldsky, Subsquid, Solana, Multicoin) and 10 high-weight
  Twitter accounts (`@nansen_ai`, `@MessariCrypto`, `@PythNetwork`,
  `@chainalysis`, `@graphprotocol`, `@goldskyio`, `@subsquid`,
  `@MulticoinCap`, `@_polynya`, `@hosseeb`). All marked `note: v1.1.9 —
  unverified` so the operator probes them before the next scheduled
  scan. Backup of pre-edit sources.yaml at `~/.agentflow/sources.yaml.bak.*`.
- **D2 voice auto-adaptation by topic_fit_score.** New helper
  `topic_profile_effective_voice(publisher, fit_score)` resolves the
  effective writing voice based on Jaccard fit:
  - `fit_score >= AGENTFLOW_VOICE_FIRST_PARTY_MIN_FIT` (default 0.20) →
    keep configured voice (typically `first_party_brand`).
  - Below that threshold but kept by D1's hard gate → force `observer`.
  - `render_publisher_account_block(publisher, fit_score | hotspot)`
    uses this signal to flip the prompt: pronoun swaps to "我（个人观察）",
    the `**可引用的产品事实**` anchor list is dropped (it's the
    temptation that produces forced-analogy articles), and an explicit
    "禁止把当前话题硬转成 publisher 自家产品的一面来讲" rule is appended.
  - Both D2 entry points (`skeleton_generator.generate_skeleton` and
    `section_filler.fill_section` via `fill_all_sections`) plumb the
    hotspot through so every prompt sees the right voice.
- **D1 hard gate default bumped 0.025 → 0.10.** The old floor still let
  in topics that triggered the v1.1.7 forced-analogy class (硬套预言机).
  Below 0.10 → drop. [0.10, 0.20) → kept but written as observer.
  >= 0.20 → first-party voice. All three thresholds are env-tunable.
- Tests: 4 new for the Brave collector (disabled-default, mock fixtures,
  enabled-without-key refuses fabrication, blocked-weight drops query),
  4 for `topic_profile_effective_voice` (no-fit / high / low / env
  override), 4 for `render_publisher_account_block` observer flips
  (high keeps facts, low drops them + adds 禁止 rule, no-fit backward
  compat, hotspot kwarg computes inline). 230/230 pytest green.

## [1.1.8] — 2026-05-07

- **Lark @-mention parity with TG bot — kills the v1.1.7 hallucination class.**
  `handle_event(event_kind="message")` previously returned `message_ignored`
  on every free-text @-mention, leaving the Lark-side LLM client with
  nothing to relay so it fabricated fake "Gate B 完成" cards. Replaced
  with a deterministic intent classifier (keyword first, no LLM): 通过 /
  approve → `approve_b`, 驳回 / reject → `reject_b`, 重写 → `gate_b_rewrite`,
  refill → `refill`, 推进到下个 gate → state-aware advance, etc. Pending-edit
  slots take priority. Unrecognized text returns a structured help card —
  never silence.
- New bridge command `lark_message` (in-process, scope=review). Brings the
  total Lark vocab to 34 commands.
- **Auto fan-out closure** — the Lark side now mirrors TG's spawn-next-gate
  pattern: `lark_gate_b_approve` → image-gate picker, `lark_gate_c_approve`
  / `lark_gate_c_skip` → Gate D card, all on a daemon thread. Previously
  the Lark loop stalled at draft_approved with no follow-up, leaving the
  operator stranded.
- **Per-action auth on Lark** with parity to TG's `_ACTION_REQ`. Implicit
  operator via env `LARK_OPERATOR_OPEN_ID` (mirrors `TELEGRAM_REVIEW_CHAT_ID`);
  additional grants in `~/.agentflow/review/lark_auth.json`. Action verbs
  reuse the existing vocabulary (`review` / `write` / `edit` / `image` /
  `publish` / `system` / `*`).
- `chat_id` plumbed through `lark_message` params → operator dict → telemetry
  payloads so OpenClaw subscribers can target the originating Lark chat for
  downstream Gate cards.
- `lark_webhook` notifier no longer pushes "去 TG 审稿/重试/标记/看详情" CTAs
  when `AGENTFLOW_LARK_APP_PRIMARY=true` — the OpenClaw-rendered card
  already carries Lark-native action buttons.
- Tests: 65 in `test_lark_callback.py` (up from 44), covering the intent
  matrix, auth gate, fan-out closure, and the literal v1.1.7 hallucination
  prompt as a regression case.
- Docs: `LARK_FIRST_REVIEW_FLOWS.md` §4–§7 added (free-text path, auth model,
  chat_id plumbing, auto fan-out); `AGENT_BRIDGE.md` Command-Sets section
  documents `lark_message` + auth; `openclaw_plugin_integration.md` updated
  with the intent matrix and auth gate; `lark_review_cards.md` notes the
  new contract; cursor reference bumped to 34 commands.

## [1.1.7] — 2026-05-06

- Added `docs/flows/LARK_FIRST_REVIEW_FLOWS.md` as the canonical Lark-first
  topology, gate flow, closure matrix, and OpenClaw callback reference.
- Synced OpenClaw skill/reference docs and Claude skills to the `blogflow`
  command surface and daemon-owned bridge model.
- Added regression tests that assert the Lark-first flow docs, bridge docs, and
  OpenClaw skill reference keep pointing at `blogflow review-daemon` and
  `/api/commands`.

## [1.1.6] — 2026-05-06

- Embedded the agent bridge HTTP API inside `blogflow review-daemon` for
  Lark-first deployments. When `AGENTFLOW_LARK_APP_PRIMARY=true`, the daemon now
  owns `/api/commands` on `127.0.0.1:7860` by default, so OpenClaw callbacks no
  longer require a separate `blogflow review-dashboard` process.
- Allowed `blogflow review-daemon` to run without Telegram polling in
  Lark-first mode while still writing heartbeat, running timeout sweeps, and
  firing scheduled article-hotspots scans.

## [1.1.5] — 2026-05-06

- Added a dedicated Lark review-card rendering contract at
  `backend/agentflow/agent_review/templates/lark_review_cards.md`, covering
  every `review.*_card` event, required buttons, textarea payload fields, and
  pending-edit fallbacks.
- Added Lark-first preflight diagnostics so `blogflow doctor` can flag
  deployments that still show legacy Custom Bot / "go to Telegram" digest cards
  instead of OpenClaw-rendered review cards.
- Reworded the legacy `notify_hotspots_digest` Custom Bot card so it is clearly
  a scan summary, not a Gate A review card, and points operators to
  `AGENTFLOW_LARK_APP_PRIMARY=true` + `review.gate_a_card`.

## [1.1.4] — 2026-05-06

- Added Lark-first review-card events for Gate A, profile setup, Gate B,
  image-gate picker, Gate C, Gate D, and Locked Takeover. Telegram can still
  receive the same cards, but Lark/OpenClaw no longer has to infer actionable
  cards from digest notifications or state transitions.
- Added Lark image-gate picker commands so operators can choose cover-only,
  cover+body, or skip-image flow directly from Lark after Gate B approval.
- Aligned Lark Gate D confirm with the Telegram dispatch path: Lark now runs
  preview, non-Medium publish, Medium package generation, and dispatch result
  notification instead of spawning a bare `blogflow publish`.

## [1.1.3] — 2026-05-06

- Renamed the installable distribution from `agentflow` to `agentflow-media`
  so this article-publishing runtime is distinct from sibling AgentFlow
  packages.
- Replaced the generic `af` console entry point with `blogflow`, plus
  `mediaflow` as an equivalent alias. Runtime subprocess helpers now prefer
  `blogflow` / `mediaflow` and keep `af` only as a legacy fallback for older
  installs.
- Updated the current deployment service, launchd helper, install snippet, and
  OpenClaw skill guidance to use `blogflow`.
- Standardized this package's D1 terminology as article hotspots: added
  `blogflow article-hotspots` / `blogflow article-hotspot-show` as the
  recommended commands and kept `hotspots` / `hotspot-show` as legacy aliases.
- Lowered the default article-hotspot topic-fit hard gate to `0.025` and
  updated the local/template config from `0.05` to reduce false all-drop Gate A
  skips on sparse but valid article-topic clusters.
- Renamed deployment and scheduling surfaces to avoid sibling-package
  collisions: systemd uses `blogflow-review`, deploy defaults to
  `/opt/blogflow` + `blogflow` user, launchd uses
  `com.blogflow.review.article-hotspots`, and new installs use
  `BLOGFLOW_ARTICLE_HOTSPOTS_SCHEDULE*` env vars. Legacy
  `AGENTFLOW_HOTSPOTS_SCHEDULE*` vars remain as fallback only.

## [1.1.2] — 2026-05-06

- Lark `refill` is now a real Gate B write path: it transitions the article
  back to `drafting` and spawns `af fill <article_id> --skeleton-only
  --auto-pick --json` instead of deferring operators to Telegram.
- `af fill` accepts `--skeleton-only --auto-pick` (plus `--ignore-prefs`)
  so existing skeleton drafts can be refilled with the same default-picking
  behavior used by `af write --auto-pick`.
- Lark card input boxes are now accepted on Gate B edit and Gate C image
  regeneration callbacks. Gate B can submit inline section/meta edit text;
  Gate C can pass image-review feedback into `af image-gate
  --cover-description`.
- `af edit --post-review` lets Lark inline edits return to Gate B after the
  edit command finishes, closing the "submitted but no fresh review card"
  gap.
- Added `lark_apply_pending_edit` so OpenClaw can forward @-bot follow-up
  messages into the latest pending Gate B / locked-takeover edit slot.
- Marked edit-spawning Lark commands as dangerous and made pending edit slots
  one-shot so follow-up messages cannot reuse the same pending request.
- Added `AGENTFLOW_LARK_APP_PRIMARY=true` notification routing: legacy
  Lark Custom Bot `notify_*` calls now emit `notify.*` agent events for
  OpenClaw instead of posting to the old webhook.

## [1.1.1] — 2026-05-06

**Full TG → Lark callback parity (27 actions across Gate A/B/C/D/L).**

v1.1.0 shipped a 6-command Lark callback skeleton (approve_b / reject_b
/ takeover / view_audit / view_meta / refill stub). v1.1.1 extends the
bridge to **29 lark_* commands** total — every TG callback the daemon
handles now has a Lark-side equivalent the OpenClaw plugin can register
as a tool.

Coverage:

* **Gate A (3)** — `lark_gate_a_write` (spawn `af write --auto-pick`),
  `lark_gate_a_reject_all`, `lark_gate_a_expand` (read-only hotspot
  detail card)
* **Gate B (5)** — adds `lark_gate_b_rewrite` (spawn `af fill --rewrite`),
  `lark_gate_b_edit` (register pending edit slot for next @-bot
  message), `lark_gate_b_diff` (read latest `d2_structure_audit`
  verdict)
* **Gate C (5)** — `lark_gate_c_approve` / `_skip` (state transitions),
  `_regen` / `_relogo` (spawn `af image-gate`), `_full` (read image
  placeholders)
* **Gate D (8)** — `_toggle` / `_select_all` (write
  metadata.gate_d_selection), `_save_default` (write
  preferences.json), `_confirm` (transition + spawn `af publish`),
  `_cancel`, `_resume`, `_extend`, `_retry`
* **Locked Takeover (3)** — `lark_locked_critique` (read audit),
  `lark_locked_edit` (register pending edit slot), `lark_locked_give_up`
  (transition to draft_rejected)
* **Generic (1)** — `lark_defer` for any-Gate defer

Heavy actions that spawn subprocess (write / rewrite / regen / relogo /
confirm / retry — 6 in total) are marked `dangerous: true` and require
`AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true` in the AgentFlow
environment. They use `subprocess.Popen(start_new_session=True)` for
fire-and-forget; results land via the existing `emit_agent_event`
webhook so the OpenClaw agent can update the original Lark card when
the subprocess emits `agent.command.completed` /
`agent.command.failed`.

State transitions remain idempotent under `StateError`
(`side_effects=["already_handled"]`, HTTP 200).

Documentation:
* `docs/openclaw_plugin_integration.md` — full 29-command vocab table
  (per Gate), event webhook schema with the 9 OpenClaw should listen
  for, listener pseudo-code, security policy update.

Tests:
* `tests/test_lark_callback.py` — 21 new tests (one+ per new handler);
  total 34 lark_callback tests now pass.
* `tests/test_v02_workflows.py::LarkBridgeCommandTests` — 3 new tests
  (full vocab presence, dangerous flag check, payload-pass-through).

Backend full regression: 173/174 (1 pre-existing
NewsletterCorrectionTests JSON-parse failure, unrelated).

## [1.0.30] — 2026-04-30

**Lark draft fan-out at Gate B.**

When Gate B fires on Telegram, also push the assembled draft body to the
Lark group (push-only — Gate B operations remain on TG since Lark
Custom Bot has no callback channel). Pre-1.0.30, Lark only saw
`notify_hotspots_digest` / `notify_publish_ready` /
`notify_dispatch_result` / `notify_spawn_failure`; the actual draft
content was invisible to anyone watching the Lark group.

New `lark_webhook.notify_draft_ready(article_id, title, draft_md=...,
mirror_url=None, audit_summary=None)`:

* If the draft body is < 17 KB (after card overhead): full markdown
  rendered inside an interactive card.
* Otherwise: truncated to ~1500 characters in the card with a
  "📄 完整稿件" button pointing at the mirror URL.

Audit verdict (v1.0.29) is surfaced in the card subtitle when present
and not `pass`/`skipped` — operators see "audit=patch (0.62)" inline.

New env keys (both optional, default off):

```
AGENTFLOW_LARK_DRAFT_FANOUT=true
AGENTFLOW_DRAFT_MIRROR_URL_TEMPLATE=https://example.com/drafts/{article_id}.md
```

Wired into `agent_review.triggers.post_gate_b` immediately after the
TG `send_long_text` + `send_document` calls, before the state
transition. Lark failure (network, webhook 404, etc.) never blocks the
TG path or state machine.

Tests: 5 new in `tests.test_v02_workflows.LarkDraftFanoutTests`
covering disabled-by-default / short-draft-full-body / long-draft-
truncated-with-mirror / long-draft-truncated-no-mirror /
audit-summary-surfaced.

## [1.0.29] — 2026-04-30

**D2 whole-article structure audit between fill and Gate B.**

Existing D2 lints (`specificity_lint`, `topic_spine_lint`,
`compliance_checker`, `language_lint`) are point checks — they catch
single-paragraph or token-presence failures. None of them score the
article as a whole. Real-world drafts were shipping with disjoint
sections, anchor density front-loaded into the first third, and
voice drift mid-piece — all individually below the existing lints'
detection thresholds, but cumulatively a "structurally weak" article
hitting Gate B.

This release adds `agent_d2/structure_audit.py`, a single LLM call
between `fill_all_sections` returning and `post_gate_b` firing, that
scores the draft on four dimensions:

* **cohesion** — does each section reference the prior section's
  conclusion or premise?
* **anchor_density** — are publisher product_facts / perspectives
  evenly distributed front-to-back, not just front-loaded?
* **thesis_callback** — does the closing restate / deepen / turn the
  opening's central claim?
* **voice_consistency** — pronoun and voice stable throughout
  (catches "我们做的" → "行业应该" mid-drift)

Three verdicts driven by two thresholds:

| score | verdict | action |
|---|---|---|
| `>= patch_threshold` (default 0.75) | `pass` | draft unchanged, Gate B fires |
| `>= rewrite_threshold` (default 0.50) | `patch` | flagged sections re-filled with audit issues appended to the per-section prompt; one round only by default |
| `< rewrite_threshold` | `rewrite` | auditor itself writes a full replacement draft from the same hotspot + ctx |

Patch is the default response (preserves D2's multi-prompt fill
pipeline + its anchor density advantage); rewrite is the hard fallback
when the draft is structurally beyond saving. Audit always passes
through Gate B — we do NOT bypass operator review on a high audit
score; Gate B remains the single decision point.

New env keys (all optional):

```
AGENTFLOW_D2_AUDIT_ENABLED=true                 # default on
AGENTFLOW_D2_AUDIT_PATCH_THRESHOLD=0.75
AGENTFLOW_D2_AUDIT_REWRITE_THRESHOLD=0.50
AGENTFLOW_D2_AUDIT_MAX_PATCH_ROUNDS=1
```

New files:
* `agentflow/agent_d2/structure_audit.py` — module
* `prompts/d2_structure_audit.md` — JSON-output audit prompt
* `prompts/d2_full_rewrite.md` — text-output rewrite prompt
* tests in `tests/test_v02_workflows.py::D2StructureAuditTests`

Wired into both `af write --auto-pick` and `af fill` (CLI). Audit
failure (LLM error, missing hotspot, parse failure on rewrite output)
never blocks Gate B — a memory event with `verdict="error"` is logged
and the original draft proceeds.

## [1.0.28] — 2026-05-04

Real-data verification on chainstream produced one residual noise
hotspot: a Hindi-mixed Twitter @-reply ("@DarkDr3am3r Ghar wapsi
kar li kya?. Crypto nahi naam me hi Joseph hai") that survived the
v1.0.23 coverage filter because "crypto" is on-domain and the tweet
is short enough that 1 hit / 12 tokens = 8% coverage > 0.03 threshold.
Conversational reply chatter has no business in a recall pool
regardless of token overlap.

### Added

- `agent_d1/main._apply_signal_quality_filter` — drops Twitter signals
  whose text starts with `@` (=mentions another user, conversational
  reply) AND total body length is below `AGENTFLOW_MIN_REPLY_LEN`
  chars (default 60). Wired into `_collect_all` BEFORE the blocklist
  + coverage filters. Default off
  (`AGENTFLOW_DROP_SHORT_REPLIES=true` to enable; chainstream-service
  overlay 1.0.8+ enables by default).
- HN signals are exempt — short HN titles are valid signals (e.g.
  `Show HN: Sequencer benchmarks`); only Twitter shape is filtered.

### `.env.template`

- `AGENTFLOW_DROP_SHORT_REPLIES=` (default off)
- `AGENTFLOW_MIN_REPLY_LEN=60`

### Tests

- `D1RecallFilterTests` × 2 new: drops short @-reply Twitter signals
  while preserving long substantive tweets and HN entries; no-op when
  flag disabled. 122/122 total regression pass.

## [1.0.27] — 2026-05-04

Bumps `top_k` default from 3 → 5 across the board. With v1.0.21–v1.0.26
filter chain trimming aggressive, top-3 left the operator with a thin
review surface; top-5 keeps the daily Gate A card density at the
"actually pick 1–2 to write" level the cron flow was designed for.

### Changed

- `cli/commands.py::hotspots --gate-a-top-k` default 3 → 5.
- `agent_review/schedule.py::fire_due` default top_k 3 → 5
  (`AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K` env default).
- `agent_review/daemon.py` `/scan` slash command default top_k 3 → 5.
- `agent_review/daemon.py::_spawn_hotspots` signature default 5.
- `.env.template` `AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K=5`.

No behavioral change for installs that explicitly pin
`AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K=3` — only the unset default moves.
120/120 regression pass.

## [1.0.26] — 2026-05-04

Adds Twitter v2 keyword search as a parallel D1 recall layer, alongside
the curated-KOL timeline pulls. Brings Twitter to feature parity with
HackerNews (which has had front-page filter + Algolia search since
v1.0.0). KOL pull is "what these 5 trusted voices posted today";
keyword search is "what the broader Twitter firehose is saying about
our specific topics today".

### Added

- `agent_d1/collectors/twitter_search.py` (~280 LOC) — new collector.
  Same behavior matrix as `twitter.py` v1.0.8:
  * `MOCK_LLM=true` → deterministic per-query fixtures with
    `raw_metadata={"mock": True, "via": "search", "query": q}`
    so v1.0.10 mock-tag drop catches them in real-mode regressions.
  * `AGENTFLOW_TWITTER_SEARCH_ENABLED!="true"` → empty (default off,
    backward compat).
  * Enabled but no `TWITTER_BEARER_TOKEN` AND `MOCK_LLM!="true"` →
    empty + warning. Refuses to fabricate.
  * Enabled + bearer present → tweepy v2
    `search_recent_tweets(query, max_results, tweet_fields=…)`,
    clamped to 10..100 per Twitter API spec.
  * Each returned `RawSignal` has `source="twitter"` (same bucket as
    KOL collector for downstream filters) but
    `raw_metadata.via="search"` to discriminate provenance for audits.
- `agent_d1/main.py::_twitter_search_enabled()` /
  `_twitter_search_queries()` helpers + a third async task in
  `_collect_all` that fires only when search is enabled and queries
  are present. `weight: blocked` and `AGENTFLOW_TWITTER_KOL_ONLY_HIGH`
  semantics mirror `twitter_kols`.
- `.env.template` — `AGENTFLOW_TWITTER_SEARCH_ENABLED=false` +
  `AGENTFLOW_TWITTER_SEARCH_MAX_RESULTS=20`.
- `config-examples/sources.example.yaml` — commented `twitter_search:`
  schema between KOL and RSS sections (brand-neutral placeholders;
  operator/overlay supplies real queries).

### Tests

- `TwitterSearchCollectorTests` × 8 (spec called for "~6-7"; agent
  added one extra belt-and-suspenders): disabled returns empty;
  mock-mode fixtures; real-mode no-bearer skip with warning;
  real-mode with bearer calls tweepy.search; weight filters
  KOL_ONLY_HIGH; weight=blocked skipped; queries empty when disabled;
  `_collect_all` runs search alongside KOL when enabled.
- 120/120 total (was 112).

### Notes

- tweepy v2 caps `max_results` at 10..100; the collector clamps via
  `_clamp_max_results` so a `max_results: 200` in `sources.yaml`
  doesn't 400 the API.
- Real-mode does NOT do an `expansions=["author_id"]` users lookup —
  author username falls back to `@search_<query_hash>` when the v2
  response doesn't carry `includes.users`. Cheap to add later if
  per-author analytics matter; doesn't affect downstream filtering
  (which is text-content driven).
- Pre-existing private-import flag (not addressed in this PR):
  `agent_d1/main.py::_signal_text_tokens` imports
  `agent_d2.topic_spine_lint._tokenize` (a private symbol). Cross-
  package coupling worth flagging for any future agent_d2 refactor.

### Recommended config (chainstream-service overlay 1.0.5+)

```env
AGENTFLOW_TWITTER_SEARCH_ENABLED=true
AGENTFLOW_TWITTER_SEARCH_MAX_RESULTS=30
```

Operator populates `sources.yaml::twitter_search` with vertical-
specific queries (e.g. `"MEV OR \"smart wallet\" OR rollup"`).

## [1.0.25] — 2026-05-04

Adds a signal-level blocklist as a separate control surface from the
v1.0.23 coverage filter. Coverage answers "is enough of this signal in
our domain?"; blocklist answers "does this signal mention something
unambiguously off-domain?". Different cuts — both useful, neither
sufficient alone.

### Added

- `agent_d1/main._apply_signal_blocklist` — drops signals whose text
  or author contains any blocklist term (case-insensitive substring).
  Wired into `_collect_all` BEFORE the coverage filter so blocked
  signals never count toward token-coverage stats either.
- `agent_d1/main._resolve_signal_blocklist` — merges:
  * `AGENTFLOW_SIGNAL_BLOCKLIST_TOKENS` env (comma-separated, e.g.
    `"OpenAI,ChatGPT,Sam Altman"`)
  * the active profile's `avoid_terms` field (already part of the
    topic_profiles.yaml schema, previously unused by D1)
  Uses the same v1.0.23 single-profile fallback chain so it works on
  fresh installs without intent / pinned active id.
- `.env.template` adds `AGENTFLOW_SIGNAL_BLOCKLIST_TOKENS=`.

### Why this is a separate filter

Token coverage (v1.0.23) treats `agent` as a chainstream-domain word —
so an "OpenAI launches new code agent product" tweet borrows that
overlap and squeaks past at threshold 0.05. The blocklist is the
exception lane: even if coverage looks OK, mentioning an explicitly
off-domain entity (the OpenAI brand, ChatGPT, Sam Altman by name)
drops the signal cleanly. Catches what coverage can't.

### Filter chain order in `_collect_all`

```
collectors → mock-tag drop → blocklist → coverage → time window → cluster
                              ^v1.0.25^   ^v1.0.23^
```

Blocklist runs first because it's substring-cheap and surfaces the most
obvious offenders before the (more expensive) per-signal tokenization
loop.

### Tests

- `D1RecallFilterTests` × 4 new: drops matching, no-op when empty,
  case-insensitive substring, env+avoid_terms merger. 8 total in
  the suite now (4 original + 4 blocklist).

### Recommended config (chainstream-service overlay 1.0.3+)

```env
AGENTFLOW_SIGNAL_BLOCKLIST_TOKENS=OpenAI,ChatGPT,Anthropic,Sam Altman,Greg Brockman,DeepSeek,Claude API,Vintage,Omega,Southwest,Stockholm
```

(Operator extends; the chainstream `avoid_terms` already covers
`general AI hype / consumer chatbot / celebrity crypto / macro politics`.)

## [1.0.24] — 2026-05-04

Fixes the actual root cause of the "every fresh install gets OpenAI
gossip / antique-watch tweets" recall problem: the example
`config-examples/sources.example.yaml` shipped with `@paulg` /
`@karpathy` / `@simonw` flagged `weight: high`, baking a generalist-
AI bias into every install bootstrapped from this template. v1.0.22
added the weight semantics; v1.0.24 makes the example honor them.

### Changed

- `config-examples/sources.example.yaml`:
  * All twitter handles default to `weight: medium` (was a mix of
    `high` and `medium` with implicit favoritism toward generalist-AI
    accounts).
  * Header comment block now documents the v1.0.22+ weight semantics
    (`high` / `medium` / `blocked`) and the 3 companion env knobs
    (`AGENTFLOW_SIGNAL_DOMAIN_THRESHOLD`,
    `AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD`,
    `AGENTFLOW_TWITTER_KOL_ONLY_HIGH`).
  * Per-handle `note:` field added to the 6 generalist accounts
    (`@paulg` / `@sama` / `@karpathy` / `@simonw` / `@patrickc` /
    `@dhh`) explaining why crypto-infra / B2B-SaaS / regulated-finance
    publishers typically block them. Pointer to the
    `chainstream-service` overlay as a worked example.
- Framework remains brand-neutral: defaults are `medium` across the
  board, no implicit favorites. Operators tune weights themselves
  based on their vertical, or apply a brand-specific overlay.

### Why this matters

Without v1.0.24, a fresh `cp config-examples/sources.example.yaml
~/.agentflow/sources.yaml` produces a recall pool dominated by 4
high-weight generalist accounts. The v1.0.22 hard fit gate + the
v1.0.23 signal-domain filter then have to do all the work of
filtering them out — and even then, cross-domain words like
`agent` / `data` / `infra` let some signals through. Removing the
implicit bias at the source is cheaper than fighting it downstream.

## [1.0.23] — 2026-05-04

Two same-day fixes from a real verification run of v1.0.22 against the
local chainstream profile.

### Fixed

- `agent_d1/main._resolve_active_publisher_tokens` — broadened the
  resolution chain so the filter actually finds a publisher in the
  common case. Previously it only checked `load_current_intent()` then
  `_read_active_profile_id()`; both return `None` on a fresh install,
  so `_apply_signal_domain_filter` no-op'd silently and ALL signals
  passed. New chain:
    1. current intent's publisher_account
    2. `_read_active_profile_id()`
    3. `AGENTFLOW_DEFAULT_TOPIC_PROFILE` env (already set by the
       chainstream-service overlay)
    4. The single profile in `topic_profiles.yaml` when there's
       exactly one (handles the "I just ran `af topic-profile init`
       and didn't pin it active" case)
- `agent_d1/main._apply_signal_domain_filter` — switched the
  per-signal scoring formula from Jaccard to **signal-anchored
  coverage**: `|sig_tokens ∩ pub_tokens| / len(sig_tokens)`. Jaccard's
  denominator is dominated by the publisher token set (typically 100+
  entries), which forces every threshold below ~0.03 to either accept
  everything or reject everything regardless of actual signal quality.
  Coverage is publisher-set-size-invariant: "1 in every 20 tokens of
  this signal is on-domain" reads the same at any pub_tokens size.
- Net effect after both fixes: a real chainstream scan goes from
  245 raw signals → 52 retained at threshold 0.03, dropping
  off-topic tweets like `@balajis: RT @MTSlive: BALAJI AND LORENZ |
  NSA TESTS MYTHOS` and `seasteading is already here` cleanly.

### Notes

- v1.0.22 functionally never worked against a freshly-init'd profile;
  v1.0.23 is the first release where the signal-domain filter has
  observable effect end-to-end. The chainstream-service overlay 1.0.2
  pin should be bumped to require v1.0.23+.

## [1.0.22] — 2026-05-04

Real-deploy report (chainstream-service): D1 recall pool was being
dominated by general-tech KOLs (`@sama`, `@paulg`, `@karpathy`) and
broad HN keywords (`AI` / `Claude` / `LLM`), so even after v1.0.21's
hard topic-fit gate, every scheduled scan was returning OpenAI
gossip / antique-watch tweets / startup commentary that got
clean-rejected, leaving the operator with empty Gate A digests.
v1.0.21 stops bad output; this one stops bad input.

### Added — D1 KOL allowlist

- `agent_d1/main.py::_twitter_handles` now respects the per-handle
  `weight` field from sources.yaml:
  - `weight: blocked` → skipped entirely (operator can keep the
    historical row without the signal flooding the pool).
  - `AGENTFLOW_TWITTER_KOL_ONLY_HIGH=true` env restricts collection
    to `weight: high` entries. Recommended for tightly-scoped
    publishers where general-tech KOLs would otherwise drown out
    the vertical signal.

### Added — D1 signal-level domain filter

- New helpers in `agent_d1/main.py`:
  `_apply_signal_domain_filter` / `_resolve_active_publisher_tokens`
  / `_signal_text_tokens`. Operates on raw signals BEFORE clustering:
  per-signal Jaccard overlap with the active publisher's domain
  tokens (drawn from `product_facts` + `perspectives` +
  `default_description` + `keyword_groups`); below
  `AGENTFLOW_SIGNAL_DOMAIN_THRESHOLD` are dropped with a warning
  log + first 3 examples for triage. Default 0 = disabled
  (backward compat). Recommended 0.03.
- Why one stage earlier than v1.0.21's hard fit gate: composite
  ranking cluster-level scoring runs after clustering, so an
  off-domain flood has already shaped the centroids by then.
  Pre-cluster filter cuts the noise at intake.
- Lazy-imports `topic_spine_lint._tokenize` and
  `topic_spine_lint._publisher_domain_tokens` so D1 doesn't take a
  load-time dep on D2.

### Tests

- `D1RecallFilterTests` × 4: KOL `weight: blocked` skipped;
  `AGENTFLOW_TWITTER_KOL_ONLY_HIGH=true` restricts to high; signal
  filter drops off-domain when enabled and publisher resolves;
  filter is no-op when disabled (default).

### Recommended config (chainstream-service overlay 1.0.2+)

```env
AGENTFLOW_SIGNAL_DOMAIN_THRESHOLD=0.03
AGENTFLOW_TWITTER_KOL_ONLY_HIGH=true
```

## [1.0.21] — 2026-05-03

Real-deploy report: drafts coming out look "anchored" (every paragraph
mentions publisher tokens like `Kafka Streams` / `MCP` / `<brand>`)
but the underlying topic is **completely off-domain** (a TCG卡牌
customs failure case, hotspot that has no business reaching an
on-chain data infra publisher). v1.0.18 specificity_lint passed — it
asks "does the body MENTION publisher tokens?", which gets gamed by
forced analogies. The actual missing question is: "is the SOURCE
material in our domain at all?"

Two-layer fix:

### Added — D1 hard topic-fit gate (process layer)

- `agent_review.triggers.post_gate_a` now reads
  `AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD` (env, default 0 = disabled,
  recommended prod 0.05). When > 0, hotspots whose Jaccard fit score
  against publisher tokens is below threshold are DROPPED before
  composite ranking, not just down-weighted. If the gate eliminates
  every candidate, post_gate_a returns None with a warning rather
  than emitting an empty Gate A card.
- v1.0.20's `AGENTFLOW_FIT_WEIGHT` (soft re-rank) still applies on
  the survivors. The hard gate is the kill-switch; fit_weight is the
  preference dial.
- Stops the LLM from getting an off-topic hotspot as input — which
  was the actual root cause of the forced-analogy article. Previously
  `--filter ".*"` kill-switched the soft fit gate by feeding broad
  signals; the hard threshold survives that.

### Added — `agent_d2/topic_spine_lint` (defense in depth)

- New module. At Gate B post, computes Jaccard overlap between
  - **A** = tokens from `metadata.topic_one_liner` +
    `metadata.source_references[*].text_snippet` (the upstream
    SOURCE material the article was built from)
  - **B** = tokens from `publisher.product_facts` +
    `perspectives` + `keyword_groups` (the publisher's DOMAIN scope)
- < 0.02 (default) → warning appended to the Gate B card's self-check:
  `⚠ topic-spine misalignment: hotspot 主题与 publisher 领域脱钩 …`
  Operator decides reject vs. proceed; lint does NOT block the card.
- Skips silently when either side has < 5 tokens (can't lint reliably).
- Different signal from `specificity_lint` — that catches "wrong
  brand voice"; this catches "wrong subject domain".

### Changed

- `cli/commands.py::write` (af write) — now stamps
  `metadata.topic_one_liner` and `metadata.source_references` from
  the chosen hotspot record so the spine_lint at Gate B has data to
  work with. Without this, the lint silently no-ops.

### Tests

- `TopicSpineLintTests` × 4: aligned topic passes; the actual TCG
  customs hotspot from the autopost report flagged with the new
  warning; thin spine skipped; thin publisher skipped.
- `TopicFitHardGateTests` × 2: threshold > 0 drops off-domain
  hotspot before Gate A even ranks; threshold 0 preserves v1.0.20
  legacy behavior (off-domain still surfaces, just downranked).

### Recommended config (chainstream-service overlay 1.0.1+)

```env
AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD=0.05
```

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
