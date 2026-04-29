"""Email newsletter publisher via Resend (``https://api.resend.com/emails``).

Publish path:

1. Mock mode short-circuits to a deterministic fake ``resend_id``.
2. Real mode expects ``RESEND_API_KEY``, ``NEWSLETTER_FROM_EMAIL``, and either
   ``NEWSLETTER_AUDIENCE_ID`` (audience-based — Resend loops for us) or one or
   more explicit recipients in ``version.metadata['to']``.

Rollback path: emails can't be un-sent. ``rollback`` always returns
``(False, ...)`` with a reason directing the user to ``af newsletter-correction``.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import PlatformVersion, PublishResult

from .base import BasePublisher

_log = get_logger("agent_d4.email")

_RESEND_ENDPOINT = "https://api.resend.com/emails"


class EmailPublisher(BasePublisher):
    platform_name = "email_newsletter"

    def _mock_url(self, short_hash: str) -> str:  # noqa: D401 — no url for email
        return f"resend://mock/{short_hash}"

    async def publish(self, version: PlatformVersion) -> PublishResult:
        if self._is_mock_mode():
            _log.info("MOCK_LLM=true — short-circuiting email publish")
            return await self._mock_publish(version)

        import requests

        api_key = (
            self.credentials.get("api_key") or os.environ.get("RESEND_API_KEY")
        )
        from_email = (
            version.metadata.get("from_email")
            or self.credentials.get("from_email")
            or os.environ.get("NEWSLETTER_FROM_EMAIL")
        )
        from_name = (
            version.metadata.get("from_name")
            or self.credentials.get("from_name")
            or os.environ.get("NEWSLETTER_FROM_NAME")
            or ""
        )
        reply_to = (
            version.metadata.get("reply_to")
            or self.credentials.get("reply_to")
            or os.environ.get("NEWSLETTER_REPLY_TO")
        )
        audience_id = (
            version.metadata.get("audience_id")
            or self.credentials.get("audience_id")
            or os.environ.get("NEWSLETTER_AUDIENCE_ID")
        )
        to_recipients = version.metadata.get("to") or []

        if not api_key or not from_email:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason="missing RESEND_API_KEY or NEWSLETTER_FROM_EMAIL",
            )

        subject = version.metadata.get("subject") or ""
        if not subject:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason="missing subject in version.metadata",
            )

        from_hdr = f"{from_name} <{from_email}>" if from_name else from_email

        # version.content is HTML; metadata['plain_text_body'] is the fallback.
        html_body = version.content
        text_body = version.metadata.get("plain_text_body") or ""

        # Resolve {unsubscribe_link} — Resend provides one automatically when
        # sending via an Audience. When we're doing an explicit to-list (e.g. a
        # preview send), the placeholder would leak into the final message so we
        # swap in a mailto: fallback.
        unsub_link = version.metadata.get("unsubscribe_link") or (
            f"mailto:{reply_to}?subject=unsubscribe" if reply_to else "mailto:?subject=unsubscribe"
        )
        if not audience_id:
            html_body = html_body.replace("{unsubscribe_link}", unsub_link)
            text_body = text_body.replace("{unsubscribe_link}", unsub_link)

        payload: dict[str, Any] = {
            "from": from_hdr,
            "subject": subject,
            "html": html_body,
        }
        if text_body:
            payload["text"] = text_body
        if reply_to:
            payload["reply_to"] = reply_to
        if audience_id:
            payload["audience_id"] = audience_id
            # When audience-based, Resend still requires a `to` field but
            # iterates over the audience itself. Send to the from-address as
            # the fallback recipient per Resend's broadcast spec.
            if not to_recipients:
                to_recipients = [from_email]
        if not to_recipients:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason="no recipients (set NEWSLETTER_AUDIENCE_ID or version.metadata['to'])",
            )
        payload["to"] = to_recipients

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(
                _RESEND_ENDPOINT, headers=headers, json=payload, timeout=30
            )
        except requests.exceptions.RequestException as exc:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=f"Resend network error: {exc}",
            )

        if resp.status_code not in (200, 201, 202):
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=f"Resend API {resp.status_code}: {resp.text[:300]}",
            )

        try:
            body = resp.json()
        except Exception:
            body = {}
        resend_id = body.get("id") or body.get("email_id")

        return PublishResult(
            platform=self.platform_name,
            status="success",
            published_url=None,  # email has no URL
            platform_post_id=resend_id,
            published_at=datetime.now(),
        )

    def rollback(self, platform_post_id: str) -> tuple[bool, str | None]:
        """Email can't be un-sent. Always returns ``(False, reason)``."""
        reason = (
            "email cannot be un-sent; use `af newsletter-correction` to send a "
            "follow-up correction"
        )
        return False, reason
