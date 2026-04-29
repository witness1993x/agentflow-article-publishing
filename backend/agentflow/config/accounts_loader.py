"""Load accounts.yaml plus publishing credentials from env vars."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from agentflow.shared.bootstrap import agentflow_home

USER_ACCOUNTS_PATH = agentflow_home() / "accounts.yaml"

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "config-examples"
# No accounts example ships by default — fall back to empty mapping.
EXAMPLE_ACCOUNTS_PATH = _EXAMPLES_DIR / "accounts.example.yaml"


def load_accounts() -> dict[str, Any]:
    """Load ~/.agentflow/accounts.yaml if present, else fall back to example/empty."""
    if USER_ACCOUNTS_PATH.exists():
        with USER_ACCOUNTS_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if isinstance(data, dict):
            return data
    if EXAMPLE_ACCOUNTS_PATH.exists():
        with EXAMPLE_ACCOUNTS_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if isinstance(data, dict):
            return data
    return {}


def load_publishing_credentials() -> dict[str, dict[str, str | None]]:
    """Pull publishing credentials from env vars.

    Keys match the ``platform`` field used by D3/D4.
    """
    return {
        "medium": {
            "integration_token": os.environ.get("MEDIUM_INTEGRATION_TOKEN"),
        },
        "linkedin_article": {
            "access_token": os.environ.get("LINKEDIN_ACCESS_TOKEN"),
            "person_urn": os.environ.get("LINKEDIN_PERSON_URN"),
        },
        "ghost_wordpress": {
            "api_url": os.environ.get("GHOST_ADMIN_API_URL"),
            "admin_key": os.environ.get("GHOST_ADMIN_API_KEY"),
        },
        # v0.5 optional — kept here so callers don't crash on .get()
        "substack": {
            "email": os.environ.get("SUBSTACK_EMAIL"),
            "password": os.environ.get("SUBSTACK_PASSWORD"),
        },
        "wechat_official": {
            "app_id": os.environ.get("WECHAT_APP_ID"),
            "app_secret": os.environ.get("WECHAT_APP_SECRET"),
        },
        "x_longform": {
            "api_key": os.environ.get("X_API_KEY"),
            "api_secret": os.environ.get("X_API_SECRET"),
        },
    }
