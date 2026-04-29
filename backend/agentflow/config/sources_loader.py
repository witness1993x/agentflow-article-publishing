"""Load sources.yaml with example fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentflow.shared.bootstrap import agentflow_home

USER_SOURCES_PATH = agentflow_home() / "sources.yaml"

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "config-examples"
EXAMPLE_SOURCES_PATH = _EXAMPLES_DIR / "sources.example.yaml"


def load_sources() -> dict[str, Any]:
    """Load ~/.agentflow/sources.yaml, falling back to the example."""
    path = USER_SOURCES_PATH if USER_SOURCES_PATH.exists() else EXAMPLE_SOURCES_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No sources file at {USER_SOURCES_PATH} and no example at {EXAMPLE_SOURCES_PATH}"
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Sources file at {path} is not a YAML mapping")
    return data
