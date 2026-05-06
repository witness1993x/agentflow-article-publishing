# Gate B — Draft Article Review (Lark-first / TG fallback)

**When fired:** after `af fill` completes (D2 finished, body has compliance
score). Draft is paused before image generation. Cannot be skipped — long-form
review is mandatory per the social-content-review proposal (📝 long-form
always requires human approval).

**Timeout:** 12h. After timeout: pings reminder, holds article in
`draft_pending_review` indefinitely (does not auto-degrade — long-form is
worth the wait).

---

## Telegram message template (Markdown V2, two-message pattern)

The draft is too long for one Telegram message (4096 char limit). The bot
sends:

1. **Card message** — title/subtitle/tags + first paragraph + sanity check
   results + buttons.
2. **Document attachment** — full body markdown as `{article_short_id}.md`.

### Card body

```text
📝 *Gate B — Draft Review*  ·  {article_short_id}  ·  {timestamp_local}

*{title}*
_{subtitle}_

━━━━━━━━━━━━━━━━━━━━━━━━
publisher: *{publisher_brand}* · voice: *{voice_label}*
words: *{word_count}* · sections: *{section_count}* · compliance: *{compliance_score}*
tags: {tags_joined}

━━━━━━━━━━━━━━━━━━━━━━━━
*Self\\-check*

{checklist_pass_or_fail_lines}

━━━━━━━━━━━━━━━━━━━━━━━━
*Opening*

{opening_first_120_chars}…

━━━━━━━━━━━━━━━━━━━━━━━━
全文已附在下一条消息
```

### Self-check lines (one per check; `✓` or `✗ — reason`)

```
✓ 视角=publisher_account.voice
✓ 无未标注外链
✗ 段落超长 — 第 3 节出现 132 词长段
✓ 合规词汇过滤通过
✓ 引用合规 — 0 处他人观点
✓ 免责声明就位（投资类未触发）
✗ SEO 元数据 — canonical_url 未填
```

## Inline keyboard layout

```
[ ✅ 通过 ]   [ ✏️ 编辑 ]
[ 🔁 重写 ]  [ 🚫 拒绝 ]
[ 📊 看 diff ]  [ ⏰ 推迟 2h ]
```

## Lark / OpenClaw card layout

Lark is the primary operator surface. Render the same summary as an
interactive card, but use command payloads instead of Telegram `callback_data`:

```json
{
  "article_id": "{article_id}",
  "action": "gate_b_edit",
  "payload": {
    "section_index": 2,
    "paragraph_index": 0,
    "comment": "<textarea value>"
  }
}
```

Recommended buttons / inputs:

```text
[ ✅ 通过 ]        -> lark_gate_b_approve
[ ✏️ 提交修改 ]    -> lark_gate_b_edit + payload.comment
[ 🔁 重写/refill ] -> lark_refill or lark_gate_b_rewrite (dangerous)
[ 🚫 拒绝 ]        -> lark_gate_b_reject
[ 📊 看 diff ]     -> lark_gate_b_diff
[ ⏰ 推迟 ]         -> lark_defer payload.gate="B"
```

Input compatibility:
- If the card has an inline textarea, send the text as `payload.comment`
  (also accepted: `edit_text`, `prompt`, `feedback`, `text`).
- If the operator @-mentions the bot later, OpenClaw must call
  `lark_apply_pending_edit` with `payload.text`. The backend consumes the
  latest pending slot once and runs `af edit --post-review`.
- Edit-spawning Lark commands are `dangerous=true`; the bridge must opt in
  with `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`.

## callback_data values

| Button | callback_data |
|---|---|
| ✅ 通过 | `B:approve:{short_id}` |
| ✏️ 编辑 | `B:edit:{short_id}` |
| 🔁 重写 | `B:rewrite:{short_id}` |
| 🚫 拒绝 | `B:reject:{short_id}` |
| 📊 看 diff | `B:diff:{short_id}` (vs last reviewed version, if any) |
| ⏰ 推迟 2h | `B:defer:{short_id}:hours=2` |

After ✏️ or 🔁, the bot enters a multi-turn flow:
- ✏️ 编辑 → TG replies "回复想改的位置 (title / opening / closing / 第 N 段)
  + 改写指令"; user replies; bot calls `af edit <article_id> --section <N>
  --command "<text>" --post-review` and re-posts an updated card. Lark either
  submits inline `payload.comment` directly or uses `lark_apply_pending_edit`
  for the @bot follow-up.
- 🔁 重写 → bot calls `af fill <article_id>` again with the same
  title/opening/closing indices but bumps a `rewrite_round` counter;
  after 2 rewrites, escalates to 🔴 (forces human edit, no further auto-rewrite).

## Sanity-check failures that block ✅ until resolved

Hard blockers (the ✅ button is disabled):
- compliance score < 0.7
- any `[IMAGE: ...]` markers still in body that aren't in `image_placeholders`
- subtitle starts with `!`/`![` (auto-extractor bug residue)

Soft blockers (✅ enabled but warning shown):
- canonical_url missing
- tags auto-inferred (no override, no publisher.default_tags)
- word count off target by >30%

## Outcomes → backend

| User action | Backend effect |
|---|---|
| ✅ 通过 | metadata.status = `draft_approved`; gate_history append; daemon advances to Gate C |
| ✏️ 编辑 | Multi-turn / inline edit; `af edit --post-review`; new version posted, ✅/🚫 again |
| 🔁 重写/refill | `af fill` / `af fill --skeleton-only --auto-pick` re-run; rewrite_round++; if =2, escalate to manual |
| 🚫 拒绝 | metadata.status = `draft_rejected`; article archived; pipeline halts |
| 📊 看 diff | Bot sends a unified diff against last reviewed version |
| ⏰ 推迟 2h | Re-post in 2h |
