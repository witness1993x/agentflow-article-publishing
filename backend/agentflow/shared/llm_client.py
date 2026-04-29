"""Unified LLM client: Claude for chat, Jina/OpenAI for embeddings, with MOCK_LLM mode.

When ``MOCK_LLM=true`` (default for dev), every call is served from deterministic
fixtures in ``agentflow/shared/mocks/``. This keeps the end-to-end pipeline
runnable without any API keys.

Embedding provider selection (only matters when MOCK_LLM is off):

- ``EMBEDDING_PROVIDER=jina`` (default, recommended) → Jina AI ``jina-embeddings-v3``
  via ``https://api.jina.ai/v1/embeddings``. 10M tokens one-time free per key,
  then ~$0.02/1M. Good Chinese support. 1024-dim by default (configurable).
- ``EMBEDDING_PROVIDER=openai`` → OpenAI ``text-embedding-3-small``. 1536-dim.
- ``EMBEDDING_PROVIDER=auto`` → pick whichever key is set; Jina wins if both.

Public surface:

- ``LLMClient().chat_json(prompt_family=..., prompt=..., ...)``
- ``LLMClient().chat_text(prompt_family=..., prompt=..., ...)``
- ``LLMClient().embed(texts)``

Each call logs one JSONL record to ``~/.agentflow/logs/llm_calls.jsonl``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from agentflow.shared.logger import get_logger, log_llm_call

_log = get_logger("llm_client")

_MOCKS_DIR = Path(__file__).resolve().parent / "mocks"

_DEFAULT_CLAUDE_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
_DEFAULT_KIMI_MODEL = os.environ.get("MOONSHOT_MODEL", "moonshot-v1-32k")
_MOONSHOT_BASE_URL = os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
_DEFAULT_OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
_DEFAULT_JINA_MODEL = os.environ.get("JINA_EMBEDDING_MODEL", "jina-embeddings-v3")
_DEFAULT_JINA_TASK = os.environ.get("JINA_EMBEDDING_TASK", "text-matching")
_DEFAULT_JINA_DIM = int(os.environ.get("JINA_EMBEDDING_DIM", "1024"))
_OPENAI_DIM = 1536


def _moonshot_api_key() -> str | None:
    return os.environ.get("MOONSHOT_API_KEY") or os.environ.get("KIMI_API_KEY")


def _anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def _env_truthy(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _resolve_gen_provider() -> str:
    """Pick 'kimi' or 'claude' based on GENERATION_PROVIDER + available keys."""
    explicit = os.environ.get("GENERATION_PROVIDER", "").lower().strip()
    if explicit in ("kimi", "moonshot"):
        return "kimi"
    if explicit in ("claude", "anthropic"):
        return "claude"
    # auto: prefer Kimi if its key is set, else Claude, else default to Kimi
    if _moonshot_api_key():
        return "kimi"
    if _anthropic_api_key():
        return "claude"
    return "kimi"


def _resolve_embed_provider() -> str:
    """Pick 'jina' or 'openai' based on EMBEDDING_PROVIDER + available keys."""
    explicit = os.environ.get("EMBEDDING_PROVIDER", "").lower().strip()
    if explicit in ("jina", "openai"):
        return explicit
    # auto: prefer Jina if its key is set, else OpenAI, else default to Jina
    if os.environ.get("JINA_API_KEY"):
        return "jina"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "jina"


def _embedding_dim() -> int:
    """Return expected embedding dim for the active provider."""
    if _resolve_embed_provider() == "jina":
        return _DEFAULT_JINA_DIM
    return _OPENAI_DIM


def _is_mock() -> bool:
    return os.environ.get("MOCK_LLM", "").lower() == "true"


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _read_mock_json(prompt_family: str) -> dict[str, Any]:
    path = _MOCKS_DIR / f"{prompt_family}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No mock fixture for prompt_family={prompt_family!r} at {path}"
        )
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_mock_text(prompt_family: str) -> str:
    path = _MOCKS_DIR / f"{prompt_family}.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"No mock fixture for prompt_family={prompt_family!r} at {path}"
        )
    return path.read_text(encoding="utf-8")


_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    # Cheap stemming: lower + trim Chinese/latin word boundaries.
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _fake_embedding(text: str, dim: int | None = None) -> list[float]:
    """Deterministic vector seeded from text's tokens.

    Uses a bag-of-words over hashed tokens -> fixed-dim vector. Two texts with
    overlapping vocabulary produce similar cosine similarity, so DBSCAN can
    cluster meaningfully in mock mode.
    """
    if dim is None:
        dim = _embedding_dim()
    vec = [0.0] * dim
    tokens = _tokens(text)
    if not tokens:
        # Still produce a deterministic, non-zero vector from the raw string.
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
        seed = int.from_bytes(digest, "big")
        idx = seed % dim
        vec[idx] = 1.0
        return vec

    for tok in tokens:
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 0x1 else -1.0
        vec[idx] += sign * 1.0

    # L2 normalize so cosine similarity works directly.
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0:
        return vec
    return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# Real client (lazy import)
# ---------------------------------------------------------------------------


class _RealClients:
    """Lazy holders for AsyncAnthropic / Moonshot(OpenAI-compat) / OpenAI / Jina."""

    def __init__(self) -> None:
        self._anthropic: Any | None = None
        self._openai: Any | None = None
        self._moonshot: Any | None = None
        self._jina_session: Any | None = None

    def anthropic(self) -> Any:
        if self._anthropic is None:
            from anthropic import AsyncAnthropic  # type: ignore

            self._anthropic = AsyncAnthropic()
        return self._anthropic

    def openai(self) -> Any:
        if self._openai is None:
            from openai import AsyncOpenAI  # type: ignore

            self._openai = AsyncOpenAI()
        return self._openai

    def moonshot(self) -> Any:
        if self._moonshot is None:
            from openai import AsyncOpenAI  # type: ignore

            api_key = _moonshot_api_key()
            if not api_key:
                raise RuntimeError(
                    "GENERATION_PROVIDER=kimi but MOONSHOT_API_KEY is not set. "
                    "Get a key at https://platform.moonshot.cn/console/api-keys."
                )
            self._moonshot = AsyncOpenAI(
                api_key=api_key,
                base_url=_MOONSHOT_BASE_URL,
            )
        return self._moonshot

    def jina_openai(self) -> Any:
        """Jina embeddings via OpenAI SDK (httpx). Avoids aiohttp DNS issues.

        Jina Embeddings API is OpenAI-compatible for the request/response shape;
        the Jina-specific ``task`` parameter goes through ``extra_body``.
        """
        if self._jina_session is None:
            from openai import AsyncOpenAI  # type: ignore

            api_key = os.environ.get("JINA_API_KEY")
            if not api_key:
                raise RuntimeError("JINA_API_KEY missing.")
            self._jina_session = AsyncOpenAI(
                api_key=api_key,
                base_url="https://api.jina.ai/v1",
            )
        return self._jina_session


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------


class LLMClient:
    """Unified async client: Kimi/Claude chat + Jina/OpenAI embeddings."""

    def __init__(self) -> None:
        self._real = _RealClients()
        self.claude_model = _DEFAULT_CLAUDE_MODEL
        self.kimi_model = _DEFAULT_KIMI_MODEL
        self.openai_embedding_model = _DEFAULT_OPENAI_EMBED_MODEL
        self.jina_embedding_model = _DEFAULT_JINA_MODEL
        self.jina_task = _DEFAULT_JINA_TASK
        self.jina_dim = _DEFAULT_JINA_DIM

    # ---- chat_json -------------------------------------------------------

    async def chat_json(
        self,
        *,
        prompt_family: str,
        prompt: str,
        max_tokens: int = 2000,
        system: str | None = None,
    ) -> dict[str, Any]:
        """Call Claude, expect a JSON object back. In mock mode returns a fixture."""
        start = time.monotonic()
        mocked = _is_mock()

        if mocked:
            data = _read_mock_json(prompt_family)
            latency_ms = (time.monotonic() - start) * 1000
            log_llm_call(
                prompt_family=prompt_family,
                tokens_in=0,
                tokens_out=0,
                latency_ms=latency_ms,
                mocked=True,
            )
            return data

        json_directive = (
            "Respond with a single valid JSON object only. "
            "No preamble, no markdown fences, no commentary."
        )
        effective_system = (
            f"{system}\n\n{json_directive}" if system else json_directive
        )
        text = await self._dispatch_chat(
            prompt=prompt,
            system=effective_system,
            max_tokens=max_tokens,
            prompt_family=prompt_family,
            expect_json=True,
        )
        latency_ms = (time.monotonic() - start) * 1000
        parsed = _extract_json(text)
        # tokens are logged in the provider call; duplicate a latency-only line? skip.
        return parsed

    # ---- chat_text -------------------------------------------------------

    async def chat_text(
        self,
        *,
        prompt_family: str,
        prompt: str,
        max_tokens: int = 2000,
        system: str | None = None,
    ) -> str:
        """Call Claude, expect a string back."""
        start = time.monotonic()
        mocked = _is_mock()

        if mocked:
            text = _read_mock_text(prompt_family)
            latency_ms = (time.monotonic() - start) * 1000
            log_llm_call(
                prompt_family=prompt_family,
                tokens_in=0,
                tokens_out=0,
                latency_ms=latency_ms,
                mocked=True,
            )
            return text

        return await self._dispatch_chat(
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            prompt_family=prompt_family,
        )

    # ---- embed -----------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts. Mock mode returns deterministic fake vectors.

        Real-mode provider is chosen by ``EMBEDDING_PROVIDER`` (default ``auto``
        prefers Jina if ``JINA_API_KEY`` is set, else OpenAI).
        """
        start = time.monotonic()

        if _is_mock():
            vectors = [_fake_embedding(t) for t in texts]
            latency_ms = (time.monotonic() - start) * 1000
            log_llm_call(
                prompt_family="embed",
                tokens_in=sum(len(_tokens(t)) for t in texts),
                tokens_out=0,
                latency_ms=latency_ms,
                mocked=True,
                extra={"batch_size": len(texts), "provider": "mock"},
            )
            return vectors

        provider = _resolve_embed_provider()
        if provider == "jina":
            return await self._jina_embed(texts, start)
        return await self._openai_embed(texts, start)

    async def _jina_embed(self, texts: list[str], start: float) -> list[list[float]]:
        if not os.environ.get("JINA_API_KEY"):
            raise RuntimeError(
                "EMBEDDING_PROVIDER=jina but JINA_API_KEY is not set. "
                "Get a free key at https://jina.ai/?sui=apikey."
            )
        client = self._real.jina_openai()
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.embeddings.create(
                    model=self.jina_embedding_model,
                    input=texts,
                    dimensions=self.jina_dim,
                    extra_body={"task": self.jina_task},
                )
                latency_ms = (time.monotonic() - start) * 1000
                usage = getattr(response, "usage", None)
                log_llm_call(
                    prompt_family="embed",
                    tokens_in=getattr(usage, "total_tokens", 0) if usage else 0,
                    tokens_out=0,
                    latency_ms=latency_ms,
                    mocked=False,
                    extra={"batch_size": len(texts), "provider": "jina"},
                )
                return [list(d.embedding) for d in response.data]
            except Exception as err:  # pragma: no cover - network
                last_err = err
                if _should_retry(err) and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        assert last_err is not None
        raise last_err

    async def _openai_embed(self, texts: list[str], start: float) -> list[list[float]]:
        client = self._real.openai()
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.embeddings.create(
                    model=self.openai_embedding_model,
                    input=texts,
                )
                latency_ms = (time.monotonic() - start) * 1000
                usage = getattr(response, "usage", None)
                log_llm_call(
                    prompt_family="embed",
                    tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
                    tokens_out=0,
                    latency_ms=latency_ms,
                    mocked=False,
                    extra={"batch_size": len(texts), "provider": "openai"},
                )
                return [list(d.embedding) for d in response.data]
            except Exception as err:  # pragma: no cover - network
                last_err = err
                if _should_retry(err) and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        assert last_err is not None
        raise last_err

    # ---- internals -------------------------------------------------------

    async def _dispatch_chat(
        self,
        *,
        prompt: str,
        system: str | None,
        max_tokens: int,
        prompt_family: str,
        expect_json: bool = False,
    ) -> str:
        provider = _resolve_gen_provider()
        if provider == "kimi":
            try:
                return await self._kimi_call(
                    prompt=prompt,
                    system=system,
                    max_tokens=max_tokens,
                    prompt_family=prompt_family,
                    expect_json=expect_json,
                )
            except Exception as err:
                if not (
                    _env_truthy("GENERATION_PROVIDER_FALLBACK")
                    and _anthropic_api_key()
                    and _should_fallback_generation(err)
                ):
                    raise
                _log.warning(
                    "Moonshot generation failed for %s (%s); falling back to Claude",
                    prompt_family,
                    type(err).__name__,
                )
                return await self._claude_call(
                    prompt=prompt,
                    system=system,
                    max_tokens=max_tokens,
                    prompt_family=prompt_family,
                    log_extra={
                        "fallback_from": "kimi",
                        "fallback_reason": type(err).__name__,
                    },
                )
        return await self._claude_call(
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            prompt_family=prompt_family,
        )

    async def _kimi_call(
        self,
        *,
        prompt: str,
        system: str | None,
        max_tokens: int,
        prompt_family: str,
        expect_json: bool = False,
    ) -> str:
        client = self._real.moonshot()
        start = time.monotonic()
        last_err: Exception | None = None

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self.kimi_model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if expect_json:
            kwargs["response_format"] = {"type": "json_object"}

        for attempt in range(3):
            try:
                response = await client.chat.completions.create(**kwargs)
                latency_ms = (time.monotonic() - start) * 1000
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
                text = (response.choices[0].message.content or "") if response.choices else ""

                log_llm_call(
                    prompt_family=prompt_family,
                    tokens_in=input_tokens,
                    tokens_out=output_tokens,
                    latency_ms=latency_ms,
                    mocked=False,
                    extra={"provider": "kimi", "model": self.kimi_model},
                )
                return text
            except Exception as err:  # pragma: no cover - network
                last_err = err
                if _should_retry(err) and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        assert last_err is not None
        raise last_err

    async def _claude_call(
        self,
        *,
        prompt: str,
        system: str | None,
        max_tokens: int,
        prompt_family: str,
        log_extra: dict[str, Any] | None = None,
    ) -> str:
        client = self._real.anthropic()
        start = time.monotonic()
        last_err: Exception | None = None
        messages = [{"role": "user", "content": prompt}]

        for attempt in range(3):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.claude_model,
                    "max_tokens": max_tokens,
                    "messages": messages,
                }
                if system:
                    kwargs["system"] = system
                response = await client.messages.create(**kwargs)
                latency_ms = (time.monotonic() - start) * 1000
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
                output_tokens = getattr(usage, "output_tokens", 0) if usage else 0

                # Extract text from content blocks
                text_parts: list[str] = []
                for block in response.content:
                    if getattr(block, "type", None) == "text":
                        text_parts.append(block.text)
                    elif hasattr(block, "text"):
                        text_parts.append(block.text)
                text = "".join(text_parts)

                log_llm_call(
                    prompt_family=prompt_family,
                    tokens_in=input_tokens,
                    tokens_out=output_tokens,
                    latency_ms=latency_ms,
                    mocked=False,
                    extra={
                        "provider": "claude",
                        "model": self.claude_model,
                        **(log_extra or {}),
                    },
                )
                return text
            except Exception as err:  # pragma: no cover - network
                last_err = err
                if _should_retry(err) and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        assert last_err is not None
        raise last_err


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of Claude's response, stripping ```json fences."""
    text = text.strip()
    match = _FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # As a last resort grab the first { ... } balanced slice.
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            return json.loads(text[first : last + 1])
        raise


def _is_rate_limit(err: Exception) -> bool:
    status = getattr(err, "status_code", None)
    if status == 429:
        return True
    msg = str(err).lower()
    return "429" in msg or "rate limit" in msg


def _is_transient_network(err: Exception) -> bool:
    """Catch flaky mid-request disconnects, read/connect timeouts, 5xx."""
    name = type(err).__name__
    if name in {
        "RemoteProtocolError",
        "APIConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "WriteError",
        "PoolTimeout",
    }:
        return True
    status = getattr(err, "status_code", None)
    if isinstance(status, int) and 500 <= status < 600:
        return True
    msg = str(err).lower()
    return (
        "disconnected" in msg
        or "connection error" in msg
        or "connection reset" in msg
    )


def _should_retry(err: Exception) -> bool:
    return _is_rate_limit(err) or _is_transient_network(err)


def _should_fallback_generation(err: Exception) -> bool:
    """Only fail over providers for transient upstream/provider failures."""
    return _should_retry(err)
