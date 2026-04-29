# AgentFlow Open Claw Examples (v2)

Three real scenarios drawn from the live review pipeline. Each follows the Discover -> Decide -> Act -> Verify -> Next loop.

## Example 1: extend `/list` to surface `ready_to_publish`

User asks:

> /list 现在不显已 dispatch 但等 medium-paste 的文章, 加上

```markdown
## Discover
- current `/list` only scans Gate B / C / D pending states
- gap: STATE_READY_TO_PUBLISH is invisible
- source of truth: `backend/agentflow/agent_review/daemon.py` plus `state.py`

## Decide
- include STATE_READY_TO_PUBLISH in the pending_states scan set
- add a filter argument so `/list ready` narrows the view
- do not invent a new state; the existing one already covers the case

## Act
- extend `list_filter` parsing in daemon.py
- add a `ready` key to the `states_by_filter` dict
- keep the default `/list` output inclusive of the new state

## Verify
- run `pytest tests/test_v02_workflows.py -q`
- manually post `/list` and `/list ready` to the review chat against a seeded fixture

## Next
- follow-up: render the article's time-in-state by reading the last `gate_history` timestamp
```

## Example 2: fix red flag R2 — `A:reject_all` does not move state

User asks:

> daemon 的 A:reject_all 现在只 revoke sid + ack, 不动 state, 修一下

```markdown
## Discover
- `A:reject_all` works at the hotspot batch level
- there is no per-article `article_id` to flip
- `state.py` has no STATE_TOPIC_POOL_STALE constant; that name is a doc ghost

## Decide
- mark each hotspot in the batch with `status="rejected_batch"`
- write a single audit row with `kind="batch_rejected"`
- do not invent a new STATE_*; topic state stays in topic_pool until A:write picks one

## Act
- read `entry.batch_path`, load the batch JSON, and stamp each hotspot's status
- on IO failure, audit warning and ack so the operator is not blocked
- keep revoke + ack ordering unchanged

## Verify
- grep `rejected_batch` in `backend/agentflow/agent_review/daemon.py`
- run `pytest tests/test_v02_workflows.py -q`

## Next
- align A 24h timeout with the same batch-rejected behavior
```

## Example 3: add `PR:mark` TG callback

User asks:

> S6 让 operator 在 TG 直接粘贴 medium URL, 不要 CLI

```markdown
## Discover
- `render_publish_ready` needs a Telegram button
- `mark_published` is reachable from CLI and should be reused

## Decide
- add a single button "我已粘贴" with callback `PR:mark`
- in `_route`, branch on `PR:mark` to register pending edit with `gate="PR"`
- in `_handle_message`, parse the pasted URL and call `mark_published`

## Act
- edit `render.py`, `daemon.py`, and `triggers.py`
- ensure `_ACTION_REQ` lists `PR:mark` under the `publish` family

## Verify
- grep `PR:mark` under `backend/agentflow/agent_review/`
- smoke `render_publish_ready`

## Next
- bundle any latent callback-scope fix separately if found
```

## Default Open Claw Response Skeleton

Use this when no better format is required:

```markdown
## Discover
- current state
- task delta
- source of truth

## Decide
- chosen approach
- assumptions
- risks / non-goals

## Act
- change made or conclusion reached

## Verify
- tests / build / manual checks / verification limits

## Next
- next step or blocker
```
