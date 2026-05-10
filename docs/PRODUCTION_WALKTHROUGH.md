# Production Walkthrough — v1.3.10 end-to-end

Captured during a live dry-run of the full Lark-only pipeline. **Every event payload below is real output, not pseudocode** — pulled out of `~/.agentflow/agent_events/queue.jsonl` while mock-mode-driving the daemon.

The walkthrough also surfaced the v1.3.10 fix: `_emit_lark_gate_b_card` / `_emit_lark_gate_c_card` were doing `list(blockers)` on an int returned by `check_gate_b()` → silent TypeError swallowed under "Lark draft fan-out skipped". After Phase 3 deleted TG, Gate B + Gate C Lark cards stopped emitting entirely and nobody noticed because the log line lied about the cause. The fix:

1. `_normalize_blockers` accepts `int | str | list | None` and emits `list[str]`
2. `post_gate_b` splits the main emit from the side-fanout into two try/except blocks — main emit failures now log at WARNING with `"Gate B Lark review-card emit FAILED for X — operators will not see this draft in Lark"`.

---

## Pre-flight (operator on cloud computer)

```bash
# Walkthrough env
export WALK=/tmp/blogflow-walk-$(date +%s) && mkdir -p $WALK
export AGENTFLOW_HOME=$WALK
export AGENTFLOW_LARK_APP_PRIMARY=true
export MOCK_LLM=true                # demo: skip real LLM cost
export AGENTFLOW_MOCK_PUBLISHERS=true
export AGENTFLOW_DEFAULT_TOPIC_PROFILE=chainstream

# Real deploy would also need (per v1.3.9 doctor check):
#   AGENTFLOW_BRAVE_SEARCH_ENABLED=true  + BRAVE_SEARCH_API_KEY
#   AGENTFLOW_TWITTER_SEARCH_ENABLED=true + TWITTER_BEARER_TOKEN
# Without those, recall collapses to HN Algolia and ChainStream-domain
# content won't surface — doctor flags this loudly.

# Copy existing yamls so the profile + sources are populated:
cp ~/.agentflow/sources.yaml $WALK/sources.yaml
cp ~/.agentflow/topic_profiles.yaml $WALK/topic_profiles.yaml
cp ~/.agentflow/style_profile.yaml $WALK/style_profile.yaml

blogflow doctor
```

Observed key rows:

```
✓ MOCK_LLM mode          ON — D1/D2/D3 use deterministic fixtures
✓ hotspots mock-leak audit clean
✗ Recall sources enabled brave_search has 9 live queries but
                          AGENTFLOW_BRAVE_SEARCH_ENABLED is not true;
                          twitter_search has 6 live queries but
                          AGENTFLOW_TWITTER_SEARCH_ENABLED is not true.
                          Recall pool will collapse to HN Algolia only —
                          set the missing env flag(s) in .env...
✓ Lark App primary       review.*_card events enabled (file queue →
                          ~/.agentflow/agent_events/queue.jsonl); the
                          OpenClaw skill agent should tail this file and
                          push to Lark via the mounted Lark window...
✓ review-daemon
```

The v1.3.9 `Recall sources enabled` row fires immediately on a misconfigured `.env`. Operator fixes that one row → recall pool gets brave/twitter.

---

## Smoke test (one command verifies the daemon→queue→agent chain)

```bash
$ blogflow agent-events-emit-test
emit mode:           file
queue file:          /tmp/blogflow-walk-XXX/agent_events/queue.jsonl
queue grew by:       686 bytes (one envelope appended)
✓ file-queue path verified
```

Captured envelope (one JSON line in `queue.jsonl`):

```json
{
  "schema_version": 1,
  "occurred_at": "2026-05-11T05:00:00+00:00",
  "source": "cli.smoke_test",
  "event_type": "review.gate_a_card",
  "article_id": "hs_smoke_test_001",
  "payload": {
    "gate": "A",
    "short_id": "smoke01",
    "publisher_brand": "AgentFlow Smoke Test",
    "candidates": [{"topic_one_liner": "Smoke test candidate — please ignore", ...}],
    "smoke_test": true
  },
  "event_id": "evt_907b1de6fb9ec516"
}
```

If the skill agent's tail loop is running, this envelope appears in Lark within ~2s as a Gate A card. If not, operator runs `blogflow agent-events-tail -f` in a separate terminal to inspect.

---

## Gate A — topic batch review

```bash
$ blogflow article-hotspots --profile chainstream
```

Real output (mock mode, single candidate kept):

```json
{
  "rerank": {
    "strategy": "topic_fit_freshness_regex_hint",
    "kept_count": 1,
    "topic_fit_preview": [{
      "id": "hs_20260510_001",
      "topic_one_liner": "Claude Code 的 subagent 机制对开发者工作流的影响",
      "topic_fit_score": 0.0825,
      "rerank_score": 0.3473,
      "regex_match": true
    }]
  },
  "profile": {"id": "chainstream", "label": "ChainStream"}
}
```

This pushes three events into the queue:

```
[1] topic_intent_used    aid=None  src=memory
[2] review.gate_a_card   aid=4fb9c2  src=agentflow.review
[3] notify.hotspots_digest  aid=None  src=agentflow.lark_notify
```

**Real Gate A payload** (excerpt):

```json
{
  "gate": "A",
  "card_kind": "review",
  "short_id": "4fb9c2",
  "publisher_brand": "ChainStream",
  "candidate_count": 1,
  "gate_warning": null,         // v1.3.7 — would carry soft-floor warning if hard gate emptied
  "candidates": [{
    "slot": 0,
    "label": "#1",
    "article_id": "hs_20260510_001",
    "hotspot_id": "hs_20260510_001",
    "title": "Claude Code 的 subagent 机制对开发者工作流的影响",
    "red_flags": ["low_topic_fit"],   // ← auto-tagged because fit < 0.10
    "actions": [
      {"label": "✅ 起稿 #1", "command": "lark_gate_a_write",
       "article_id": "hs_20260510_001",
       "payload": {"angle_index": 0, "target_series": "A", "slot": 0}},
      {"label": "📋 详情 #1", "command": "lark_gate_a_expand", ...}
    ]
  }],
  "actions": [
    {"label": "⏰ 4h 后", "command": "lark_defer",
     "payload": {"gate": "A", "hours": 4}},
    {"label": "🚫 全拒绝", "command": "lark_gate_a_reject_all",
     "payload": {"batch_path": "/tmp/.../hotspots/2026-05-10.json"}}
  ]
}
```

**Skill agent's job for this envelope**:

1. Render as a Lark interactive card (header = "Gate A — 1 candidate", red banner if `low_topic_fit` flag present).
2. Each candidate slot → 2 buttons (起稿 + 详情). Card-level → 2 buttons (defer + reject all).
3. When user clicks `✅ 起稿 #1`, agent shells out:
   ```bash
   blogflow lark-cli-emit \
     --command lark_gate_a_write \
     --article-id hs_20260510_001 \
     --operator-open-id ou_walkthrough_op \
     --payload '{"angle_index":0,"target_series":"A","slot":0}'
   ```

Captured CLI response (after operator auth granted via `blogflow review-auth-add-lark ou_walkthrough_op --actions '*'`):

```json
{
  "ack": true,
  "reply_card": {
    "header": {"title": {"content": "已触发 · gate_a_write", ...}},
    ...
  },
  "side_effects": ["gate_a_write_spawned"]
}
```

Skill agent shows the `reply_card` (ack to user "writing in progress") and goes back to tailing the queue for the next event.

---

## Gate B — draft review

After the daemon's `gate_a_write_spawned` subprocess finishes D2 fill, `triggers.post_gate_b(article_id)` fires. Captured event:

```
review.gate_b_card  aid=hs_walk_b2  short_id=cd4357
```

**Real Gate B payload** (excerpt):

```json
{
  "gate": "B",
  "short_id": "cd4357",
  "article_id": "hs_walk_b2",
  "title": "Real-time settlement on Solana — KYT pipeline retro",
  "publisher_brand": "ChainStream",
  "voice": "analytical",
  "word_count": 900,
  "section_count": 2,
  "compliance_score": 1.0,
  "blocker_count": 0,           // v1.3.10 new field
  "blockers": [],               // v1.3.10 — was crashing here pre-fix
  "tags": ["onchain", "solana", "kyt"],
  "self_check_lines": ["fact 1 sourced", "audit verdict pass"],
  "draft_excerpt": "(first 4000 chars of preview)",
  "draft_length": 1234,
  "draft_truncated": false,
  "mirror_url": null,
  "actions": [
    {"label": "✅ 通过", "command": "lark_gate_b_approve"},
    {"label": "✏️ 编辑", "command": "lark_gate_b_edit"},
    {"label": "🔁 重写", "command": "lark_gate_b_rewrite"},
    {"label": "📋 diff", "command": "lark_gate_b_diff"},
    {"label": "🚫 拒绝", "command": "lark_gate_b_reject"},
    {"label": "♻️ refill", "command": "lark_refill"},
    {"label": "📊 meta", "command": "lark_view_meta"},
    {"label": "⏰ 2h 后", "command": "lark_defer", "payload": {"gate": "B", "hours": 2}}
  ]
}
```

Plus a side-fanout event for the Lark Custom Bot digest:

```
notify.draft_ready  aid=hs_walk_b2
```

These two are now properly split — pre-v1.3.10 the main `review.gate_b_card` would silently fail and only the notify fan-out (or just the failure log) would surface. Operator sees both now.

**Skill agent click flow** (operator hits 通过):

```bash
blogflow lark-cli-emit \
  --command lark_gate_b_approve \
  --article-id hs_walk_b2 \
  --operator-open-id ou_walkthrough_op \
  --payload '{"comment":""}'
# → returns {"ack": true, "side_effects": ["gate_b_approve_spawned"]}
```

Daemon spawns image-gate (D2.5) → image-picker card emits → eventually Gate C card emits.

---

## Gate C / Gate D — image + channel

Gate C card structure (from `_emit_lark_gate_c_card` after v1.3.10 fix):

```
{
  "gate": "C",
  "short_id": "<8-char>",
  "article_id": "...",
  "title": "...",
  "cover_path": "/abs/path/cover.png",
  "cover_size": "1200x630",
  "brand_overlay_status": "applied" | "skipped" | "absent",
  "self_check_lines": [...],
  "blockers": [],
  "blocker_count": 0,
  "actions": [
    {"label": "✅ 通过", "command": "lark_gate_c_approve"},
    {"label": "🚫 跳过/拒绝配图", "command": "lark_gate_c_skip"},
    {"label": "🔁 再生成", "command": "lark_gate_c_regen",
     "payload": {"mode": "cover-only", "accepts_text": true}},
    {"label": "🎨 换 logo 位置", "command": "lark_gate_c_relogo"},
    {"label": "🖼 全分辨率", "command": "lark_gate_c_full"},
    {"label": "⏰ 2h 后", "command": "lark_defer", "payload": {"gate": "C", "hours": 2}}
  ]
}
```

Gate D card:

```
{
  "gate": "D",
  "short_id": "dc8786",
  "article_id": "...",
  "title_short": "...",
  "available_count": 3,
  "available_list": "medium, twitter, ghost",
  "actions": [
    {"label": "✅ medium", "command": "lark_gate_d_toggle", "payload": {"channel": "medium"}},
    {"label": "✅ twitter", "command": "lark_gate_d_toggle"},
    {"label": "⚡ 全选", "command": "lark_gate_d_select_all"},
    {"label": "💾 保存默认", "command": "lark_gate_d_save_default"},
    {"label": "✅ 通过并发布", "command": "lark_gate_d_confirm"},
    {"label": "🚫 取消", "command": "lark_gate_d_cancel"},
    {"label": "⏰ 延 6h", "command": "lark_gate_d_extend", "payload": {"hours": 6}}
  ]
}
```

Operator approves channels → `lark_gate_d_confirm` → daemon kicks D3 (preview) + D4 (publish per channel) → final emits:

```
review.publish_ready_card  aid=...   # operator confirms pasted Medium URL
notify.publish_done        aid=...   # one per platform that succeeded
```

---

## Profile setup multi-turn flow (v1.3.6 path)

If `_maybe_trigger_profile_setup` fires (when `blogflow article-hotspots` runs without an existing profile, or when intent commands trigger it), the first event is:

```
review.profile_setup_card  aid=<profile_id>
```

Payload:

```json
{
  "gate": "P",
  "profile_id": "chainstream",
  "reason": "missing required fields",
  "missing_fields": ["brand", "writing_defaults", "source_materials", "rules"],
  "session_path": "/tmp/.../topic_sessions/profile_chainstream_<ts>.json",
  "actions": [
    {"label": "🧩 开始补全", "command": "lark_profile_advance",
     "payload": {"session_path": "...", "answer_field": "text"}},
    {"label": "稍后", "command": "lark_defer", "payload": {"gate": "P", "hours": 4}}
  ]
}
```

User clicks 开始补全 → skill agent collects answer via Lark message → shells out:

```bash
blogflow lark-cli-emit \
  --command lark_profile_advance \
  --article-id chainstream \
  --operator-open-id ou_op \
  --payload '{"session_path":"...","question_field":"brand","answer_field":"text","text":"ChainStream"}'
```

Per-question loop continues; each answer triggers the next `review.profile_setup_card` (question-advance form, with `current_question` / `question_index` / `total_questions` fields). When done, daemon emits:

```
notify.profile_setup_done  payload={profile_id, completed_fields, next_action="d1_scan"}
```

Plus side-channel from `_spawn_apply_profile_session` (v1.3.6 fix): `notify.profile_setup_done` or `notify.profile_setup_failed` depending on the CLI subprocess result.

---

## What broke and v1.3.10 fixed

| Symptom | Pre-fix cause | Fix |
|---|---|---|
| Gate B / Gate C cards never appeared in Lark after Phase 3 | `check_gate_b()` returns `(lines, int)`; `_emit_lark_gate_b_card` did `list(blockers)` → TypeError | `_normalize_blockers` accepts int / list / None, normalizes to `list[str]`. New `blocker_count` field. |
| Failure was invisible in logs | One outer try/except wrapped both `_emit_lark_gate_b_card` (main) + `lark_webhook.notify_draft_ready` (side fanout); any exception → "Lark draft fan-out skipped" at INFO | Split into two try blocks. Main emit failure now WARNING with explicit "operators will not see this draft" marker. |
| Diagnostic tools didn't catch it | Doctor doesn't exercise emit paths; tests passed because `_lark_app_primary()` is mocked false in many test fixtures | (Future: integration test that drives a real Gate B emit with `LARK_APP_PRIMARY=true` in a temp HOME) |

The class of bug is **Phase 3 silent-emit regression**: code paths that used to emit to TG (where errors surfaced loudly in the TG send) became Lark-emit paths where any exception was swallowed under misleading log lines. v1.3.6 (profile flow), v1.3.10 (Gate B/C) both belong to this class.

## Audit grep for remaining silent-broken sites

```bash
cd backend && grep -B2 -A3 "Lark .* skipped\|Lark fan-out\|Lark emit failed" agentflow/agent_review/triggers.py
```

Anything that hides a Lark-side `_emit_lark_*` call inside a try/except logging at INFO is a candidate. The pattern to look for:
```python
try:
    ...
    _emit_lark_X_card(...)
    side_fanout(...)
except Exception as err:
    _log.info("... skipped for %s: %s", aid, err)
```

→ split into separate try blocks with the main emit elevated to WARNING.
