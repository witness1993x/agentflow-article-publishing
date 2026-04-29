"""Substack relay receiver — bridges agentflow into Substack via email.

Why an SMTP relay
    Substack has no public publishing API. Every Substack newsletter exposes a
    private "post by email" address (Settings -> Publish -> Post by email).
    Anything sent to that address from an authorized author becomes a draft
    post. This receiver accepts agentflow's webhook JSON and forwards it as
    an HTML email to that address.

How to run
    pip install fastapi 'uvicorn[standard]'   # markdown is optional, see below
    export WEBHOOK_RECEIVER_TOKEN=devsecret
    export SUBSTACK_POSTING_EMAIL=post-abc123@substack.com
    export SMTP_HOST=smtp.gmail.com
    export SMTP_PORT=587
    export SMTP_USER=you@example.com
    export SMTP_PASS=an-app-password
    uvicorn substack-relay-receiver:app --port 9001

What it does NOT do
    - Multipart payloads (use the JSON mode of the webhook publisher)
    - Inline image embedding beyond the cover (Substack's email pipeline
      strips most extras anyway)
    - Schedule / publish — drafts only
"""

from __future__ import annotations

import base64
import os
import smtplib
import uuid
from email.message import EmailMessage
from typing import Any

from fastapi import FastAPI, Header, HTTPException

EXPECTED_TOKEN = os.environ.get("WEBHOOK_RECEIVER_TOKEN", "devsecret")
SUBSTACK_POSTING_EMAIL = os.environ.get("SUBSTACK_POSTING_EMAIL", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

app = FastAPI(title="agentflow -> Substack email relay")


def _check_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != EXPECTED_TOKEN:
        raise HTTPException(status_code=403, detail="bad token")


def _md_to_html(md: str) -> str:
    """Best-effort markdown -> HTML. Uses `markdown` lib if installed."""
    try:
        import markdown  # type: ignore
        return markdown.markdown(md, extensions=["extra"])
    except Exception:
        # Fallback: wrap blank-line-separated paragraphs in <p>.
        chunks = [c.strip() for c in (md or "").split("\n\n") if c.strip()]
        return "\n".join(f"<p>{c}</p>" for c in chunks)


def _decode_cover(payload: dict[str, Any]) -> tuple[bytes, str, str] | None:
    cover = payload.get("cover_image") or {}
    if not cover or not cover.get("data_base64"):
        return None
    return (
        base64.b64decode(cover["data_base64"]),
        cover.get("content_type") or "image/png",
        cover.get("filename") or "cover.png",
    )


def _build_email(payload: dict[str, Any]) -> EmailMessage:
    if not SUBSTACK_POSTING_EMAIL:
        raise HTTPException(status_code=500, detail="SUBSTACK_POSTING_EMAIL not configured")
    msg = EmailMessage()
    msg["Subject"] = payload.get("title") or "(untitled)"
    msg["From"] = SMTP_USER or "agentflow@localhost"
    msg["To"] = SUBSTACK_POSTING_EMAIL

    body_html = _md_to_html(payload.get("body_markdown") or "")
    cover = _decode_cover(payload)
    if cover:
        cid = uuid.uuid4().hex
        body_html = f'<p><img src="cid:{cid}" alt="cover"/></p>\n{body_html}'
    msg.set_content(payload.get("body_markdown") or "")
    msg.add_alternative(body_html, subtype="html")
    if cover:
        data, ctype, name = cover
        maintype, _, subtype = ctype.partition("/")
        # Attach inline so the cid: reference resolves.
        msg.get_payload()[1].add_related(
            data, maintype=maintype or "image", subtype=subtype or "png",
            cid=f"<{cid}>", filename=name,
        )
    return msg


def _send(msg: EmailMessage) -> None:
    if not SMTP_HOST:
        raise HTTPException(status_code=500, detail="SMTP_HOST not configured")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        if SMTP_USER and SMTP_PASS:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


@app.post("/publish")
async def publish(payload: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, str]:
    _check_auth(authorization)
    msg = _build_email(payload)
    _send(msg)
    article_id = payload.get("article_id") or "unknown"
    # Substack doesn't return a draft URL via email; point at the dashboard.
    return {
        "published_url": "https://substack.com/publish/posts",
        "id": article_id,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9001)
