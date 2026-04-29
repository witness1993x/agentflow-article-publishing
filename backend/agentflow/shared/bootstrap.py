"""Ensure AgentFlow runtime directories exist."""

from __future__ import annotations

import os
from pathlib import Path

AGENTFLOW_HOME = Path(
    os.environ.get("AGENTFLOW_HOME", "").strip() or Path.home() / ".agentflow"
).expanduser()

SUBDIRS = (
    "hotspots",
    "search_results",
    "drafts",
    "logs",
    "memory",
    "constraint_suggestions",
    "constraint_sessions",
    "style_corpus",
    "style_corpus/raw",
)


def ensure_user_dirs() -> Path:
    """Create ~/.agentflow runtime directories.

    Idempotent. Returns AGENTFLOW_HOME.
    """
    AGENTFLOW_HOME.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (AGENTFLOW_HOME / sub).mkdir(parents=True, exist_ok=True)
    return AGENTFLOW_HOME


def agentflow_home() -> Path:
    return AGENTFLOW_HOME
