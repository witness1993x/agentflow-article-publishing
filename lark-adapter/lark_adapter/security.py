"""Lark signature verification + AES-CBC payload decryption."""

from __future__ import annotations

import base64
import hashlib
import hmac

from Crypto.Cipher import AES


class DecryptError(ValueError):
    """Raised when AES-CBC body decryption fails."""


def verify_signature(
    token: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    header_sig: str,
) -> bool:
    """Verify Lark's HMAC-SHA256 signature.

    Lark builds the signature as HMAC_SHA256(key=token,
    msg=timestamp + nonce + body), hex-encoded. Returns True on match.
    Empty/None header_sig always returns False.
    """
    if not header_sig:
        return False
    msg = (timestamp or "").encode("utf-8") + (nonce or "").encode("utf-8") + (body or b"")
    digest = hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header_sig.strip().lower())


def decrypt_body(encrypt_key: str, encrypted_b64: str) -> bytes:
    """Decrypt a Lark-encrypted event body.

    Lark encrypts payloads with AES-CBC where:
      - key = SHA256(encrypt_key) (full 32 bytes)
      - iv  = first 16 bytes of the decoded ciphertext
      - body = remaining bytes, PKCS#7-padded
    """
    if not encrypt_key:
        raise DecryptError("encrypt_key is empty")
    try:
        raw = base64.b64decode(encrypted_b64)
    except (ValueError, TypeError) as exc:
        raise DecryptError(f"invalid base64: {exc}") from exc
    if len(raw) < 32 or len(raw) % 16 != 0:
        raise DecryptError("ciphertext too short or not block-aligned")

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv, ct = raw[:16], raw[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = cipher.decrypt(ct)
    # Strip PKCS#7 padding manually so a bad padding doesn't leak via Crypto's unpad.
    pad_len = padded[-1] if padded else 0
    if pad_len < 1 or pad_len > 16 or padded[-pad_len:] != bytes([pad_len]) * pad_len:
        raise DecryptError("invalid PKCS#7 padding")
    return padded[:-pad_len]
