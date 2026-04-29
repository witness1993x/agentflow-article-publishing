"""Agent D2 — collaborative writer (skeleton + filler + editor + compliance)."""

from agentflow.agent_d2.skeleton_generator import generate_skeleton
from agentflow.agent_d2.section_filler import fill_section
from agentflow.agent_d2.interactive_editor import apply_edit, PRESET_COMMANDS
from agentflow.agent_d2.compliance_checker import check, scan_draft
from agentflow.agent_d2.main import (
    generate_skeleton_for_hotspot,
    fill_all_sections,
    apply_user_edit,
    load_draft,
    save_draft,
)

__all__ = [
    "generate_skeleton",
    "fill_section",
    "apply_edit",
    "PRESET_COMMANDS",
    "check",
    "scan_draft",
    "generate_skeleton_for_hotspot",
    "fill_all_sections",
    "apply_user_edit",
    "load_draft",
    "save_draft",
]
