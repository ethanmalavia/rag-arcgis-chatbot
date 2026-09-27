"""Single LLM provider: Claude Haiku via the official Anthropic Python SDK.

Supersedes llm_provider.py's multi-provider (Anthropic/Groq) abstraction for
the live answer pipeline — see llm_provider.py's module docstring, now
marked deprecated. That module is left in place (some scripts/diagnostics
may still import it directly) but nothing in the active chat pipeline calls
it anymore.

Model is the single config value LLM_MODEL (config.py). Client is
constructed lazily (not at import time) so importing this module never
hard-fails when ANTHROPIC_API_KEY isn't set yet — matching rag_path.py's
existing lazy-import convention.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from config import LLM_MODEL

# Self-contained .env loading (mirrors the old llm_provider.py) so standalone
# scripts that import this module directly (scripts/eval_answers.py, etc.)
# pick up ANTHROPIC_API_KEY without needing to import app.py first — app.py
# also loads the same file at startup, so this is a harmless no-op there.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

_REQUEST_TIMEOUT_SECONDS = 20.0
_MAX_ATTEMPTS = 2  # one try + one retry, per the "timeout plus one retry" spec

_client: anthropic.Anthropic | None = None

# Errors worth retrying once (transient): timeouts, connection issues, rate
# limits, and 5xx. Anything else (400/401/403/etc.) fails immediately — a
# retry can't fix a bad request or bad key.
_RETRYABLE_EXC = (
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


class ClaudeError(Exception):
    """Raised when a Claude call fails after the timeout+retry budget. Call
    sites should catch this and show a friendly error message, never a raw
    stack trace, in the chat UI."""


@dataclass(frozen=True)
class ClaudeResult:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ClaudeError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.Anthropic(api_key=api_key, timeout=_REQUEST_TIMEOUT_SECONDS)
    return _client


def generate(system: str, user: str, *, max_tokens: int = 1024, temperature: float = 0.2) -> ClaudeResult:
    """One completion via Claude Haiku. Retries once on a transient error
    (timeout/connection/rate-limit/5xx); raises ClaudeError if both attempts
    fail, or immediately for a non-retryable error (bad request, auth)."""
    client = _get_client()
    last_exc: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            response = client.messages.create(
                model=LLM_MODEL,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except _RETRYABLE_EXC as exc:
            last_exc = exc
            print(f"[claude_client] attempt {attempt}/{_MAX_ATTEMPTS} failed ({exc!r}); "
                  f"{'retrying' if attempt < _MAX_ATTEMPTS else 'giving up'}")
            continue
        except anthropic.APIStatusError as exc:
            # Non-5xx status (400/401/403/...) — not retryable.
            raise ClaudeError(f"Claude request failed (status {exc.status_code}): {exc}") from exc

        latency_ms = (time.perf_counter() - start) * 1000
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        return ClaudeResult(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
        )

    raise ClaudeError(f"Claude request failed after {_MAX_ATTEMPTS} attempt(s): {last_exc}") from last_exc
