"""LIVE AI SELF-CHECK, per city. OPT-IN, NOT part of pytest.

For each city (Tokyo, Singapore, Seoul, Kuala Lumpur) it runs 5 checks through the SAME validation pipeline the API uses:
  1 typical request parsed        2 wrong-city destination stopped      3 ambiguous name flagged
  4 currency symbol read safely   5 explanation of a real computed result passes the grounding check
Runs only with RUN_LIVE_LLM_EVAL=1 and OPENROUTER_API_KEY + LLM_MODEL (exported, or in backend/.env). Hard cap: config.SELFCHECK_MAX_CALLS.
Never prints the key or any prompt; prints PASS/FAIL per check with a short reason.
Run from backend/:   RUN_LIVE_LLM_EVAL=1 .venv/bin/python scripts/ai_selfcheck.py
Zero-call plan:      .venv/bin/python scripts/ai_selfcheck.py --plan
Writes ../docs/ai-selfcheck.md (override the folder with SELFCHECK_OUT_DIR, used by tests with a MOCK client).
"""
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_common as lc  # noqa: E402
from app import config  # noqa: E402
from app.llm.explain import explain  # noqa: E402
from app.llm.extract import extract_preferences  # noqa: E402
from app.llm.prompts import PROMPT_VERSION  # noqa: E402

CITIES = ("tokyo", "singapore", "seoul", "kuala_lumpur")
FIXTURES = lc.ROOT / "tests" / "fixtures"
CHECKS = ("parse_typical", "wrong_city", "ambiguous_name", "currency_symbol", "explain_real_result")
# Check 4: a budget written with the symbol people actually type. expect = local-currency budget the CODE must derive, or None = must ask the user.
CURRENCY_CASES = {
    "tokyo": ("2 days in Tokyo, budget ¥25,000.", 25000),
    "seoul": ("2 days in Seoul, budget ₩250,000.", 250000),
    "singapore": ("2 days in Singapore, budget $200.", None),   # "$" is never guessed: it must come back as a confirmation
    "kuala_lumpur": ("2 days in Kuala Lumpur, budget RM 300.", 300),
}


@dataclass
class Check:
    city: str
    name: str
    passed: bool
    reason: str
    calls: int
    inconclusive: bool = False       # the model was unreachable: this says nothing about its answers; it is NOT a pass and NOT a model failure, and it must be re-run


def _norm(text: str) -> str:
    """'the museum', 'Museums' and 'museum' are the same word for this check (a real model rewords the user's phrase)."""
    w = text.strip().lower()
    w = w[4:] if w.startswith("the ") else w
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _prompts() -> dict:
    out: dict = {}
    for p in json.loads((FIXTURES / "live_eval_prompts_by_city.json").read_text()):
        out.setdefault(p["city"], {})[p["kind"]] = p
    return out


def run_city(city: str, ds, all_ds, client, prompts: dict) -> list[Check]:
    def parse(text):
        before = client.total_calls
        r = extract_preferences(text, ds, client, max_input_chars=4000, all_datasets=all_ds)
        return r, client.total_calls - before

    out: list[Check] = []
    p = prompts[city]["typical"]
    r, n = parse(p["text"])
    got = {x.id for x in r.resolved_names} | set(r.draft.must_visit if r.draft else [])
    missing = sorted(set(p["expect_resolved"]) - got)
    ok = r.draft is not None and not missing
    if r.status == "llm_unavailable":
        out.append(Check(city, CHECKS[0], False, "INCONCLUSIVE: the model could not be reached (no answer to judge); re-run this check", n, inconclusive=True))
    else:
      out.append(Check(city, CHECKS[0], ok, "draft built and expected places resolved" if ok else
                       (f"no draft (status {r.status})" if r.draft is None else f"places not resolved: {', '.join(missing)}"), n))

    p = prompts[city]["wrong_city"]
    r, n = parse(p["text"])
    want, d = p["expect_wrong_city"], r.destination or {}
    ok = r.draft is None and (d.get("status") == "unsupported" if want == "unsupported" else
                              d.get("status") in ("other_city", "mixed") and (r.switch_offer or {}).get("city_id") == want)
    out.append(Check(city, CHECKS[1], ok, ("stopped, nothing planned" + (", switch offered to " + want if want != "unsupported" else ", city not available")
                                          + (" (by the code check: 0 model calls)" if n == 0 else "")) if ok else
                     f"not stopped: draft {'built' if r.draft else 'none'}, destination status {d.get('status')}", n))

    p = prompts[city]["ambiguous"]
    r, n = parse(p["text"])
    want = _norm(p["expect_ambiguous"][0])
    ok = any(want in _norm(a.input) or _norm(a.input) in want for a in r.ambiguities) and not any(want in _norm(x.input) for x in r.resolved_names)
    out.append(Check(city, CHECKS[2], ok, "flagged as ambiguous, not guessed" if ok else "ambiguity not reported (a place may have been guessed)", n))

    text, expect = CURRENCY_CASES[city]
    r, n = parse(text)
    budget = r.draft.budget if r.draft else None
    if expect is None:
        ok = bool(r.currency_confirmations) and budget is None
        why = "ambiguous symbol sent back for confirmation, no amount guessed" if ok else "ambiguous symbol was not confirmed"
    else:
        ok = budget == expect and not r.currency_confirmations
        why = f"read as {expect:,} {ds.city.currency_code}" if ok else f"expected {expect:,} {ds.city.currency_code}, got {budget}"
    out.append(Check(city, CHECKS[3], ok, why, n))

    before = client.total_calls
    e = explain(lc.real_result(ds), ds, client, all_datasets=all_ds)
    n = client.total_calls - before
    ok = e.source == "llm"
    out.append(Check(city, CHECKS[4], ok, f"model wording passed the grounding check ({e.grounding.checked} figures/names checked)" if ok else
                     f"template used (safe fallback): {e.reason}; {len(e.grounding.rejected_tokens)} item(s) rejected"
                     + (f": {', '.join(str(t)[:40] for t in e.grounding.rejected_tokens[:5])}" if e.grounding.rejected_tokens else ""), n))
    return out


def run_all(client, all_ds, report=print) -> list[Check]:
    prompts, results = _prompts(), []
    for city in CITIES:
        for c in run_city(city, all_ds[city], all_ds, client, prompts):
            results.append(c)
            report(f"{'PASS' if c.passed else 'INCONCLUSIVE' if c.inconclusive else 'FAIL'}  {all_ds[city].city.display_name:<13} {c.name:<20} {c.calls} call(s)  {c.reason}")
    return results


def write_report(results: list[Check], client, all_ds, out_dir: Path) -> Path:
    n_pass = sum(c.passed for c in results)
    per = {city: [c for c in results if c.city == city] for city in CITIES}
    lines = ["# AI self-check (live)", "",
             f"Model: `{client.model}`. {lc.endpoint_stamp(client)} PROMPT_VERSION: `{PROMPT_VERSION}`. LLM code fingerprint: `{lc.llm_fingerprint()}`. Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
             f"Git: `{lc.git_hash()}`. Checks: N = {len(results)} ({len(CHECKS)} per city, 4 cities). Model calls used: {client.total_calls} of a hard cap of {config.SELFCHECK_MAX_CALLS}. "
             "Produced by `backend/scripts/ai_selfcheck.py`.", "",
             "This is a smoke check of the live pipeline, not a benchmark: one run, one prompt per check, a handful of checks per city. "
             "A FAIL is reported as it happened; a template fallback on check 5 is safe behaviour but counts as FAIL here because the model's wording was not accepted.", "",
             f"**{n_pass} of {len(results)} checks passed.**" + (f" {sum(c.inconclusive for c in results)} inconclusive (the model could not be reached; not a pass, not a model failure; re-run needed)." if any(c.inconclusive for c in results) else ""), "",
             "| City | " + " | ".join(CHECKS) + " | Passed |", "|---|" + "---|" * (len(CHECKS) + 1)]
    for city in CITIES:
        lines.append(f"| {all_ds[city].city.display_name} | " + " | ".join("PASS" if c.passed else "INCONCLUSIVE" if c.inconclusive else "FAIL" for c in per[city]) + f" | {sum(c.passed for c in per[city])}/{len(per[city])} |")
    lines += ["", "## Reasons", "", "| City | Check | Result | Calls | Reason |", "|---|---|---|---|---|"]
    lines += [f"| {all_ds[c.city].city.display_name} | {c.name} | {'PASS' if c.passed else 'INCONCLUSIVE' if c.inconclusive else 'FAIL'} | {c.calls} | {c.reason} |" for c in results]
    lines += ["", "No prompts, no model replies and no key are stored in this file. All data is sample data."]
    path = out_dir / "ai-selfcheck.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def plan() -> int:
    print(f"Checks: {len(CHECKS)} per city x {len(CITIES)} cities = {len(CHECKS) * len(CITIES)}. "
          f"Worst-case model calls: 9 per city (4 parses with one retry each, plus 1 explanation) = 36; hard cap {config.SELFCHECK_MAX_CALLS}. "
          "A wrong-city request that names another supported city is stopped by the code check and uses 0 calls. Zero calls were made.")
    return 0


def main() -> int:
    if "--plan" in sys.argv:
        return plan()
    lc.load_env_file()
    msg = lc.gate("scripts/ai_selfcheck.py", config.SELFCHECK_MAX_CALLS)
    if msg:
        print(msg)
        return 0
    from app.data.loader import load_all_city_datasets
    from app.llm.client import OpenRouterLLMClient

    all_ds = load_all_city_datasets()
    client = lc.Capped(OpenRouterLLMClient.from_env(), config.SELFCHECK_MAX_CALLS)
    try:
        results = run_all(client, all_ds)
    except lc.RunAborted as e:
        print(f"\nStopped after {client.total_calls} call(s): {e}\nNo report was written.")
        return 2
    out_dir = Path(os.environ.get("SELFCHECK_OUT_DIR") or lc.REPO / "docs")
    path = write_report(results, client, all_ds, out_dir)
    print(f"\n{sum(c.passed for c in results)} of {len(results)} checks passed; {client.total_calls} model call(s) used. Wrote {path}.")
    return 0 if all(c.passed for c in results) else 1


if __name__ == "__main__":
    sys.exit(main())
