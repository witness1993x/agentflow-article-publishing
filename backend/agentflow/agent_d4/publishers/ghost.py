"""Ghost Admin API publisher — JWT HS256 auth."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from os.path import basename
from pathlib import Path
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import PlatformVersion, PublishResult

from .base import BasePublisher

_log = get_logger("agent_d4.ghost")


# Match both double- and single-quoted src attributes in <img> tags. The callback
# decides whether each captured src is a local path (and thus needs uploading).
_IMG_SRC_RE = re.compile(
    r"""(<img\b[^>]*?\bsrc=)(["'])(.*?)\2([^>]*>)""",
    flags=re.IGNORECASE | re.DOTALL,
)


def _markdown_to_html(md_text: str) -> str:
    """Convert markdown to HTML using mistune (lazy import)."""
    import mistune  # type: ignore

    # mistune 3.x: mistune.html ; mistune 2.x: mistune.markdown()
    html_fn = getattr(mistune, "html", None)
    if callable(html_fn):
        return html_fn(md_text)
    try:
        renderer = mistune.create_markdown()  # type: ignore[attr-defined]
        return renderer(md_text)
    except Exception:  # pragma: no cover
        return md_text


def _build_jwt(admin_key: str) -> str:
    """Decode ``<id>:<secret>`` admin key and sign a 5-minute JWT."""
    import jwt  # PyJWT

    if ":" not in admin_key:
        raise ValueError("GHOST_ADMIN_API_KEY must be in '<id>:<secret>' format")
    key_id, key_secret_hex = admin_key.split(":", 1)

    iat = int(datetime.now().timestamp())
    payload = {
        "iat": iat,
        "exp": iat + 5 * 60,
        "aud": "/admin/",
    }
    headers = {"kid": key_id, "alg": "HS256", "typ": "JWT"}
    return jwt.encode(
        payload,
        bytes.fromhex(key_secret_hex),
        algorithm="HS256",
        headers=headers,
    )


class GhostPublisher(BasePublisher):
    platform_name = "ghost_wordpress"

    def _mock_url(self, short_hash: str) -> str:
        return f"https://blog.mock/art_{short_hash}"

    # ------------------------------------------------------------------
    # Image helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_local_path(p: str | None) -> bool:
        """True if ``p`` looks like a local filesystem path.

        Treats ``file://`` URIs, absolute POSIX paths, and any string that
        resolves to an existing file on disk as local. http(s):// URLs and
        data: URIs are not local.
        """
        if not p or not isinstance(p, str):
            return False
        s = p.strip()
        if not s:
            return False
        low = s.lower()
        if low.startswith(("http://", "https://", "data:", "//")):
            return False
        if low.startswith("file://"):
            return True
        if s.startswith("/"):
            return True
        # Relative path that happens to exist on disk.
        try:
            return Path(s).is_file()
        except (OSError, ValueError):
            return False

    def _upload_image(self, local_path: str, purpose: str = "image") -> str | None:
        """Upload a local image file to Ghost Storage. Returns CDN URL.

        Returns ``None`` on failure. Mock mode returns a fake URL and does
        not touch the network.
        """
        if self._is_mock_mode():
            return f"https://blog.mock/cdn/{basename(local_path)}"

        # Strip file:// prefix if present.
        path = local_path
        if path.lower().startswith("file://"):
            path = path[7:]

        if not Path(path).is_file():
            _log.warning("ghost image upload: file not found: %s", path)
            return None

        import requests  # lazy

        api_url = (
            self.credentials.get("api_url") or os.environ.get("GHOST_ADMIN_API_URL")
        )
        admin_key = (
            self.credentials.get("admin_key") or os.environ.get("GHOST_ADMIN_API_KEY")
        )
        if not api_url or not admin_key:
            _log.warning("ghost image upload: missing api_url or admin_key")
            return None

        try:
            token = _build_jwt(admin_key)
        except Exception as exc:
            _log.warning("ghost image upload jwt error: %s", exc)
            return None

        base = api_url.rstrip("/")
        if "/ghost/api/admin" not in base:
            base = f"{base}/ghost/api/admin"
        endpoint = f"{base}/images/upload/"

        headers = {"Authorization": f"Ghost {token}"}

        try:
            with open(path, "rb") as fh:
                files = {"file": (basename(path), fh, "application/octet-stream")}
                data = {"purpose": purpose or "image"}
                resp = requests.post(
                    endpoint, headers=headers, files=files, data=data, timeout=30
                )
        except requests.exceptions.RequestException as exc:
            _log.warning("ghost image upload network error for %s: %s", path, exc)
            return None
        except OSError as exc:
            _log.warning("ghost image upload read error for %s: %s", path, exc)
            return None

        # Be nice to Ghost's rate limiter regardless of success.
        time.sleep(0.3)

        if resp.status_code not in (200, 201):
            _log.warning(
                "ghost image upload %s returned %s: %s",
                path,
                resp.status_code,
                resp.text[:300],
            )
            return None

        try:
            payload = resp.json()
        except ValueError:
            _log.warning("ghost image upload: non-JSON response")
            return None

        images = payload.get("images") or []
        if images and isinstance(images, list):
            url = images[0].get("url")
            if isinstance(url, str) and url:
                return url
        # Older Ghost variants return {"url": "..."} at the top level.
        url = payload.get("url")
        if isinstance(url, str) and url:
            return url
        _log.warning("ghost image upload: unexpected response shape: %s", payload)
        return None

    def _rewrite_inline_images(self, html: str) -> str:
        """Swap local <img src="..."> values for Ghost CDN URLs.

        Non-local srcs (http(s), data:, protocol-relative) pass through
        unchanged. Local srcs that fail to upload are left as-is so Ghost's
        API surfaces a clear error rather than silently breaking.
        """
        if not html:
            return html

        def _replace(match: re.Match[str]) -> str:
            prefix, quote, src, suffix = match.groups()
            if not self._is_local_path(src):
                return match.group(0)
            cdn = self._upload_image(src, purpose="image")
            if not cdn:
                _log.warning("inline image upload failed, leaving original src: %s", src)
                return match.group(0)
            return f"{prefix}{quote}{cdn}{quote}{suffix}"

        return _IMG_SRC_RE.sub(_replace, html)

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, version: PlatformVersion) -> PublishResult:
        if self._is_mock_mode():
            _log.info("MOCK_LLM=true — short-circuiting ghost publish")
            # Even in mock mode, exercise the image-swap path so callers can
            # see CDN URLs substituted into metadata/html if they inspect the
            # PlatformVersion after publish. We mutate a local copy only.
            metadata = dict(version.metadata or {})
            if metadata.get("feature_image") and self._is_local_path(
                metadata["feature_image"]
            ):
                cdn = self._upload_image(metadata["feature_image"], purpose="image")
                if cdn:
                    metadata["feature_image"] = cdn
            # Rewrite inline images in a mocked html_body too, but discard —
            # the mock publish result doesn't carry html. This is here so the
            # code path is exercised under tests.
            _ = self._rewrite_inline_images(_markdown_to_html(version.content))
            return await self._mock_publish(version)

        import requests

        api_url = (
            self.credentials.get("api_url") or os.environ.get("GHOST_ADMIN_API_URL")
        )
        admin_key = (
            self.credentials.get("admin_key") or os.environ.get("GHOST_ADMIN_API_KEY")
        )
        if not api_url or not admin_key:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason="missing GHOST_ADMIN_API_URL or GHOST_ADMIN_API_KEY",
            )

        try:
            token = _build_jwt(admin_key)
        except Exception as exc:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=f"ghost jwt error: {exc}",
            )

        metadata = dict(version.metadata or {})
        html_body = _markdown_to_html(version.content)
        tag_names = metadata.get("tags") or []

        # Swap local paths for CDN URLs before POSTing the post.
        if metadata.get("feature_image") and self._is_local_path(
            metadata["feature_image"]
        ):
            cdn = self._upload_image(metadata["feature_image"], purpose="image")
            if cdn:
                metadata["feature_image"] = cdn
            else:
                # Keep original; Ghost API will reject cleanly, which
                # surfaces the real error rather than silently succeeding.
                _log.warning("feature_image upload failed, leaving local path")

        # Rewrite inline <img src="/local/..."> in html_body.
        html_body = self._rewrite_inline_images(html_body)

        status = os.environ.get("GHOST_STATUS", "published").lower()
        if status not in {"published", "draft", "scheduled"}:
            status = "published"

        post: dict[str, Any] = {
            "title": metadata.get("title", ""),
            "html": html_body,
            "status": status,
            "tags": [{"name": t} for t in tag_names],
        }
        if metadata.get("meta_description"):
            post["meta_description"] = metadata["meta_description"]
        if metadata.get("feature_image"):
            post["feature_image"] = metadata["feature_image"]
        if metadata.get("canonical_url"):
            post["canonical_url"] = metadata["canonical_url"]

        body = {"posts": [post]}

        # Ghost Admin API requires the /ghost/api/admin/ prefix. Accept both a
        # bare site URL (https://site.ghost.io) and a fully-qualified admin
        # base (https://site.ghost.io/ghost/api/admin/).
        base = api_url.rstrip("/")
        if "/ghost/api/admin" not in base:
            base = f"{base}/ghost/api/admin"
        endpoint = f"{base}/posts/?source=html"

        headers = {
            "Authorization": f"Ghost {token}",
            "Content-Type": "application/json",
        }

        resp = requests.post(endpoint, headers=headers, json=body, timeout=30)
        if resp.status_code != 201:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=f"Ghost API {resp.status_code}: {resp.text[:300]}",
            )

        out = resp.json()["posts"][0]
        return PublishResult(
            platform=self.platform_name,
            status="success",
            published_url=out.get("url"),
            platform_post_id=out.get("id"),
            published_at=datetime.now(),
        )

    def rollback(self, platform_post_id: str) -> tuple[bool, str | None]:
        """DELETE the post from Ghost. Returns ``(ok, failure_reason)``."""
        if self._is_mock_mode():
            _log.info("MOCK_LLM=true — pretending to delete ghost post %s", platform_post_id)
            return True, None

        import requests

        api_url = (
            self.credentials.get("api_url") or os.environ.get("GHOST_ADMIN_API_URL")
        )
        admin_key = (
            self.credentials.get("admin_key") or os.environ.get("GHOST_ADMIN_API_KEY")
        )
        if not api_url or not admin_key:
            return False, "missing GHOST_ADMIN_API_URL or GHOST_ADMIN_API_KEY"

        try:
            token = _build_jwt(admin_key)
        except Exception as exc:
            return False, f"ghost jwt error: {exc}"

        base = api_url.rstrip("/")
        if "/ghost/api/admin" not in base:
            base = f"{base}/ghost/api/admin"
        endpoint = f"{base}/posts/{platform_post_id}/"
        headers = {"Authorization": f"Ghost {token}"}

        try:
            resp = requests.delete(endpoint, headers=headers, timeout=30)
        except requests.exceptions.RequestException as exc:
            return False, f"Ghost DELETE network error: {exc}"

        # Ghost returns 204 No Content on successful delete.
        if resp.status_code not in (200, 204):
            return False, f"Ghost DELETE {resp.status_code}: {resp.text[:300]}"
        return True, None
