"""Minimal AgentFlow bridge client example.

Usage:
    export AGENTFLOW_BASE_URL=http://127.0.0.1:7860
    export AGENTFLOW_READ_TOKEN=read-token
    export AGENTFLOW_WRITE_TOKEN=write-token
    python docs/integrations/examples/python_bridge_client.py
"""

from __future__ import annotations

import json
import os

import requests


BASE_URL = os.environ.get("AGENTFLOW_BASE_URL", "http://127.0.0.1:7860").rstrip("/")
READ_TOKEN = os.environ.get("AGENTFLOW_READ_TOKEN", "")
WRITE_TOKEN = os.environ.get("AGENTFLOW_WRITE_TOKEN", "")


def _headers(token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def main() -> None:
    bridge = requests.get(f"{BASE_URL}/api/bridge", headers=_headers(READ_TOKEN), timeout=10)
    bridge.raise_for_status()
    bridge_payload = bridge.json()
    print("bridge capabilities:")
    print(json.dumps(bridge_payload, indent=2, ensure_ascii=False))

    health = requests.get(f"{BASE_URL}/api/health", headers=_headers(READ_TOKEN), timeout=10)
    health.raise_for_status()
    print("\nhealth:")
    print(json.dumps(health.json(), indent=2, ensure_ascii=False))

    if WRITE_TOKEN:
        cmd = requests.post(
            f"{BASE_URL}/api/commands",
            headers=_headers(WRITE_TOKEN),
            json={"command": "doctor"},
            timeout=30,
        )
        cmd.raise_for_status()
        print("\ncommand result:")
        print(json.dumps(cmd.json(), indent=2, ensure_ascii=False))
    else:
        print("\nWRITE token missing; skipping POST /api/commands example")


if __name__ == "__main__":
    main()
