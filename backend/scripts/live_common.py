"""Shared pieces for the opt-in live scripts (ai_selfcheck.py, score_gold.py). Import-safe: nothing here calls a model or reads a key by itself.
The key is read only by OpenRouterLLMClient.from_env(); this module never prints it."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent

from app import config  # noqa: E402
from app.llm.client import LLMResponse, LLMUnavailable  # noqa: E402
from app.llm.client import OPENROUTER_HOST as DEFAULT_HOST  # noqa: E402
from app.optimiser.hotel_compare import compare_hotels  # noqa: E402
from app.schemas import Preferences  # noqa: E402

FATAL = {"authentication", "api_error"}  # no point spending more calls: the key or the model name is wrong
ADVICE = {
    "authentication": "The key was rejected by {host}. Check that API_KEY is a current key for that service (not a key for a different provider or gateway) and has no stray spaces or quotes.",
    "api_error": "{host} returned an error. The most common cause is an invalid model name in MODEL_NAME (set to '{model}'): check the exact model ID your service offers. Model ids look like 'claude-haiku-4-5-20251001' (the gateway's /models endpoint lists the IDs your key may use).",
    "network": "{host} could not be reached. Check your internet connection and any proxy or firewall settings.",
    "timeout": "The API did not answer in time.",
    "rate_limited": "The API rate-limited the request. Wait a minute and run again.",
    "call_cap": "The call cap for this script was reached.",
    "error": "An unexpected client error occurred.",
}


class RunAborted(Exception):
    """Raised by Capped when further calls would be wasted. Not a subclass of LLMUnavailable, so the pipeline cannot swallow it."""

    def __init__(self, category: str, model: str, host: str = "the API", status: int | None = None, error_type: str | None = None, detail: str | None = None):
        self.category, self.model, self.host = category, model, host
        self.status, self.error_type, self.detail = status, error_type, detail
        msg = ADVICE.get(category, ADVICE["error"]).format(model=model, host=host)
        if status or error_type or detail:  # what the service itself said (status, type, a short message); never the key or the prompt
            msg += f" The service answered: HTTP {status or '?'}{' ' + error_type if error_type else ''}{': ' + detail if detail else ''}"
        super().__init__(msg)


class Capped:
    """Wraps a client: counts calls, hard cap, records every reply (kept in memory only), aborts the run on a fatal error."""

    def __init__(self, inner, cap: int):
        self.inner, self.cap, self.model = inner, cap, inner.model
        self.host = getattr(inner, "endpoint_host", None) or DEFAULT_HOST
        self.relay = bool(getattr(inner, "relay", False))
        self.total_calls = 0
        self.log: list[LLMResponse] = []
        self.failures: list[str] = []

    def complete(self, system, messages, max_tokens):
        if self.total_calls >= self.cap:
            raise RunAborted("call_cap", self.model, self.host)
        self.total_calls += 1
        try:
            r = self.inner.complete(system, messages, max_tokens)
        except LLMUnavailable as e:
            self.failures.append(e.category)
            if e.category in FATAL or len(self.failures) >= 3:
                raise RunAborted(e.category, self.model, self.host, e.status, e.error_type, e.detail) from None
            raise
        self.log.append(r)
        return r


def endpoint_stamp(client) -> str:
    """For report headers: which host answered, and an honest note when that was a third-party relay."""
    host = getattr(client, "host", None) or getattr(client, "endpoint_host", None) or DEFAULT_HOST
    line = f"Endpoint host: `{host}`."
    if getattr(client, "relay", False) or getattr(getattr(client, "endpoint", None), "relay", False):
        line += (" Served via a third-party relay: the cleaned text and computed facts pass through that gateway to the model provider, "
                 "so model behaviour and availability are the gateway's as well as the provider's.")
    return line


# (name, old name): like the app (app/llm/client.py), the live scripts read the new name first and fall back to the old one.
LIVE_VARS = (("API_KEY", "OPENROUTER_API_KEY"), ("MODEL_NAME", "LLM_MODEL"))


def env_value(name: str) -> str:
    """A LIVE_VARS value under its new or old name ('' when neither is set). Callers must never print the key."""
    old = dict(LIVE_VARS)[name]
    return (os.environ.get(name, "").strip() or os.environ.get(old, "")).strip()


def gate(script: str, cap: int) -> str | None:
    """None when a live run may proceed, otherwise the plain message to print. Never echoes the key."""
    if os.environ.get("RUN_LIVE_LLM_EVAL") != "1":
        return f"{script} is opt-in and made no model calls. To run it: RUN_LIVE_LLM_EVAL=1 plus API_KEY and MODEL_NAME (at most {cap} calls)."
    missing = [new for new, _ in LIVE_VARS if not env_value(new)]
    if missing:
        return (f"Cannot run live: {' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set. Put {'it' if len(missing) == 1 else 'them'} in backend/.env (copy .env.example) "
                f"or export them in this shell, then run again. Nothing was called.")
    return None


def git_hash() -> str:
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
        return h + (" + uncommitted changes" if dirty else "")
    except Exception:
        return "unknown"


def llm_fingerprint() -> str:
    """A short hash of the code that sits between the user's text and the screen (everything under backend/app/llm). A live report stamps it, and the Proof page
    compares it with the CURRENT value (frontend/scripts/sync-proof.mjs computes the same hash), so a report made before an LLM-code change reads "not run on this version"."""
    import hashlib
    d = ROOT / "app" / "llm"
    h = hashlib.sha256()
    for p in sorted(x for x in d.rglob("*") if x.is_file() and x.suffix in (".py", ".json") and "__pycache__" not in x.parts):
        h.update(p.relative_to(d).as_posix().encode() + b"\n" + p.read_bytes().replace(b"\r\n", b"\n") + b"\n--\n")
    return h.hexdigest()[:12]


def real_result(ds):
    """A real hotel comparison computed by the solver for this city (the model only words it)."""
    hotels = list(ds.hotels)
    first = next(iter(ds.attractions))
    pr = Preferences(days=2, must_visit=[first], day_start=ds.city.default_day_start, day_end=ds.city.default_day_end,
                     value_of_time_per_hour=ds.city.default_value_of_time_per_hour)
    return compare_hotels(pr, hotels, ds, time_limit=60)


def load_env_file() -> None:
    """Load backend/.env into the process environment WITHOUT overriding variables already set, and without printing anything."""
    p = Path(os.environ.get("LIVE_ENV_FILE") or ROOT / ".env")
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if not os.environ.get(k.strip()):
                os.environ[k.strip()] = v.strip().strip('"').strip("'")
