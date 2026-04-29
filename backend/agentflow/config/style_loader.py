"""Load / save style_profile.yaml with example fallback + timestamped backup."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agentflow.shared.bootstrap import agentflow_home

USER_STYLE_PATH = agentflow_home() / "style_profile.yaml"

# config-examples lives at project_root/config-examples
_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "config-examples"
EXAMPLE_STYLE_PATH = _EXAMPLES_DIR / "style_profile.example.yaml"


def load_style_profile() -> dict[str, Any]:
    """Load ~/.agentflow/style_profile.yaml, falling back to the example."""
    path = USER_STYLE_PATH if USER_STYLE_PATH.exists() else EXAMPLE_STYLE_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No style profile at {USER_STYLE_PATH} and no example at {EXAMPLE_STYLE_PATH}"
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Style profile at {path} is not a YAML mapping")
    return data


def save_style_profile(profile: dict[str, Any]) -> Path:
    """Write profile to ~/.agentflow/style_profile.yaml.

    If a file already exists there, back it up to
    ``style_profile.<yyyymmdd-HHMMSS>.yaml`` next to the target first.
    """
    USER_STYLE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if USER_STYLE_PATH.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = USER_STYLE_PATH.parent / f"style_profile.{ts}.yaml"
        shutil.copy2(USER_STYLE_PATH, backup)

    with USER_STYLE_PATH.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            profile,
            fh,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    return USER_STYLE_PATH
