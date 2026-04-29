# Webhook Receiver Presets

agentflow's webhook publisher (`agentflow.agent_d4.publishers.webhook.WebhookPublisher`)
POSTs the finished article — markdown body, metadata, cover + inline images — to
whatever HTTP URL you set in `WEBHOOK_PUBLISH_URL`. Anyone can plug a custom CMS,
a Substack-style email relay, or a Hashnode bridge into agentflow without
touching backend code.

This folder contains a few minimal receiver examples you can copy and run.

---

## Wire format (JSON mode)

This is the default. Set `WEBHOOK_FORMAT=json` (or leave it unset).

```
POST $WEBHOOK_PUBLISH_URL
Authorization: <WEBHOOK_AUTH_HEADER>     # passed through verbatim
Content-Type: application/json
```

Body:

| Field | Type | Notes |
|---|---|---|
| `article_id` | `string` | Stable id agentflow assigns to the draft. Use it as your idempotency key. |
| `title` | `string` | Required. Plain text. |
| `subtitle` | `string \| null` | Optional one-liner. |
| `tags` | `string[]` | Already lowercased / deduped by upstream agents. |
| `canonical_url` | `string \| null` | Set when this is a cross-post; respect it for SEO. |
| `body_markdown` | `string` | The article. CommonMark + standard image embeds (`![alt](path)`). |
| `cover_image` | `object \| null` | See image block below. |
| `inline_images` | `object[]` | Every local image referenced by `body_markdown`, deduped, cover excluded. |
| `metadata` | `object` | Free-form: `publisher_brand`, `voice`, `agentflow_version`, plus any platform-specific keys. |

Image block:

```jsonc
{
  "filename": "cover.png",
  "content_type": "image/png",
  "local_path": "/abs/path/on/agentflow/host/cover.png",
  "data_base64": "iVBORw0KGgoAAAANS..."   // omitted when WEBHOOK_INCLUDE_IMAGE_BASE64=false
}
```

If the local file is missing the block degrades to
`{filename, content_type: null, missing_local_file: true, local_path}` — your
receiver should treat that as a soft warning, not a hard fail.

Verbatim sample payload:

```json
{
  "article_id": "art_2026_04_25_ai_writing_loops",
  "title": "Why AI Writing Loops Plateau at 80%",
  "subtitle": "And the editor pass that closes the gap",
  "tags": ["ai", "writing", "agents"],
  "canonical_url": null,
  "body_markdown": "# Why AI Writing Loops Plateau at 80%\n\nMost agent stacks ship a draft that *feels* finished...\n\n![diagram](/tmp/agentflow/art_2026_04_25/inline_1.png)\n\n## The 80% wall\n\nThe last 20% is voice...",
  "cover_image": {
    "filename": "cover.png",
    "content_type": "image/png",
    "local_path": "/tmp/agentflow/art_2026_04_25/cover.png",
    "data_base64": "iVBORw0KGgoAAAANS..."
  },
  "inline_images": [
    {
      "filename": "inline_1.png",
      "content_type": "image/png",
      "local_path": "/tmp/agentflow/art_2026_04_25/inline_1.png",
      "data_base64": "iVBORw0KGgoAAAANS..."
    }
  ],
  "metadata": {
    "platform": "webhook",
    "agentflow_version": "0.1",
    "published_via": "webhook",
    "publisher_brand": "agentflow",
    "voice": "punchy_engineer"
  }
}
```

---

## Wire format (multipart mode)

Set `WEBHOOK_FORMAT=multipart` when you want the receiver to stream large image
binaries without round-tripping through base64. The request becomes:

```
POST $WEBHOOK_PUBLISH_URL
Authorization: <WEBHOOK_AUTH_HEADER>
Content-Type: multipart/form-data; boundary=...
```

Parts:

| Part name | Content-Type | Notes |
|---|---|---|
| `meta` | `application/json` | The full JSON envelope **minus** `body_markdown`, `cover_image.data_base64`, `inline_images[].data_base64`. |
| `body` | `text/markdown` | Raw `body_markdown` as a UTF-8 file. |
| `cover` | `image/*` (per file) | Present iff a cover exists. Filename matches `meta.cover_image.filename`. |
| `inline_0`, `inline_1`, ... | `image/*` | One part per entry in `meta.inline_images`, in the same order. |

When to pick which:

- **JSON** — small images, simplest receivers, you want one self-contained blob to log/replay.
- **Multipart** — covers/inline images > a few MB, or you want to stream straight to S3 without buffering whole base64 strings in memory.

> Note: as of agentflow 0.1 the multipart mode lands in a parallel branch. If
> your installed publisher only emits JSON, ignore the multipart sections of
> these examples — the receivers still work, the multipart code paths just
> never fire.

---

## What the receiver MUST return

On success: HTTP 2xx with a JSON body containing **at minimum**:

```json
{ "published_url": "https://your.cms.example/posts/abc123", "id": "abc123" }
```

- `published_url` (or `url` / `link`) — required for agentflow to record the
  publish location. The publisher accepts any of these three keys.
- `id` (or `post_id`) — optional. Stored as the platform post id for later
  edits / retractions.

Anything else (non-2xx, malformed JSON, missing url) fails the publish.
agentflow auto-retries **once** via the D4 retry layer, then surfaces the
failure to the operator.

---

## Auth

`WEBHOOK_AUTH_HEADER` is passed through to the receiver verbatim. Three
shapes are supported:

| Format | Header sent |
|---|---|
| `Bearer abc123` | `Authorization: Bearer abc123` |
| `Basic dXNlcjpwYXNz` | `Authorization: Basic dXNlcjpwYXNz` |
| `X-API-Key:abc123` | `X-API-Key: abc123` |

Rule of thumb: if the value starts with `Bearer ` or `Basic ` it goes into
`Authorization`. Otherwise the publisher splits on the **first colon** and
sends `<key>: <value>`. Pick whichever your CMS expects; no transformation
happens on agentflow's side.

---

## Receivers in this folder

| File | One-liner |
|---|---|
| `generic-fastapi-receiver.py` | FastAPI receiver — accepts JSON or multipart, dumps to `/tmp/agentflow-inbox/`. Good first integration target. |
| `substack-relay-receiver.py` | Bridges agentflow into Substack via "post by email" (no public API exists). |
| `hashnode-graphql-receiver.js` | Node receiver that forwards to Hashnode's GraphQL `publishPost` mutation. |
