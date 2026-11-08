"""Environment settings with safe defaults. No secrets live in code."""
import os
from dataclasses import dataclass

# SAMPLE exchange rate, for display only. Not a live rate; JPY is the stored currency.
SAMPLE_FX_JPY_PER_SGD = 110.0
FX_LABEL = "SAMPLE FX rate (approximate, not live): 1 SGD = 110 JPY"

# CORS origins come from the CORS_ORIGINS env var (see app/hardening/config.py).

# Any OpenRouter model id; override with LLM_MODEL. The prompts and the safety log were tuned on Claude Haiku 4.5.
DEFAULT_LLM_MODEL = "deepseek-v4-1-flash-260910"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    llm_api_key: str
    llm_model: str
    max_input_chars: int
    rate_limit_per_min: int

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @property
    def mode(self) -> str:
        return "llm" if self.llm_enabled else "sample"


def get_settings() -> Settings:
    return Settings(
        llm_api_key=(os.environ.get("API_KEY", "").strip() or os.environ.get("OPENROUTER_API_KEY", "")).strip(),
        llm_model=(os.environ.get("MODEL_NAME", "").strip() or os.environ.get("LLM_MODEL", "")).strip() or DEFAULT_LLM_MODEL,
        max_input_chars=_int_env("MAX_INPUT_CHARS", 2000),
        rate_limit_per_min=_int_env("RATE_LIMIT_PER_MIN", 30),
    )


# ---------------------------------------------------------------------------
# Optimiser settings (Phase 2)
# ---------------------------------------------------------------------------
# Pace -> maximum attractions per day (hard cap).
PACE_MAX_PER_DAY = {"relaxed": 2, "balanced": 3, "packed": 4}

# Soft-objective weights. Each term is first normalised to roughly 0..1 (see docs/SOLVER_DESIGN.md),
# then multiplied by its weight. Missed attractions dominate so the plan fills its pace cap;
# travel and cost then decide *which* attractions and in what order.
W_TRAVEL = 3         # total travel minutes / (days * day window minutes)
W_COST = 2           # (admissions + transport JPY) / (COST_REF_JPY_PER_DAY * days)
W_MISSED = 5         # missed nice_to_have attractions / number of nice_to_have candidates
W_PACE = 1           # (busiest day - quietest day) / pace cap: spreads load evenly
W_OVER_BUDGET = 4    # overshoot of a NON-strict budget / cost reference
COST_REF_JPY_PER_DAY = 5000
OBJECTIVE_SCALE = 100_000  # 1.0 of normalised weighted penalty == this many integer units

# CP-SAT determinism: fixed seed, one worker, and a deterministic-time limit.
SOLVER_SEED = 42
SOLVER_MAX_SECONDS = 20.0         # wall-clock safety net
SOLVER_MAX_DETERMINISTIC_TIME = 5.0
SOLVER_PROBE_SECONDS = 5.0        # for alternative-finding probe solves


# ---------------------------------------------------------------------------
# Hotel comparison (Phase 3)
# ---------------------------------------------------------------------------
HOTEL_COMPARE_TIME_LIMIT_SECONDS = 10.0   # whole comparison (all hotels + sensitivity)
MISSED_PREFERENCE_PENALTY_JPY = 3000      # JUDGEMENT CALL: cost charged per visitable nice-to-have not scheduled
VOT_RANGE_DEFAULT = (0, 3000)             # JPY per hour
VOT_GRID_STEP = 500
SENS_MAX_REFINE_DEPTH = 3                 # interval halvings where a hotel's plan changes
SENS_MAX_SOLVES = 36                      # hard cap on solves in one sensitivity run
MIN_SOLVE_SECONDS = 0.3                   # below this remaining budget a hotel is skipped (partial)


# ---------------------------------------------------------------------------
# Replanning and unknown-price handling (Phase 4)
# ---------------------------------------------------------------------------
# JPY-equivalent cost charged per UNNECESSARILY MOVED activity (see docs/SOLVER_DESIGN.md).
# Judgement call: about half a missed-preference penalty, so it discourages shuffling without forbidding it.
W_DISRUPTION = 1500
MOVE_THRESHOLD_MINUTES = 30        # an unchanged activity is "moved" if its day changes or its start shifts by MORE than this
REPLAN_TIME_LIMIT_SECONDS = 5.0
MAX_REPLAN_OPERATIONS = 20

# For OPTIMISATION ONLY, an unknown admission price is valued at the median of known prices.
# Displayed totals still treat unknown prices as "unknown, excluded". None = use the median.
UNKNOWN_PRICE_PLACEHOLDER_OVERRIDE_JPY: int | None = None


# ---------------------------------------------------------------------------
# LLM layer (Phase 5)
# ---------------------------------------------------------------------------
# NOTE: no temperature/top_p is sent. Determinism comes from a strict JSON schema in code and validation, not from sampling settings.
LLM_TIMEOUT_SECONDS = 10.0
LLM_MAX_TOKENS_EXTRACT = 2000         # thinking tokens count toward max_tokens, so keep headroom
LLM_MAX_TOKENS_EXPLAIN = 1200
LLM_MAX_CALLS_PARSE = 2               # /parse and /parse-replan: first attempt + ONE retry
LLM_MAX_CALLS_EXPLAIN = 1
LLM_MAX_NAMES = 20                    # cap on names per extraction
EXPLAIN_MAX_WORDS = 200               # prompt asks for ~120; longer output is rejected
NAME_SUGGESTION_RATIO = 0.92          # difflib ratio for "did you mean" (never auto-accepted)
LIVE_EVAL_MAX_CALLS = 150
REPAIR_SELFCHECK_MAX_CALLS = 40      # scripts/ai_repair_selfcheck.py: 4 cities x 4 checks, worst case 2 calls each
SELFCHECK_MAX_CALLS = 40              # scripts/ai_selfcheck.py: 4 cities x 5 checks, worst case 9 calls per city (parse retries)
REDTEAM_LIVE_MAX_CALLS = 60           # scripts/redteam_live.py: a stratified sample, at most 2 calls per parse attack and 1 per explanation attack
GOLD_MAX_CALLS = 100                  # scripts/score_gold.py live part: 48 prompts, at most 2 calls each

# Defaults for low-stakes fields (always disclosed in `defaults_applied`). `days` is NEVER defaulted.
DEFAULT_PACE = "balanced"
DEFAULT_DAY_START = "09:00"
DEFAULT_DAY_END = "19:00"
DEFAULT_VALUE_OF_TIME_JPY_PER_HOUR = 1000
DEFAULT_MAX_WALKING_MINUTES = 90

# Natural-language replanning: the model says a day part, code maps it to a clock time.
DAY_PART_START_TIMES = {"morning": "09:30", "midday": "12:00", "afternoon": "14:00", "evening": "17:30"}

BUDGET_SCOPE_TEXT = ("Budget covers attraction admissions + estimated transport fares only "
                     "(not hotel rooms, food or shopping).")
