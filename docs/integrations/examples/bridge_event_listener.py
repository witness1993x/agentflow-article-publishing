"""Minimal local listener for AgentFlow bridge events.

Usage:
    export BRIDGE_LISTENER_HOST=127.0.0.1
    export BRIDGE_LISTENER_PORT=7861
    export BRIDGE_LISTENER_OUTPUT=/tmp/agentflow-bridge-events.jsonl
    python docs/integrations/examples/bridge_event_listener.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


HOST = os.environ.get("BRIDGE_LISTENER_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_LISTENER_PORT", "7861"))
OUTPUT_PATH = Path(
    os.environ.get("BRIDGE_LISTENER_OUTPUT", "/tmp/agentflow-bridge-events.jsonl")
).expanduser()

app = FastAPI(title="AgentFlow bridge event listener", version="1.0")


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "output_path": str(OUTPUT_PATH)})


@app.post("/events")
async def events(request: Request) -> JSONResponse:
    payload: dict[str, Any] = await request.json()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return JSONResponse({"ok": True, "event_id": payload.get("event_id")})


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
