"""Generic webhook publisher.

POSTs the article — markdown + metadata + cover/inline images — to an
arbitrary HTTP endpoint configured via env. Lets the operator wire any
custom CMS / Substack-style relay / Mirror.xyz draft / internal queue
without bespoke code per integration.

Two wire formats are supported, switched via ``WEBHOOK_FORMAT``:

1. ``json`` (default) — single JSON body with images either inlined as
   base64 or referenced by ``local_path``::

     POST $WEBHOOK_PUBLISH_URL
     Authorization: <WEBHOOK_AUTH_HEADER>          # e.g. "Bearer xxx" — verbatim
     Content-Type: application/json

     {
       "article_id": "...",
       "title": "...", "subtitle": "...", "tags": [...], "canonical_url": null,
       "body_markdown": "# ...",
       "cover_image": {filename, content_type, data_base64} | null,
       "inline_images": [{filename, content_type, data_base64}, ...],
       "metadata": {publisher_brand, voice, agentflow_version, ...}
     }

   When ``WEBHOOK_INCLUDE_IMAGE_BASE64=false`` the cover/inline image objects
   shrink to ``{filename, content_type, local_path}`` so the receiver fetches
   binaries itself (useful for local-dev or when both sides share storage).

2. ``multipart`` — ``multipart/form-data`` with raw image bytes. Friendlier
   for FastAPI / Django / generic CMS upload endpoints that natively want
   file parts. Requests builds the boundary; do not set Content-Type::

     POST $WEBHOOK_PUBLISH_URL
     Content-Type: multipart/form-data; boundary=...

     --boundary
     Content-Disposition: form-data; name="meta"
     Content-Type: application/json

     {"article_id": "...", "title": "...", "subtitle": "...", "tags": [...],
      "canonical_url": null, "metadata": {...}}
     --boundary
     Content-Disposition: form-data; name="body"
     Content-Type: text/markdown; charset=utf-8

     # ...
     --boundary
     Content-Disposition: form-data; name="cover"; filename="cover.png"
     Content-Type: image/png

     <raw bytes>
     --boundary
     Content-Disposition: form-data; name="inline_0"; filename="fig.png"
     Content-Type: image/png

     <raw bytes>
     --boundary--

   Inline images are sent as ``inline_0``, ``inline_1``, … (indexed flat
   field names, since most multipart parsers — incl. cgi.FieldStorage —
   treat repeated names ambiguously).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import PlatformVersion, PublishResult

from .base import BasePublisher

_log = get_logger("agent_d4.webhook")


class WebhookPublisher(BasePublisher):
    platform_name = "webhook"

    def _mock_url(self, short_hash: str) -> str:
        return f"https://webhook.mock/agentflow/{short_hash}"

    async def publish(self, version: PlatformVersion) -> PublishResult:
        if self._is_mock_mode():
            _log.info("MOCK_LLM=true — short-circuiting webhook publish")
            return await self._mock_publish(version)

        url = (
            self.credentials.get("publish_url")
            or os.environ.get("WEBHOOK_PUBLISH_URL")
            or ""
        ).strip()
        if not url:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason="WEBHOOK_PUBLISH_URL not set",
            )
        auth_header = (
            self.credentials.get("auth_header")
            or os.environ.get("WEBHOOK_AUTH_HEADER")
            or ""
        ).strip()
        include_b64 = (
            (
                self.credentials.get("include_image_base64")
                if "include_image_base64" in self.credentials
                else os.environ.get("WEBHOOK_INCLUDE_IMAGE_BASE64", "true")
            )
        )
        include_b64 = str(include_b64).strip().lower() != "false"
        fmt = (
            self.credentials.get("format")
            or os.environ.get("WEBHOOK_FORMAT", "json")
            or "json"
        )
        fmt = str(fmt).strip().lower()
        if fmt not in ("json", "multipart"):
            fmt = "json"

        import requests

        headers: dict[str, str] = {}
        if fmt == "json":
            headers["Content-Type"] = "application/json"
        if auth_header:
            # Pass-through verbatim; supports "Bearer xxx", "Basic ...", "X-API-Key xxx" etc.
            if ":" in auth_header and not auth_header.lower().startswith(
                ("bearer ", "basic ")
            ):
                key, _, value = auth_header.partition(":")
                headers[key.strip()] = value.strip()
            else:
                headers["Authorization"] = auth_header

        try:
            if fmt == "multipart":
                files = self._build_multipart(version)
                # Do NOT set Content-Type — requests builds the boundary.
                resp = requests.post(url, headers=headers, files=files, timeout=60)
            else:
                body = self._build_payload(version, include_b64=include_b64)
                resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)
        except Exception as exc:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=f"webhook POST raised: {exc}",
            )

        if resp.status_code >= 400:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=f"webhook {resp.status_code}: {resp.text[:300]}",
            )

        # Parse the receiver's response. Any of these keys are accepted:
        #   {"published_url": "..."} — preferred
        #   {"url": "..."}
        #   {"id": "..."} — fallback for post id
        published_url: str | None = None
        platform_post_id: str | None = None
        try:
            data = resp.json()
            if isinstance(data, dict):
                published_url = (
                    data.get("published_url") or data.get("url") or data.get("link")
                )
                platform_post_id = data.get("id") or data.get("post_id")
        except Exception:
            pass

        return PublishResult(
            platform=self.platform_name,
            status="success",
            published_url=published_url,
            platform_post_id=platform_post_id,
            published_at=datetime.now(),
            failure_reason=None,
        )

    # ------------------------------------------------------------------
    # Payload assembly
    # ------------------------------------------------------------------

    def _build_payload(
        self, version: PlatformVersion, *, include_b64: bool
    ) -> dict[str, Any]:
        meta = dict(version.metadata or {})
        body = version.content or ""
        cover_path = (
            self.credentials.get("cover_image_path")
            or meta.pop("cover_image_path", None)
        )
        # Inline images: scan the body for ![](path) embeds and grab any local
        # files. Deduplicate, skip the cover_path so we don't ship it twice.
        inline_paths = self._extract_inline_image_paths(body, exclude=cover_path)
        return {
            "article_id": meta.pop("article_id", None),
            "title": meta.pop("title", None),
            "subtitle": meta.pop("subtitle", None),
            "tags": list(meta.pop("tags", []) or []),
            "canonical_url": meta.pop("canonical_url", None),
            "body_markdown": body,
            "cover_image": self._image_block(cover_path, include_b64=include_b64),
            "inline_images": [
                blk for p in inline_paths
                if (blk := self._image_block(p, include_b64=include_b64)) is not None
            ],
            "metadata": {
                "platform": self.platform_name,
                "agentflow_version": "0.1",
                "published_via": "webhook",
                **meta,
            },
        }

    @staticmethod
    def _extract_inline_image_paths(body: str, *, exclude: str | None) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        # ![alt](path) — local absolute paths only; skip http(s)
        for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", body or ""):
            path = match.group(1).strip()
            if not path or path.startswith(("http://", "https://", "data:")):
                continue
            if exclude and Path(path).resolve() == Path(exclude).resolve():
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    def _build_multipart(
        self, version: PlatformVersion
    ) -> list[tuple[str, tuple[str | None, Any, str]]]:
        """Assemble multipart/form-data parts: meta JSON + body markdown + raw images.

        Returns a list of (field_name, (filename, content, content_type)) tuples;
        ``requests.post(..., files=...)`` accepts this shape and preserves order.
        """
        meta = dict(version.metadata or {})
        body = version.content or ""
        cover_path = (
            self.credentials.get("cover_image_path")
            or meta.pop("cover_image_path", None)
        )
        inline_paths = self._extract_inline_image_paths(body, exclude=cover_path)
        meta_dict: dict[str, Any] = {
            "article_id": meta.pop("article_id", None),
            "title": meta.pop("title", None),
            "subtitle": meta.pop("subtitle", None),
            "tags": list(meta.pop("tags", []) or []),
            "canonical_url": meta.pop("canonical_url", None),
            "metadata": {
                "platform": self.platform_name,
                "agentflow_version": "0.1",
                "published_via": "webhook",
                **meta,
            },
        }
        parts: list[tuple[str, tuple[str | None, Any, str]]] = [
            ("meta", (None, json.dumps(meta_dict, ensure_ascii=False).encode("utf-8"), "application/json")),
            ("body", (None, body.encode("utf-8"), "text/markdown; charset=utf-8")),
        ]
        cover = self._image_part(cover_path)
        if cover is not None:
            parts.append(("cover", cover))
        for idx, p in enumerate(inline_paths):
            part = self._image_part(p)
            if part is not None:
                parts.append((f"inline_{idx}", part))
        return parts

    @staticmethod
    def _image_part(path: str | None) -> tuple[str, bytes, str] | None:
        if not path:
            return None
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return None
        ctype, _ = mimetypes.guess_type(p.name)
        return (p.name, p.read_bytes(), ctype or "application/octet-stream")

    @staticmethod
    def _image_block(
        path: str | None, *, include_b64: bool
    ) -> dict[str, Any] | None:
        if not path:
            return None
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return {
                "filename": p.name,
                "content_type": None,
                "missing_local_file": True,
                "local_path": str(p),
            }
        ctype, _ = mimetypes.guess_type(p.name)
        block: dict[str, Any] = {
            "filename": p.name,
            "content_type": ctype or "application/octet-stream",
            "local_path": str(p),
        }
        if include_b64:
            block["data_base64"] = base64.b64encode(p.read_bytes()).decode("ascii")
        return block
