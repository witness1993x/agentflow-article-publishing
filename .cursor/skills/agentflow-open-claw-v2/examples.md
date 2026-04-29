# AgentFlow Open Claw Examples (v2)

Three real scenarios drawn from the live review pipeline. Each follows the Discover → Decide → Act → Verify → Next loop.

## Example 1: extend `/list` to surface `ready_to_publish`

User asks:

> /list 现在不显已 dispatch 但等 medium-paste 的文章, 加上

```markdown
## Discover
- current `/list` (daemon.py) only scans Gate B / C / D pending states
- gap: STATE_READY_TO_PUBLISH (S6 dispatch finished, awaiting manual paste) is invisible
- source of truth: `backend/agentflow/agent_review/daemon.py` around line 497, plus `state.py`

## Decide
- include STATE_READY_TO_PUBLISH in the pending_states scan set
- add a filter argument so `/list ready` narrows the view
- do not invent a new state; the existing one already covers the case

## Act
- extend `list_filter` parsing in daemon.py:497
- add a `'ready'` key to the `states_by_filter` dict that maps to {STATE_READY_TO_PUBLISH}
- keep the default `/list` output inclusive of the new state

## Verify
- run `pytest tests/test_v02_workflows.py -q` and check the `/list` smoke case
- manually post `/list` and `/list ready` to the review chat against a seeded fixture

## Next
- follow-up: render the article's time-in-state by reading the last `gate_history` timestamp
```

## Example 2: fix red flag R2 — `A:reject_all` does not move state

User asks:

> daemon 的 A:reject_all 现在只 revoke sid + ack, 不动 state, 修一下

```markdown
## Discover
- daemon.py:821 currently does revoke + ack only on A:reject_all
- the batch unit at Gate A is the hotspot batch; there is no per-article `article_id` to flip
- `state.py` has no STATE_TOPIC_POOL_STALE constant — that name in older notes is a doc ghost

## Decide
- mark each hotspot in `~/.agentflow/hotspots/<date>.json` with `status="rejected_batch"`
- write a single audit row with `kind="batch_rejected"`
- do not invent a new STATE_*; the topic state stays in topic_pool until A:write picks one

## Act
- read `entry.batch_path`, load the batch JSON, and stamp each hotspot's status
- on IO failure, fall through to `_audit` warn + ack so the operator is not blocked
- keep revoke + ack ordering unchanged

## Verify
- `grep "rejected_batch" backend/agentflow/agent_review/daemon.py` shows the new write
- `pytest tests/test_v02_workflows.py -q` stays green

## Next
- next pass: align A 24h timeout (R3) with the same batch-rejected behavior
```

## Example 3: add `PR:mark` TG callback (S6 Q6)

User asks:

> S6 让 operator 在 TG 直接粘贴 medium URL, 不要 CLI

```markdown
## Discover
- `render_publish_ready` currently returns text only — sid is cosmetic, no buttons
- `mark_published` is reachable only via `af mark-published` CLI

## Decide
- change `render_publish_ready` to a 3-tuple (text, sid, kb)
- add a single button "📌 我已粘贴" with callback `PR:mark`
- in `daemon._route`, branch on `PR:mark` to register a pending edit with `gate="PR"` and a long ttl (~99999 min)
- in `_handle_message`, when the active gate is `PR`, parse the pasted URL and call `mark_published`

## Act
- four edits across `render.py`, `daemon.py`, and `triggers.py`
- ensure `_ACTION_REQ` lists `PR:mark` under the `publish` action family

## Verify
- `grep "PR:mark" backend/agentflow/agent_review/` shows the route + auth entries
- smoke `render_publish_ready` and confirm it returns a 3-tuple with the PR:mark callback wired

## Next
- bundle the latent fix for the `cb` out-of-scope reference in `B:edit` / `L:edit` into the same pass
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
