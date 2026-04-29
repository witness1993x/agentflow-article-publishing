/**
 * Hashnode GraphQL receiver for the agentflow webhook publisher.
 *
 * Purpose
 *     Pure Node.js (>= 18) HTTP receiver that accepts agentflow's webhook
 *     JSON payload and forwards it to Hashnode's public GraphQL API
 *     (https://gql.hashnode.com) via the `publishPost` mutation.
 *
 * How to run
 *     export RECEIVER_TOKEN=devsecret
 *     export HASHNODE_API_TOKEN=hn_...
 *     export HASHNODE_PUBLICATION_ID=000000000000000000000000
 *     # Optional: lets the receiver upload the cover image to imgbb so
 *     # Hashnode can attach it (Hashnode requires a publicly hostable URL).
 *     export IMGBB_API_KEY=...
 *     node hashnode-graphql-receiver.js
 *
 *     # Then in agentflow .env:
 *     #   WEBHOOK_PUBLISH_URL=http://127.0.0.1:9100/publish
 *     #   WEBHOOK_AUTH_HEADER=Bearer devsecret
 *     #   WEBHOOK_FORMAT=json
 *
 * Required env
 *     RECEIVER_TOKEN          - shared secret; must match the Bearer value
 *                               agentflow sends in WEBHOOK_AUTH_HEADER.
 *     HASHNODE_API_TOKEN      - Personal Access Token from
 *                               https://hashnode.com/settings/developer
 *                               (sent verbatim, no "Bearer " prefix).
 *     HASHNODE_PUBLICATION_ID - 24-char hex publication id.
 *     IMGBB_API_KEY           - Optional; enables cover-image upload
 *                               fallback via https://api.imgbb.com.
 *
 * What it does NOT support
 *     - multipart/form-data payloads (JSON only — agentflow's default).
 *     - Inline body images. The article markdown is forwarded as-is, so any
 *       `![](path)` references that point at the agentflow host's local
 *       filesystem will appear as broken links on Hashnode. Either pre-host
 *       images, or add a separate uploader before this receiver.
 *
 * Known limitation: cover image
 *     Hashnode's `coverImageOptions.coverImageURL` requires a publicly
 *     hostable URL. agentflow's webhook payload sends inline base64
 *     (`cover_image.data_base64`). When IMGBB_API_KEY is set this receiver
 *     uploads the cover to imgbb and uses the returned URL. When not set,
 *     a warning is logged and the post is published WITHOUT a cover.
 */

'use strict';

const http = require('http');

const HASHNODE_GQL_ENDPOINT = 'https://gql.hashnode.com';
const PORT = parseInt(process.env.PORT || '9100', 10);

const RECEIVER_TOKEN = process.env.RECEIVER_TOKEN;
const HASHNODE_API_TOKEN = process.env.HASHNODE_API_TOKEN;
const HASHNODE_PUBLICATION_ID = process.env.HASHNODE_PUBLICATION_ID;
const IMGBB_API_KEY = process.env.IMGBB_API_KEY || '';

const PUBLISH_POST_MUTATION = `
  mutation PublishPost($input: PublishPostInput!) {
    publishPost(input: $input) {
      post { id slug url }
    }
  }
`;


function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on('data', (c) => {
      total += c.length;
      // 25 MB hard cap — base64 covers can be a few MB but anything larger
      // is almost certainly a misuse.
      if (total > 25 * 1024 * 1024) {
        reject(new Error('payload too large'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}


function checkAuth(req) {
  const auth = req.headers['authorization'] || '';
  if (!auth.startsWith('Bearer ')) {
    return { ok: false, status: 401, detail: 'missing bearer token' };
  }
  if (auth.slice('Bearer '.length).trim() !== RECEIVER_TOKEN) {
    return { ok: false, status: 403, detail: 'bad token' };
  }
  return { ok: true };
}


async function uploadCoverToImgbb(coverBlock) {
  if (!IMGBB_API_KEY) {
    console.warn('[hashnode] cover image present but IMGBB_API_KEY not set — skipping cover');
    return null;
  }
  const dataB64 = coverBlock && coverBlock.data_base64;
  if (!dataB64) {
    console.warn('[hashnode] cover image has no data_base64 — skipping cover');
    return null;
  }
  const form = new URLSearchParams();
  form.append('key', IMGBB_API_KEY);
  form.append('image', dataB64);
  if (coverBlock.filename) form.append('name', coverBlock.filename);
  const resp = await fetch('https://api.imgbb.com/1/upload', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: form.toString(),
  });
  if (!resp.ok) {
    console.warn(`[hashnode] imgbb upload failed: HTTP ${resp.status} — skipping cover`);
    return null;
  }
  const body = await resp.json();
  const url = body && body.data && (body.data.url || body.data.display_url);
  if (!url) {
    console.warn('[hashnode] imgbb response missing data.url — skipping cover');
    return null;
  }
  return url;
}


function normalizeTags(rawTags) {
  if (!Array.isArray(rawTags)) return [];
  return rawTags
    .filter((t) => typeof t === 'string' && t.trim())
    .slice(0, 5)
    .map((t) => {
      const name = t.trim();
      const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
      return { slug: slug || 'misc', name };
    });
}


async function buildPublishInput(payload) {
  const input = {
    title: payload.title || '(untitled)',
    contentMarkdown: payload.body_markdown || '',
    publicationId: HASHNODE_PUBLICATION_ID,
    tags: normalizeTags(payload.tags),
  };
  if (payload.subtitle) input.subtitle = payload.subtitle;
  if (payload.slug) input.slug = payload.slug;

  const cover = payload.cover_image;
  if (cover && cover.data_base64) {
    const coverUrl = await uploadCoverToImgbb(cover);
    if (coverUrl) input.coverImageOptions = { coverImageURL: coverUrl };
  }
  return input;
}


async function callHashnode(input) {
  const resp = await fetch(HASHNODE_GQL_ENDPOINT, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      // Hashnode convention: raw token, no "Bearer " prefix.
      'Authorization': HASHNODE_API_TOKEN,
    },
    body: JSON.stringify({ query: PUBLISH_POST_MUTATION, variables: { input } }),
  });
  const text = await resp.text();
  let body;
  try { body = JSON.parse(text); } catch { body = { raw: text }; }
  if (!resp.ok || (body && body.errors)) {
    const msg = (body && body.errors && body.errors[0] && body.errors[0].message) || `HTTP ${resp.status}`;
    const err = new Error(`hashnode publishPost failed: ${msg}`);
    err.status = 502;
    err.detail = body;
    throw err;
  }
  const post = body && body.data && body.data.publishPost && body.data.publishPost.post;
  if (!post || !post.url) {
    const err = new Error('hashnode publishPost returned no post.url');
    err.status = 502;
    err.detail = body;
    throw err;
  }
  return post;
}


function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}


async function handlePublish(req, res) {
  const auth = checkAuth(req);
  if (!auth.ok) return sendJson(res, auth.status, { detail: auth.detail });

  const ctype = (req.headers['content-type'] || '').toLowerCase();
  if (!ctype.startsWith('application/json')) {
    return sendJson(res, 415, {
      detail: `unsupported content-type: ${ctype} (multipart not supported by this receiver)`,
    });
  }

  let payload;
  try {
    payload = await readJsonBody(req);
  } catch (err) {
    return sendJson(res, 400, { detail: `invalid JSON: ${err.message}` });
  }

  try {
    const input = await buildPublishInput(payload);
    const post = await callHashnode(input);
    return sendJson(res, 200, { published_url: post.url, id: post.id });
  } catch (err) {
    const status = err.status || 500;
    console.error(`[hashnode] publish failed: ${err.message}`);
    return sendJson(res, status, { detail: err.message });
  }
}


const server = http.createServer((req, res) => {
  if (req.method === 'POST' && req.url === '/publish') {
    handlePublish(req, res).catch((err) => {
      console.error('[hashnode] unexpected error:', err);
      sendJson(res, 500, { detail: `internal: ${err.message}` });
    });
    return;
  }
  sendJson(res, 404, { detail: 'not found' });
});


function preflight() {
  const missing = [];
  if (!RECEIVER_TOKEN) missing.push('RECEIVER_TOKEN');
  if (!HASHNODE_API_TOKEN) missing.push('HASHNODE_API_TOKEN');
  if (!HASHNODE_PUBLICATION_ID) missing.push('HASHNODE_PUBLICATION_ID');
  if (missing.length) {
    console.error(`[hashnode] missing required env: ${missing.join(', ')}`);
    process.exit(2);
  }
}


if (require.main === module) {
  preflight();
  server.listen(PORT, '127.0.0.1', () => {
    console.log(`[hashnode] receiver listening on http://127.0.0.1:${PORT}/publish`);
  });
}

module.exports = { server };
