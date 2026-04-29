"""Abstract base class for platform publishers.

Every concrete publisher MUST short-circuit to ``_mock_publish`` when the env
var ``MOCK_LLM=true`` is set, before making any network call.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Any

from agentflow.shared.models import PlatformVersion, PublishResult


def _hash_short(version: PlatformVersion) -> str:
    """Deterministic short hash used in mock URLs."""
    seed = f"{version.platform}|{version.content}".encode("utf-8")
    return hashlib.sha1(seed).hexdigest()[:10]


class BasePublisher:
    """Abstract platform publisher.

    Subclasses override :attr:`platform_name`, :meth:`publish`, and
    :meth:`_mock_publish` (to provide a platform-specific mock URL).
    """

    platform_name: str = ""

    def __init__(self, credentials: dict[str, Any] | None = None) -> None:
        self.credentials: dict[str, Any] = credentials or {}

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    async def publish(self, version: PlatformVersion) -> PublishResult:
        """Publish ``version`` to this platform and return a ``PublishResult``."""
        raise NotImplementedError

    async def _mock_publish(self, version: PlatformVersion) -> PublishResult:
        """Return a deterministic fake ``PublishResult`` without touching the network."""
        short = _hash_short(version)
        url = self._mock_url(short)
        return PublishResult(
            platform=self.platform_name,
            status="success",
            published_url=url,
            platform_post_id=f"mock_{short}",
            published_at=datetime.now(),
            failure_reason=None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mock_url(self, short_hash: str) -> str:
        """Override in subclass to return a platform-specific fake URL."""
        return f"https://mock.example.com/{self.platform_name}/art_{short_hash}"

    @staticmethod
    def _is_mock_mode() -> bool:
        """Should this publisher return a fake URL instead of calling the real platform?

        Reads ``AGENTFLOW_MOCK_PUBLISHERS`` (default: false). Previously this
        was coupled to ``MOCK_LLM``, which meant any developer running with
        deterministic LLM fixtures would also pollute ``publish_history.jsonl``
        with ``https://*mock*`` URLs that surfaced in production digests and
        Gate D summaries.

        ``MOCK_LLM`` is still honoured for backward compatibility but emits a
        deprecation warning so existing setups don't silently break.
        """
        explicit = os.getenv("AGENTFLOW_MOCK_PUBLISHERS", "").strip().lower()
        if explicit in {"true", "1", "yes"}:
            return True
        if explicit in {"false", "0", "no"}:
            return False
        # Legacy fallback: MOCK_LLM=true used to imply mock publishers too.
        if os.getenv("MOCK_LLM", "").strip().lower() == "true":
            import warnings
            warnings.warn(
                "Publisher mock mode is being inferred from MOCK_LLM=true. "
                "Set AGENTFLOW_MOCK_PUBLISHERS explicitly (true/false). "
                "This compatibility shim will be removed.",
                DeprecationWarning,
                stacklevel=3,
            )
            return True
        return False
