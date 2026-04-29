"""Agent Email — newsletter drafting + system notification.

Public surface:

- ``draft_newsletter`` — derive a newsletter dict from an existing draft or from
  scratch. Uses ``LLMClient().chat_json`` with prompt family ``email_newsletter``.
- storage helpers — write / read / list newsletters under
  ``~/.agentflow/newsletters/<newsletter_id>/``.
"""

from agentflow.agent_email.drafter import draft_newsletter, edit_section
from agentflow.agent_email.storage import (
    load_newsletter,
    make_newsletter_id,
    newsletter_dir,
    newsletters_root,
    save_newsletter,
    list_newsletters,
)

__all__ = [
    "draft_newsletter",
    "edit_section",
    "load_newsletter",
    "make_newsletter_id",
    "newsletter_dir",
    "newsletters_root",
    "save_newsletter",
    "list_newsletters",
]
