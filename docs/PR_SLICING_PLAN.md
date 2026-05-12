# PR Slicing Plan: lark-parity → agentflow-article-publishing

**Goal**: take the 28 commits on `lark-parity` (ahead of upstream HEAD `f50918c`) and land them as 6 reviewable PRs on `witness1993x/agentflow-article-publishing`.

**Why slice**: a single mega-PR (~5700 net lines deleted, file removals, behavior changes) is unreviewable and risks reverts. Six logical PRs let each be merged / reverted independently.

**State as of 2026-05-11**:
- lark-parity HEAD: see `git log --oneline f50918c..HEAD`
- upstream `main` HEAD (origin): `f50918c` (v1.1.9)
- upstream working branch: `v1.1.2-lark-refill-real-path` (3 commits ahead of origin with uncommitted skill MD edits) — **do not slice against this**; cut all PRs against `main`.
- Test baseline on lark-parity: 297/297 passing

**Risk inventory**:
- Phase 3 (PR 3) deletes `tg_client.py`, `render.py`, ~3886 lines from `daemon.py`, 30 TG-runtime tests. Conflicts with any upstream changes touching those files are guaranteed if upstream moved.
- PR 5 (Agent-Lark Window) introduces a behavioral break: `run()` raises `SystemExit` if `AGENTFLOW_LARK_APP_PRIMARY` isn't set. Document prominently in PR body.
- Each PR carries a `pyproject.toml::version` bump; cherry-picking out of order needs version reconciliation.

---

## The 6 PRs

### PR 1 — Phase 1: Lark feature parity foundations

**Commits** (5):
```
3106718 lark-parity: snapshot from agentflow-article-publishing v1.1.9 + WIP blogflow rename
c0b8e2f Wave 1 (parallel): IND-1/2/3/6 + GAP-NOTIFY foundations
f3c8651 Wave 2 (sequential): GAP-S + GAP-P2 + GAP-CHROME + GAP-AUDIT-LIST
01d760a Wave 4: Lark-pure e2e test — Phase 2 happy-path acceptance
9d147e7 docs: phase 1 completion report (§11) — 303 tests, e2e verified
```

**Branch**: `phase-1-lark-parity-foundations`

**Title**: `Phase 1: Lark feature parity — 17 new lark_* commands + 7 review.*_card schemas + e2e test`

**Body**:
```
Adds Lark-side parity for what was previously a Telegram-only review loop.
TG path remains intact — this PR is additive.

Highlights:
- 17 new `lark_*` commands (3 suggestion + 1 profile_advance + 12 chrome + 1 audit_recent)
- 7 new review.*_card event schemas (Gate A/B/C/D + setup + locked + image picker)
- _authorize_or_deny_v2 fail-closed auth helper (alongside legacy fail-open path)
- Pure-Lark e2e test (test_e2e_lark_pure.py) — drives D1→published with
  TELEGRAM_BOT_TOKEN unset, asserts zero tg_client calls
- Phase 1 completion report in docs/BLOGFLOW_TG_TO_LARK_PARITY.md §11

Test suite: baseline → 303 passing.
No behavior change for TG-only operators.
```

### PR 2 — Phase 2: closure (L-1 through L-5)

**Commits** (8):
```
d57985e v1.2.0: phase 2 partial closure (L-1) + version bump + docs
896ad19 L-4: migrate legacy _authorize_or_deny → v2 (fail-closed) + 3-state auth.json
a8b53fa L-3: chrome_defer real wiring to _schedule_deferred_repost
24648c3 L-3 follow-up: fix _handle_defer (lark_defer button) parity
b1a90ca L-2: profile yaml writeback at completion
6fa00cf v1.2.1: phase 2 closure (L-2/L-3/L-4) version bump + CHANGELOG
7605a49 L-5: doctor --fresh no-TG validation in pytest
be20c1a v1.2.2: Phase 2 truly final — L-5 + skill v3.0 path fix
```

**Branch**: `phase-2-closure-l1-l5`

**Title**: `Phase 2: close L-1..L-5 (lazy tg_client import + auth migration + chrome_defer + profile writeback + doctor smoke test)`

**Body**:
```
Closes the 5 leftover items from Phase 1's e2e verification:

- L-1: lazy/import-tolerant tg_client in triggers.py (Phase 3 prep)
- L-2: profile yaml writeback at lark_profile_advance completion
- L-3: chrome_defer wires to _schedule_deferred_repost (was ack-only)
- L-4: 5 inline _authorize_or_deny → v2 migration + 3-state auth.json
- L-5: blogflow doctor --fresh no-TG validation as a subprocess test

Test suite: 303 → 333 passing.
No behavior change for TG path.
```

### PR 3 — Phase 3: delete the Telegram surface (4 commits + closure tag)

**Commits** (6):
```
d717afa Phase 3 Wave A: drop TG-only CLI commands and flags
4db98d4 Phase 3 Wave B: strip 29 tg_client.* callsites from triggers.py
24a6e9c Phase 3 Wave C: delete TG handlers + poll loop from daemon.py
3166ccd Phase 3 Wave D: prune TG-only tests from test_v02_workflows.py
6875963 Phase 3 Wave D: delete tg_client.py + scrub remaining TG scaffolding
28ef11e v1.3.0: Phase 3 closure — Telegram removed, Lark-only
```

**Branch**: `phase-3-delete-telegram`

**Title**: `Phase 3: delete Telegram surface (-5700 lines) — Lark-only daemon`

**Body**:
```
🚨 BREAKING CHANGE: AgentFlow daemon is now Lark-only.

run() raises SystemExit if AGENTFLOW_LARK_APP_PRIMARY is not truthy.
Operators on TELEGRAM_BOT_TOKEN-only deploys must opt into Lark mode.

Removed:
- tg_client.py (330 lines, REST client)
- _handle_message / _handle_callback / _route + _ACTION_REQ map
- All TG slash handlers + command registry + slash helpers
- TG poll loop branch in run() (get_updates)
- triggers.py 29 tg_client.* callsites
- review-init / review-publish-stats --tg / learning-review --post-tg
  CLI surfaces
- 30 TG-runtime tests in test_v02_workflows.py

daemon.py: 5634 → 1748 lines.
Test suite: 333 → 303 passing (30 TG tests retired, no new failures).

If you currently rely on the Telegram fallback: do NOT merge this PR.
Wait for the Agent-Lark Window mode (PR 5) which makes Lark-only
viable on cloud-computer deploys without a webhook listener.
```

### PR 4 — v1.3.x cleanup: render.py removal + timeout-sweeper regression fix

**Commits** (2):
```
87b1004 v1.3.x: refactor triggers.py off render.py + delete the module
c4c847f v1.3.1: render.py removal + timeout-sweeper fix release
```

**Branch**: `v1.3.1-render-removal`

**Title**: `v1.3.1: delete render.py (TG-Markdown renderer, -802 lines) + fix Phase 3 timeout-sweeper regression`

**Body**:
```
v1.3.0 left render.py on disk because triggers.py's _sid.register() calls
were embedded inside each render_X(). This release:

1. Refactors 15 render.X(...) callsites in triggers.py to call
   _sid.register directly with the right gate/article_id/ttl.
2. Inlines export_body_markdown + gate_b/c TTL helpers into triggers.py.
3. Deletes render.py (-802 lines).
4. Fixes a regression in daemon._scan_timeouts: _safe_send was reduced
   to `return False` in Phase 3 Wave C, but the surrounding
   `if _safe_send(text): timeout_state.mark_first_pinged(aid)` guards
   meant timeout state never got marked → audit log spam every 60s.

Stacks on PR 3.
Test suite: 303 → 296 (7 review_render-asserting tests retired).
```

### PR 5 — Agent-Lark Window mode (no-webhook deploy)

**Commits** (4):
```
094f791 v1.3.2: Agent-Lark Window mode + file-queue fallback
4c6590e v1.3.3: deliverable mode — smoke test + runbook + bundled card schema
9c5cbbb v1.3.4: skill self-contained for cloud-computer ops
b6ad3a2 v1.3.5: SKILL.md webhook framing rewrite
```

**Branch**: `v1.3.x-agent-lark-window-mode`

**Title**: `Agent-Lark Window mode: file-queue delivery for daemon → OpenClaw events (no HTTP webhook required)`

**Body**:
```
Solves the cloud-computer deployment scenario where the operator has
Python but can NOT stand up an HTTP listener for the OpenClaw bridge.

New delivery mode for review/notify events:
- AGENTFLOW_AGENT_EVENT_MODE ∈ {webhook, file, both}; auto-resolves to
  `webhook` if AGENTFLOW_AGENT_EVENT_WEBHOOK_URL is set, else `file`.
- `file` mode appends each envelope as one JSON line to
  ~/.agentflow/agent_events/queue.jsonl (append-only, audit-friendly).
- The OpenClaw skill agent tails the queue and pushes Lark cards via
  its already-mounted Lark window — no inbound HTTP listener needed.

New CLI helpers:
- blogflow agent-events-tail [-f] [--from-start]
- blogflow lark-cli-emit --command lark_X ... (injects Lark callback
  directly into lark_callback.handle_event; no /api/commands HTTP
  bridge required)
- blogflow agent-events-emit-test (synthetic Gate A card for end-to-end
  smoke verification)

Operator runbook: docs/CLOUD_COMPUTER_DEPLOY.md (8 steps,
no sudo / no Docker / no webhook).

Skill bundle (agentflow-open-claw-v2) is now self-contained:
- references/lark_review_cards.md (card schema, was in backend/templates)
- references/CLOUD_COMPUTER_DEPLOY.md (runbook copy)
- SKILL.md §"Cloud-Computer First-Time Deploy" inlines 8-step flow +
  5-row troubleshooting
- SKILL.md framed so Mode A (file queue) is THE default; Mode B
  (webhook) retitled "advanced". Anti-pattern #11 forbids asking
  user for webhook/bridge/dashboard vars on cloud-computer deploys.

Stacks on PR 4.
Test suite: 295 → 297 (2 file-queue tests added).
```

### PR 6 — Profile flow + too_narrow soft-floor + signal misalignment

**Commits** (3):
```
52a8084 v1.3.6: profile flow Lark-event regressions fixed
3f560ab v1.3.7: hard topic-fit gate gains soft-floor fallback
<v1.3.8 commit>  Direction A: consecutive soft-floor-fallback detection
```

**Branch**: `v1.3.x-profile-soft-floor-misalignment`

**Title**: `v1.3.6–8: profile flow Lark events + too_narrow soft-floor fallback + signal-misalignment streak detection`

**Body**:
```
Three closely-related operator-visibility fixes uncovered post-1.3.5:

1. v1.3.6: Phase 3 Wave C left two profile-flow stubs (_send_profile_
   setup_question, _spawn_apply_profile_session) as silent no-ops /
   log-only. Now emit Lark events: review.profile_setup_card (question-
   advance form) + notify.profile_setup_done / _failed.

2. v1.3.7: AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD=0.10 used to silently
   return None when all hotspots dropped → narrow profiles saw zero
   Gate A cards. Added soft-floor fallback (env AGENTFLOW_TOPIC_FIT_
   SOFT_FLOOR=0.02) + gate_warning payload + low_topic_fit red flags
   on each candidate. Operator always sees a Gate A card with a
   visible "signal off-domain" banner.

3. v1.3.8: when the soft floor fires for N consecutive days (default 3,
   env AGENTFLOW_SIGNAL_MISALIGNMENT_DAYS), daemon emits a one-time-per-
   streak notify.signal_misalignment event suggesting the operator add
   seed sources (blogflow learn-from-handle) rather than chase regex
   widening or threshold knobs that won't fix root cause.

Stacks on PR 5.
Test suite: 297/297 still passing.
```

---

## Cherry-pick + branch creation (run from upstream repo)

```bash
cd ~/Desktop/experimental/medium\&blog_posting_agent/agentflow-article-publishing
git fetch origin

# Make sure your working tree is clean. The v1.1.2-lark-refill-real-path
# branch has uncommitted skill MD edits — stash or commit those first
# so cherry-picks land on a clean base.

# Add the lark-parity tree as a remote so we can cherry-pick from it.
git remote add lark-parity ../agentflow-lark-parity 2>/dev/null || true
git fetch lark-parity

# PR 1
git checkout -b phase-1-lark-parity-foundations origin/main
git cherry-pick 3106718 c0b8e2f f3c8651 01d760a 9d147e7
# resolve any conflicts, then:
git push origin phase-1-lark-parity-foundations
gh pr create --base main --head phase-1-lark-parity-foundations \
  --title "Phase 1: Lark feature parity ..." \
  --body-file path/to/PR1_BODY.md

# PR 2 (stacked on PR 1)
git checkout -b phase-2-closure-l1-l5 phase-1-lark-parity-foundations
git cherry-pick d57985e 896ad19 a8b53fa 24648c3 b1a90ca 6fa00cf 7605a49 be20c1a
git push origin phase-2-closure-l1-l5
gh pr create --base phase-1-lark-parity-foundations --head phase-2-closure-l1-l5 ...

# PR 3 (stacked on PR 2)
git checkout -b phase-3-delete-telegram phase-2-closure-l1-l5
git cherry-pick d717afa 4db98d4 24a6e9c 3166ccd 6875963 28ef11e
git push origin phase-3-delete-telegram
gh pr create --base phase-2-closure-l1-l5 --head phase-3-delete-telegram ...

# PR 4
git checkout -b v1.3.1-render-removal phase-3-delete-telegram
git cherry-pick 87b1004 c4c847f
git push origin v1.3.1-render-removal
gh pr create --base phase-3-delete-telegram --head v1.3.1-render-removal ...

# PR 5
git checkout -b v1.3.x-agent-lark-window-mode v1.3.1-render-removal
git cherry-pick 094f791 4c6590e 9c5cbbb b6ad3a2
git push origin v1.3.x-agent-lark-window-mode
gh pr create --base v1.3.1-render-removal --head v1.3.x-agent-lark-window-mode ...

# PR 6
git checkout -b v1.3.x-profile-soft-floor-misalignment v1.3.x-agent-lark-window-mode
git cherry-pick 52a8084 3f560ab <v1.3.8 SHA>
git push origin v1.3.x-profile-soft-floor-misalignment
gh pr create --base v1.3.x-agent-lark-window-mode --head v1.3.x-profile-soft-floor-misalignment ...
```

---

## Cherry-pick conflict expectations

| PR | Most likely conflict source | Resolution strategy |
|---|---|---|
| PR 1 | None expected — additive on top of f50918c | Should apply clean |
| PR 2 | Possible if upstream got a parallel L-1..L-5 fix; unlikely | Prefer lark-parity version (it's been e2e validated) |
| PR 3 | **HIGH** — tg_client.py / daemon.py / triggers.py see heavy deletion. Any upstream commit touching these will conflict. | Manual merge; favor lark-parity deletion side, re-verify daemon.py wc -l ≈ 1748 |
| PR 4 | LOW — render.py was untouched upstream | Should apply clean post PR 3 |
| PR 5 | LOW — additive on agent_bridge.py + new CLI commands + new docs | Should apply clean |
| PR 6 | LOW — additive on triggers.py + preflight.py | Should apply clean |

**Single biggest risk**: PR 3 + PR 4 deletions vs any upstream commits touching daemon.py / triggers.py / tg_client.py / render.py. If upstream moved, expect manual conflict resolution. Verify post-cherry-pick:

```bash
wc -l backend/agentflow/agent_review/daemon.py        # → ~1681
ls backend/agentflow/agent_review/tg_client.py 2>&1   # → No such file
ls backend/agentflow/agent_review/render.py 2>&1      # → No such file
cd backend && source .venv/bin/activate && python -m pytest tests/ --timeout=15 -q  # → 297 passed
```

---

## What NOT to do

- ❌ Squash all 28 commits into a single PR — unreviewable, can't revert pieces.
- ❌ Cherry-pick onto `v1.1.2-lark-refill-real-path` — has uncommitted skill MD edits + diverges from origin/main.
- ❌ Push directly to `main` without PR — bypasses review + CI.
- ❌ Skip PR 3 (Phase 3) — without it, later PRs reference deleted symbols and will fail CI.
- ❌ Land PR 3 alone without PR 5 — PR 3 introduces the SystemExit-on-no-Lark behavior; without PR 5's file-queue mode, cloud-computer deployers have no fallback to land on.

## What's safe to do independently

- ✅ Merge PRs in order (1 → 6). Each is a clean superset of the previous.
- ✅ Revert PR 3 if Phase 3 turns out to be premature — PRs 1+2 stand on their own as additive Lark parity.
- ✅ Skip PR 6 if you don't care about the operator-visibility fixes — PRs 1-5 are a stable v1.3.5 release line.

---

## Status

- Plan generated 2026-05-11 against lark-parity HEAD `<HEAD-after-v1.3.8>` and upstream `f50918c`.
- **No commits have been pushed to upstream**. This document is the runbook the operator executes when ready.
- Update commit SHAs if the lark-parity tree gets new commits before slicing begins.
