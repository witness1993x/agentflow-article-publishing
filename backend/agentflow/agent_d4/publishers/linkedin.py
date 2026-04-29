"""LinkedIn publisher — UGC Post API with optional cover image upload.

Image flow is LinkedIn's 3-step asset protocol:
  1. POST /v2/assets?action=registerUpload — register the upload, get back a
     signed uploadUrl + a digitalmediaAsset URN.
  2. PUT the image bytes to uploadUrl (with the headers LinkedIn returns).
  3. POST /v2/ugcPosts with shareMediaCategory=IMAGE + media=[{asset URN}].

If the cover_image_path is missing or upload fails, we degrade to a
text-only post (shareMediaCategory=NONE) — never block the publish on a
failed image step.
"""

from __future__ import annotations

import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import PlatformVersion, PublishResult

from .base import BasePublisher

_log = get_logger("agent_d4.linkedin")


class LinkedInPublisher(BasePublisher):
    platform_name = "linkedin_article"

    UGC_ENDPOINT = "https://api.linkedin.com/v2/ugcPosts"
    REGISTER_UPLOAD_ENDPOINT = "https://api.linkedin.com/v2/assets?action=registerUpload"
    FEEDSHARE_RECIPE = "urn:li:digitalmediaRecipe:feedshare-image"

    def _mock_url(self, short_hash: str) -> str:
        return f"https://www.linkedin.com/feed/update/mock_{short_hash}"

    async def publish(self, version: PlatformVersion) -> PublishResult:
        if self._is_mock_mode():
            _log.info("MOCK_LLM=true — short-circuiting linkedin publish")
            return await self._mock_publish(version)

        import requests

        access_token = (
            self.credentials.get("access_token")
            or os.environ.get("LINKEDIN_ACCESS_TOKEN")
        )
        person_urn = (
            self.credentials.get("person_urn")
            or os.environ.get("LINKEDIN_PERSON_URN")
        )

        if not access_token or not person_urn:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason="missing LINKEDIN_ACCESS_TOKEN or LINKEDIN_PERSON_URN",
            )

        author_urn = person_urn if person_urn.startswith("urn:li:") else f"urn:li:person:{person_urn}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

        # Optional: upload cover image and reference its asset URN in the
        # share. Best-effort — fall back to text-only on any failure.
        meta = version.metadata or {}
        cover_path = (
            self.credentials.get("cover_image_path")
            or meta.get("cover_image_path")
        )
        asset_urn: str | None = None
        if cover_path:
            asset_urn = self._upload_image_asset(
                requests=requests,
                access_token=access_token,
                author_urn=author_urn,
                image_path=cover_path,
            )
            if asset_urn:
                _log.info("linkedin: image uploaded, asset=%s", asset_urn)
            else:
                _log.warning("linkedin: image upload failed; degrading to text-only")

        # Build the share content. With an asset URN we use shareMediaCategory=IMAGE;
        # without one, fall back to NONE (text-only).
        share_content: dict[str, Any] = {
            "shareCommentary": {"text": version.content},
        }
        if asset_urn:
            share_content["shareMediaCategory"] = "IMAGE"
            media_entry: dict[str, Any] = {
                "status": "READY",
                "media": asset_urn,
            }
            title = meta.get("title")
            subtitle = meta.get("subtitle")
            if title:
                media_entry["title"] = {"text": title[:200]}
            if subtitle:
                media_entry["description"] = {"text": subtitle[:300]}
            share_content["media"] = [media_entry]
        else:
            share_content["shareMediaCategory"] = "NONE"

        post_data: dict[str, Any] = {
            "author": author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC",
            },
        }

        resp = requests.post(
            self.UGC_ENDPOINT, headers=headers, json=post_data, timeout=30
        )
        if resp.status_code != 201:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=f"LinkedIn API {resp.status_code}: {resp.text[:300]}",
            )

        post_id = resp.headers.get("x-restli-id")
        if not post_id:
            try:
                post_id = resp.json().get("id")
            except Exception:
                post_id = None

        published_url = (
            f"https://www.linkedin.com/feed/update/{post_id}" if post_id else None
        )
        return PublishResult(
            platform=self.platform_name,
            status="success",
            published_url=published_url,
            platform_post_id=post_id,
            published_at=datetime.now(),
        )

    # ------------------------------------------------------------------
    # 3-step asset upload
    # ------------------------------------------------------------------

    def _upload_image_asset(
        self,
        *,
        requests: Any,
        access_token: str,
        author_urn: str,
        image_path: str,
    ) -> str | None:
        """Upload ``image_path`` and return its digitalmediaAsset URN.

        Returns ``None`` on any failure — caller falls back to text-only.
        """
        path = Path(image_path).expanduser()
        if not path.exists() or not path.is_file():
            _log.warning("linkedin: cover_image_path does not exist: %s", path)
            return None

        # Step 1: register upload
        register_body = {
            "registerUploadRequest": {
                "recipes": [self.FEEDSHARE_RECIPE],
                "owner": author_urn,
                "serviceRelationships": [
                    {
                        "relationshipType": "OWNER",
                        "identifier": "urn:li:userGeneratedContent",
                    }
                ],
            }
        }
        try:
            reg = requests.post(
                self.REGISTER_UPLOAD_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Restli-Protocol-Version": "2.0.0",
                    "Content-Type": "application/json",
                },
                json=register_body,
                timeout=30,
            )
        except Exception as exc:
            _log.warning("linkedin registerUpload raised: %s", exc)
            return None
        if reg.status_code not in (200, 201):
            _log.warning(
                "linkedin registerUpload %d: %s", reg.status_code, reg.text[:200]
            )
            return None
        try:
            value = reg.json().get("value", {})
            mech = value.get("uploadMechanism", {})
            http_req = mech.get(
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {}
            )
            upload_url = http_req.get("uploadUrl")
            extra_headers = http_req.get("headers") or {}
            asset_urn = value.get("asset")
        except Exception as exc:
            _log.warning("linkedin registerUpload parse failed: %s", exc)
            return None
        if not upload_url or not asset_urn:
            _log.warning("linkedin registerUpload missing uploadUrl/asset: %s", value)
            return None

        # Step 2: PUT bytes
        ctype, _ = mimetypes.guess_type(path.name)
        upload_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": ctype or "application/octet-stream",
            **{str(k): str(v) for k, v in extra_headers.items()},
        }
        try:
            with path.open("rb") as fh:
                up = requests.put(upload_url, headers=upload_headers, data=fh, timeout=60)
        except Exception as exc:
            _log.warning("linkedin PUT upload raised: %s", exc)
            return None
        if up.status_code not in (200, 201):
            _log.warning("linkedin PUT upload %d: %s", up.status_code, up.text[:200])
            return None

        return str(asset_urn)
