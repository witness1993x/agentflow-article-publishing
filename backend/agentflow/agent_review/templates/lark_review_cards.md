# Lark Review Card Rendering Contract

This file is the source of truth for OpenClaw's `renderReviewCard(payload)`.
AgentFlow emits `review.*_card` events; OpenClaw renders them as Lark
interactive cards and sends callbacks to `/api/commands`.

`notify.*` events are broadcast/status cards only. They must not be rendered as
review cards, and `notify.hotspots_digest` must never be treated as Gate A.

## v1.1.8 — free-text @-mention path

In addition to button callbacks, OpenClaw must forward every @-bot text
message to the daemon as `lark_message`:

```json
{
  "command": "lark_message",
  "params": {
    "text": "推进到下个 gate",
    "operator_open_id": "ou_xxx",
    "operator_name": "Alice",
    "chat_id": "oc_lark_chat_42"
  }
}
```

The daemon classifies the intent deterministically and returns the same
`{ack, reply_card, reply_text, side_effects}` envelope as a button callback.
**OpenClaw must render `reply_card` as-is and never let an LLM fabricate a
result** — silence on a `lark_message` call is a contract violation. The full
intent matrix lives in `docs/flows/LARK_FIRST_REVIEW_FLOWS.md` §4.

`chat_id` should be included in **every** `lark_message` and card-callback
body so the daemon can target downstream Gate cards back to the same chat.

## Common Rules

- Use `payload.actions[]` for buttons. Each action already contains
  `label`, `command`, `article_id`, and `payload`.
- Button callbacks call `/api/commands` with the action's `command`, `article_id`,
  and nested `payload`.
- Commands marked `dangerous=true` in `/api/bridge` require
  `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`.
- Cards that collect text must submit the text in the documented payload field.
  If the operator opens an edit flow without text, OpenClaw should show a prompt
  and later call `lark_apply_pending_edit` with the @bot message body.
- Render Lark buttons in the same order as listed here so the flow stays close
  to the Telegram review muscle memory.

## Gate A: `review.gate_a_card`

Purpose: select one article hotspot for writing.

Required fields: `short_id`, `publisher_brand`, `target_series`,
`candidate_count`, `candidates[]`.

Required per-candidate buttons:

| Button | Command | Payload |
|---|---|---|
| `起稿 #N` | `lark_gate_a_write` | `angle_index`, `target_series`, `slot` |
| `详情 #N` | `lark_gate_a_expand` | none |

Required card-level buttons:

| Button | Command | Payload |
|---|---|---|
| `全拒绝` | `lark_gate_a_reject_all` | `batch_path` |
| `推迟` | `lark_defer` | `gate="A"`, `hours` |

Input fields: none.

## Profile Setup: `review.profile_setup_card`

Purpose: collect missing topic profile fields before D1/D2 continues.

Required fields: `profile_id`, `reason`, `missing_fields`, `session_path`.

Required buttons:

| Button | Command | Payload |
|---|---|---|
| `开始补全` | OpenClaw profile setup flow | `profile_id`, `session_path` |
| `稍后` | `lark_defer` | `gate="P"` |

Input fields:

| Field | Required | Meaning |
|---|---|---|
| `payload.text` | yes during setup steps | free-form operator answer |
| `payload.answer` | accepted alias | free-form operator answer |

## Gate B: `review.gate_b_card`

Purpose: review the generated draft.

Required fields: `article_id`, `title`, `word_count`, `section_count`,
`compliance_score`, `self_check_lines`, `draft_excerpt`, `actions[]`.

Required buttons:

| Button | Command | Payload |
|---|---|---|
| `通过` | `lark_gate_b_approve` | none |
| `编辑` | `lark_gate_b_edit` | optional section/paragraph target plus text |
| `重写` | `lark_gate_b_rewrite` | optional rewrite instruction |
| `refill` | `lark_refill` | none |
| `拒绝` | `lark_gate_b_reject` | none |
| `diff` | `lark_gate_b_diff` | none |
| `meta` | `lark_view_meta` | none |
| `推迟` | `lark_defer` | `gate="B"`, `hours` |

Input fields for edit/rewrite/refill guidance:

| Field | Priority | Meaning |
|---|---|---|
| `payload.comment` | 1 | edit or rewrite instruction from textarea |
| `payload.edit_text` | 2 | accepted alias |
| `payload.prompt` | 3 | accepted alias |
| `payload.feedback` | 4 | accepted alias |
| `payload.text` | 5 | accepted alias or @bot body |

When `lark_gate_b_edit` has no text, OpenClaw must show a pending-edit prompt.
The follow-up @bot message calls `lark_apply_pending_edit` with `payload.text`.

## Image Picker: `review.image_gate_picker_card`

Purpose: choose whether and how to generate article images after Gate B.

Required fields: `article_id`, `title`, `actions[]`.

Required buttons:

| Button | Command | Payload |
|---|---|---|
| `cover-only` | `lark_image_gate_cover_only` | `mode="cover-only"` |
| `cover+body` | `lark_image_gate_cover_plus_body` | `mode="cover-plus-body"` |
| `跳过封面` | `lark_image_gate_skip` | `mode="none"` |

Input fields for generation direction:

| Field | Priority | Meaning |
|---|---|---|
| `payload.prompt` | 1 | visual direction or constraints |
| `payload.cover_description` | 2 | accepted alias |
| `payload.comment` | 3 | accepted alias |
| `payload.feedback` | 4 | accepted alias |
| `payload.text` | 5 | accepted alias |

## Gate C: `review.gate_c_card`

Purpose: review the generated cover image.

Required fields: `article_id`, `title`, `cover_path`, `cover_size`,
`brand_overlay_status`, `self_check_lines`, `actions[]`.

Required buttons:

| Button | Command | Payload |
|---|---|---|
| `通过` | `lark_gate_c_approve` | none |
| `跳过/不用图` | `lark_gate_c_skip` | none |
| `再生成` | `lark_gate_c_regen` | `mode`, optional text |
| `换 logo 位置` | `lark_gate_c_relogo` | none |
| `全分辨率` | `lark_gate_c_full` | none |
| `推迟` | `lark_defer` | `gate="C"`, `hours` |

Input fields for regeneration:

| Field | Priority | Meaning |
|---|---|---|
| `payload.prompt` | 1 | image regeneration instruction |
| `payload.comment` | 2 | accepted alias |
| `payload.feedback` | 3 | accepted alias |
| `payload.text` | 4 | accepted alias |

Backend passes the text to `blogflow image-gate --cover-description`.

## Gate D: `review.gate_d_card`

Purpose: choose publishing platforms and start dispatch.

Required fields: `article_id`, `title`, `available[]`, `selected[]`,
`actions[]`.

Required buttons:

| Button | Command | Payload |
|---|---|---|
| platform toggle | `lark_gate_d_toggle` | `platform` |
| `全选` | `lark_gate_d_select_all` | `platforms` |
| `保存默认` | `lark_gate_d_save_default` | none |
| `确认发布` | `lark_gate_d_confirm` | none |
| `取消` | `lark_gate_d_cancel` | none |
| `延长` | `lark_gate_d_extend` | `gate="D"` |

Input fields: none.

`lark_gate_d_confirm` must run the full dispatch chain: preview, non-Medium
publish, Medium package generation, publish-ready notification, dispatch
summary, and `notify.dispatch_result`.

## Locked Takeover: `review.locked_takeover_card`

Purpose: force a human edit after repeated rewrite rounds.

Required fields: `article_id`, `title`, `rewrite_count`, `actions[]`.

Required buttons:

| Button | Command | Payload |
|---|---|---|
| `critique` | `lark_locked_critique` | none |
| `接管编辑` | `lark_locked_edit` | optional replacement/instruction text |
| `放弃` | `lark_locked_give_up` | none |

Input fields for takeover edit:

| Field | Priority | Meaning |
|---|---|---|
| `payload.comment` | 1 | edit instruction or replacement text |
| `payload.edit_text` | 2 | accepted alias |
| `payload.prompt` | 3 | accepted alias |
| `payload.feedback` | 4 | accepted alias |
| `payload.text` | 5 | accepted alias or @bot body |

When `lark_locked_edit` has no text, OpenClaw must ask the operator to @bot
with the edit body, then call `lark_apply_pending_edit`.
