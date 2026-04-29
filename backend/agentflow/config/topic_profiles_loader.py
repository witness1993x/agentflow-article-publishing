"""Load topic_profiles.yaml with example fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentflow.shared.bootstrap import agentflow_home

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "config-examples"
EXAMPLE_TOPIC_PROFILES_PATH = _EXAMPLES_DIR / "topic_profiles.example.yaml"


def user_topic_profiles_path() -> Path:
    return agentflow_home() / "topic_profiles.yaml"


def load_topic_profiles() -> dict[str, Any]:
    """Load ~/.agentflow/topic_profiles.yaml, falling back to the example."""
    user_path = user_topic_profiles_path()
    path = user_path if user_path.exists() else EXAMPLE_TOPIC_PROFILES_PATH
    if not path.exists():
        raise FileNotFoundError(
            "No topic profiles file at "
            f"{user_path} and no example at {EXAMPLE_TOPIC_PROFILES_PATH}"
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Topic profiles file at {path} is not a YAML mapping")
    return data
