# prompt: email_newsletter (derive a newsletter email from a blog draft)

You turn a published (or drafted) blog post into a short newsletter email. The goal: a subscriber who opens this should learn the one thing the post is about, feel a human voice, and know whether to click through.

## Hard rules

1. Subject line **≤ 45 chars (English)** or **≤ 22 chars (Chinese)**. Must be a hook — not a summary, not "大家好".
2. The first sentence of the body must answer "why should this subscriber open this email *today*" — not a greeting.
3. Body has exactly 3 sections:
   - **Intro** (60–100 words): one-sentence editorial lead in your own voice.
   - **Body**: restate the core argument from the blog + one concrete example + link to the full post.
   - **Closing**: a clear call to action — forward, reply, or subscribe. Pick exactly one.
4. At most **2 inline images** (track them in `images_used`). None is fine.
5. The HTML **must** include the literal token `{unsubscribe_link}` as an href target — downstream code will substitute the real URL. Do **not** substitute or rename this token.
6. The plain-text body is mandatory — mirror the HTML structure without markup.
7. Do not invent URLs. If `published_urls_if_any` is empty, point to `{article_url}` and let the pipeline fill it in later. If there is no URL and no published link, say "(read the full post on the site — link coming in the next send)".

## Inputs

- `draft_markdown`: {draft_markdown}
- `published_urls_if_any`: {published_urls_if_any}
- `user_handle`: {user_handle}
- `from_scratch_title` (only when starting without a blog draft): {from_scratch_title}

## Output

Respond with **a single JSON object** (no markdown fences, no preamble):

```
{
  "subject": "...",                  // ≤ 45 chars
  "preview_text": "...",             // ≤ 90 chars; what the email client shows in the list view
  "html_body": "<!doctype html> ...",// full HTML, must contain {unsubscribe_link}
  "plain_text_body": "...",          // text/plain mirror; must contain {unsubscribe_link}
  "images_used": ["path/or/url", ...]// [] if none
}
```

Style reminders:

- Voice = the blog's voice. Be direct, concrete, personal. No marketing filler.
- Inline CSS only (many clients strip `<style>` blocks). Keep it simple; avoid background images.
- Every heading is `<h3>` at most — no `<h1>`/`<h2>` (clients render those as huge).
- Prefer `<a href="{article_url}">read the full post</a>` style links; keep external links to ≤ 3.
