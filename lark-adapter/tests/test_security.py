"""Tests for HMAC signature verification and AES-CBC body decryption."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from Crypto.Cipher import AES

from lark_adapter.security import DecryptError, decrypt_body, verify_signature


def _build_sig(token: str, ts: str, nonce: str, body: bytes) -> str:
    msg = ts.encode() + nonce.encode() + body
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


class TestVerifySignature:
    def test_known_good_vector(self) -> None:
        token = "v_token_123"
        ts = "1700000000"
        nonce = "n0nce"
        body = b'{"hello":"world"}'
        sig = _build_sig(token, ts, nonce, body)
        assert verify_signature(token, ts, nonce, body, sig) is True

    def test_tampered_body_rejected(self) -> None:
        token = "v_token_123"
        ts = "1700000000"
        nonce = "n0nce"
        body = b'{"hello":"world"}'
        sig = _build_sig(token, ts, nonce, body)
        tampered = b'{"hello":"WORLD"}'
        assert verify_signature(token, ts, nonce, tampered, sig) is False

    def test_empty_signature_rejected(self) -> None:
        assert verify_signature("t", "1", "n", b"x", "") is False

    def test_uppercase_signature_accepted(self) -> None:
        token = "tk"
        ts = "1"
        nonce = "n"
        body = b"abc"
        sig = _build_sig(token, ts, nonce, body)
        assert verify_signature(token, ts, nonce, body, sig.upper()) is True


def _encrypt(plaintext: bytes, encrypt_key: str, iv: bytes) -> str:
    """Mirror Lark's AES-CBC encryption for the round-trip test."""
    key = hashlib.sha256(encrypt_key.encode()).digest()
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(iv + cipher.encrypt(padded)).decode("ascii")


class TestDecryptBody:
    def test_round_trip_fixed_vector(self) -> None:
        key = "test-encrypt-key"
        iv = bytes(range(16))  # deterministic 0..15
        plain = b'{"challenge":"abc","type":"url_verification"}'
        ct_b64 = _encrypt(plain, key, iv)
        assert decrypt_body(key, ct_b64) == plain

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(DecryptError):
            decrypt_body("k", "not-base64!!!")

    def test_too_short_raises(self) -> None:
        with pytest.raises(DecryptError):
            decrypt_body("k", base64.b64encode(b"short").decode())

    def test_empty_key_raises(self) -> None:
        with pytest.raises(DecryptError):
            decrypt_body("", "anything")
