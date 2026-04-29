"""Generate draft images via AtlasCloud relay -> GPT Image 2."""

from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from agentflow.agent_d2.main import load_draft, save_draft
from agentflow.shared.bootstrap import agentflow_home

_DEFAULT_BASE_URL = os.environ.get("ATLASCLOUD_BASE_URL", "https://api.atlascloud.ai")
_DEFAULT_MODEL = os.environ.get(
    "ATLASCLOUD_IMAGE_MODEL",
    "openai/gpt-image-2-developer/text-to-image",
)
_DEFAULT_SIZE = os.environ.get("ATLASCLOUD_IMAGE_SIZE", "16:9")
_DEFAULT_RESOLUTION = os.environ.get("ATLASCLOUD_IMAGE_RESOLUTION", "2k")
_DEFAULT_POLL_SECONDS = float(os.environ.get("ATLASCLOUD_POLL_INTERVAL_SECONDS", "3"))
_DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("ATLASCLOUD_TIMEOUT_SECONDS", "180"))
_DEFAULT_HTTP_RETRIES = int(os.environ.get("ATLASCLOUD_HTTP_RETRIES", "5"))
_DEFAULT_HTTP_RETRY_BASE = float(os.environ.get("ATLASCLOUD_HTTP_RETRY_BASE_SECONDS", "1.5"))

_RETRYABLE_NETWORK_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _request_with_retry(
    method: str,
    url: str,
    *,
    retries: int | None = None,
    **kwargs: Any,
) -> requests.Response:
    # Dispatch to requests.post / requests.get directly so existing test
    # mocks that patch ``image_generator.requests.post`` / ``.get`` keep
    # intercepting.
    method_upper = method.upper()
    fn = {
        "GET": requests.get,
        "POST": requests.post,
        "PUT": requests.put,
        "DELETE": requests.delete,
        "PATCH": requests.patch,
    }.get(method_upper, requests.request)
    attempts = _DEFAULT_HTTP_RETRIES if retries is None else retries
    last_err: BaseException | None = None
    for i in range(attempts + 1):
        try:
            if fn is requests.request:
                return fn(method_upper, url, **kwargs)
            return fn(url, **kwargs)
        except _RETRYABLE_NETWORK_ERRORS as err:
            last_err = err
            if i >= attempts:
                break
            wait = _DEFAULT_HTTP_RETRY_BASE * (2 ** i) + random.uniform(0, 0.5)
            try:
                from agentflow.shared.logger import get_logger
                get_logger("agent_d2.image_generator").warning(
                    "AtlasCloud %s %s transient %s; retry %d/%d in %.1fs",
                    method.upper(), url.split("?", 1)[0][-80:],
                    type(err).__name__, i + 1, attempts, wait,
                )
            except Exception:
                pass
            time.sleep(wait)
    assert last_err is not None
    raise last_err

_STYLE_HINTS = {
    "editorial": (
        "Create a clean editorial illustration for a professional Medium article. "
        "Use a modern infrastructure aesthetic, dark or neutral background, blue-green accents, "
        "clear composition, and minimal text."
    ),
    "diagram": (
        "Create a clean conceptual diagram-style illustration for a technical article. "
        "Prioritize information structure, flowing data paths, system components, and simple shapes. "
        "No UI screenshots."
    ),
    "cover": (
        "Create a striking Medium cover image with strong hierarchy and a polished infrastructure-tech aesthetic. "
        "The image should feel premium, modern, and suitable as a hero visual."
    ),
}

_NEGATIVE_HINT = (
    "Avoid coins, rockets, cartoon robots, meme aesthetics, generic blockchain icons, exchange candlestick screenshots, "
    "watermarks, dense text, or low-quality infographic styling."
)


def _api_key() -> str:
    key = os.environ.get("ATLASCLOUD_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ATLASCLOUD_API_KEY is not set.")
    return key


def _headers() -> dict[str, str]:
    key = _api_key()
    return {
        "Authorization": f"Bearer {key}",
        "x-api-key": key,
        "Content-Type": "application/json",
    }


def _generated_dir(article_id: str) -> Path:
    raw = os.environ.get("AGENTFLOW_GENERATED_IMAGE_DIR", "").strip()
    if raw:
        base = Path(raw).expanduser()
        return base / article_id
    return agentflow_home() / "drafts" / article_id / "generated-images"


def _ext_from_url_or_type(url: str, content_type: str | None) -> str:
    if content_type:
        lowered = content_type.lower()
        if "png" in lowered:
            return ".png"
        if "jpeg" in lowered or "jpg" in lowered:
            return ".jpg"
        if "webp" in lowered:
            return ".webp"
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".webp"} else ".png"


def _domain_hint() -> str:
    """Resolve a domain-flavor hint for the image prompt.

    Resolution order:
      1. Active publisher_account.image_prompt_hints (from current intent's
         topic profile; accepts ``list[str]`` or a single ``str``) — lets each
         brand inject their own visual vocabulary without forking the framework.
      2. Env var ``AGENTFLOW_IMAGE_DOMAIN_HINT`` — useful for one-off runs.
      3. A neutral generic fallback.

    The resolved hint goes into the generation prompt as
    ``Visual concepts to emphasize: …``.
    """
    try:
        from agentflow.shared.memory import load_current_intent
        from agentflow.shared.topic_profiles import (
            resolve_publisher_account_from_intent,
        )

        publisher = resolve_publisher_account_from_intent(load_current_intent())
        hints = publisher.get("image_prompt_hints") if publisher else None
        if isinstance(hints, list) and hints:
            return ", ".join(str(h).strip() for h in hints if h)
        if isinstance(hints, str) and hints.strip():
            return hints.strip()
    except Exception:
        pass
    env_hint = os.environ.get("AGENTFLOW_IMAGE_DOMAIN_HINT", "").strip()
    if env_hint:
        return env_hint
    # Generic neutral fallback — no domain bias.
    return (
        "modern editorial composition, clean information hierarchy, "
        "production-grade aesthetics, technical clarity"
    )


def _build_prompt(
    *,
    article_title: str,
    placeholder_description: str,
    section_heading: str,
    style: str,
) -> str:
    style_hint = _STYLE_HINTS.get(style, _STYLE_HINTS["editorial"])
    return (
        f"{style_hint}\n\n"
        f"Article title: {article_title}\n"
        f"Section heading: {section_heading or 'N/A'}\n"
        f"Image purpose: {placeholder_description}\n\n"
        f"Visual concepts to emphasize: {_domain_hint()}.\n"
        f"{_NEGATIVE_HINT}"
    )


def _request_generation(
    *,
    prompt: str,
    size: str,
    resolution: str,
) -> dict[str, Any]:
    resp = _request_with_retry(
        "POST",
        f"{_DEFAULT_BASE_URL.rstrip('/')}/api/v1/model/generateImage",
        headers=_headers(),
        json={
            "model": _DEFAULT_MODEL,
            "prompt": prompt,
            "size": size,
            "resolution": resolution,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    payload = data.get("data") or {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"AtlasCloud returned malformed payload: {data}")
    return payload


def _poll_prediction(
    request_id: str,
    *,
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()
    url = f"{_DEFAULT_BASE_URL.rstrip('/')}/api/v1/model/prediction/{request_id}"
    last_payload: dict[str, Any] | None = None
    while True:
        resp = _request_with_retry("GET", url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json() or {}
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            raise RuntimeError(f"AtlasCloud returned malformed prediction payload: {data}")
        last_payload = payload
        status = str(payload.get("status") or "").lower()
        if status in {"completed", "succeeded", "success"}:
            return payload
        if status in {"failed", "error", "unknown", "cancelled"}:
            raise RuntimeError(payload.get("error") or payload.get("status") or "image generation failed")
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError(f"AtlasCloud polling timed out after {timeout_seconds:.0f}s for request {request_id}")
        time.sleep(poll_seconds)


def _download_image(url: str, target_path: Path) -> Path:
    resp = _request_with_retry("GET", url, timeout=60)
    resp.raise_for_status()
    ext = _ext_from_url_or_type(url, resp.headers.get("Content-Type"))
    final_path = target_path.with_suffix(ext)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(resp.content)
    return final_path


def _brand_overlay_config() -> dict[str, Any] | None:
    """Read brand overlay config from preferences.yaml. None = disabled."""
    try:
        from agentflow.shared import preferences as _prefs

        prefs = _prefs.load() or {}
    except Exception:
        return None
    cfg = (
        ((prefs.get("image_generation") or {}).get("brand_overlay")) or {}
    )
    if not cfg.get("enabled") or not cfg.get("logo_path"):
        return None
    return cfg


def generate_images(
    article_id: str,
    *,
    only_placeholder_id: str | None = None,
    size: str = _DEFAULT_SIZE,
    resolution: str = _DEFAULT_RESOLUTION,
    style: str = "editorial",
    skip_body: bool = False,
    skip_cover: bool = False,
) -> dict[str, Any]:
    draft = load_draft(article_id)
    output_dir = _generated_dir(article_id).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    overlay_cfg = _brand_overlay_config()
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for idx, placeholder in enumerate(draft.image_placeholders, start=1):
        if only_placeholder_id and placeholder.id != only_placeholder_id:
            continue
        if placeholder.role == "body" and skip_body:
            skipped.append(
                {
                    "placeholder_id": placeholder.id,
                    "reason": "skip_body_mode",
                }
            )
            continue
        if placeholder.role == "cover" and skip_cover:
            skipped.append(
                {
                    "placeholder_id": placeholder.id,
                    "reason": "skip_cover_mode",
                }
            )
            continue
        if placeholder.resolved_path:
            skipped.append(
                {
                    "placeholder_id": placeholder.id,
                    "reason": "already_resolved",
                    "resolved_path": placeholder.resolved_path,
                }
            )
            continue

        prompt = _build_prompt(
            article_title=draft.title,
            placeholder_description=placeholder.description,
            section_heading=placeholder.section_heading,
            style=style,
        )
        created = _request_generation(prompt=prompt, size=size, resolution=resolution)
        request_id = str(created.get("id") or "").strip()
        if not request_id:
            raise RuntimeError(f"AtlasCloud did not return request id: {created}")
        completed = _poll_prediction(request_id)
        outputs = completed.get("outputs") or []
        if not isinstance(outputs, list) or not outputs:
            raise RuntimeError(f"AtlasCloud completed without image outputs for {request_id}")
        image_url = str(outputs[0])
        file_stub = output_dir / f"{idx:02d}_{placeholder.id}"
        saved_path = _download_image(image_url, file_stub)

        # Cover-role images get the brand overlay if prefs enable it.
        overlay_applied = False
        if placeholder.role == "cover" and overlay_cfg:
            try:
                from agentflow.agent_d2 import brand_overlay

                brand_overlay.apply_overlay(saved_path, overlay_cfg)
                overlay_applied = True
            except Exception as err:  # pragma: no cover - surface but don't abort
                _log_overlay_failure(saved_path, err)

        placeholder.resolved_path = str(saved_path)
        generated.append(
            {
                "placeholder_id": placeholder.id,
                "request_id": request_id,
                "status": completed.get("status"),
                "image_url": image_url,
                "saved_path": str(saved_path),
                "section_heading": placeholder.section_heading,
                "description": placeholder.description,
                "role": placeholder.role,
                "brand_overlay_applied": overlay_applied,
            }
        )

    if generated:
        save_draft(draft)

    remaining_unresolved = sum(1 for p in draft.image_placeholders if not p.resolved_path)
    return {
        "article_id": article_id,
        "output_dir": str(output_dir),
        "style": style,
        "size": size,
        "resolution": resolution,
        "skip_body": skip_body,
        "skip_cover": skip_cover,
        "generated_count": len(generated),
        "remaining_unresolved_count": remaining_unresolved,
        "generated": generated,
        "skipped": skipped,
    }


def _log_overlay_failure(path: Path, err: Exception) -> None:
    try:
        from agentflow.shared.logger import get_logger

        get_logger("agent_d2.image_generator").warning(
            "brand overlay failed for %s: %s", path, err
        )
    except Exception:
        pass
