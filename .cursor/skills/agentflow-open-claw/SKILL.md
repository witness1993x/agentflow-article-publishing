---
name: agentflow-open-claw
description: Understands the current AgentFlow project baseline and enforces a three-layer closed loop: discover current state, decide against project constraints, then act and verify before ending. Use when working in this repository, especially when the user says open claw or asks for planning, implementation, review, handoff, PRD/solution updates, auto-draft flow, memory layer, workflow queue, or preview/publish behavior.
---

# AgentFlow Open Claw

## Purpose

Use this skill to keep work in this repository aligned with the current product and architecture direction.

This skill is mandatory for tasks that touch:

- auto-draft flow
- memory layer
- workflow queue
- write / publish UX
- PRD / solution / handoff docs
- CC review preparation
- tasks the user refers to as `open claw`

## Project Baseline

Assume these are the current truths unless the repo proves otherwise:

- The default path is `hotspot -> auto full draft -> local human edits -> preview/publish`
- Do not treat `skeleton-first` as the main workflow
- The project is single-user, local-first, and mock-first
- Single-article state lives in `~/.agentflow/drafts/<article_id>/metadata.json`
- Cross-article behavior lives in `~/.agentflow/memory/events.jsonl`
- `run-once` is a minimal orchestrator, not a background job system
- Mock validation passing does not mean real-key readiness is complete

## Required Reading Order

Before substantial planning, implementation, or review, read in this order:

1. `docs/CC_ONE_PAGE_SUMMARY.md`
2. `CCREVIEW_HANDOFF.md`
3. `docs/SOLUTION_OVERVIEW.md`
4. `docs/PRD_OVERVIEW.md` when product scope or roadmap matters
5. `.cursor/skills/agentflow-open-claw/reference.md` when runtime artifacts, status boundaries, or API-key scope matter
6. `.cursor/skills/agentflow-open-claw/template.md` when a profile-aware execution template is needed

If the task is narrow, stop once you have enough context. Do not read more than needed.

## Three-Layer Decision Loop

Every non-trivial task must pass all three layers.

### Layer 1: Discover

Establish the current state before proposing changes.

Minimum checks:

- What is the user actually asking for?
- What is already implemented?
- What is the delta from the current baseline?
- Is the task about mock mode, real-key mode, or both?
- Which files or runtime artifacts are the source of truth?

Output a short discovery conclusion before moving on.

### Layer 2: Decide

Make an explicit decision against project constraints.

Check these questions:

- Does this preserve the auto-draft-first path?
- Does this keep single-article state separate from cross-article memory?
- Does this stay within current v0.1 boundaries unless the user asks otherwise?
- Is the task better solved in API, UI, docs, or skill/rule layer?
- What assumption is being made? If false, what changes?

Output a short decision conclusion with assumptions and risks.

### Layer 3: Act

Implement, review, or update documentation only after the first two layers are clear.

When acting:

- make the minimum coherent change
- verify with the best available check
- prefer repository truth over earlier conversation memory
- update docs when the system baseline changes

## Closed-Loop Finish Rule

A task is not complete unless all of the following are present:

1. `Discover` conclusion
2. `Decide` conclusion
3. `Act` result
4. `Verification` result
5. `Next step or blocker`

If any one is missing, the loop is still open.

For final responses, prefer this structure:

```markdown
## Discover
- current state
- task delta

## Decide
- chosen approach
- assumptions / risks

## Act
- what changed or what was concluded

## Verify
- tests, build, manual checks, or why verification was limited

## Next
- next step or blocker
```

## Guardrails

Never do these without explicit evidence or user direction:

- revert the project back to `skeleton-first` thinking
- mix memory events into per-article `metadata.json`
- present mock success as proof of production readiness
- assume `run-once` is a full orchestration engine
- introduce database / async infra / multi-user assumptions as if they already exist

## Task-Specific Guidance

### If implementing code

- Prefer preserving the current API-led orchestration model
- Re-check queue/state interactions when touching write, preview, publish, or retry flows
- Verify status transitions and memory-event side effects together

### If writing docs

- Ground the doc in the current baseline, not the old MVP flow
- State what is already done, what is missing, and what is still bounded by mock-first local-first constraints

### If reviewing

- Prioritize regressions in auto-draft flow, memory boundaries, state transitions, and contract consistency

## Reference Docs

- `docs/CC_ONE_PAGE_SUMMARY.md`
- `CCREVIEW_HANDOFF.md`
- `docs/SOLUTION_OVERVIEW.md`
- `docs/PRD_OVERVIEW.md`
- `.cursor/skills/agentflow-open-claw/reference.md`
- `.cursor/skills/agentflow-open-claw/examples.md`
- `.cursor/skills/agentflow-open-claw/template.md`
