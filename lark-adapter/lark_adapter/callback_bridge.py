"""Bridge into AgentFlow's review callback module (lazy import + stub fallback)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_real_handler = None
_loaded = False
_load_error: str | None = None


def _try_load() -> None:
    """Attempt to import the real handler exactly once."""
    global _real_handler, _loaded, _load_error
    if _loaded:
        return
    _loaded = True
    try:
        from agentflow.agent_review import lark_callback  # type: ignore[import-not-found]

        _real_handler = lark_callback.handle_event
        logger.info("callback_bridge: agent_review.lark_callback loaded")
    except ImportError as exc:
        _load_error = str(exc)
        logger.warning("callback_bridge: agent_review.lark_callback unavailable (%s)", exc)


def is_loaded() -> bool:
    """Whether the real AgentFlow handler is wired up."""
    _try_load()
    return _real_handler is not None


def handle_event(
    *,
    event_kind: str,
    article_id: str | None,
    action: str | None,
    payload: dict[str, Any],
    operator: dict[str, Any],
) -> dict[str, Any]:
    """Forward into the real handler or return a stub response.

    Contract — see SHARED INTERFACE CONTRACT in the service README:
        returns {"ack": bool, "reply_card": dict|None, "reply_text": str|None,
                 "side_effects": list[str]}
    """
    _try_load()
    if _real_handler is not None:
        return _real_handler(
            event_kind=event_kind,
            article_id=article_id,
            action=action,
            payload=payload,
            operator=operator,
        )
    return {
        "ack": True,
        "reply_card": None,
        "reply_text": None,
        "side_effects": ["agentflow_not_installed"],
    }
