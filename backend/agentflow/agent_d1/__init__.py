"""Agent D1 — hotspot discovery + viewpoint mining.

Public surface: ``run_d1_scan`` (async entry point) + ``run`` (sync wrapper
used by the CLI).
"""

from agentflow.agent_d1.main import run, run_d1_scan

__all__ = ["run", "run_d1_scan"]
