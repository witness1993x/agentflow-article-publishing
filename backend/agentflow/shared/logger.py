"""Stdout + file logging plus LLM call logging."""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs

_LOG_DIR = agentflow_home() / "logs"
_MAIN_LOG = _LOG_DIR / "agentflow.log"
_LLM_LOG = _LOG_DIR / "llm_calls.jsonl"

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return

    ensure_user_dirs()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    root = logging.getLogger("agentflow")
    root.setLevel(logging.INFO)

    # Avoid duplicate handlers on reload.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)

    if not any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
    ):
        file_handler = logging.handlers.RotatingFileHandler(
            _MAIN_LOG, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    _configured = True


def get_logger(name: str = "agentflow") -> logging.Logger:
    """Return a configured logger. Safe to call multiple times."""
    _configure()
    if not name.startswith("agentflow"):
        name = f"agentflow.{name}"
    return logging.getLogger(name)


def log_llm_call(
    prompt_family: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: float,
    mocked: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one JSON line to ~/.agentflow/logs/llm_calls.jsonl."""
    ensure_user_dirs()
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt_family": prompt_family,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "latency_ms": float(latency_ms),
        "mocked": bool(mocked),
    }
    if extra:
        record.update(extra)
    with _LLM_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def llm_log_path() -> Path:
    return _LLM_LOG


def main_log_path() -> Path:
    return _MAIN_LOG
