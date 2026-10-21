"""Hardening configuration read from the environment, validated at startup (fail fast, no secrets printed)."""
import os
from dataclasses import dataclass, field


class ConfigError(ValueError):
    """Invalid configuration: the app refuses to start."""


DEFAULT_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173", "http://127.0.0.1:4173")


def _int(env, name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name, "")
    if raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer (got a non-integer value)") from None
    if not lo <= v <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi} (got {v})")
    return v


@dataclass(frozen=True)
class HConfig:
    body_max_bytes: int = 64 * 1024
    rate_expensive_per_min: int = 6
    rate_standard_per_min: int = 30
    rate_cheap_per_min: int = 120
    rate_window_seconds: float = 60.0
    llm_daily_call_cap: int = 300
    trusted_proxy_hops: int = 0
    request_deadline_seconds: float = 25.0
    allowed_origins: tuple = DEFAULT_ORIGINS
    docs_enabled: bool = False
    max_json_depth: int = 20
    optimise_time_limit_seconds: float = 10.0
    alternatives_budget_seconds: float = 8.0
    hotel_compare_time_limit_seconds: float = 10.0
    replan_time_limit_seconds: float = 5.0
    solver_concurrency: int = 1
    solver_busy_retry_after_seconds: int = 10

    @classmethod
    def from_env(cls, env=None) -> "HConfig":
        env = os.environ if env is None else env
        origins_raw = env.get("CORS_ORIGINS", "").strip()
        origins = tuple(o.strip().rstrip("/") for o in origins_raw.split(",") if o.strip()) if origins_raw else DEFAULT_ORIGINS
        c = cls(
            body_max_bytes=_int(env, "BODY_MAX_BYTES", 64 * 1024, 1024, 5_000_000),
            rate_expensive_per_min=_int(env, "RATE_LIMIT_EXPENSIVE_PER_MIN", 6, 1, 10_000),
            rate_standard_per_min=_int(env, "RATE_LIMIT_PER_MIN", 30, 1, 10_000),
            rate_cheap_per_min=_int(env, "RATE_LIMIT_CHEAP_PER_MIN", 120, 1, 100_000),
            llm_daily_call_cap=_int(env, "LLM_DAILY_CALL_CAP", 300, 0, 1_000_000),
            trusted_proxy_hops=_int(env, "TRUSTED_PROXY_HOPS", 0, 0, 5),
            request_deadline_seconds=float(_int(env, "REQUEST_DEADLINE_SECONDS", 25, 2, 300)),
            optimise_time_limit_seconds=float(_int(env, "OPTIMISE_TIME_LIMIT_SECONDS", 10, 1, 300)),
            alternatives_budget_seconds=float(_int(env, "ALTERNATIVES_BUDGET_SECONDS", 8, 0, 300)),
            hotel_compare_time_limit_seconds=float(_int(env, "HOTEL_COMPARE_TIME_LIMIT_SECONDS", 10, 1, 300)),
            replan_time_limit_seconds=float(_int(env, "REPLAN_TIME_LIMIT_SECONDS", 5, 1, 300)),
            solver_concurrency=_int(env, "SOLVER_CONCURRENCY", 1, 1, 16),
            solver_busy_retry_after_seconds=_int(env, "SOLVER_BUSY_RETRY_AFTER_SECONDS", 10, 1, 300),
            allowed_origins=origins,
            docs_enabled=env.get("ENABLE_DOCS", "") in ("1", "true", "True"),
        )
        c.validate()
        return c

    def validate(self) -> None:
        for o in self.allowed_origins:
            if "*" in o:
                raise ConfigError("CORS_ORIGINS must list exact origins; '*' is refused")
            if not (o.startswith("http://") or o.startswith("https://")) or "/" in o.split("://", 1)[1]:
                raise ConfigError(f"CORS_ORIGINS entry '{o}' must be an exact origin like https://example.com (scheme + host, no path)")
        if self.rate_expensive_per_min > self.rate_standard_per_min:
            raise ConfigError("expensive-tier limit must not exceed the standard-tier limit")

    def summary(self, mode: str) -> dict:
        """Safe to log: no secrets."""
        return {"mode": mode, "rate_per_min": {"expensive": self.rate_expensive_per_min, "standard": self.rate_standard_per_min,
                                              "cheap": self.rate_cheap_per_min},
                "llm_daily_call_cap": self.llm_daily_call_cap, "trusted_proxy_hops": self.trusted_proxy_hops,
                "request_deadline_s": self.request_deadline_seconds, "body_max_bytes": self.body_max_bytes,
                "solver": {"concurrency": self.solver_concurrency, "optimise_time_limit_s": self.optimise_time_limit_seconds,
                           "compare_time_limit_s": self.hotel_compare_time_limit_seconds,
                           "replan_time_limit_s": self.replan_time_limit_seconds},
                "allowed_origins": list(self.allowed_origins), "docs_enabled": self.docs_enabled}
