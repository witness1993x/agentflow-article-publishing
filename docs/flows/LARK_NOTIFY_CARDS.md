# Lark Notify Card Rendering Contract

This file is the source of truth for OpenClaw's `renderNotifyCard(payload)`.
AgentFlow emits `notify.*_*` events from `agentflow/shared/lark_webhook.py`
(via `_emit_notify_event`) when `AGENTFLOW_LARK_APP_PRIMARY=true`. OpenClaw
renders them as Lark interactive **broadcast / status** cards. They are
push-only side-channels around the review pipeline — not actionable Gate
cards.

Sibling contract for the actionable side: see
[`../../backend/agentflow/agent_review/templates/lark_review_cards.md`](../../backend/agentflow/agent_review/templates/lark_review_cards.md)
for the `review.*_card` family.

## Common Rules for `notify.*`

- `notify.*` are **NOT** review cards. OpenClaw MUST NOT route them through
  the same renderer as `review.*_card`. Use a dedicated `renderNotifyCard`
  path keyed on the `notify.` prefix.
- `notify.*` MUST NOT be treated as actionable Gate cards. In particular,
  `notify.hotspots_digest` is **push-only**; the OpenClaw v1.1.7 footgun
  was rendering it as Gate A. The digest carries no `candidates[]`, no
  `short_id`, no per-candidate buttons — treating it as Gate A produces a
  dead card.
- Most `notify.*` events have **no buttons**. Exceptions are explicit
  per-card below (currently only `notify.dispatch_result` when it has
  failed platforms).
- Severity / styling hints map to Lark card `template` colors:
  `info`→`blue`, `success`→`green`, `warning`→`orange`, `error`→`red`,
  `muted`→`grey`.
- `article_id` rides on the agent-event envelope (top-level), not always
  on `payload`. Pull from the envelope for the card identifier; pull
  display fields from `payload`.
- A `notify.*` event missing its required fields MUST render as a
  degraded text-only summary, never as a fabricated Gate card.

## `notify.dispatch_preview`

Purpose: pre-publish preview shown before Gate D dispatch fires. Lets the
operator sanity-check platform selection one last time. **Push-only** —
the actionable PD:dispatch / PD:cancel buttons live on the source
`review.gate_d_card`, not on this notification. Today only the TG render
`render_dispatch_preview` exists; this event is reserved for future
emission so OpenClaw can render a parallel push card. Documented now so
the contract is fixed.

Required fields: `article_id`, `title`, `selected_platforms[]`,
`per_platform_info{}` (optional, keyed by platform — `word_count`,
`manual`, `note`).

Layout: list-style, one bullet per platform with optional
`(N 字 · manual paste)` annotations.

Buttons: none.

Severity / styling hint: `info` (blue).

## `notify.dispatch_result`

Purpose: post-publish dispatch summary. Emitted by
`triggers.post_publish_dispatch` after the publish chain finishes (mix
of success / failed / manual results). Mirrors `render_dispatch_summary`
on the TG side.

Emit site: `backend/agentflow/agent_review/triggers.py:2282` →
`lark_webhook.notify_dispatch_result` →
`backend/agentflow/shared/lark_webhook.py:340`.

Required fields: `article_id`, `title`, `succeeded[]` (list of platform
ids), `failed[]` (list of `{platform, reason}` objects).

Layout: two-section list — "已发布" with green check per success,
"未发布" with red cross + reason per failure. Reason strings may be
truncated to ~120 chars; do not re-truncate beyond that.

Buttons: only when `failed[]` is non-empty.

| Button | Command | Payload |
|---|---|---|
| `🔁 重试失败` | `lark_gate_d_retry` | `article_id`, `platforms` (the failed platforms list only) |

The retry payload shape matches `lark_gate_d_retry` as documented in
`lark_review_cards.md` (Gate D row). When `platforms` is omitted, the
backend falls back to metadata — but OpenClaw SHOULD always include the
failed list so the retry is scoped to the failures, not the entire
original selection.

Severity / styling hint:
- all succeeded → `success` (green)
- partial failure → `warning` (orange)
- all failed → `error` (red)

## `notify.publish_ready`

Purpose: a Medium-track article has finished package generation and is
waiting for the operator's manual paste step. URL-only nudge — the
actionable "📌 我已粘贴 + URL" button lives on the source TG `PR:mark`
card, or in the CLI command `af review-publish-mark`.

Emit site: `backend/agentflow/agent_review/triggers.py:1589` →
`lark_webhook.notify_publish_ready` →
`backend/agentflow/shared/lark_webhook.py:401`.

Required fields: `article_id`, `title`.

Layout: single-line summary plus a hint that the operator should mark
the published Medium URL via TG card or CLI.

Buttons: none on the Lark side. (Legacy Custom Bot mode adds a URL
button to the TG bot; Lark-app-primary mode does not, because there is
no actionable Lark callback for publish-mark yet.)

Severity / styling hint: `info` (blue).

## `notify.publish_digest`

Purpose: periodic (24h cooldown) digest of articles stuck in
`ready_to_publish` for more than 24 hours. Read-only nudge produced by
`triggers.post_publish_digest` — operators run
`af review-publish-mark <aid> <url> --platform medium` from the CLI.

> Note: today the daemon only sends this digest to TG (see
> `triggers.py:2546`). The `_emit_notify_event("publish_digest", ...)`
> call is a future addition; document the contract now so OpenClaw
> renders consistently when wired.

Required fields: `count` (int), `items[]` where each item is
`{article_id, title, age_hours}`.

Layout: header line `📌 待 publish-mark · N 篇`, then one bullet per
item:

```
• <aid_short> — <title> (ready <age_hours>h+)
```

Followed by the CLI hint `af review-publish-mark <aid> <url> --platform medium`.

Buttons: none.

Severity / styling hint: `muted` (grey) when `count == 0`, `info` (blue)
otherwise.

## `notify.hotspots_digest`

Purpose: scheduled (typically 09:00 / 20:00) hotspot scan summary. Tells
the operator how many candidates were found and the top titles.
**Push-only fan-out from v1.0.30** — there is no Gate A intent here.

Emit site: `backend/agentflow/agent_review/triggers.py:637` →
`lark_webhook.notify_hotspots_digest` →
`backend/agentflow/shared/lark_webhook.py:431`.

Required fields: `scan_count` (int), `top_titles[]` (list of strings).

Layout: when `scan_count == 0`, a single muted line "今日扫描完成: 暂无可写
热点". Otherwise a numbered list of `top_titles` (max ~5).

Buttons: none. **Do not render Gate A buttons here** — that was the
v1.1.7 footgun. The actual Gate A card arrives separately as
`review.gate_a_card` once the operator chooses to write.

Severity / styling hint: `muted` (grey) when `scan_count == 0`,
`warning` (orange) otherwise.

## `notify.draft_ready`

Purpose: Gate B fan-out — the assembled draft body is ready. In
Lark-app-primary mode this carries the draft excerpt so OpenClaw can
render a live Gate B preview alongside the actionable
`review.gate_b_card`. Push-only on this channel.

Emit site: `backend/agentflow/agent_review/triggers.py:943` →
`lark_webhook.notify_draft_ready` →
`backend/agentflow/shared/lark_webhook.py:471`.

Required fields: `article_id`, `title`. Optional: `audit_summary`,
`mirror_url`, `draft_md_path`, `draft_excerpt` (≤ 2000 chars),
`draft_length` (int), `draft_truncated` (bool).

Layout: header (`title` + `article_id` + optional `audit_summary`), then
the excerpt. When `draft_truncated == true`, append a `…(完整 N 字)` line
and surface `mirror_url` as a non-callback URL link if present.

Buttons: none on this card. The actionable Gate B controls live on
`review.gate_b_card`.

Severity / styling hint: `success` (green) when not truncated,
`info` (blue) when truncated.

## `notify.spawn_failure`

Purpose: child subprocess (write / publish / image-gate) failed to spawn
or crashed. Mirrors `daemon._notify_spawn_failure` into Lark for the
on-call channel.

Emit site: `backend/agentflow/agent_review/triggers.py:2113` and
`backend/agentflow/agent_review/daemon.py:4423` →
`lark_webhook.notify_spawn_failure` →
`backend/agentflow/shared/lark_webhook.py:590`.

Required fields: `label` (e.g. `write`, `publish`, `image-gate`),
`target_id` (article_id or hotspot id), `error_tail` (last N chars of
stderr, default 2000).

Layout: header `子任务失败`, then `label` + `target_id`, then the
`error_tail` inside a fenced code block.

Buttons: none. Triage flow is human (CLI / dashboard).

Severity / styling hint: `error` (red).

## `notify.profile_setup_done`

Purpose: announce that a multi-turn topic profile setup flow finished
for a given `profile_id`. Followed by the daemon advancing the article
to D1 hotspots scan.

> Status: contract documented now; emit site lands with the GAP-P2
> implementation. See `docs/BLOGFLOW_TG_TO_LARK_PARITY.md` §3.2 for the
> end-to-end flow and the `review.profile_setup_card` schema extensions
> that drive this event.

Required fields: `profile_id`, `completed_fields[]` (list of field names
that were collected this round).

Optional fields: `session_path` (where the answers were persisted),
`next_action` (typically `"d1_scan"` so OpenClaw can show a "扫描已启动"
hint).

Layout: short single-paragraph banner — `Profile <profile_id> 已补全 N
项: <completed_fields joined>`.

Buttons: none. The next user-facing card is whatever D1 produces (e.g.
`review.gate_a_card` after the scan completes).

Severity / styling hint: `success` (green).

---

## Field Reference Quick Table

| Event | Required fields | Buttons | Severity |
|---|---|---|---|
| `notify.dispatch_preview` | `article_id`, `title`, `selected_platforms[]` | none | info |
| `notify.dispatch_result` | `article_id`, `title`, `succeeded[]`, `failed[]` | retry (only on failure) | success / warning / error |
| `notify.publish_ready` | `article_id`, `title` | none | info |
| `notify.publish_digest` | `count`, `items[]` | none | info / muted |
| `notify.hotspots_digest` | `scan_count`, `top_titles[]` | none | warning / muted |
| `notify.draft_ready` | `article_id`, `title` | none | success / info |
| `notify.spawn_failure` | `label`, `target_id`, `error_tail` | none | error |
| `notify.profile_setup_done` | `profile_id`, `completed_fields[]` | none | success |

An unlisted `notify.*` event MUST render as a generic text card (title =
event_type, body = pretty-printed payload) and log a warning — never
invent buttons or treat it as a review card.
