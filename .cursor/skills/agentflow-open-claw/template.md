# AgentFlow Open Claw Template

## Purpose

Use this template when a task needs to stay aligned with:

- the current AgentFlow repository baseline
- the user's stable preferences
- the current session goal
- the three-layer decision loop
- the closed-loop finish rule

Do not skip the `User Profile` section for non-trivial work. If profile data is missing, say so explicitly and proceed with cautious defaults.

## Input Layers

Treat inputs in this order:

1. `User Profile`
2. `Session Intent`
3. `Task Constraints`
4. `Current Project State`
5. `Requested Delta`

This ordering matters:

- profile tells you how to work
- session intent tells you why this task matters now
- task constraints tell you what is allowed
- project state tells you what is already true
- delta tells you what must change

## User Profile

Capture only the profile fields that materially affect the task.

```markdown
User Profile
- automation_preference:
- intervention_preference:
- decision_style:
- risk_tolerance:
- review_depth:
- output_style:
- execution_bias:
```

Field guidance:

- `automation_preference`: how strongly the user prefers automation over manual control
- `intervention_preference`: where the user wants human intervention to happen
- `decision_style`: whether the user prefers fast convergence, option comparison, or staged checkpoints
- `risk_tolerance`: low / medium / high appetite for risky changes
- `review_depth`: lightweight / normal / deep
- `output_style`: concise / structured / detailed
- `execution_bias`: docs-first / code-first / review-first / shipping-first

If unknown:

- mark as `unknown`
- do not invent a profile
- explain when the missing profile meaningfully affects the decision

## Session Intent

```markdown
Session Intent
- current goal:
- success condition for this session:
- why now:
```

Examples:

- current goal: prepare CC handoff
- success condition for this session: produce stable handoff docs
- why now: reviewer context needs to be frozen before the next implementation wave

## Task Constraints

```markdown
Task Constraints
- explicit user constraints:
- repo constraints:
- non-goals:
- allowed verification level:
```

Examples:

- explicit user constraints: do not edit plan file; do not touch API keys
- repo constraints: preserve auto-draft-first; keep memory separate from metadata
- non-goals: no database; no async job system unless explicitly requested
- allowed verification level: docs-only / targeted tests / build / full mock path

## Open Claw Working Template

Use this as the default response skeleton:

```markdown
## Discover
- user profile:
- session intent:
- task constraints:
- current project state:
- requested delta:
- source of truth:

## Decide
- chosen approach:
- why this fits the user profile:
- why this fits the project baseline:
- assumptions:
- risks / non-goals:

## Act
- changes made or conclusion reached:
- what was intentionally personalized for this user:

## Verify
- verification performed:
- verification not performed:
- profile alignment check:

## Next
- next step or blocker:
```

## Layer-Specific Questions

### Layer 1: Discover

Ask:

- what does the user want done?
- what does the user care about in how it gets done?
- what is already true in the repo?
- what is not yet true?
- what runtime artifact or document is the source of truth?

### Layer 2: Decide

Ask:

- which approach best matches this user's profile?
- which approach best preserves the repo baseline?
- what tradeoff is being accepted?
- what assumption could invalidate this approach?

### Layer 3: Act

Ask:

- what is the minimum coherent change?
- what must be updated together?
- what verification is proportionate to the risk?
- what remains open after this action?

## Profile-Aware Defaults

If no explicit profile is given, use these cautious defaults:

```markdown
Default Working Assumptions
- automation_preference: medium-high
- intervention_preference: local review over early manual gating
- decision_style: staged and explicit
- risk_tolerance: medium-low
- review_depth: normal
- output_style: concise but structured
- execution_bias: preserve current architecture before expanding scope
```

These defaults reflect the current repo direction and should be overridden when the user signals otherwise.

## Example Mini-Use

```markdown
## Discover
- user profile: prefers automation first, then local intervention
- session intent: extend the project skill
- task constraints: do not touch API keys
- current project state: skill already has rules and reference layers
- requested delta: add profile-aware templates
- source of truth: existing skill files in `.cursor/skills/agentflow-open-claw/`

## Decide
- chosen approach: add a dedicated template file instead of bloating `SKILL.md`
- why this fits the user profile: supports structured but reusable execution
- why this fits the project baseline: preserves progressive disclosure
- assumptions: this skill is meant for repeated repo work, not one-off tasks
- risks / non-goals: not adding runtime memory consumption yet

## Act
- added `template.md`
- linked it from `SKILL.md`

## Verify
- verification performed: readback and lint check
- verification not performed: no runtime execution because docs-only change
- profile alignment check: template now explicitly captures user profile before decisions

## Next
- next step or blocker: add a checklist layer if repeated execution needs a shorter operator form
```
