---
name: agentflow-tweet
description: |
  Generate, review, publish a Twitter single or thread from a hotspot or article.

  TRIGGER: "/agentflow-tweet", "af tweet-*", "twitter", "thread", "推文", "X 平台".

  SKIP for: editing Twitter API credentials (.env); modifying the D3 twitter adapter internals.
---

# agentflow-tweet — from idea to a posted thread

Wraps `af tweet-draft`, `tweet-show`, `tweet-edit`, `tweet-publish`, `tweet-rollback`, `tweet-list`. Goal: take either a `<hotspot_id>` or an `<article_id>` and ship a single tweet or a thread, with an explicit review gate before publish. This is a separate flow from long-form `af publish`.

## Input

Expect either `<hotspot_id>` or `<article_id>` from the user invocation. If missing: "Give me a hotspot id (or an article id) first."

## Setup

```bash
cd /Users/witness/Desktop/experimental/medium\&blog_posting_agent/agentflow-article-publishing/backend
source .venv/bin/activate
```

Prefix `af` with `PYTHONPATH=.`.

## Step 0 — detect form

Read the user's intent from their message:

- "single tweet / 一条推 / 发一条" → `single`
- "thread / 串 / 长推 / 多条 / 拆成几条" → `thread` (default)
- "围绕 X 写一条" → `single`
- If unclear, ask: "single tweet or thread?"

## Step 1 — draft

```bash
PYTHONPATH=. af tweet-draft <source_id> --form <form> [--from-article] [--angle N] --json
```

- From a hotspot: `af tweet-draft <hotspot_id> --form thread --json`
- From an article: `af tweet-draft <article_id> --form thread --from-article --json`

Parse the returned `tweet_id`; keep it for every later step.

## Step 2 — review (Step 1b Twitter edition)

```bash
PYTHONPATH=. af tweet-show <tweet_id> --json
```

Summarise for the user:

- **Lineage**: source_type / source_id / angle_index
- **Form**: single | thread (and tweet count)
- **Chars per tweet**: flag any that exceed 275 or drop below 180
- **Intended hook**: one-liner from the draft
- **Refs used**: `source_refs` array → look each up in the hotspot's `source_references[]` and show `[source author] url + first 90 chars of text_snippet`
- **Hallucination flag**: if tweet 0 (hook) says something that isn't in any ref, call it out — do not hide
- **Images**: for each tweet with `image_slot`, surface the `image_hint`; warn if > 4 per tweet (Twitter limit)

End with:

> "Looks good? (yes / edit / regen)"

## Step 3 — edit loop

Map user commands:

- `改短 tweet N` → `af tweet-edit <tid> --index N --command "改短"`
- `去AI味 tweet 0` → same pattern
- `把第 2 条拆成两条` → `af tweet-edit <tid> --split 1`
- `合并第 2 和第 3 条` → `af tweet-edit <tid> --merge 1,2`
- `重排 0,3,1,2,4` → `af tweet-edit <tid> --reorder 0,3,1,2,4`
- `regen` → re-run Step 1 (creates a new tweet_id; old one stays on disk)

After each edit: re-run Step 2 condensed — show only the changed tweet(s) with old/new char counts.

## Step 4 — pre-publish hard confirm

Before publishing, print:

```
ABOUT TO PUBLISH:
  form      : thread (5 tweets)
  total     : 493 chars
  images    : 1 (cover on tweet 0)
  platform  : twitter_thread → @<handle>
  rate-limit: ~8s expected (1.5s between tweets)
Refs alignment: OK  |  ⚠ tweet 2 claim not in refs
```

Then ask literally: `"type 'yes send' to publish, or 'cancel'"`. Anything else is cancel.

## Step 5 — dry-run or publish

If user wants an extra safety net:

```bash
PYTHONPATH=. af tweet-publish <tweet_id> --dry-run --json
```

Real publish:

```bash
PYTHONPATH=. af tweet-publish <tweet_id> --json
```

Parse `status`:

- `success` → show `published_url`
- `partial_success` → show how many posted + the `failure_reason`. Offer: "retry from tweet N" (run `af tweet-publish` again is not safe for partial; surface the posted ids and let user decide next step)
- `failed` → show reason; common: `missing TWITTER_CONSUMER_KEY` (go to `.env`), `tweepy not installed` (run `pip install tweepy`)

## Step 6 — report

```
Thread tw_20260424_... posted:
  first tweet: https://twitter.com/<handle>/status/<id>
  thread_tweet_ids: [id1, id2, id3, id4, id5]
  posted at: 2026-04-24T...
```

## Rollback — taking the thread back down

Trigger on: "delete / undo / rollback / 删掉 / 撤下".

```bash
PYTHONPATH=. af tweet-rollback <tweet_id> --json
```

Always confirm first: `"This deletes N tweets from your timeline. Proceed? (yes / cancel)"`.

Twitter's delete API is best-effort — very old tweets or account-transferred tweets may refuse deletion. On partial failure, show which ids still exist.

## State side-effects

- `~/.agentflow/tweets/<tweet_id>/` — metadata + tweets.json + tweets.md + images/
- `~/.agentflow/publish_history.jsonl` — one row per publish or rollback
- `~/.agentflow/memory/events.jsonl` — `tweet_draft_created` / `tweet_edited` / `tweet_published` / `tweet_rolled_back`

## NEVER

- Never publish without the "type 'yes send'" hard confirm
- Never silently drop tweets that exceed 280 chars — surface and ask
- Never auto-retry a failed publish; Twitter rate-limits aggressively and duplicate thread = spam ban risk
- Never add `🧵` or `1/N` prefixes — Twitter natively numbers threads

## MOCK_LLM

`MOCK_LLM=true` short-circuits both LLM drafting (fixture: `shared/mocks/twitter-draft.json`) and tweepy calls (fake URLs `https://twitter.com/<handle>/status/mock_...`). Perfect for end-to-end smoke without spending API quota or polluting your timeline.

## Credentials

`.env` needs all four for real publishing:

```
TWITTER_CONSUMER_KEY
TWITTER_CONSUMER_SECRET
TWITTER_USER_ACCESS_TOKEN
TWITTER_USER_ACCESS_SECRET
TWITTER_HANDLE       # optional, only for prettier URLs
```

The read-only `TWITTER_BEARER_TOKEN` is not enough. Generate the user-context tokens from your Twitter Developer app's "Keys and tokens" tab.
