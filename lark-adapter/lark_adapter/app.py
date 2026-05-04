"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .config import get_settings
from .routes_card import router as card_router
from .routes_event import router as event_router
from .routes_health import router as health_router


def create_app() -> FastAPI:
    """Build the FastAPI app. Wired so `uvicorn ... --factory` can boot it."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = FastAPI(
        title="Lark Bot Adapter",
        version="0.1.0",
        description="Bridges Lark/Feishu open-platform events to AgentFlow review.",
    )
    app.include_router(health_router)
    app.include_router(event_router)
    app.include_router(card_router)
    return app
