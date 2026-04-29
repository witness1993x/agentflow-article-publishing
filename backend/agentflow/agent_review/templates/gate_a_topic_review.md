# Gate A — Topic Selection Review (Telegram)

**When fired:** after `af hotspots` produces a fresh batch of candidates and
auto-classifies them. Top-K (default 3) are pushed to the review chat.

**Risk class:** Medium (long-form Medium article workflow). Human must approve
before D2 skeleton/fill spends LLM budget.

**Timeout:** 24h. After timeout, the daemon auto-degrades the candidates to
"backlog" status and pings the user once. Pipeline does not advance.

---

## Message template (Markdown V2, with inline keyboard)

```text
🟡 *Gate A — Topic Review*  ·  hourly batch  ·  {timestamp_local}

profile: *{publisher_brand}*  · series: *{target_series}*  · candidates: *{candidate_count}*

━━━━━━━━━━━━━━━━━━━━━━━━

*1\\. {topic_1_title}*
↳ angle: _{topic_1_angle}_
↳ heat: {topic_1_score} · age: {topic_1_age_h}h · source: {topic_1_source}
↳ keywords: {topic_1_keywords}
↳ red flags: {topic_1_red_flags_or_dash}

*2\\. {topic_2_title}*
↳ angle: _{topic_2_angle}_
↳ heat: {topic_2_score} · age: {topic_2_age_h}h · source: {topic_2_source}
↳ keywords: {topic_2_keywords}
↳ red flags: {topic_2_red_flags_or_dash}

*3\\. {topic_3_title}*
↳ angle: _{topic_3_angle}_
↳ heat: {topic_3_score} · age: {topic_3_age_h}h · source: {topic_3_source}
↳ keywords: {topic_3_keywords}
↳ red flags: {topic_3_red_flags_or_dash}

━━━━━━━━━━━━━━━━━━━━━━━━
auto\\-degrade if no action in 24h
```

## Inline keyboard layout

```
[ ✅ 起稿 #1 ] [ ✅ 起稿 #2 ] [ ✅ 起稿 #3 ]
[ 🚫 全拒绝 ]  [ 📋 看详细 ]  [ ⏰ 推迟 4h ]
```

## callback_data values

| Button | callback_data |
|---|---|
| ✅ 起稿 #N | `A:write:{short_id}:angle=0` |
| 🚫 全拒绝 | `A:reject_all:{short_id}` |
| 📋 看详细 | `A:expand:{short_id}` |
| ⏰ 推迟 4h | `A:defer:{short_id}:hours=4` |

`short_id` is a 6-char ref into the bot's pending-batch table; resolves to
the hotspot batch path on the backend side. Each candidate inside the batch
has its own slot index (`#1` etc).

## Self-check checklist (Agent runs before posting, surfaces failures inline)

- [ ] Topic in keyword universe of the active publisher_account / topic_profile
- [ ] No duplicate within last 7 days (`grep` over publish_history.jsonl)
- [ ] Heat score within window — drops topics older than `heat_window_hours`
- [ ] No red-line keywords (regulatory / price prediction / unannounced parties)
- [ ] Topic doesn't require knowledge outside `publisher_account.product_facts`

If 3+ checks fail, candidate is flagged inline with `⚠` prefix on the title.

## Outcomes → backend

| User action | Backend effect |
|---|---|
| ✅ 起稿 #N | `af write <hotspot_id_for_slot_N> --angle 0` (server-side, with intent profile already set) |
| 🚫 全拒绝 | All candidates marked rejected in hotspot json, never re-suggested for 7d |
| 📋 看详细 | Bot replies with full hotspot details (suggested_angles, raw signals, refs) |
| ⏰ 推迟 4h | Re-post the same card in 4h |

After ✅, the daemon moves to **Gate B for that article_id** automatically once
`af fill` completes.
