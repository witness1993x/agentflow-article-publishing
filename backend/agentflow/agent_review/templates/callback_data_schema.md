# callback_data Schema for Telegram Inline Buttons

Telegram inline-keyboard callback_data has a hard 64-byte UTF-8 cap. Article
ids in this project are ~50 chars (`hs_topic_id_001-20260425042222-b563bc3c`),
which alone blows the budget once any action verb is appended.

## Format

```
<gate>:<action>:<short_id>[:<extra>]
```

| Segment | Length cap | Examples |
|---------|-----------|----------|
| `<gate>` | 1 char | `A`, `B`, `C` |
| `<action>` | ≤ 12 chars | `approve`, `reject`, `regen`, `defer`, `skip`, `edit`, `rewrite`, `expand`, `relogo`, `full`, `diff` |
| `<short_id>` | 6 chars | `a3f7b2`, `c91d04` |
| `<extra>` | ≤ 32 chars (optional) | `angle=0`, `hours=2`, `cover`, `round=2` |

Worst case: `B:rewrite:a3f7b2:round=2` = 24 bytes. Well within budget.

## short_id ↔ article_id resolution

The bot daemon maintains `~/.agentflow/review/short_id_index.json`:

```json
{
  "a3f7b2": {
    "article_id": "hs_topic_id_001-20260425042222-b563bc3c",
    "gate": "B",
    "created_at": "2026-04-25T08:00:00+00:00",
    "expires_at": "2026-04-26T08:00:00+00:00"
  },
  "c91d04": {
    "kind": "topic_batch",
    "batch_path": "~/.agentflow/hotspots/2026-04-25.json",
    "slot_count": 3,
    "created_at": "...",
    "expires_at": "..."
  }
}
```

- `short_id` is generated as `secrets.token_hex(3)` (6 hex chars, ~17M unique).
- Each entry has a TTL ≥ the gate's timeout. Expired entries are GC'd hourly.
- Collisions detected on insert; regen on collision.

When a callback_query arrives:

```python
gate, action, short_id, *extra = callback_data.split(":", 3)
entry = read_index()[short_id]
if not entry or expired(entry):
    answer_callback(query, "已失效，请等待新一轮")
    return
dispatch(gate, action, entry, extra)
```

## Action vocabulary (closed set)

```
approve     — go to next state
reject      — terminal; no further automation
edit        — multi-turn: bot waits for follow-up message
rewrite     — re-run the same generator step (round counter ++)
regen       — alias for image-side rewrite (clearer in UI)
skip        — skip this gate and advance (e.g., "no cover")
defer       — re-post the same card after `hours=N`
expand      — bot sends a more detailed message inline
diff        — bot sends a unified diff vs last reviewed version
full        — bot sends original full-resolution image as a document
relogo      — multi-turn: pick logo anchor, then `regen`
write       — Gate A only: kick off `af write` for the picked slot
reject_all  — Gate A only: reject every candidate in the batch
```

Any other string → `answer_callback("未知操作")`, no state change.

## Why not pickle a full state into callback_data

Considered: signed token with the article_id + action embedded. Rejected
because:
- Telegram caps at 64 bytes; even base64-signed payloads with article_id
  alone exceed that.
- An indirection table is recoverable across daemon restarts (it's a JSON
  file). Tokens-in-callback would die on restart.
- Audit trails are easier when the index file is the persisted truth.

## Security note

callback_data is round-tripped via Telegram, so a malicious actor with
access to the chat could inject any callback string they like. Treat the
short_id table as the trust boundary:
- Reject callbacks whose short_id is missing or expired.
- Reject callbacks whose `gate` doesn't match the indexed entry's gate
  (e.g., a `B:approve:c91d04` against an entry registered as Gate A).
- Log all callbacks (including rejected ones) to `~/.agentflow/review/audit.jsonl`.
