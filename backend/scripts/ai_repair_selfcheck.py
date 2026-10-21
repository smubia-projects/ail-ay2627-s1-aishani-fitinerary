"""LIVE self-check of the AI repair and AI travel-style features, per city. OPT-IN, NOT part of pytest.

Four checks per city, through the SAME code path the API uses (app/llm/repair.py, app/llm/profile.py):
  repair_fit      "I can only do one thing a day but I want all of them: what can I change?" -> an allowlisted, solver-verified proposal that does NOT give up a must-visit
  repair_keep     "Keep my hotel but remove optional sights before touching my must-visits" with the hotel kept -> no hotel switch, no must-visit given up, nothing applied
  style_slow      "I prefer a slow trip with lots of breaks" -> inferred settings only, every one needing confirmation, no invented number the user did not write
  style_budget    "I have a limited budget but <place> is essential" -> a question instead of an invented amount, the place proposed as a must-visit
Runs only with RUN_LIVE_LLM_EVAL=1 and OPENROUTER_API_KEY + LLM_MODEL. Hard cap: config.REPAIR_SELFCHECK_MAX_CALLS. Never prints the key or any prompt. One run, a handful of checks: a smoke test, not a benchmark.
Run from backend/:   RUN_LIVE_LLM_EVAL=1 .venv/bin/python scripts/ai_repair_selfcheck.py        Writes ../docs/ai-repair-selfcheck.md (REPAIR_SELFCHECK_OUT_DIR overrides, used by tests with a MOCK client)."""
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_common as lc  # noqa: E402
from app import config  # noqa: E402
from app.llm.profile import parse_profile  # noqa: E402
from app.llm.prompts import PROMPT_VERSION  # noqa: E402
from app.llm.repair import parse_repair  # noqa: E402
from app.schemas import Preferences  # noqa: E402

CITIES = ("tokyo", "singapore", "seoul", "kuala_lumpur")
CHECKS = ("repair_fit", "repair_keep", "style_slow", "style_budget")
FORBIDDEN_FOR_KEEP = {"switch_hotel", "demote_must_visit"}


@dataclass
class Check:
    city: str
    name: str
    passed: bool
    reason: str
    calls: int


def scenario(ds):
    """A real infeasible request whose cause the actions can fix: three must-visits that DO fit in one day with an activity limit of 3 (checked by the optimiser), but the request says 1.
    The first such triple in id order is used, so the scenario is the same on every run."""
    from itertools import combinations

    from app.optimiser.solver import solve
    hotel = sorted(ds.hotels)[0]
    cands = [a for a in sorted(ds.attractions) if ds.attractions[a].priority != "skip"][:9]
    for ids in combinations(cands, 3):
        ok = solve(Preferences(days=1, must_visit=list(ids), pace="relaxed", start_weekday=2, max_activities_per_day=3), hotel, ds, with_alternatives=False, max_seconds=5)
        if ok.status == "feasible":
            return Preferences(days=1, must_visit=list(ids), pace="relaxed", start_weekday=2, max_activities_per_day=1), hotel, list(ids)
    raise RuntimeError(f"no fitting triple found for {ds.city.id}")


def run_city(city, ds, client):
    prefs, hotel, ids = scenario(ds)
    out = []
    n0 = client.total_calls
    r = parse_repair("I can only do one thing a day but I really want all of them. What can I change?", prefs, hotel, False, ds, client, solve_seconds=10)
    bad = [a.action for a in r.actions if a.action == "demote_must_visit"]
    ok = r.status == "ok" and bool(r.actions) and not bad and (r.verification or {}).get("state") in ("verified", "conditional") and not r.executed
    out.append(Check(city, CHECKS[0], ok, "allowlisted proposal, solver-verified, no must-visit given up" if ok else
                     f"status {r.status}, actions {[a.action for a in r.actions]}, given-up must-visit {bad}, verification {(r.verification or {}).get('state')}", client.total_calls - n0))
    n0 = client.total_calls
    r = parse_repair("Keep my hotel, but remove optional attractions before touching my must-visits.", prefs, hotel, True, ds, client, solve_seconds=10)
    bad = [a.action for a in r.actions if a.action in FORBIDDEN_FOR_KEEP]
    ok = not bad and not r.executed and all(a.id for a in r.actions)
    out.append(Check(city, CHECKS[1], ok, "no hotel switch and no must-visit given up" if ok else f"forbidden action proposed: {bad}", client.total_calls - n0))
    n0 = client.total_calls
    cur = {"pace": "balanced", "maxActivities": None, "maxDailyTravel": None, "valueOfTime": ds.city.default_value_of_time_per_hour}
    s = parse_profile("I prefer a slow trip with lots of breaks.", cur, [], ds, client)
    ok = s.status == "ok" and bool(s.settings) and all(x.source == "inferred" and x.requires_confirmation for x in s.settings) and not s.executed
    out.append(Check(city, CHECKS[2], ok, "inferred settings only, each needs confirmation" if ok else f"status {s.status}, sources {[x.source for x in s.settings]}", client.total_calls - n0))
    n0 = client.total_calls
    name = ds.attractions[ids[0]].name
    s = parse_profile(f"I have a limited budget but {name} is essential.", cur, [], ds, client)
    ok = bool(s.questions) and not any(x.field == "budget" for x in s.settings) and any(x.field == "must_visit" and x.target == ids[0] for x in s.settings)
    out.append(Check(city, CHECKS[3], ok, "asked for an amount instead of inventing one; the essential place proposed" if ok else
                     f"questions {len(s.questions)}, budget settings {[x.field for x in s.settings if x.field == 'budget']}, must-visit {[x.target for x in s.settings if x.field == 'must_visit']}", client.total_calls - n0))
    return out


def run_all(client, all_ds, report=print):
    results = []
    for city in CITIES:
        for c in run_city(city, all_ds[city], client):
            results.append(c)
            report(f"{'PASS' if c.passed else 'FAIL'}  {all_ds[city].city.display_name:<13} {c.name:<14} {c.calls} call(s)  {c.reason}")
    return results


def write_report(results, client, all_ds, out_dir):
    n = sum(c.passed for c in results)
    L = ["# AI repair and travel-style self-check (live)", "",
         f"Model: `{client.model}`. {lc.endpoint_stamp(client)} PROMPT_VERSION: `{PROMPT_VERSION}`. LLM code fingerprint: `{lc.llm_fingerprint()}`. Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
         f"Git: `{lc.git_hash()}`. Checks: N = {len(results)} ({len(CHECKS)} per city, 4 cities). Model calls used: {client.total_calls} of a hard cap of {config.REPAIR_SELFCHECK_MAX_CALLS}. Produced by `backend/scripts/ai_repair_selfcheck.py`.", "",
         "A smoke check of the live pipeline, not a benchmark: one run, one prompt per check, developer-written. A FAIL is reported as it happened.", "", f"**{n} of {len(results)} checks passed.**", "",
         "| City | " + " | ".join(CHECKS) + " | Passed |", "|---|" + "---|" * (len(CHECKS) + 1)]
    for city in CITIES:
        cs = [c for c in results if c.city == city]
        L.append(f"| {all_ds[city].city.display_name} | " + " | ".join("PASS" if c.passed else "FAIL" for c in cs) + f" | {sum(c.passed for c in cs)}/{len(cs)} |")
    L += ["", "## Reasons", "", "| City | Check | Result | Calls | Reason |", "|---|---|---|---|---|"]
    L += [f"| {all_ds[c.city].city.display_name} | {c.name} | {'PASS' if c.passed else 'FAIL'} | {c.calls} | {c.reason} |" for c in results]
    L += ["", "No prompts, no model replies and no key are stored in this file. All data is sample data."]
    path = out_dir / "ai-repair-selfcheck.md"
    path.write_text("\n".join(L) + "\n")
    return path


def main():
    if "--plan" in sys.argv:
        print(f"Checks: {len(CHECKS)} per city x {len(CITIES)} = {len(CHECKS) * len(CITIES)}; worst case 2 calls each = {2 * len(CHECKS) * len(CITIES)}; hard cap {config.REPAIR_SELFCHECK_MAX_CALLS}. Zero calls were made.")
        return 0
    lc.load_env_file()
    msg = lc.gate("scripts/ai_repair_selfcheck.py", config.REPAIR_SELFCHECK_MAX_CALLS)
    if msg:
        print(msg)
        return 0
    from app.data.loader import load_all_city_datasets
    from app.llm.client import OpenRouterLLMClient
    all_ds = load_all_city_datasets()
    client = lc.Capped(OpenRouterLLMClient.from_env(), config.REPAIR_SELFCHECK_MAX_CALLS)
    try:
        results = run_all(client, all_ds)
    except lc.RunAborted as e:
        print(f"\nStopped after {client.total_calls} call(s): {e}\nNo report was written.")
        return 2
    path = write_report(results, client, all_ds, Path(os.environ.get("REPAIR_SELFCHECK_OUT_DIR") or lc.REPO / "docs"))
    print(f"\n{sum(c.passed for c in results)} of {len(results)} checks passed; {client.total_calls} model call(s) used. Wrote {path}.")
    return 0 if all(c.passed for c in results) else 1


if __name__ == "__main__":
    sys.exit(main())
