"""Agent D4 entry point — multi-platform publisher.

``publish_all`` fans out one publisher per ``PlatformVersion`` using
``asyncio.gather``. Each platform gets 1 retry with a 3-second delay on
failure. Every result is appended to ``~/.agentflow/publish_history.jsonl``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import D3Output, PlatformVersion, PublishResult

from .publishers.base import BasePublisher
from .publishers.ghost import GhostPublisher
from .publishers.linkedin import LinkedInPublisher
from .publishers.medium import MediumPublisher
from .publishers.twitter import TwitterPublisher
from .publishers.webhook import WebhookPublisher
from .storage import append_publish_record

_log = get_logger("agent_d4.main")

_PLATFORM_MAP: dict[str, type[BasePublisher]] = {
    "medium": MediumPublisher,
    "linkedin_article": LinkedInPublisher,
    "ghost_wordpress": GhostPublisher,
    "webhook": WebhookPublisher,
    "twitter_thread": TwitterPublisher,
    "twitter_single": TwitterPublisher,
}

RETRY_DELAY_SECONDS = 3
MAX_ATTEMPTS = 2  # initial + 1 retry


async def _publish_with_retry(
    publisher: BasePublisher,
    version: PlatformVersion,
) -> PublishResult:
    """Call ``publisher.publish`` with 1 retry (3s delay) on failure/exception."""
    last_result: PublishResult | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            result = await publisher.publish(version)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "publisher %s raised on attempt %d/%d: %s",
                publisher.platform_name,
                attempt + 1,
                MAX_ATTEMPTS,
                exc,
            )
            result = PublishResult(
                platform=publisher.platform_name,
                status="failed",
                failure_reason=f"exception: {exc}",
            )
        if result.status in {"success", "manual"}:
            return result
        last_result = result
        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    assert last_result is not None
    return last_result


async def _publish_one(
    article_id: str,
    version: PlatformVersion,
    credentials: dict[str, Any] | None,
) -> PublishResult:
    """Resolve publisher for ``version.platform`` and publish with retry."""
    cls = _PLATFORM_MAP.get(version.platform)
    if cls is None:
        result = PublishResult(
            platform=version.platform,
            status="skipped",
            failure_reason="unsupported in v0.1",
        )
        append_publish_record(article_id, result)
        return result

    per_platform_creds: dict[str, Any] = {}
    if credentials:
        maybe = credentials.get(version.platform)
        if isinstance(maybe, dict):
            per_platform_creds = maybe
    if version.platform == "medium":
        per_platform_creds = {**per_platform_creds, "article_id": article_id}

    publisher = cls(credentials=per_platform_creds)
    result = await _publish_with_retry(publisher, version)
    # Make sure the platform name is stamped even if a publisher forgot.
    if not result.platform:
        result.platform = version.platform
    if result.status == "success" and result.published_at is None:
        result.published_at = datetime.now()
    append_publish_record(article_id, result)
    return result


async def publish_all(
    article_id: str,
    d3_output: D3Output,
    credentials: dict[str, Any] | None = None,
) -> list[PublishResult]:
    """Publish every ``PlatformVersion`` in ``d3_output`` in parallel."""
    versions = d3_output.platform_versions or []
    if not versions:
        _log.warning("publish_all: no platform_versions for %s", article_id)
        return []

    tasks = [
        _publish_one(article_id, v, credentials) for v in versions
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return list(results)


# ---------------------------------------------------------------------------
# Thin sync entry used by the CLI
# ---------------------------------------------------------------------------


def run(article_id: str) -> None:
    """CLI shim — loads ``~/.agentflow/drafts/<article_id>/`` and publishes."""
    # Imported here to avoid heavy deps when only publish_all is used.
    from agentflow.cli.commands import _cli_publish_impl  # type: ignore

    _cli_publish_impl(article_id)
