# Review bot security

The Telegram review bot is the only network-exposed surface in AgentFlow that
mutates state on operator command. Auth is enforced at two layers:

1. **Telegram identity** — every callback / message carries `from.id` (the
   Telegram uid). DMs additionally satisfy `chat_id == uid`.
2. **AgentFlow grant table** — `~/.agentflow/review/auth.json`, managed via
   `af review-auth-*` from the operator's terminal.

## Operator (env-bound, implicit)

The operator's uid is the value of `TELEGRAM_REVIEW_CHAT_ID` (set at install
time, captured automatically on the first `/start`). The operator:

- is never stored in `auth.json`
- implicitly has `["*"]` (full access)
- is non-removable via `af review-auth-remove`

## Explicit grants (terminal-managed)

```
af review-auth-add 12345 --note "Alice"
af review-auth-list
af review-auth-remove 12345
```

Default grants get `allowed_actions = ["*"]` (legacy, fully privileged).

## Per-action grants

Grants can be narrowed to a subset of actions. Closed vocabulary:

| verb      | covers                                                  |
|-----------|---------------------------------------------------------|
| `review`  | Gate B/C ✅ approve / reject / skip / diff / full / defer |
| `write`   | Gate A ✅ 起稿 #N (kicks off `af write` + `af fill`)      |
| `edit`    | ✏️ edit / 🔁 rewrite                                     |
| `image`   | 🎨 relogo / 🔁 regen on Gate C                           |
| `publish` | reserved (publish-mark is CLI-only today)                |
| `*`       | full access (default; legacy entries assume this)        |

`(gate, action) → required` map enforced by the daemon:

| callback             | required verb |
|----------------------|---------------|
| `A:write`            | `write`       |
| `A:reject_all`       | `review`      |
| `A:expand`           | `review`      |
| `A:defer`            | `review`      |
| `B:approve`          | `review`      |
| `B:reject`           | `review`      |
| `B:rewrite`          | `edit`        |
| `B:edit`             | `edit`        |
| `B:diff`             | `review`      |
| `B:defer`            | `review`      |
| `C:approve`          | `review`      |
| `C:skip`             | `review`      |
| `C:regen`            | `image`       |
| `C:relogo`           | `image`       |
| `C:full`             | `review`      |
| `C:defer`            | `review`      |
| ✏️ edit-followup msg | `edit`        |

A grant of `*` always wins. The operator implicitly has `*`.

### Examples

A reviewer-only teammate (can approve drafts + covers, can refine via edit,
but cannot kick off new write jobs or regenerate images):

```
af review-auth-add 12345 --note "Alice (reviewer)" --actions review,edit
```

Drop `image` from a teammate without removing them:

```
af review-auth-set-actions 12345 review,edit
```

Promote to full access:

```
af review-auth-set-actions 12345 "*"
```

## Backward compatibility

Entries persisted before this change (no `allowed_actions` key) continue to
behave as `["*"]`. The file is only re-written when an entry is touched via
`add` / `set-actions`, so a read-only daemon keeps working against an
un-migrated `auth.json`.

## Audit trail

Every denied callback writes a `callback_action_denied` event to
`~/.agentflow/review/audit.jsonl` with `{gate, action, required, uid,
short_id}`. Denied edit-followup messages write `edit_reply_denied`. Use
`jq` over that file to spot abuse / misconfiguration.
