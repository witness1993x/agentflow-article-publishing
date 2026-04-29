"""Credential preflight: per-service health checks + readiness gate.

Used by ``af doctor`` for at-a-glance status, and by command preflight hooks
to fail fast before pipelines spend network / LLM / image budget on a known
broken config.

Each checker returns a :class:`CheckResult`. The result table groups checks
into "critical for X" buckets so callers can ask "is review-daemon
ready?" or "is hotspots ready?" without re-running everything.

Probe results are cached at ``~/.agentflow/review/preflight_cache.json`` for
``_PROBE_CACHE_SECONDS`` (default 1h) so back-to-back command invocations
don't hammer remote APIs.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agentflow.shared.bootstrap import agentflow_home


_PROBE_CACHE_SECONDS = 3600  # 1h


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    env_var: str | None = None       # canonical env var (None for synthetic checks)
    present: bool = False            # is the var set in env?
    valid: bool | None = None        # remote probe result; None = not probed
    message: str = ""                # human readable status
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        if not self.present:
            return False
        if self.valid is False:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Probe cache
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    p = agentflow_home() / "review" / "preflight_cache.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_cache() -> dict[str, Any]:
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_cache(data: dict[str, Any]) -> None:
    _cache_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_get(name: str) -> dict[str, Any] | None:
    cache = _read_cache().get(name)
    if not cache:
        return None
    ts = cache.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    if time.time() - ts > _PROBE_CACHE_SECONDS:
        return None
    return cache


def _cache_put(name: str, valid: bool, message: str, extra: dict[str, Any] | None = None) -> None:
    cache = _read_cache()
    cache[name] = {
        "ts": time.time(),
        "valid": valid,
        "message": message,
        "extra": extra or {},
    }
    _write_cache(cache)


def clear_cache() -> None:
    p = _cache_path()
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_present(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return None


def _probe(
    name: str,
    fn: Callable[[], tuple[bool, str, dict[str, Any] | None]],
    *,
    fresh: bool,
) -> tuple[bool, str, dict[str, Any]]:
    """Run a remote probe with cache. ``fresh`` bypasses the cache."""
    if not fresh:
        hit = _cache_get(name)
        if hit:
            return bool(hit["valid"]), str(hit.get("message", "")), dict(hit.get("extra") or {})
    try:
        valid, message, extra = fn()
    except Exception as err:  # pragma: no cover
        valid, message, extra = False, f"probe error: {err}", None
    _cache_put(name, valid, message, extra)
    return valid, message, dict(extra or {})


# ---------------------------------------------------------------------------
# Per-service checkers
# ---------------------------------------------------------------------------


def check_telegram(*, fresh: bool = False) -> CheckResult:
    token = _env_present("TELEGRAM_BOT_TOKEN")
    cr = CheckResult(name="Telegram bot", env_var="TELEGRAM_BOT_TOKEN")
    if not token:
        cr.message = "TELEGRAM_BOT_TOKEN not set"
        return cr
    cr.present = True

    def _probe_fn() -> tuple[bool, str, dict[str, Any] | None]:
        from agentflow.agent_review import tg_client
        me = tg_client.get_me()
        username = me.get("username")
        return True, f"@{username}", {"username": username, "id": me.get("id")}

    valid, msg, extra = _probe("telegram", _probe_fn, fresh=fresh)
    cr.valid = valid
    cr.message = msg
    cr.extra = extra
    return cr


def check_atlas(*, fresh: bool = False) -> CheckResult:
    """Atlas (image gen): we only check token presence — its API needs a real
    inference call to validate, which costs $$$. Trust the token."""
    token = _env_present("ATLASCLOUD_API_KEY")
    cr = CheckResult(name="AtlasCloud (image)", env_var="ATLASCLOUD_API_KEY")
    if not token:
        cr.message = "ATLASCLOUD_API_KEY not set (image generation will fail)"
        return cr
    cr.present = True
    cr.valid = None  # not remotely probed
    cr.message = "token set (skip remote probe to save quota)"
    return cr


def _probe_openai_compat(name: str, env_token: str, base_url: str, fresh: bool) -> CheckResult:
    """Generic OpenAI-compatible probe via /v1/models."""
    cr = CheckResult(name=name, env_var=env_token)
    token = _env_present(env_token)
    if not token:
        cr.message = f"{env_token} not set"
        return cr
    cr.present = True

    def _probe_fn() -> tuple[bool, str, dict[str, Any] | None]:
        import requests
        resp = requests.get(
            base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
                count = len((data.get("data") or []))
                return True, f"valid ({count} models)", {"models": count}
            except Exception:
                return True, "valid", None
        return False, f"HTTP {resp.status_code}: {resp.text[:120]}", None

    valid, msg, extra = _probe(name.replace(" ", "_").lower(), _probe_fn, fresh=fresh)
    cr.valid = valid
    cr.message = msg
    cr.extra = extra
    return cr


def check_moonshot(*, fresh: bool = False) -> CheckResult:
    base = os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
    return _probe_openai_compat("Moonshot Kimi", "MOONSHOT_API_KEY", base, fresh)


def check_anthropic(*, fresh: bool = False) -> CheckResult:
    """Anthropic /v1/models with x-api-key header."""
    cr = CheckResult(name="Anthropic Claude", env_var="ANTHROPIC_API_KEY")
    token = _env_present("ANTHROPIC_API_KEY")
    if not token:
        cr.message = "ANTHROPIC_API_KEY not set"
        return cr
    cr.present = True

    def _probe_fn() -> tuple[bool, str, dict[str, Any] | None]:
        import requests
        resp = requests.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": token, "anthropic-version": "2023-06-01"},
            timeout=10,
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
                count = len((data.get("data") or []))
                return True, f"valid ({count} models)", {"models": count}
            except Exception:
                return True, "valid", None
        return False, f"HTTP {resp.status_code}: {resp.text[:120]}", None

    valid, msg, extra = _probe("anthropic", _probe_fn, fresh=fresh)
    cr.valid = valid
    cr.message = msg
    cr.extra = extra
    return cr


def check_jina(*, fresh: bool = False) -> CheckResult:
    """Jina embeddings — token check via small embedding call."""
    cr = CheckResult(name="Jina embeddings", env_var="JINA_API_KEY")
    token = _env_present("JINA_API_KEY")
    if not token:
        cr.message = "JINA_API_KEY not set"
        return cr
    cr.present = True

    def _probe_fn() -> tuple[bool, str, dict[str, Any] | None]:
        import requests
        resp = requests.post(
            "https://api.jina.ai/v1/embeddings",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"input": ["health"], "model": "jina-embeddings-v3"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "valid", None
        return False, f"HTTP {resp.status_code}: {resp.text[:120]}", None

    valid, msg, extra = _probe("jina", _probe_fn, fresh=fresh)
    cr.valid = valid
    cr.message = msg
    cr.extra = extra
    return cr


def check_openai(*, fresh: bool = False) -> CheckResult:
    return _probe_openai_compat("OpenAI", "OPENAI_API_KEY", "https://api.openai.com/v1", fresh)


def check_twitter(*, fresh: bool = False) -> CheckResult:
    """Twitter v2 /users/me via Bearer token."""
    cr = CheckResult(name="Twitter (read)", env_var="TWITTER_BEARER_TOKEN")
    token = _env_present("TWITTER_BEARER_TOKEN")
    if not token:
        cr.message = "TWITTER_BEARER_TOKEN not set (HN+RSS only)"
        return cr
    cr.present = True
    cr.valid = None
    cr.message = "token set (skip probe — Twitter rate limits are punishing)"
    return cr


def check_ghost(*, fresh: bool = False) -> CheckResult:
    cr = CheckResult(name="Ghost Admin API", env_var="GHOST_ADMIN_API_URL")
    url = _env_present("GHOST_ADMIN_API_URL")
    key = _env_present("GHOST_ADMIN_API_KEY")
    if not url or not key:
        cr.message = "GHOST_ADMIN_{URL,KEY} not set"
        return cr
    cr.present = True
    cr.valid = None
    cr.message = "URL+key set (probe disabled — exercised on first publish)"
    return cr


def check_linkedin(*, fresh: bool = False) -> CheckResult:
    cr = CheckResult(name="LinkedIn", env_var="LINKEDIN_ACCESS_TOKEN")
    token = _env_present("LINKEDIN_ACCESS_TOKEN")
    if not token:
        cr.message = "LINKEDIN_ACCESS_TOKEN not set"
        return cr
    cr.present = True
    cr.valid = None
    cr.message = "token set"
    return cr


def check_medium() -> CheckResult:
    cr = CheckResult(name="Medium API", env_var="MEDIUM_INTEGRATION_TOKEN")
    token = _env_present("MEDIUM_INTEGRATION_TOKEN")
    if not token:
        cr.message = "DEPRECATED — Medium closed API 2025-01-01; use browser-ops package"
    else:
        cr.present = True
        cr.valid = None
        cr.message = "legacy token set"
    return cr


def check_review_chat_id() -> CheckResult:
    """Synthetic check: TG operator chat_id is configured."""
    cr = CheckResult(name="TG operator chat_id", env_var="TELEGRAM_REVIEW_CHAT_ID")
    raw = _env_present("TELEGRAM_REVIEW_CHAT_ID")
    captured = None
    cfg_path = agentflow_home() / "review" / "config.json"
    if cfg_path.exists():
        try:
            captured = json.loads(cfg_path.read_text(encoding="utf-8")).get("review_chat_id")
        except Exception:
            captured = None
    if raw or captured:
        cr.present = True
        cr.valid = True
        cr.message = f"chat_id={raw or captured} ({'env' if raw else 'captured'})"
    else:
        cr.message = "no chat_id — send /start to the bot once to capture"
    return cr


def check_daemon_liveness(stale_seconds: float = 120.0) -> CheckResult:
    """Read ~/.agentflow/review/last_heartbeat.json — fail if missing/stale."""
    cr = CheckResult(name="review-daemon liveness", env_var=None)
    hb_path = agentflow_home() / "review" / "last_heartbeat.json"
    if not hb_path.exists():
        cr.message = "no heartbeat file (run `af review-daemon`)"
        return cr
    try:
        data = json.loads(hb_path.read_text(encoding="utf-8")) or {}
        ts_raw = str(data.get("timestamp") or "")
        beat = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except (json.JSONDecodeError, OSError, ValueError) as err:
        cr.message = f"heartbeat unreadable: {err}"
        return cr
    cr.present = True
    now = datetime.now(timezone.utc)
    age = (now - (beat if beat.tzinfo else beat.replace(tzinfo=timezone.utc))).total_seconds()
    if age > stale_seconds:
        cr.valid = False
        cr.message = f"stale by {age:.0f}s (daemon may be down)"
        return cr
    cr.valid = True
    if age < 60:
        rel = f"{age:.0f}s ago"
    elif age < 3600:
        rel = f"{age / 60:.1f}m ago"
    else:
        rel = f"{age / 3600:.1f}h ago"
    cr.message = f"last beat: {rel}"
    cr.extra = {"age_seconds": age}
    return cr


def check_mock_mode() -> CheckResult:
    """Synthetic: highlight if MOCK_LLM=true so the operator knows what they
    are running."""
    cr = CheckResult(name="MOCK_LLM mode", env_var="MOCK_LLM")
    raw = (os.environ.get("MOCK_LLM") or "").strip().lower()
    cr.present = bool(raw)
    cr.valid = True
    if raw == "true":
        cr.message = "ON — D1/D2/D3 use deterministic fixtures; no real LLM cost"
    else:
        cr.message = "OFF — real LLM calls, watch the budget"
    return cr


# ---------------------------------------------------------------------------
# Aggregations per command
# ---------------------------------------------------------------------------


def all_checks(*, fresh: bool = False) -> list[CheckResult]:
    return [
        check_mock_mode(),
        check_telegram(fresh=fresh),
        check_review_chat_id(),
        check_atlas(),
        check_moonshot(fresh=fresh),
        check_anthropic(fresh=fresh),
        check_jina(fresh=fresh),
        check_openai(fresh=fresh),
        check_twitter(),
        check_ghost(),
        check_linkedin(),
        check_medium(),
        check_daemon_liveness(),
    ]


def critical_for_review_daemon(*, fresh: bool = False) -> list[CheckResult]:
    return [check_telegram(fresh=fresh), check_review_chat_id()]


def critical_for_hotspots(*, fresh: bool = False) -> list[CheckResult]:
    """At least one LLM provider AND at least one embedding provider must work."""
    moonshot = check_moonshot(fresh=fresh)
    anthropic = check_anthropic(fresh=fresh)
    jina = check_jina(fresh=fresh)
    openai = check_openai(fresh=fresh)
    return [moonshot, anthropic, jina, openai]


def critical_for_image_gate() -> list[CheckResult]:
    return [check_atlas()]


# ---------------------------------------------------------------------------
# Readiness gates
# ---------------------------------------------------------------------------


class PreflightError(RuntimeError):
    pass


def assert_ready_for_review_daemon(*, fresh: bool = False) -> None:
    checks = critical_for_review_daemon(fresh=fresh)
    fails = [c for c in checks if not c.ok]
    if fails:
        msgs = "; ".join(f"{c.name}: {c.message}" for c in fails)
        raise PreflightError(f"review-daemon not ready: {msgs}")


def assert_ready_for_hotspots(*, fresh: bool = False) -> None:
    checks = critical_for_hotspots(fresh=fresh)
    moonshot, anthropic, jina, openai = checks
    if not (moonshot.ok or anthropic.ok):
        raise PreflightError(
            "hotspots not ready: need a working LLM (Moonshot or Anthropic)"
        )
    if not (jina.ok or openai.ok):
        raise PreflightError(
            "hotspots not ready: need a working embedding provider (Jina or OpenAI)"
        )


def assert_ready_for_image_gate() -> None:
    if not check_atlas().ok:
        raise PreflightError(
            "image-gate not ready: ATLASCLOUD_API_KEY not set"
        )
