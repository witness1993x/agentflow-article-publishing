"""Liveness probe."""

from __future__ import annotations

import os

from fastapi import APIRouter

from . import callback_bridge

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, object]:
    return {
        "ok": True,
        "lark_app_id_present": bool(os.environ.get("LARK_APP_ID")),
        "callback_bridge_loaded": callback_bridge.is_loaded(),
    }
