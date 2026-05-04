"""Encode/decode the hidden article-id+action payload that travels in card actions."""

from __future__ import annotations

from typing import Any

# Vocabulary mirrored on agent_review side. Keep in lockstep with the C agent.
VALID_ACTIONS = frozenset(
    {"approve_b", "reject_b", "refill", "takeover", "view_audit", "view_meta"}
)


def encode_meta(article_id: str, action: str) -> dict[str, Any]:
    """Build a Lark `action.value` payload referencing an article + action."""
    if not article_id:
        raise ValueError("article_id required")
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    return {"article_id": article_id, "action": action, "v": 1}


def decode_meta(action_value: Any) -> tuple[str | None, str | None]:
    """Best-effort decode of a card callback's `action.value`.

    Returns (article_id, action). Either may be None if missing/invalid.
    Unknown actions are returned as-is so downstream can log+reject.
    """
    if not isinstance(action_value, dict):
        return None, None
    article_id = action_value.get("article_id")
    action = action_value.get("action")
    if article_id is not None and not isinstance(article_id, str):
        article_id = str(article_id)
    if action is not None and not isinstance(action, str):
        action = str(action)
    return article_id, action
