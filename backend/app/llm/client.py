"""LLM client: one-method Protocol, an OpenRouter implementation, and a safe error type.

All model calls go through apiyi's OpenAI-compatible /chat/completions endpoint (https://api.apiyi.com/v1).
No tools, no web access, no streaming, no automatic retries, so the per-request call caps in config.py are exact.
Any failure raises LLMUnavailable with a safe message; callers fall back to sample mode.
The API key is never put in messages, logs or repr().
"""
import logging
import os
import time
from functools import lru_cache
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Protocol

from app import config

log = logging.getLogger("app.llm")  # metadata only: never prompts, user text or keys

OPENROUTER_BASE_URL = (os.environ.get("API_BASE_URL", "").strip() or os.environ.get("OPENROUTER_BASE_URL", "").strip() or "https://api.apiyi.com/v1").rstrip("/")
OPENROUTER_HOST = urlparse(OPENROUTER_BASE_URL).hostname or "openrouter.ai"


class LLMUnavailable(Exception):
    """The model could not be used. `category` is a safe, fixed label."""

    def __init__(self, category: str = "unavailable", *, status: int | None = None, error_type: str | None = None, detail: str | None = None):
        super().__init__(f"The language model is unavailable ({category}). Sample mode is used instead.")
        self.category = category
        # Diagnostics for the opt-in live scripts ONLY. They are never put in an API response or a log line (callers use `category`).
        self.status, self.error_type, self.detail = status, error_type, detail


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    stop_reason: str | None = None


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> LLMResponse: ...


def _provider_error(body) -> dict:
    """OpenRouter's error code and a short message, for the live scripts. Truncated; the request (prompt, key) is never part of it."""
    err = body.get("error", body) if isinstance(body, dict) else {}
    etype = err.get("code") or err.get("type") if isinstance(err, dict) else None
    msg = err.get("message") if isinstance(err, dict) else None
    return {"error_type": str(etype)[:60] if etype else None, "detail": str(msg or "")[:240] or None}


class OpenRouterLLMClient:
    """OpenRouter chat-completions client. Reads OPENROUTER_API_KEY and LLM_MODEL from the environment."""

    endpoint_host = OPENROUTER_HOST
    relay = True  # OpenRouter is a third-party gateway: the cleaned text and computed facts pass through it to the model provider

    def __init__(self, api_key: str, model: str = config.DEFAULT_LLM_MODEL, timeout: float = config.LLM_TIMEOUT_SECONDS, transport=None):
        import httpx  # imported lazily so sample mode never needs it configured

        self.model = model
        self._client = httpx.Client(
            base_url=OPENROUTER_BASE_URL, timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://www.smubia.com/ailodge", "X-Title": "Fitinerary"})

    def __repr__(self) -> str:  # never expose the key
        return f"OpenRouterLLMClient(model={self.model!r}, endpoint={self.endpoint_host!r}, api_key='***')"

    @classmethod
    def from_env(cls) -> "OpenRouterLLMClient | None":
        key = (os.environ.get("API_KEY", "").strip() or os.environ.get("OPENROUTER_API_KEY", "")).strip()
        if not key:
            return None
        return _cached_client(key, (os.environ.get("MODEL_NAME", "").strip() or os.environ.get("LLM_MODEL", "")).strip() or config.DEFAULT_LLM_MODEL)

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> LLMResponse:
        import httpx

        payload = {"model": self.model, "max_tokens": max_tokens, "messages": [{"role": "system", "content": system}, *messages]}
        if "deepseek" in self.model.lower():
            payload["reasoning_effort"] = "none"  # DeepSeek thinks first by default; calls run under LLM_TIMEOUT_SECONDS
        t0 = time.perf_counter()
        try:
            resp = self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException:
            raise LLMUnavailable("timeout") from None
        except httpx.TransportError:
            raise LLMUnavailable("network") from None
        except Exception:  # never let a client detail (which could echo request data) escape
            raise LLMUnavailable("error") from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code in (401, 403):
            raise LLMUnavailable("authentication", status=resp.status_code, **_provider_error(body))
        if resp.status_code == 429:
            raise LLMUnavailable("rate_limited", status=429, **_provider_error(body))
        if resp.status_code >= 400 or not isinstance(body, dict) or body.get("error") or not body.get("choices"):
            raise LLMUnavailable("api_error", status=resp.status_code, **_provider_error(body))
        choice = body["choices"][0] or {}
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = body.get("usage") or {}
        return LLMResponse(text=content or "", input_tokens=int(usage.get("prompt_tokens") or 0),
                           output_tokens=int(usage.get("completion_tokens") or 0),
                           latency_seconds=round(time.perf_counter() - t0, 3), stop_reason=choice.get("finish_reason"))


@lru_cache(maxsize=4)
def _cached_client(key: str, model: str) -> OpenRouterLLMClient:
    """One pooled HTTP client per (key, model), instead of a new connection pool on every request."""
    return OpenRouterLLMClient(key, model)


def get_llm_client() -> "LLMClient | None":
    """FastAPI dependency. Tests override it (app.dependency_overrides) with a mock. None = sample mode."""
    return OpenRouterLLMClient.from_env()
