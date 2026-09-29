"""DEPRECATED — disabled from the live chat pipeline. Provider-agnostic
(Anthropic/Groq) LLM generation layer, superseded by claude_client.py, which
is now the sole LLM call path (Claude Haiku only, one config value:
config.LLM_MODEL). Nothing in app.py/orchestrator.py/rag_path.py imports
this module anymore.

Left in place, not deleted, only because it's still importable for any
standalone script/diagnostic that calls generate() directly — new code
should use claude_client.generate() instead.
"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

import anthropic
import groq

from pricing import estimate_cost_usd

_BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=_BACKEND_DIR / ".env")

ANTHROPIC_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
GROQ_MODEL_DEFAULT = "llama-3.1-8b-instant"

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0

_USAGE_LOG_PATH = _BACKEND_DIR / "logs" / "llm_usage.csv"
_USAGE_LOG_HEADERS = [
    "timestamp",
    "provider",
    "model",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "estimated_cost_usd",
    "query_preview",
]


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    latency_ms: float


class LLMProviderError(Exception):
    """Raised when the configured LLM provider fails after all retry attempts
    (or immediately, for a non-retryable error). `status_code` carries the
    provider's HTTP status when known — None for connection-level failures.
    """

    def __init__(self, message: str, *, status_code: "int | None" = None, provider: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider


# ─────────────────────────────────────────────
# Provider selection — validated and constructed ONCE, at import time.
# ─────────────────────────────────────────────
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
_SUPPORTED_PROVIDERS = {"anthropic", "groq"}
if LLM_PROVIDER not in _SUPPORTED_PROVIDERS:
    raise ValueError(
        f"Unsupported LLM_PROVIDER={LLM_PROVIDER!r}. "
        f"Must be one of {sorted(_SUPPORTED_PROVIDERS)}."
    )

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", ANTHROPIC_MODEL_DEFAULT)
GROQ_MODEL = os.getenv("GROQ_MODEL", GROQ_MODEL_DEFAULT)

_anthropic_client: "anthropic.Anthropic | None" = None
_groq_client: "groq.Groq | None" = None
_retry_exc_types: tuple

if LLM_PROVIDER == "anthropic":
    _api_key = os.getenv("ANTHROPIC_API_KEY")
    if not _api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set — required when LLM_PROVIDER=anthropic.")
    _anthropic_client = anthropic.Anthropic(api_key=_api_key)
    _retry_exc_types = (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError)
else:
    _api_key = os.getenv("GROQ_API_KEY")
    if not _api_key:
        raise ValueError("GROQ_API_KEY is not set — required when LLM_PROVIDER=groq.")
    _groq_client = groq.Groq(api_key=_api_key)
    _retry_exc_types = (groq.RateLimitError, groq.APIStatusError, groq.APIConnectionError)


# ─────────────────────────────────────────────
# Retry: 3 attempts total, exponential backoff, ONLY on rate limits and 5xx.
# Never retries 400/401/403 (or any other non-5xx status) — those raise on
# the first attempt. Connection errors also fail immediately (not in the
# retryable set) so a broken network doesn't add latency before failing.
# ─────────────────────────────────────────────
def _call_with_retry(fn: Callable[[], Any], *, provider: str) -> Any:
    rate_limit_exc, status_exc, connection_exc = _retry_exc_types
    last_exc: Exception | None = None
    status_code: "int | None" = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return fn()
        except rate_limit_exc as exc:
            last_exc = exc
            status_code = getattr(exc, "status_code", 429)
            retryable = True
        except connection_exc as exc:
            print(f"[llm_provider] {provider} connection error (not retried): {exc}")
            raise LLMProviderError(
                f"{provider} connection error: {exc}", status_code=None, provider=provider
            ) from exc
        except status_exc as exc:
            last_exc = exc
            status_code = getattr(exc, "status_code", None)
            retryable = status_code is not None and status_code >= 500

        if not retryable or attempt == _MAX_ATTEMPTS:
            print(
                f"[llm_provider] {provider} request failed after {attempt} attempt(s) "
                f"(status={status_code}): {last_exc}"
            )
            raise LLMProviderError(
                f"{provider} request failed after {attempt} attempt(s): {last_exc}",
                status_code=status_code,
                provider=provider,
            ) from last_exc

        delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        print(
            f"[llm_provider] {provider} retryable error (status={status_code}), "
            f"backing off {delay:.1f}s before attempt {attempt + 1}/{_MAX_ATTEMPTS}: {last_exc}"
        )
        time.sleep(delay)

    raise AssertionError("unreachable: retry loop always returns or raises")


def _log_usage(result: LLMResult, query_preview: str) -> None:
    """Append one row to logs/llm_usage.csv. Never raises — a logging
    failure (missing directory, disk full, permissions) must not break a
    user-facing request."""
    try:
        _USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _USAGE_LOG_PATH.exists()
        with open(_USAGE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(_USAGE_LOG_HEADERS)
            cost = estimate_cost_usd(result.model, result.input_tokens, result.output_tokens)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                result.provider,
                result.model,
                result.input_tokens,
                result.output_tokens,
                f"{result.latency_ms:.1f}",
                f"{cost:.6f}",
                query_preview[:60],
            ])
    except Exception as exc:  # noqa: BLE001 — logging must never break a request
        print(f"[llm_provider] usage logging failed (non-fatal): {exc}")


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────
def generate(system: str, user: str, max_tokens: int = 1024, temperature: float | None = None) -> LLMResult:
    """Generate one completion via the configured provider.

    `user[:60]` is what gets logged as the usage CSV's query preview, so
    call sites should put the actual question near the start of `user` if
    they want a useful preview there. `temperature` is left as the
    provider's own default (None) unless a call site opts in.
    """
    if LLM_PROVIDER == "anthropic":
        result = _generate_anthropic(system, user, max_tokens, temperature)
    else:
        result = _generate_groq(system, user, max_tokens, temperature)

    _log_usage(result, query_preview=user)
    return result


def _generate_anthropic(system: str, user: str, max_tokens: int, temperature: float | None = None) -> LLMResult:
    assert _anthropic_client is not None  # constructed at import time

    def _call():
        kwargs: dict[str, Any] = dict(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        return _anthropic_client.messages.create(**kwargs)

    start = time.perf_counter()
    response = _call_with_retry(_call, provider="anthropic")
    latency_ms = (time.perf_counter() - start) * 1000

    text = "".join(b.text for b in response.content if b.type == "text")
    return LLMResult(
        text=text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=ANTHROPIC_MODEL,
        provider="anthropic",
        latency_ms=latency_ms,
    )


def _generate_groq(system: str, user: str, max_tokens: int, temperature: float | None = None) -> LLMResult:
    assert _groq_client is not None  # constructed at import time

    def _call():
        kwargs: dict[str, Any] = dict(
            model=GROQ_MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        return _groq_client.chat.completions.create(**kwargs)

    start = time.perf_counter()
    response = _call_with_retry(_call, provider="groq")
    latency_ms = (time.perf_counter() - start) * 1000

    text = response.choices[0].message.content or ""
    return LLMResult(
        text=text,
        input_tokens=response.usage.prompt_tokens,
        output_tokens=response.usage.completion_tokens,
        model=GROQ_MODEL,
        provider="groq",
        latency_ms=latency_ms,
    )
