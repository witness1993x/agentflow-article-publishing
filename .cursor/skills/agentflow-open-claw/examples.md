# AgentFlow Open Claw Examples

## Example 1: Planning a New Feature

User asks:

> 给这个项目加一个 memory summary 面板

Use the loop like this:

```markdown
## Discover
- current state: memory events are recorded in `~/.agentflow/memory/events.jsonl`
- delta: there is no memory summary or preference aggregation UI yet
- source of truth: `docs/PRD_OVERVIEW.md`, `docs/SOLUTION_OVERVIEW.md`, `backend/agentflow/shared/memory.py`

## Decide
- approach: add a summary/aggregation layer first, then a lightweight UI
- assumptions: append-only events are sufficient input for phase 1 summary
- risks: avoid writing cross-article memory into `metadata.json`

## Act
- propose API surface, aggregation model, and UI placement

## Verify
- verify proposal matches current repo boundaries and does not assume a database

## Next
- next step: define summary schema and read model
```

## Example 2: Implementing a Behavior Change

User asks:

> 把 preview 行为也影响后续默认平台选择

Use the loop like this:

```markdown
## Discover
- current state: preview events are logged, but no preference defaults are derived
- delta: add memory consumption, not just logging

## Decide
- approach: aggregate preview frequency outside `metadata.json`
- assumptions: preview intent is a valid weak signal for platform preference
- risks: preview may not equal publish intent, so weight it lower than publish

## Act
- implement aggregation in a separate memory-derived layer
- keep article metadata and memory artifacts separate

## Verify
- test that preview events still log correctly
- test that defaults read from aggregated memory, not per-article state

## Next
- blocker or follow-up: define precedence between preview and publish signals
```

## Example 3: Reviewing a Change

User asks:

> review 这次 write/publish 的改动

Use the loop like this:

```markdown
## Discover
- current state: auto-draft-first is the canonical path
- delta: inspect whether the change reintroduces skeleton-first assumptions or breaks status flow

## Decide
- focus review on:
  - auto-draft contract
  - state transition integrity
  - memory boundary integrity
  - frontend/backend contract consistency

## Act
- report findings ordered by severity

## Verify
- cite the changed code paths or tests reviewed

## Next
- identify fixes needed before merge, or state residual risk
```

## Example 4: Writing Handoff / PRD Docs

User asks:

> 更新交接文档，给 CC 用

Use the loop like this:

```markdown
## Discover
- current state: docs already exist for PRD, solution, one-page summary, and CC handoff
- delta: update docs to match the latest repo truth

## Decide
- approach: keep docs grounded in current workflow, not old MVP wording
- assumptions: auto-draft-first remains the current baseline

## Act
- update the smallest set of docs needed
- keep product summary, technical solution, and review handoff separated

## Verify
- check doc consistency across baseline, risks, and next steps

## Next
- state whether a shorter or review-specific version is still needed
```

## Example 5: When Not to Overbuild

User asks:

> 给这个项目补一个完整后台任务系统

Use the loop like this:

```markdown
## Discover
- current state: `run-once` is intentionally minimal and synchronous
- delta: a full async task system would expand the v0.1 boundary significantly

## Decide
- approach: do not jump straight to full infra unless the user explicitly wants that expansion
- assumptions: current workload still fits sync calls for mock and limited real usage
- risks: overbuilding beyond current repo stage

## Act
- propose phased options instead of silently implementing a large infra shift

## Verify
- show which current constraints make the larger change premature

## Next
- recommend smaller next step: task IDs, polling contract, then job runner
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
