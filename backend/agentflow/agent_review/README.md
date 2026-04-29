# Agent Review — Human-in-the-loop via Telegram

Review surface for the AgentFlow publishing pipeline. Wraps the existing
D1 → D2 → D3 → D4 chain in three opt-in gates (A topic, B draft, C image),
each rendered as a Telegram message with inline-keyboard callbacks.

Only Medium is in scope for v0.1, so Gate D (cross-channel) from the
`social-content-review-skill-proposal.md` design is skipped.

## Layout

```
agent_review/
├── README.md                    # this file
├── templates/
│   ├── gate_a_topic_review.md   # topic selection summary card
│   ├── gate_b_draft_review.md   # finished article summary card
│   ├── gate_c_image_review.md   # cover/inline image summary card
│   ├── state_machine.md         # gate state transitions + dependency rules
│   └── callback_data_schema.md  # how button taps encode their target
└── (impl modules — added in next iteration)
```

## Gates active for Medium

| Gate | Triggered after | Required for | Default mode |
|------|-----------------|--------------|--------------|
| A | `af hotspots` (cron or manual) | `af write` | 🟡 medium-risk → human approves |
| B | `af fill` completes | `af image-gate` | 📝 long-form → human approves (always) |
| C | `af image-gate --mode cover-only/cover-plus-body` finishes | `af preview` + `af medium-package` | 🟢 self-check, prompt only if image generation actually ran |

Each gate writes its outcome to `metadata.json.gate_history[]` so future re-runs
can replay or audit. Implementation deferred — these templates + the state
machine + callback schema lock the contract.
