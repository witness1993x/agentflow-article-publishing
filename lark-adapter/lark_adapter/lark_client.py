"""Thin async client for Lark/Feishu open-platform APIs (skeleton)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://open.larksuite.com/open-apis"
_TOKEN_PATH = "/auth/v3/tenant_access_token/internal"
_SEND_MESSAGE_PATH = "/im/v1/messages?receive_id_type=chat_id"


class LarkAPIError(RuntimeError):
    """Raised when a Lark API call returns non-2xx or non-zero `code`."""

    def __init__(self, status: int, body: str):
        truncated = body[:512]
        super().__init__(f"Lark API error status={status} body={truncated!r}")
        self.status = status
        self.body = truncated


class LarkClient:
    """Async client. One instance per app credential pair, safe for concurrent use."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str = _BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def get_tenant_access_token(self) -> str:
        """Return a cached tenant_access_token, refreshing at 90% of expiry."""
        async with self._lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at:
                return self._token
            client = await self._get_client()
            payload = {"app_id": self._app_id, "app_secret": self._app_secret}
            resp = await client.post(f"{self._base_url}{_TOKEN_PATH}", json=payload)
            if resp.status_code // 100 != 2:
                raise LarkAPIError(resp.status_code, resp.text)
            data = resp.json()
            if data.get("code") != 0:
                raise LarkAPIError(resp.status_code, resp.text)
            token = data.get("tenant_access_token")
            if not token:
                raise LarkAPIError(resp.status_code, "missing tenant_access_token")
            self._token = token
            self._token_expires_at = now + int(data.get("expire", 7200)) * 0.9
            return token

    async def _send(self, chat_id: str, msg_type: str, content: str) -> dict[str, Any]:
        token = await self.get_tenant_access_token()
        client = await self._get_client()
        body = {"receive_id": chat_id, "msg_type": msg_type, "content": content}
        resp = await client.post(
            f"{self._base_url}{_SEND_MESSAGE_PATH}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        if resp.status_code // 100 != 2:
            raise LarkAPIError(resp.status_code, resp.text)
        data = resp.json()
        if data.get("code") not in (0, None):
            raise LarkAPIError(resp.status_code, resp.text)
        return data

    async def send_card(self, chat_id: str, card_payload: dict[str, Any]) -> dict[str, Any]:
        return await self._send(chat_id, "interactive", json.dumps(card_payload))

    async def send_text(self, chat_id: str, text: str) -> dict[str, Any]:
        return await self._send(chat_id, "text", json.dumps({"text": text}))
