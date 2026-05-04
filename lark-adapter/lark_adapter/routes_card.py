"""POST /lark/card — interactive-card action callback handler."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from . import callback_bridge
from .card_meta import decode_meta
from .config import get_settings
from .security import verify_signature

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_action_value(raw_value: Any) -> dict[str, Any]:
    """Lark sends action.value as either a dict or a JSON-encoded string."""
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str) and raw_value:
        try:
            decoded = json.loads(raw_value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


@router.post("/lark/card")
async def receive_card(
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

    # Card challenge (Lark also fires url_verification on the card endpoint).
    if body_json.get("type") == "url_verification" and "challenge" in body_json:
        return {"challenge": str(body_json["challenge"])}

    action_blob = body_json.get("action") or {}
    action_value = _parse_action_value(action_blob.get("value"))
    article_id, action_name = decode_meta(action_value)

    operator = {
        "open_id": body_json.get("open_id") or body_json.get("user_id"),
        "name": body_json.get("user_name"),
    }
    chat_id = body_json.get("chat_id")
    message_id = body_json.get("message_id")
    logger.info(
        "card: article_id=%s action=%s chat_id=%s message_id=%s",
        article_id,
        action_name,
        chat_id,
        message_id,
    )

    result = callback_bridge.handle_event(
        event_kind="card_action",
        article_id=article_id,
        action=action_name,
        payload=body_json,
        operator=operator,
    )

    # Lark accepts a few ack shapes: {"toast": ...} | {"card": ...} | {}.
    response: dict[str, Any] = {}
    if result.get("reply_card"):
        response["card"] = result["reply_card"]
    if result.get("reply_text"):
        response.setdefault("toast", {"type": "info", "content": result["reply_text"]})
    return response
