"""POST /lark/event — verify sig, optionally decrypt, dispatch."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from . import callback_bridge
from .config import get_settings
from .security import DecryptError, decrypt_body, verify_signature

logger = logging.getLogger(__name__)
router = APIRouter()


def _maybe_decrypt(body_json: dict[str, Any], encrypt_key: str | None) -> dict[str, Any]:
    """If body has shape {"encrypt": "..."}, decrypt and return parsed dict."""
    if "encrypt" not in body_json:
        return body_json
    if not encrypt_key:
        raise HTTPException(status_code=400, detail="encrypted body but no encrypt_key configured")
    try:
        plain = decrypt_body(encrypt_key, body_json["encrypt"])
        return json.loads(plain.decode("utf-8"))
    except (DecryptError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("event: decrypt failed: %s", exc)
        raise HTTPException(status_code=400, detail="decrypt_failed") from exc


def _extract_challenge(decoded: dict[str, Any]) -> str | None:
    """Lark v1 puts type=url_verification at top level; v2 nests via header."""
    if decoded.get("type") == "url_verification" and "challenge" in decoded:
        return str(decoded["challenge"])
    if "challenge" in decoded and "header" not in decoded:
        return str(decoded["challenge"])
    return None


@router.post("/lark/event")
async def receive_event(
    request: Request,
    x_lark_signature: str | None = Header(default=None, alias="X-Lark-Signature"),
    x_lark_request_timestamp: str | None = Header(
        default=None, alias="X-Lark-Request-Timestamp"
    ),
    x_lark_request_nonce: str | None = Header(default=None, alias="X-Lark-Request-Nonce"),
) -> dict[str, Any]:
    settings = get_settings()
    raw = await request.body()

    if not verify_signature(
        settings.lark_verification_token,
        x_lark_request_timestamp or "",
        x_lark_request_nonce or "",
        raw,
        x_lark_signature or "",
    ):
        raise HTTPException(status_code=401, detail="bad_signature")

    try:
        body_json = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc

    decoded = _maybe_decrypt(body_json, settings.lark_encrypt_key)

    challenge = _extract_challenge(decoded)
    if challenge is not None:
        return {"challenge": challenge}

    header = decoded.get("header") or {}
    event_type = header.get("event_type") or decoded.get("type") or "unknown"

    if event_type == "im.message.receive_v1":
        event = decoded.get("event") or {}
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        operator = {
            "open_id": sender_id.get("open_id"),
            "name": sender.get("sender_type"),
        }
        result = callback_bridge.handle_event(
            event_kind="message",
            article_id=None,
            action=None,
            payload=decoded,
            operator=operator,
        )
        logger.info("event: message handled side_effects=%s", result.get("side_effects"))
        return {"ack": True}

    logger.info("event: ignoring event_type=%s", event_type)
    return {"ack": True}
