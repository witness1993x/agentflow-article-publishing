"""Per-platform adapter implementations."""

from agentflow.agent_d3.adapters.base import BasePlatformAdapter
from agentflow.agent_d3.adapters.ghost import GhostAdapter
from agentflow.agent_d3.adapters.linkedin import LinkedInAdapter
from agentflow.agent_d3.adapters.medium import MediumAdapter
from agentflow.agent_d3.adapters.twitter import TwitterAdapter, TwitterSingleAdapter
from agentflow.agent_d3.adapters.webhook import WebhookAdapter

ADAPTERS: dict[str, type[BasePlatformAdapter]] = {
    "medium": MediumAdapter,
    "linkedin_article": LinkedInAdapter,
    "ghost_wordpress": GhostAdapter,
    "webhook": WebhookAdapter,
    "twitter_thread": TwitterAdapter,
    "twitter_single": TwitterSingleAdapter,
}

__all__ = [
    "ADAPTERS",
    "BasePlatformAdapter",
    "MediumAdapter",
    "LinkedInAdapter",
    "GhostAdapter",
    "WebhookAdapter",
    "TwitterAdapter",
    "TwitterSingleAdapter",
]
