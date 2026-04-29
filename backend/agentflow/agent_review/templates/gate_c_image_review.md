# Gate C — Image (Cover) Review (Telegram)

**When fired:** after `af image-gate` completes a generation pass. Only fires
when generation actually ran (`mode=cover-only` or `cover-plus-body`).
For `mode=none`, Gate C is skipped entirely and the daemon advances directly
to publish-ready.

**Risk class:** v1.0 self-check only (per the social-content-review proposal,
Gate C is occupying its slot but doesn't block on human unless self-check
finds something concrete). When self-check is clean, the daemon may still
post a "preview" card depending on `preferences.image_generation.review_mode`:

- `auto`     — self-check only, advance silently if clean
- `confirm`  — always show the cover and wait for ✅
- `strict`   — always show + require explicit ✅ (no defer/skip)

**Timeout:** 6h (covers are cheap to regenerate; don't let one hold the
pipeline overnight). After timeout: pings reminder, holds for another 6h, then
auto-rejects the cover and falls back to "no cover" mode.

---

## Message template

The bot sends a **photo message** (the rendered cover, with logo overlay
already applied) with a caption — Telegram caption limit is 1024 chars,
plenty for the structured summary.

### Photo caption

```text
🖼 *Gate C — Cover Review*  ·  {article_short_id}  ·  {timestamp_local}

article: *{title_truncated_to_60}*
mode: *{image_mode}*  · style: *{cover_style}*  · size: *{cover_size}*

━━━━━━━━━━━━━━━━━━━━━━━━
*Self\\-check*

{checklist_pass_or_fail_lines}

━━━━━━━━━━━━━━━━━━━━━━━━
brand overlay: *{brand_overlay_status}* @ {anchor}
inline body images: *{inline_body_count}* (cover\\-only mode → 0)
```

### Self-check lines

```
✓ 16:9 aspect ratio
✓ 2k resolution
✓ no readable text (OCR)
✓ no detected face / human likeness
✓ brand wordmark present (logo overlay applied)
✗ wordmark contrast — dark navy bottom-left zone is not dark enough; logo blends in
```

## Inline keyboard layout

```
[ ✅ 用这张 ]      [ 🔁 再生成一张 ]
[ 🎨 换 logo 位置 ] [ 🚫 不用图 ]
[ 🖼 看完整尺寸 ]   [ ⏰ 推迟 2h ]
```

## callback_data values

| Button | callback_data |
|---|---|
| ✅ 用这张 | `C:approve:{short_id}` |
| 🔁 再生成一张 | `C:regen:{short_id}:cover` |
| 🎨 换 logo 位置 | `C:relogo:{short_id}` (multi-turn: pick from 7 anchors) |
| 🚫 不用图 | `C:skip:{short_id}` (pipeline continues with `--skip-images` semantics) |
| 🖼 看完整尺寸 | `C:full:{short_id}` (bot sends as document so it's not downsampled) |
| ⏰ 推迟 2h | `C:defer:{short_id}:hours=2` |

## Self-check rules (v1.0)

These are mandatory; failures auto-escalate to human review even if
review_mode=auto.

| Rule | Action on hit |
|---|---|
| File missing or unreadable | escalate, surface error |
| Image dims don't match cover_size | escalate, "regenerate" suggestion |
| OCR finds readable English text >= 4 chars | escalate, possible model-leaked text |
| Face detection finds a human likeness | escalate, possible person identity issue |
| Brand overlay enabled but `brand_overlay_applied=false` in metadata | escalate, overlay step failed |

Optional (warning only, doesn't block):
- Mean luminance in expected logo zone is too high (logo would blend) — recommend re-anchor
- Image is mostly cool palette but article uses warm tone words — possible mismatch
- File size > 4MB (Telegram limit, may need compression for the chat)

## Outcomes → backend

| User action | Backend effect |
|---|---|
| ✅ 用这张 | gate_history append; daemon advances to publish-ready |
| 🔁 再生成一张 | `af image-cover-add --reset && af image-generate --style cover --skip-body`; new card |
| 🎨 换 logo 位置 | bot prompts anchor pick; `brand_overlay.apply_overlay` re-run with new anchor; new card |
| 🚫 不用图 | metadata cleared of cover; pipeline continues with no feature image |
| 🖼 看完整尺寸 | bot replies with original 2k PNG as document |
| ⏰ 推迟 2h | re-post in 2h |
