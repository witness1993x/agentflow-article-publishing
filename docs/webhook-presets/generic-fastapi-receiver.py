"""Generic FastAPI receiver for the agentflow webhook publisher.

Purpose
    Minimal reference implementation that accepts BOTH JSON and multipart
    payloads from agentflow's webhook publisher and writes them to a local
    inbox directory. Use it as a template for any custom CMS integration.

How to run
    pip install fastapi 'uvicorn[standard]' python-multipart
    export WEBHOOK_RECEIVER_TOKEN=devsecret
    uvicorn generic-fastapi-receiver:app --port 9000

How to test
    curl -X POST http://127.0.0.1:9000/publish \\
         -H "Authorization: Bearer devsecret" \\
         -H "Content-Type: application/json" \\
         -d '{"article_id":"t1","title":"hi","body_markdown":"# hi","tags":[],"inline_images":[],"cover_image":null,"metadata":{}}'

What it does NOT do
    - Render the markdown
    - Push to any real CMS
    - Verify image content-types beyond what the client claims
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

INBOX = Path("/tmp/agentflow-inbox")
EXPECTED_TOKEN = os.environ.get("WEBHOOK_RECEIVER_TOKEN", "devsecret")

app = FastAPI(title="agentflow webhook receiver (generic)")


def _check_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != EXPECTED_TOKEN:
        raise HTTPException(status_code=403, detail="bad token")


def _save_image_block(article_dir: Path, block: dict[str, Any], fallback_name: str) -> None:
    if not block:
        return
    name = block.get("filename") or fallback_name
    target = article_dir / name
    if "data_base64" in block and block["data_base64"]:
        target.write_bytes(base64.b64decode(block["data_base64"]))
    elif block.get("local_path") and Path(block["local_path"]).exists():
        target.write_bytes(Path(block["local_path"]).read_bytes())


def _persist_json_payload(payload: dict[str, Any]) -> dict[str, str]:
    article_id = payload.get("article_id") or "unknown"
    article_dir = INBOX / article_id
    article_dir.mkdir(parents=True, exist_ok=True)
    (article_dir / "body.md").write_text(payload.get("body_markdown") or "", encoding="utf-8")
    meta = {k: v for k, v in payload.items() if k not in {"body_markdown", "cover_image", "inline_images"}}
    (article_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if payload.get("cover_image"):
        _save_image_block(article_dir, payload["cover_image"], "cover")
    for i, blk in enumerate(payload.get("inline_images") or []):
        _save_image_block(article_dir, blk, f"inline_{i}")
    return {"published_url": f"https://your.example/{article_id}", "id": article_id}


async def _persist_multipart(request: Request) -> dict[str, str]:
    form = await request.form()
    meta_raw = form.get("meta")
    if meta_raw is None:
        raise HTTPException(status_code=400, detail="multipart missing 'meta' part")
    meta = json.loads(meta_raw if isinstance(meta_raw, str) else await meta_raw.read())
    article_id = meta.get("article_id") or "unknown"
    article_dir = INBOX / article_id
    article_dir.mkdir(parents=True, exist_ok=True)
    (article_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    body_part = form.get("body")
    if body_part is not None and not isinstance(body_part, str):
        (article_dir / "body.md").write_bytes(await body_part.read())
    elif isinstance(body_part, str):
        (article_dir / "body.md").write_text(body_part, encoding="utf-8")
    for key, value in form.multi_items():
        if key in {"meta", "body"} or isinstance(value, str):
            continue
        filename = getattr(value, "filename", None) or key
        (article_dir / filename).write_bytes(await value.read())
    return {"published_url": f"https://your.example/{article_id}", "id": article_id}


@app.post("/publish")
async def publish(request: Request, authorization: str | None = Header(default=None)) -> dict[str, str]:
    _check_auth(authorization)
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("application/json"):
        payload = await request.json()
        return _persist_json_payload(payload)
    if ctype.startswith("multipart/form-data"):
        return await _persist_multipart(request)
    raise HTTPException(status_code=415, detail=f"unsupported content-type: {ctype}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9000)
