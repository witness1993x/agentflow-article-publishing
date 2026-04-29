---
name: agentflow-newsletter
description: |
  Turn an article draft (or a scratch topic) into a newsletter email, test-send to yourself, and fan out to subscribers via Resend.

  TRIGGER: "/agentflow-newsletter", "af newsletter-*", "af notify", "newsletter", "email", "Resend", "邮件", "newsletter-correction".

  SKIP for: modifying the EmailPublisher internals (agent_d4/publishers/email.py); editing Resend audience configuration (in .env); modifying prompts/email_newsletter.md.
---

# agentflow-newsletter — ship the email version of a post

Wraps `af newsletter-draft`, `af newsletter-show`, `af newsletter-edit`, `af newsletter-preview-send`, `af newsletter-send`, `af newsletter-correction`, and `af newsletter-list-show`. Goal: deliver `<article_id>` (or a new scratch topic) as a newsletter, with one hard stop before the irreversible blast.

**Platform status (v0.1):**
- Provider: Resend (`https://api.resend.com/emails`). Requires `RESEND_API_KEY`, `NEWSLETTER_FROM_EMAIL`, and either `NEWSLETTER_AUDIENCE_ID` (recommended) or an explicit recipient list.
- Mock mode (`MOCK_LLM=true`) works end-to-end without any key — safe for dry runs.
- Rollback: **emails cannot be un-sent.** If the user says "undo", use `af newsletter-correction` to send a manual follow-up correction to the audience.

## Input

Expect either `<article_id>` from a previous `/agentflow-write` run, or a scratch title (`--from-scratch "..."`). If missing: "Give me an article id or a working title, or run `/agentflow-write <hotspot_id>` first."

## Setup

```bash
cd /Users/witness/Desktop/experimental/medium\&blog_posting_agent/agentflow-article-publishing/backend
source .venv/bin/activate
```

Prefix every command with `PYTHONPATH=.`.

## Step 1 — derive / create the draft

```bash
PYTHONPATH=. af newsletter-draft <article_id> --json
```

or scratch mode:

```bash
PYTHONPATH=. af newsletter-draft --from-scratch "this week in agent infra" --json
```

Capture `newsletter_id` from the JSON output. Tell the user: "drafted `<newsletter_id>` — subject `<S>` (`<N>` chars)."

## Step 2 — show the draft

```bash
PYTHONPATH=. af newsletter-show <newsletter_id>
```

Read `html_body` length, `plain_text_body` length, subject, preview_text. Print the first ~20 lines of plain text so the user can skim.

## Step 3 — edit loop (optional, repeatable)

Let the user direct edits by section. Sections are a fixed enum: `subject | preview_text | intro | body | closing`.

```bash
PYTHONPATH=. af newsletter-edit <newsletter_id> --section intro --command "make it warmer, drop the jargon"
```

Loop until the user says "good" or "looks fine" or equivalent.

## Step 4 — Step 1b: pre-send overview (hard stop)

Before test-send, do a readiness scan. Skip only if user says "just send".

Check:

1. **Subject**: `len(subject) <= 45` (English) / `<= 22` (Chinese). If over: flag as "subject may truncate in Gmail/Apple Mail inbox preview".
2. **Preview text**: `len(preview_text) <= 90`. Warn if empty — many clients will fall back to the first body sentence.
3. **Images**: `images_used` length. 0 is fine; > 2 violates the prompt spec — ask user to trim.
4. **Unsubscribe placeholder**: grep `html_body` and `plain_text_body` for `{unsubscribe_link}`. If missing from either, flag (the publisher will inject a mailto: fallback, but a real Audience unsub is better).
5. **Audience size**: read `NEWSLETTER_AUDIENCE_ID` from env. If empty, say "no audience configured — `newsletter-send` will only work in mock mode".
6. **Deliverability**: if `NEWSLETTER_FROM_EMAIL` has a domain that's NOT DKIM/SPF-verified in Resend (you can't check this from CLI; rely on user knowledge), warn: "If you haven't verified this domain in Resend, emails will land in spam. Set up DKIM/SPF before sending to real subscribers."

Present as:

```
Ready check for <newsletter_id>:
  Subject          : "<S>"  (<N> chars)         [ok | WARN: too long]
  Preview text     : "<P>"  (<N> chars)         [ok | WARN: too long | WARN: empty]
  Images           : <N> inline                  [ok | WARN: exceeds 2]
  Unsubscribe link : present / missing in <html|plain>
  Audience         : <NEWSLETTER_AUDIENCE_ID>     [configured | not set]
  From             : <NEWSLETTER_FROM_EMAIL>      [domain verified? (you tell me)]

Risks:
  - <list each WARN>
```

Then pause: "Proceed to test-send to yourself? (yes / edit first / cancel)"

## Step 5 — test-send to self

```bash
PYTHONPATH=. af newsletter-preview-send <newsletter_id> --to self --json
```

If `MOCK_LLM=true`, this returns a fake `resend_id` and no email goes out — say so explicitly.

Otherwise, tell the user: "Test email sent to `<NEWSLETTER_REPLY_TO>`. Open it in Gmail / Apple Mail / Outlook and confirm:
- subject renders (not truncated)
- preview text shows in the inbox list
- body renders (images, links, spacing)
- unsubscribe link works
- **reply-to** is correct
Reply 'looks good' to proceed to full send, or 'edit' to go back."

## Step 6 — hard confirm before sending

This is the irreversible step. Email, once sent, cannot be recalled. Require a literal confirmation string:

```
You are about to send '<subject>' to every subscriber in audience <NEWSLETTER_AUDIENCE_ID>.
This cannot be undone. To proceed, type: yes send
```

Only "yes send" (exact, lowercase) proceeds. Anything else (including "yes", "send", "ok", "confirm") cancels and says "cancelled — send aborted".

Optionally offer `--dry-run` first:

```bash
PYTHONPATH=. af newsletter-send <newsletter_id> --dry-run --json
```

## Step 7 — send for real

```bash
PYTHONPATH=. af newsletter-send <newsletter_id> --json
```

On success, parse `platform_post_id` (Resend id). Tell the user: "newsletter `<id>` sent — Resend id `<resend_id>`. Check the dashboard at https://resend.com/emails/<resend_id> for delivery stats."

On failure: show `failure_reason` and do NOT retry automatically. Ask the user to fix credentials / domain verification / audience id and re-run.

## Step 8 — archive + follow-up

- Memory: one `newsletter_sent` event is appended to `~/.agentflow/memory/events.jsonl`.
- Publish history: one line in `~/.agentflow/publish_history.jsonl` (platform `email_newsletter`).
- Newsletter status: `~/.agentflow/newsletters/<id>/metadata.json` flips to `"status": "sent"` with `last_sent_at` + `last_platform_post_id`.

Close with one line: "Newsletter `<newsletter_id>` archived. Use `af newsletter-list-show` anytime to see the full history."

## Correction flow — when the user says "undo" or "send a correction"

Reminder: this does **not** retract the original email. It sends a new follow-up correction to the configured audience.

1. Ask the user what needs correcting.
2. Apply any edits first with `af newsletter-edit <newsletter_id> ...` so the stored draft reflects the corrected copy.
3. Offer a dry run:

```bash
PYTHONPATH=. af newsletter-correction <newsletter_id> --dry-run --json
```

4. Require an explicit confirmation: `yes send`.
5. Then send:

```bash
PYTHONPATH=. af newsletter-correction <newsletter_id> --json
```

On success, report the new Resend id and remind the user that the first email remains in subscribers' inboxes.

## Notifications — `af notify`

Out of the main workflow but useful for skills: `af notify "<message>" [--event <type>]` sends a one-paragraph system email to `NEWSLETTER_REPLY_TO`. Use this to signal completion of long-running jobs (hotspots scan, publish retries, credential rotation). `MOCK_LLM=true` mocks silently.

## NEVER

- Never skip Step 4 (pre-send overview) unless the user explicitly says "just send".
- Never accept any confirmation other than the literal string `yes send` for Step 6.
- Never call `agentflow.agent_d4.publishers.email` directly — always via `af newsletter-*` or `af notify`.
- Never email a real audience from mock mode (the publisher short-circuits, so this is already impossible — don't work around it).

## MOCK_LLM

With `MOCK_LLM=true`, `newsletter-draft` returns the fixture at `backend/agentflow/shared/mocks/email_newsletter.json`, and every send (`newsletter-preview-send`, `newsletter-send`, `notify`) returns a fake `resend_id` without touching the network. Use this for skill rehearsal and CI.
