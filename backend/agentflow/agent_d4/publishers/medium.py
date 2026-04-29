"""Medium publisher — POST to medium.com integration API.

DEPRECATED in v0.1. Medium closed their public API to new integrations on
2025-01-01. Accounts that never generated an integration token before that
date can no longer obtain one (the "Integration tokens" section no longer
appears in Settings → Security and apps). Legacy tokens issued before
2025-01-01 still work.

This publisher remains in the codebase for users who already hold a legacy
token. It is excluded from the default `af preview` / `af publish` platform
list; include it explicitly via `--platforms ...,medium`.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import PlatformVersion, PublishResult

from .base import BasePublisher, _hash_short

_log = get_logger("agent_d4.medium")


class MediumPublisher(BasePublisher):
    platform_name = "medium"

    API_BASE = "https://api.medium.com/v1"

    def __init__(self, credentials: dict[str, Any] | None = None) -> None:
        super().__init__(credentials)
        # Cache the Medium user id across a single publisher instance.
        self._user_id: str | None = None

    def _mock_url(self, short_hash: str) -> str:
        return f"https://medium.com/@mock/art_{short_hash}"

    async def publish(self, version: PlatformVersion) -> PublishResult:
        if self._is_mock_mode():
            _log.info("MOCK_LLM=true — short-circuiting medium publish")
            return await self._mock_publish(version)

        token = (
            self.credentials.get("integration_token")
            or os.environ.get("MEDIUM_INTEGRATION_TOKEN")
        )
        if not token:
            article_id = (
                self.credentials.get("article_id")
                or self.credentials.get("_article_id")
                or (version.metadata or {}).get("article_id")
            )
            if not article_id:
                return PublishResult(
                    platform=self.platform_name,
                    status="failed",
                    failure_reason=(
                        "Medium browser paste required, but article_id was not "
                        "available to generate medium-package artifacts."
                    ),
                )
            from agentflow.agent_medium.workflow import build_medium_manual_publish_package

            package_info = build_medium_manual_publish_package(str(article_id))
            return PublishResult(
                platform=self.platform_name,
                status="manual",
                failure_reason=(
                    "Medium browser paste required; generated medium-package "
                    f"at {package_info.get('package_path')}"
                ),
                raw_response=package_info,
            )

        import requests  # local import so mock/manual paths have zero network deps

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Resolve user id (cached per publisher instance).
        if self._user_id is None:
            me = requests.get(f"{self.API_BASE}/me", headers=headers, timeout=20)
            if me.status_code != 200:
                return PublishResult(
                    platform=self.platform_name,
                    status="failed",
                    failure_reason=f"Medium /me {me.status_code}: {me.text[:200]}",
                )
            self._user_id = me.json()["data"]["id"]

        metadata = version.metadata or {}
        payload: dict[str, Any] = {
            "title": metadata.get("title", ""),
            "contentFormat": "markdown",
            "content": version.content,
            "tags": (metadata.get("tags") or [])[:5],
            "publishStatus": "public",
        }
        if metadata.get("canonical_url"):
            payload["canonicalUrl"] = metadata["canonical_url"]

        url = f"{self.API_BASE}/users/{self._user_id}/posts"
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 201:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=f"Medium API {resp.status_code}: {resp.text[:300]}",
            )

        data = resp.json()["data"]
        return PublishResult(
            platform=self.platform_name,
            status="success",
            published_url=data.get("url"),
            platform_post_id=data.get("id"),
            published_at=datetime.now(),
        )
