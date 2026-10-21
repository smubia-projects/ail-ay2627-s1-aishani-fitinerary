"""Project adapter over the shared AI Lodge rate-limit engine (cost control for model calls).

The engine (`rate_limit_engine.py`) is vendored unmodified from DOCS/shared/rate-limit/engine/fastapi/. This adapter supplies the
project slug and one bucket per LLM action; the engine handles central config (Upstash), X-Forwarded-For, event mode, the kill
switch and fail-open. Without UPSTASH_* env vars the engine is a no-op (local development and the test suite).

Two ways in:
  * check_rate_limit(request, bucket): the pure-LLM routes. Over the limit -> HTTP 429 {"detail": {"queries_used"}}, paused -> 503.
  * explain_allowed(request): the optional explanation inside /optimise, /compare-hotels and /replan. NON-FATAL: over the limit
    (or paused) it returns False and the route falls back to the built-in template explanation; the plan itself is never refused.

The in-memory SlidingWindowLimiter (app/hardening/limiter.py) is separate: a per-minute flood guard for every route, mainly to
protect the CPU-bound solver. It is not the AI quota.
"""
import os

from app.rate_limit_engine import RateLimiter, client_ip

PROJECT = "ail-ay2627-s1-aishani-fitinerary"
BUCKETS = ("parse", "repair", "profile", "replan", "copilot", "explain")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env(*names: str) -> str:
    return next((os.environ[n].strip() for n in names if os.environ.get(n, "").strip()), "")


limiter = RateLimiter(
    project=PROJECT,
    buckets={b: _int_env(f"RATE_LIMIT_{b.upper()}_MAX", 5) for b in BUCKETS},
    redis_url=_env("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_URL"),
    redis_token=_env("UPSTASH_REDIS_REST_TOKEN", "UPSTASH_REDIS_TOKEN"),
    default_window=_int_env("RATE_LIMIT_WINDOW_SECONDS", 432000),
)


def check_rate_limit(request, bucket: str) -> None:
    """Raise 429 (quota used up) or 503 (demo paused) before a model call. Call only when a model call will actually be made."""
    limiter.enforce(request, bucket)


def explain_allowed(request) -> bool:
    """Count one explanation against the 'explain' bucket; False when over the limit or paused (use the template instead)."""
    return limiter.status(client_ip(request), "explain").status == "ok"
