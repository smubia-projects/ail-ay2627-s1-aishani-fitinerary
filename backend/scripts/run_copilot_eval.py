"""Reproducible copilot evaluation: 76 realistic questions over four cities, five metrics reported SEPARATELY, three predictors clearly labelled.

  oracle    scripted intent = the author's expected answer. Measures INTEGRATION only (routing, grounding, no mutation, AI-off behaviour). It is a mock; its intent accuracy is 100% by construction
            and says nothing about any model.
  keyword   a deterministic keyword classifier. NOT a model. Written by the case author AFTER seeing the cases, so it is optimistic and case-aware (not a lower bound, not independent); it only shows that the
            missing-data and repair-phrased cases are not solved by simple keyword rules.
  live      the real model (one call per AI case, hard cap). NEVER run without approval: `--plan` prints the call count, token envelope and the price formula first; `--live` needs RUN_LIVE_LLM_EVAL=1.

Metrics: intent accuracy (the operation the classifier chose), clarification appropriateness (recall on cases whose right answer is CLARIFY; over-clarification where it is not), routing (the hand-off the
page would show), factual grounding (every evidence value equals an INDEPENDENT recomputation from the dataset and the schedule; no number in a message that is not in the evidence), task success (all of those
plus nothing executed and no model call for AI-off questions). Cases and expectations are in tests/fixtures/copilot_eval_cases.json: developer-authored, frozen before any model ran on them, not independent.
Usage (from backend/):  python scripts/run_copilot_eval.py [--mode oracle|keyword] [--out DIR]    |   python scripts/run_copilot_eval.py --plan    |    RUN_LIVE_LLM_EVAL=1 python scripts/run_copilot_eval.py --live"""
import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.data.loader import load_all_city_datasets  # noqa: E402
from app.llm.client import LLMResponse, get_llm_client  # noqa: E402
from app.llm.prompts import PROMPT_VERSION  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schemas import Preferences  # noqa: E402

CASES = ROOT / "tests" / "fixtures" / "copilot_eval_cases.json"
DOCS = ROOT.parent / "docs" / "eval"
UNKNOWN = {"tokyo": ("teamlab_planets", "akihabara"), "singapore": ("maxwell_food_centre", "sentosa"), "seoul": ("gwangjang_market", "myeongdong"), "kuala_lumpur": ("jalan_alor", "national_mosque_malaysia")}
LIVE_CALL_CAP = 80


# ---- predictors ----------------------------------------------------------------------------------------------------------------------------
class Oracle:
    model = "oracle-mock"

    def __init__(self, op, name=None):
        self.op, self.name, self.calls = op, name, 0

    def complete(self, system, messages, max_tokens):
        self.calls += 1
        return LLMResponse(text=json.dumps({"operation": self.op, "attraction_name": self.name}), input_tokens=1, output_tokens=1)


RULES = [("CLARIFY", ["make it better", "improve it"]), ("UNSUPPORTED", ["book ", "taxi", "safe for", "wheelchair", "accessible"]),
         ("EXPLAIN_OMISSION", ["isn't ", "not in my plan", "left out", "omitted"]), ("PREVIEW_SCENARIO", ["what if"]),
         ("EXPLAIN_CONSTRAINT", ["can't fit", "doesn't fit", "why can't", "no opening hours", "doesn't work"]), ("PROPOSE_REPAIR", ["what would need to change", "what can i change"]),
         ("ANALYSE_TRADEOFF", ["cheaper", "less travel", "cheapest"]), ("COMPARE_ALTERNATIVES", ["compare", "arrangements", "options"]),
         ("EXPLAIN_COST_BREAKDOWN", ["expensive", "included in the total", "price", "cost"]), ("IDENTIFY_UNCERTAINTY", ["reliable", "double-check", "uncertain"]), ("EXPLAIN_PLAN", ["why did you", "why this"])]


class Keyword:
    """A deterministic keyword classifier: NOT a language model."""
    model = "keyword-baseline"

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, max_tokens):
        self.calls += 1
        text = messages[0]["content"].lower()
        # a clarification cue: two goals joined by "but also"/"and also" is conflicting
        if "as cheap as possible but also" in text:
            op = "CLARIFY"
        else:
            op = next((o for o, keys in RULES if any(k in text for k in keys)), "CLARIFY")
        m = re.search(r"why isn't (.+?) in my plan", text)
        return LLMResponse(text=json.dumps({"operation": op, "attraction_name": None}), input_tokens=1, output_tokens=1)


# ---- scenarios ----------------------------------------------------------------------------------------------------------------------------
def build_prefs(ds, scenario: str, city: str) -> Preferences:
    from ai_repair_selfcheck import scenario as triple
    prefs, hotel, ids = triple(ds)
    if scenario == "feasible":
        return prefs.model_copy(update={"max_activities_per_day": 3}), hotel
    if scenario == "infeasible":
        return prefs, hotel
    price, hours = UNKNOWN[city]
    if scenario == "blocked":
        return Preferences(days=2, must_visit=[hours], start_weekday=2), hotel
    return Preferences(days=2, must_visit=[price], start_weekday=2), hotel


def independent_totals(ds, hotel_id, result, nights):
    """Spending and travel recomputed from the dataset and the schedule's own order, sharing no code with the cost summary or the solver."""
    adm = sum(ds.attractions[s["attraction_id"]]["admission_cost"] if False else (ds.attractions[s["attraction_id"]].admission_cost or 0) for d in result["days"] for s in d["stops"])
    tr = fare = 0
    for d in result["days"]:
        prev = hotel_id
        for s in d["stops"]:
            a = s["attraction_id"]
            tr += ds.minutes[prev][a]; fare += ds.fare_jpy[prev][a]; prev = a
        if d["stops"]:
            tr += ds.minutes[prev][hotel_id]; fare += ds.fare_jpy[prev][hotel_id]
    rooms = ds.hotels[hotel_id].nightly_rate * nights
    return {"admissions": adm, "transport": fare, "rooms": rooms, "cash_total_with_rooms": adm + fare + rooms, "travel_minutes": tr}


def grounded(out, ds, plan, prefs):
    """True if every checkable evidence value equals an independent recomputation and no message number is unexplained. Returns (ok, notes)."""
    notes = []
    res = plan["result"]
    feasible = res["status"] == "feasible"
    nights = prefs.days - 1 if prefs.nights is None else prefs.nights
    tot = independent_totals(ds, res["hotel_id"], res, nights) if feasible else None
    scheduled = sum(len(d["stops"]) for d in res["days"])
    known = set()
    for e in out["evidence"]:
        ref, val = e["reference"], e["value"]
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            known.add(str(val))
        elif isinstance(val, str) and e["kind"] == "calculated":
            known.update(re.findall(r"\d[\d,]*", val))
        if feasible and ref.startswith("cost_summary.trip."):
            key = ref.rsplit(".", 1)[1]
            if key in tot and val != tot[key]:
                notes.append(f"{ref}: {val} != independent {tot[key]}")
        if feasible and ref == "result.totals.travel_minutes" and val != tot["travel_minutes"]:
            notes.append(f"{ref}: {val} != independent {tot['travel_minutes']}")
        if feasible and ref == "result.totals.attractions_scheduled" and val != scheduled:
            notes.append(f"{ref}: {val} != {scheduled}")
        if ref == "preferences.must_visit" and sorted(val) != sorted(ds.attractions[a].name for a in prefs.must_visit):
            notes.append("preferences.must_visit names differ")
        if e["kind"] == "dataset" and ref.startswith("attractions.") and val not in {a.name for a in ds.attractions.values()}:
            notes.append(f"dataset value not in the data: {val}")
    for n in re.findall(r"\d[\d,]*", out["message"]):
        if n.replace(",", "") not in {k.replace(",", "") for k in known} and n not in ("1", "2", "3"):
            notes.append(f"message number {n} is not in the evidence")
    return not notes, notes


def expected_handoff(case, absent_present: bool | None):
    h = case["expected_handoff"]
    if h == "*":
        return None if absent_present else "scenario"
    return h


# ---- run ---------------------------------------------------------------------------------------------------------------------------------------
def run(mode: str, client_factory=None, label=None):
    cases = json.loads(CASES.read_text())["cases"]
    all_ds = load_all_city_datasets()
    rows = []
    import os
    saved = {k: os.environ.get(k) for k in ("RATE_LIMIT_EXPENSIVE_PER_MIN", "RATE_LIMIT_PER_MIN", "RATE_LIMIT_CHEAP_PER_MIN", "SOLVER_CONCURRENCY", "LLM_DAILY_CALL_CAP")}
    os.environ.update(RATE_LIMIT_EXPENSIVE_PER_MIN="10000", RATE_LIMIT_PER_MIN="10000", RATE_LIMIT_CHEAP_PER_MIN="10000", SOLVER_CONCURRENCY="4", LLM_DAILY_CALL_CAP="10000")   # the evaluation is not abuse; the shipped limits are tested elsewhere
    app = create_app()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    with TestClient(app) as api:
        plans = {}
        for city in all_ds:
            ds = all_ds[city]
            for sc in ("feasible", "infeasible", "blocked", "unknown_price"):
                prefs, hotel = build_prefs(ds, sc, city)
                app.dependency_overrides[get_llm_client] = lambda: None
                plan = api.post("/optimise", json={"city_id": city, "preferences": prefs.model_dump(), "hotel_id": hotel}).json()
                plans[(city, sc)] = (prefs, plan)
        for c in cases:
            city, sc = c["city"], c["scenario"]
            ds = all_ds[city]
            prefs, plan = plans[(city, sc)]
            text, name = c["text"], c["attraction_name"]
            absent_present = None
            if name == "__ABSENT__":
                scheduled = {s["attraction_id"] for d in plan["result"]["days"] for s in d["stops"]}
                pick = next(a for a in sorted(ds.attractions) if a not in scheduled and a not in prefs.skip and a not in prefs.must_visit)
                name = ds.attractions[pick].name
                text = f"Why isn't {name} in my plan?"
                absent_present = False
            body = {"city_id": city, "preferences": prefs.model_dump(), "current_result": plan["result"], "fingerprint": plan["fingerprint"], "text": text, "operation": None}
            if not c["ai_available"]:
                body.update(text="", operation=c["expected_operation"])
                app.dependency_overrides[get_llm_client] = lambda: None
                client = None
            else:
                client = client_factory(c, name)
                app.dependency_overrides[get_llm_client] = lambda cl=client: cl
            r = api.post("/copilot", json=body)
            out = r.json()
            ok_http = r.status_code == 200
            op = out.get("operation") if ok_http else None
            if ok_http and out.get("status") == "fallback":
                op = "CLARIFY"                       # the code fell back safely: the person is asked to rephrase / use the controls
            exp_h = expected_handoff(c, absent_present)
            g, notes = grounded(out, ds, plan, prefs) if ok_http else (False, ["http error"])
            intent_ok = (op == c["expected_operation"]) if c["ai_available"] else None
            route_ok = ok_http and out.get("handoff") == exp_h
            executed = bool(out.get("executed"))
            ai_off_ok = (out.get("call_count") == 0 and out.get("mode") == "deterministic") if not c["ai_available"] else None
            success = ok_http and (intent_ok is not False) and route_ok and g and not executed and (ai_off_ok is not False)
            rows.append({"id": c["id"], "city": city, "category": c["category"], "scenario": sc, "ai": c["ai_available"], "expected": c["expected_operation"], "got": op, "intent_ok": intent_ok,
                         "expected_handoff": exp_h, "handoff": out.get("handoff"), "route_ok": route_ok, "grounded": g, "grounding_notes": notes, "executed": executed, "ai_off_ok": ai_off_ok, "success": success})
    app.dependency_overrides.pop(get_llm_client, None)
    return rows


def pct(n, d):
    return None if not d else round(100 * n / d, 1)


def summarise(rows):
    ai = [r for r in rows if r["ai"]]
    clar_exp = [r for r in ai if r["expected"] == "CLARIFY"]
    non_clar = [r for r in ai if r["expected"] != "CLARIFY"]
    s = {"cases": len(rows), "ai_cases": len(ai), "ai_off_cases": len(rows) - len(ai),
         "intent_accuracy": {"correct": sum(r["intent_ok"] for r in ai), "of": len(ai), "pct": pct(sum(r["intent_ok"] for r in ai), len(ai))},
         "clarification": {"recall_correct": sum(r["got"] == "CLARIFY" for r in clar_exp), "recall_of": len(clar_exp), "over_clarified": sum(r["got"] == "CLARIFY" for r in non_clar), "over_of": len(non_clar)},
         "routing": {"correct": sum(r["route_ok"] for r in rows), "of": len(rows), "pct": pct(sum(r["route_ok"] for r in rows), len(rows))},
         "grounding": {"grounded": sum(r["grounded"] for r in rows), "of": len(rows), "pct": pct(sum(r["grounded"] for r in rows), len(rows))},
         "ai_off": {"correct": sum(bool(r["ai_off_ok"]) for r in rows if not r["ai"]), "of": len(rows) - len(ai)},
         "task_success": {"success": sum(r["success"] for r in rows), "of": len(rows), "pct": pct(sum(r["success"] for r in rows), len(rows))},
         "executed_anything": sum(r["executed"] for r in rows)}
    by = defaultdict(list)
    for r in rows:
        by[r["category"]].append(r)
    s["by_category"] = {k: {"n": len(v), "intent": f"{sum(bool(x['intent_ok']) for x in v if x['ai'])}/{sum(x['ai'] for x in v)}", "route": f"{sum(x['route_ok'] for x in v)}/{len(v)}", "success": f"{sum(x['success'] for x in v)}/{len(v)}"} for k, v in sorted(by.items())}
    byc = defaultdict(list)
    for r in rows:
        byc[r["city"]].append(r)
    s["by_city"] = {k: {"n": len(v), "intent": f"{sum(bool(x['intent_ok']) for x in v if x['ai'])}/{sum(x['ai'] for x in v)}", "success": f"{sum(x['success'] for x in v)}/{len(v)}"} for k, v in byc.items()}
    return s


def render(mode, s, rows, extra=""):
    label = {"oracle": "MOCK ORACLE (integration only: intent accuracy is 100% by construction and says nothing about any model)", "keyword": "KEYWORD BASELINE (deterministic rules written by the case author AFTER seeing the cases: optimistic and case-aware, NOT a language model, and not an independent or lower-bound measure)", "live": "LIVE MODEL"}[mode]
    L = [f"# Copilot evaluation: {label}", "", f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by `backend/scripts/run_copilot_eval.py`. Prompt version `{PROMPT_VERSION}`. Cases: `backend/tests/fixtures/copilot_eval_cases.json` "
         f"({s['cases']} cases, {s['ai_cases']} need a classifier, {s['ai_off_cases']} are AI-off quick questions), developer-authored, frozen before any model ran on them, **not independent**. Sample data, fictional hotels. {extra}", "",
         "| Metric | Result |", "|---|---|",
         f"| Intent accuracy | {s['intent_accuracy']['correct']} of {s['intent_accuracy']['of']} ({s['intent_accuracy']['pct']}%) |",
         f"| Clarification: right answer was CLARIFY and it was chosen | {s['clarification']['recall_correct']} of {s['clarification']['recall_of']} |",
         f"| Clarification: asked when it should have answered (over-clarification) | {s['clarification']['over_clarified']} of {s['clarification']['over_of']} |",
         f"| Routing (hand-off shown) | {s['routing']['correct']} of {s['routing']['of']} ({s['routing']['pct']}%) |",
         f"| Factual grounding (evidence equals an independent recomputation; no unexplained number) | {s['grounding']['grounded']} of {s['grounding']['of']} ({s['grounding']['pct']}%) |",
         f"| AI-off quick questions answered with no model call | {s['ai_off']['correct']} of {s['ai_off']['of']} |",
         f"| Task success (all of the above, nothing executed) | {s['task_success']['success']} of {s['task_success']['of']} ({s['task_success']['pct']}%) |",
         f"| Anything executed by a question | {s['executed_anything']} |", "", "## By category", "", "| Category | Cases | Intent | Routing | Success |", "|---|---|---|---|---|"]
    L += [f"| {k} | {v['n']} | {v['intent']} | {v['route']} | {v['success']} |" for k, v in s["by_category"].items()]
    L += ["", "## By city (never pooled in the claims)", "", "| City | Cases | Intent | Success |", "|---|---|---|---|"] + [f"| {k} | {v['n']} | {v['intent']} | {v['success']} |" for k, v in s["by_city"].items()]
    bad = [r for r in rows if not r["success"]]
    L += ["", f"## Cases that did not succeed ({len(bad)})", ""]
    L += ([f"- `{r['id']}`: expected `{r['expected']}`, got `{r['got']}`; hand-off expected `{r['expected_handoff']}`, got `{r['handoff']}`" + (f"; grounding: {'; '.join(r['grounding_notes'])}" if r["grounding_notes"] else "") for r in bad] or ["None."])
    L += ["", "## Limits", "", "- Expectations are the author's judgement; a legitimately ambiguous question may be answered differently by a reasonable classifier.", "- Grounding compares evidence with an independent recomputation of spending and travel and with the dataset; constraint findings come from the solver's own checks and are not independently recomputed here.",
          "- Mock and keyword results are not model results. A live run has not been made unless a live file exists.", ""]
    return "\n".join(L)


def plan_text():
    n = sum(1 for c in json.loads(CASES.read_text())["cases"] if c["ai_available"])
    return (f"Live run plan (nothing was run): {n} model calls (one per AI case, no retries), hard cap {LIVE_CALL_CAP}. Token envelope: about 400 input tokens per call (fixed classification prompt + the question) = {n * 400:,} "
            f"and about 40 output tokens per call = {n * 40:,}; planning envelope with margin: {n * 600:,} input / {n * 100:,} output. Estimated price = {n * 600 / 1e6:.3f} x (input price per million tokens) + {n * 100 / 1e6:.3f} x (output price per million tokens); "
            "no provider price was assumed. Requires your explicit approval and RUN_LIVE_LLM_EVAL=1 plus API_KEY and MODEL_NAME.")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["oracle", "keyword"], default="oracle")
    ap.add_argument("--out", default=str(DOCS))
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args(argv)
    if a.plan:
        print(plan_text())
        return 0
    if a.live:
        import live_common as lc
        lc.load_env_file()
        msg = lc.gate("scripts/run_copilot_eval.py", LIVE_CALL_CAP)
        if msg:
            print(msg)
            return 0
        from app.llm.client import OpenRouterLLMClient
        capped = lc.Capped(OpenRouterLLMClient.from_env(), LIVE_CALL_CAP)
        rows = run("live", lambda c, name: capped)
        s = summarise(rows)
        out = Path(a.out)
        (out / "copilot-eval-live.json").write_text(json.dumps({"summary": s, "rows": rows, "model": capped.model, "calls": capped.total_calls}, indent=1) + "\n")
        (out / "copilot-eval-live.md").write_text(render("live", s, rows, f"Model `{capped.model}`, {capped.total_calls} calls. LLM code fingerprint `{lc.llm_fingerprint()}`."))
        print(f"wrote live report; {capped.total_calls} calls")
        return 0
    factory = (lambda c, name: Oracle(c["expected_operation"], name)) if a.mode == "oracle" else (lambda c, name: Keyword())
    rows = run(a.mode, factory)
    s = summarise(rows)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"copilot-eval-{a.mode}.json").write_text(json.dumps({"summary": s, "rows": rows}, indent=1) + "\n")
    (out / f"copilot-eval-{a.mode}.md").write_text(render(a.mode, s, rows))
    print(json.dumps({k: s[k] for k in ("cases", "intent_accuracy", "routing", "grounding", "task_success")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
