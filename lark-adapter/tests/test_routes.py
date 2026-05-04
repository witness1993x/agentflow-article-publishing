"""End-to-end route tests via FastAPI TestClient."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from lark_adapter import config


def _sign(token: str, ts: str, nonce: str, body: bytes) -> str:
    return hmac.new(
        token.encode(), ts.encode() + nonce.encode() + body, hashlib.sha256
    ).hexdigest()


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "secret_test")
    monkeypatch.setenv("LARK_VERIFICATION_TOKEN", "verif_test")
    monkeypatch.delenv("LARK_ENCRYPT_KEY", raising=False)
    config.reset_settings_cache()
    from lark_adapter.app import create_app

    return TestClient(create_app())


class TestHealthz:
    def test_returns_status(self, app_client: TestClient) -> None:
        r = app_client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["lark_app_id_present"] is True
        # bridge stub: agent_review module not installed in tests
        assert body["callback_bridge_loaded"] is False


class TestEventChallenge:
    def test_challenge_handshake_returns_value(self, app_client: TestClient) -> None:
        body = {"type": "url_verification", "challenge": "abc123", "token": "verif_test"}
        raw = json.dumps(body).encode()
        ts, nonce = "1700000000", "nx"
        sig = _sign("verif_test", ts, nonce, raw)
        r = app_client.post(
            "/lark/event",
            content=raw,
            headers={
                "X-Lark-Signature": sig,
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Request-Nonce": nonce,
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {"challenge": "abc123"}

    def test_missing_signature_returns_401(self, app_client: TestClient) -> None:
        body = {"type": "url_verification", "challenge": "abc123"}
        r = app_client.post("/lark/event", json=body)
        assert r.status_code == 401

    def test_bad_signature_returns_401(self, app_client: TestClient) -> None:
        body = {"type": "url_verification", "challenge": "abc"}
        raw = json.dumps(body).encode()
        r = app_client.post(
            "/lark/event",
            content=raw,
            headers={
                "X-Lark-Signature": "deadbeef",
                "X-Lark-Request-Timestamp": "1",
                "X-Lark-Request-Nonce": "n",
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 401


class TestCardAction:
    def test_valid_card_action_reaches_bridge_stub(self, app_client: TestClient) -> None:
        body = {
            "open_id": "ou_test_user",
            "user_name": "Tester",
            "chat_id": "oc_chat_1",
            "message_id": "om_msg_1",
            "action": {
                "tag": "button",
                "value": {"article_id": "a-42", "action": "approve_b", "v": 1},
            },
        }
        raw = json.dumps(body).encode()
        ts, nonce = "1700000000", "nx"
        sig = _sign("verif_test", ts, nonce, raw)
        r = app_client.post(
            "/lark/card",
            content=raw,
            headers={
                "X-Lark-Signature": sig,
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Request-Nonce": nonce,
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200
        # Stub returns ack=True, no reply_card/reply_text -> empty ack body.
        assert r.json() == {}

    def test_card_missing_signature_returns_401(self, app_client: TestClient) -> None:
        r = app_client.post("/lark/card", json={"action": {"value": {}}})
        assert r.status_code == 401

    def test_card_action_value_as_json_string(self, app_client: TestClient) -> None:
        # Lark sometimes sends action.value as a JSON-encoded string.
        body = {
            "open_id": "ou_x",
            "action": {"value": json.dumps({"article_id": "a-9", "action": "refill"})},
        }
        raw = json.dumps(body).encode()
        ts, nonce = "ts", "nn"
        sig = _sign("verif_test", ts, nonce, raw)
        r = app_client.post(
            "/lark/card",
            content=raw,
            headers={
                "X-Lark-Signature": sig,
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Request-Nonce": nonce,
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200
