"""Thin Telegram Bot API client.

Only the calls we actually use:
- send_message / send_photo / send_document
- answer_callback_query / edit_message_reply_markup
- get_updates (long-poll)
- get_me
- set_my_commands / set_chat_menu_button

No third-party SDK — keeps the dependency footprint tiny and the call surface
explicit. The HTTP layer is ``requests``, already used by the image generator.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests


_DEFAULT_BASE = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """Raised when the Telegram API returns ok=false or non-2xx."""


def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set in env")
    return token


def _api(method: str) -> str:
    return f"{_DEFAULT_BASE}/bot{_token()}/{method}"


def _post(method: str, payload: dict[str, Any], *, timeout: float = 30) -> dict[str, Any]:
    resp = requests.post(_api(method), json=payload, timeout=timeout)
    return _unwrap(resp, method)


def _post_multipart(
    method: str,
    data: dict[str, Any],
    files: dict[str, Any],
    *,
    timeout: float = 60,
) -> dict[str, Any]:
    resp = requests.post(_api(method), data=data, files=files, timeout=timeout)
    return _unwrap(resp, method)


def _unwrap(resp: requests.Response, method: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        raise TelegramError(f"{method}: non-JSON response (status={resp.status_code})")
    if not body.get("ok"):
        raise TelegramError(
            f"{method}: {body.get('description') or 'unknown error'} "
            f"(error_code={body.get('error_code')})"
        )
    return body.get("result") or {}


def get_me() -> dict[str, Any]:
    return _post("getMe", {})


def set_my_commands(commands: list[dict[str, str]]) -> dict[str, Any]:
    return _post("setMyCommands", {"commands": commands})


def set_chat_menu_button(
    *,
    chat_id: int | str | None = None,
    menu_button: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"menu_button": menu_button or {"type": "commands"}}
    if chat_id is not None:
        payload["chat_id"] = chat_id
    return _post("setChatMenuButton", payload)


def send_message(
    chat_id: int | str,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "MarkdownV2",
    disable_web_page_preview: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _post("sendMessage", payload)


_PHOTO_OPTIMIZE_THRESHOLD_BYTES = 1_000_000   # 1 MB
_PHOTO_OPTIMIZE_LONG_EDGE_PX   = 1600
_PHOTO_OPTIMIZE_JPEG_QUALITY   = 85


def _optimize_photo_for_telegram(photo_path: Path) -> Path:
    """Return a path safe to send to Telegram's sendPhoto endpoint.

    Telegram's photo cap is technically 10MB but in practice large PNGs
    (esp. cover assets > 1MB from the Atlas image generator) hit upload
    timeouts on flaky links. v1.0.16: when the source is over
    ``_PHOTO_OPTIMIZE_THRESHOLD_BYTES``, transcode to JPEG, downscale to
    1600px long edge, and write a sibling ``*_tgopt.jpg``. Source file is
    untouched. Falls back to the original path if Pillow is unavailable
    or the optimize step itself raises.
    """
    try:
        size = photo_path.stat().st_size
    except OSError:
        return photo_path
    if size <= _PHOTO_OPTIMIZE_THRESHOLD_BYTES:
        return photo_path
    try:
        from PIL import Image
    except ImportError:
        return photo_path
    try:
        with Image.open(photo_path) as im:
            im = im.convert("RGB") if im.mode != "RGB" else im
            w, h = im.size
            long_edge = max(w, h)
            if long_edge > _PHOTO_OPTIMIZE_LONG_EDGE_PX:
                ratio = _PHOTO_OPTIMIZE_LONG_EDGE_PX / long_edge
                im = im.resize(
                    (int(w * ratio), int(h * ratio)),
                    Image.LANCZOS,
                )
            out_path = photo_path.with_name(photo_path.stem + "_tgopt.jpg")
            im.save(
                out_path, "JPEG",
                quality=_PHOTO_OPTIMIZE_JPEG_QUALITY,
                optimize=True,
            )
        return out_path
    except Exception:
        return photo_path


def send_photo(
    chat_id: int | str,
    photo_path: Path | str,
    *,
    caption: str | None = None,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "MarkdownV2",
) -> dict[str, Any]:
    """Send a photo to Telegram.

    v1.0.16: large source images (> ~1MB) are transcoded to JPEG +
    resized before upload to avoid sendPhoto timeouts. Upload-layer
    failures (Timeout / ConnectionError / 413 entity too large) fall
    back to ``send_document`` with the original file so the operator
    still gets the asset; the Gate flow can decide what to do next.
    """
    photo_path = Path(photo_path)
    if not photo_path.exists():
        raise TelegramError(f"send_photo: file not found {photo_path}")
    upload_path = _optimize_photo_for_telegram(photo_path)
    data: dict[str, Any] = {"chat_id": chat_id}
    if parse_mode is not None:
        data["parse_mode"] = parse_mode
    if caption is not None:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        with upload_path.open("rb") as fh:
            return _post_multipart("sendPhoto", data, {"photo": fh})
    except (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ) as upload_err:
        # Upload-layer failure (network timeout, RST). Fall back to
        # send_document with the ORIGINAL file so operators still see
        # the asset; sendDocument tolerates larger payloads and isn't
        # subject to the same image-pipeline timeouts.
        try:
            return send_document(
                chat_id, photo_path,
                caption=(caption or "")
                + "\n\n_(image upload fell back to file: "
                + str(upload_err)[:80] + ")_",
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except Exception:
            raise upload_err


def send_document(
    chat_id: int | str,
    doc_path: Path | str,
    *,
    caption: str | None = None,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "MarkdownV2",
) -> dict[str, Any]:
    doc_path = Path(doc_path)
    if not doc_path.exists():
        raise TelegramError(f"send_document: file not found {doc_path}")
    data: dict[str, Any] = {"chat_id": chat_id}
    if parse_mode is not None:
        data["parse_mode"] = parse_mode
    if caption is not None:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    with doc_path.open("rb") as fh:
        return _post_multipart("sendDocument", data, {"document": fh})


def answer_callback_query(
    callback_query_id: str,
    *,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    payload["show_alert"] = show_alert
    _post("answerCallbackQuery", payload)


def edit_message_reply_markup(
    chat_id: int | str,
    message_id: int,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _post("editMessageReplyMarkup", payload)


def send_long_text(
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str | None = "MarkdownV2",
    chunk_size: int = 4000,
    disable_web_page_preview: bool = True,
) -> list[dict[str, Any]]:
    """Send a body of text that may exceed Telegram's 4096-char message cap.

    Splits at paragraph boundaries first, then sentence boundaries, then a
    hard char cut as last resort. Each chunk is sent as a separate message
    with the same parse_mode. Returns the list of API responses (one per
    chunk).

    The caller is responsible for any required Markdown V2 escaping. Pass
    ``parse_mode=None`` if you want the chunks delivered as plain text and
    the markdown chars shown literally (no rendering).
    """
    chunks = _chunk_text(text, max_size=chunk_size)
    out: list[dict[str, Any]] = []
    for chunk in chunks:
        out.append(
            send_message(
                chat_id,
                chunk,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
        )
    return out


def _chunk_text(text: str, *, max_size: int = 4000) -> list[str]:
    text = text or ""
    if len(text) <= max_size:
        return [text]
    chunks: list[str] = []
    remainder = text
    while remainder:
        if len(remainder) <= max_size:
            chunks.append(remainder)
            break
        head = remainder[:max_size]
        # Prefer splitting at the last paragraph break inside head.
        cut = head.rfind("\n\n")
        if cut < max_size // 2:  # too early, fall through
            cut = -1
        if cut == -1:
            # Try the last sentence terminator.
            for term in ("。", "！", "？", ".", "!", "?", "\n"):
                idx = head.rfind(term)
                if idx >= max_size // 2:
                    cut = idx + len(term)
                    break
        if cut == -1:
            cut = max_size  # hard cut
        chunks.append(remainder[:cut].rstrip())
        remainder = remainder[cut:].lstrip()
    return [c for c in chunks if c]


def get_updates(
    *,
    offset: int | None = None,
    timeout: int = 25,
    allowed_updates: list[str] | None = None,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        payload["offset"] = offset
    if allowed_updates is not None:
        payload["allowed_updates"] = allowed_updates
    # network timeout = long-poll + small slack
    return _post("getUpdates", payload, timeout=timeout + 10) or []
