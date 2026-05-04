"""Environment-driven settings for the Lark adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Settings:
    """Adapter settings loaded from environment variables."""

    lark_app_id: str
    lark_app_secret: str
    lark_verification_token: str
    lark_encrypt_key: str | None
    bind_host: str
    bind_port: int
    log_level: str

    @property
    def encrypt_enabled(self) -> bool:
        return bool(self.lark_encrypt_key)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required env var: {name}")
    return value


def _parse_bind(raw: str) -> tuple[str, int]:
    if ":" not in raw:
        raise ConfigError(f"LARK_ADAPTER_BIND must be host:port, got {raw!r}")
    host, _, port_s = raw.rpartition(":")
    try:
        port = int(port_s)
    except ValueError as exc:
        raise ConfigError(f"Invalid port in LARK_ADAPTER_BIND: {port_s!r}") from exc
    return host or "0.0.0.0", port


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Raises ConfigError if required vars missing."""
    bind_raw = os.environ.get("LARK_ADAPTER_BIND", "0.0.0.0:8765")
    host, port = _parse_bind(bind_raw)
    return Settings(
        lark_app_id=_require("LARK_APP_ID"),
        lark_app_secret=_require("LARK_APP_SECRET"),
        lark_verification_token=_require("LARK_VERIFICATION_TOKEN"),
        lark_encrypt_key=os.environ.get("LARK_ENCRYPT_KEY") or None,
        bind_host=host,
        bind_port=port,
        log_level=os.environ.get("LARK_ADAPTER_LOG_LEVEL", "INFO").upper(),
    )


def reset_settings_cache() -> None:
    """Test helper — clears the lru_cache so env changes take effect."""
    get_settings.cache_clear()
