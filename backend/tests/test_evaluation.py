"""Phase 7: the evaluation harness. No LLM, no network (sockets are blocked by conftest), no wall-clock-dependent assertions
except the one --quick budget required by the brief.

The 15 scenarios are defined ONCE in app/eval/scenarios.py; this file and scripts/run_evaluation.py both import them.
"""
import ast
import importlib
import json
import re
import sys
import time
from pathlib import Path

import pytest

from app import config
from app.data.loader import load_dataset
from app.eval import claims, generator, independent, metrics
from app.eval.limits import WALL_CLOCK_LIMIT_KEYS, deterministic_limits, pace_balance_off
from app.eval.scenarios import SCENARIOS, run_all, run_scenario
from app.optimiser.baseline import baseline, nearest_neighbour
from app.optimiser.solver import solve
from app.schemas import Preferences
from app.validator.feasibility_report import build_report

BACKEND = Path(__file__).resolve().parents[1]
DS = load_dataset()
ASA = "hotel_lantern_asakusa"
SEED = 20261013


def ids(res):
    return [s.attraction_id for d in res.days for s in d.stops]


def validate(res, prefs, hotel=ASA):
    return build_report(res.model_copy(update={"status": "feasible"}), DS, prefs, hotel)


def strip_wall_clock(x):
    if isinstance(x, dict):
        return {k: strip_wall_clock(v) for k, v in x.items() if k not in ("runtime_seconds", "machine_dependent")}
    if isinstance(x, list):
        return [strip_wall_clock(v) for v in x]
    return x


# ---------------------------------------------------------------------------------------------------------------------
# The 15 scenarios: defined once, run here and by the script
# ---------------------------------------------------------------------------------------------------------------------
def test_fifteen_scenarios_are_defined_exactly_once_in_the_shared_module():
    assert [s.num for s in SCENARIOS] == list(range(1, 16))
    assert len({s.name for s in SCENARIOS}) == 15
    assert [s.num for s in SCENARIOS if s.mocked_llm] == [13, 14, 15]
    defs, assigns = [], []
    for path in list((BACKEND / "app").rglob("*.py")) + list((BACKEND / "scripts").glob("*.py")) + list((BACKEND / "tests").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Scenario" and node.args \
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value in range(1, 16):
                defs.append(path.name)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(getattr(t, "id", "") == "SCENARIOS" for t in targets):
                    assigns.append(path.name)
    assert defs == ["scenarios.py"] * 15 and assigns == ["scenarios.py"], (defs, assigns)
    script = (BACKEND / "scripts" / "run_evaluation.py").read_text()
    assert "from app.eval.scenarios import run_all" in script and "def s0" not in script


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: f"s{s.num:02d}")
def test_each_shared_scenario_passes(sc):
    r = run_scenario(sc, DS)
    assert r.passed, r.observed
    assert r.mocked_llm == (sc.num >= 13)


def test_a_failing_scenario_is_reported_not_raised():
    from app.eval.scenarios import Scenario
    boom = Scenario(99, "x", "boom", "never", lambda ds: 1 / 0)
    lie = Scenario(98, "x", "lie", "never", lambda ds: (False, "observed something else"))
    assert not run_scenario(boom, DS).passed and "ZeroDivisionError" in run_scenario(boom, DS).observed
    assert not run_scenario(lie, DS).passed


def test_deterministic_limits_lift_wall_clock_limits_and_restore_them():
    before = {k: getattr(config, k) for k in WALL_CLOCK_LIMIT_KEYS}
    with deterministic_limits() as lim:
        assert all(getattr(config, k) >= 3600 for k in WALL_CLOCK_LIMIT_KEYS)
        assert lim["max_deterministic_time"] == config.SOLVER_MAX_DETERMINISTIC_TIME and lim["solver_workers"] == 1
    assert {k: getattr(config, k) for k in WALL_CLOCK_LIMIT_KEYS} == before
    with pytest.raises(RuntimeError):
        with deterministic_limits():
            raise RuntimeError("restored even on error")
    assert {k: getattr(config, k) for k in WALL_CLOCK_LIMIT_KEYS} == before


def test_pace_balance_diagnostic_changes_only_the_weight_and_always_restores_it():
    w = config.W_PACE
    assert w > 0
    with pace_balance_off():
        assert config.W_PACE == 0
    assert config.W_PACE == w
    with pytest.raises(RuntimeError):
        with pace_balance_off():
            raise RuntimeError("restored even on error")
    assert config.W_PACE == w
    t = next(t for t in generator.generate_trips(SEED, 12, DS) if len(t["order"]) >= 6 and t["days"] >= 3)
    with deterministic_limits():
        shipped = metrics.solver_record(t, DS, "phase2_default")
        product = metrics.solver_record(t, DS)
        with pace_balance_off():
            off = metrics.solver_record(t, DS, "phase2_default")
            product_off = metrics.solver_record(t, DS)
    assert shipped["status"] == off["status"] and len(shipped["stops_per_day"]) == t["days"] == len(off["stops_per_day"])
    assert strip_wall_clock(product) == strip_wall_clock(product_off)   # the product (true-cost) objective has no pace-balance term


def test_headline_solver_runs_the_same_objective_as_post_optimise(monkeypatch):
    """The evaluation must measure what ships: compare its solver record with what the real route returns for the same trips.
    The live app adds a small balance nudge (money.empty_day_penalty / spread_penalty) that the committed evaluation does not have, so the nudge is switched
    off here: with it off, /optimise must reproduce the evaluation exactly. The evidence therefore describes the shipped objective minus that nudge."""
    from fastapi.testclient import TestClient
    import app.main as main_module
    from app.main import app
    monkeypatch.setattr(main_module, "empty_day_penalty", lambda ds: 0)
    monkeypatch.setattr(main_module, "spread_penalty", lambda ds: 0)
    checked = 0
    with TestClient(app) as client, deterministic_limits():
        for t in generator.generate_trips(SEED, 8, DS):
            rec = metrics.evaluate_trip(t, DS)["solver"]
            if rec["status"] != "feasible":
                continue
            p = generator.prefs_for(t, DS)
            r = client.post("/optimise", json={"preferences": p.model_dump(), "hotel_id": t["hotel_id"]})
            assert r.status_code == 200, r.text
            res = r.json()["result"]
            assert [s["attraction_id"] for d in res["days"] for s in d["stops"]] == rec["placed"], t["id"]
            assert res["totals"]["travel_minutes"] == rec["travel_minutes"] and res["totals"]["total_jpy"] == rec["total_jpy"], t["id"]
            checked += 1
    assert checked >= 4
    assert metrics.product_spec(Preferences(days=2, value_of_time_per_hour=1500)) == \
        __import__("app.optimiser.solver", fromlist=["ObjectiveSpec"]).ObjectiveSpec("true_cost", 1500, config.MISSED_PREFERENCE_PENALTY_JPY)


# ---------------------------------------------------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------------------------------------------------
def test_same_seed_gives_byte_identical_scenarios_and_a_different_seed_differs():
    a, b = generator.scenario_file_text(SEED, 30, DS), generator.scenario_file_text(SEED, 30, DS)
    assert a == b and a.encode() == b.encode()
    assert generator.scenario_file_text(SEED + 1, 30, DS) != a


def test_generated_trips_respect_the_documented_ranges_and_mix():
    trips = generator.generate_trips(SEED, 120, DS)
    assert len(trips) == 120 and len({t["id"] for t in trips}) == 120
    for t in trips:
        assert 2 <= t["days"] <= 4 and 3 <= len(t["order"]) <= 9 and len(set(t["order"])) == len(t["order"])
        assert len(t["must_visit"]) <= 3 and set(t["must_visit"]) <= set(t["order"]) and set(t["order"]) <= set(DS.attractions)
        assert 0 <= t["start_weekday"] <= 6 and t["hotel_id"] in DS.hotels
        assert (t["budget"] is None) == (t["budget_kind"] == "none")
        generator.prefs_for(t, DS)  # valid Preferences (strict budget has a budget, etc.)
    assert {t["budget_kind"] for t in trips} == {"none", "soft", "strict"}
    assert {t["hotel_id"] for t in trips} == set(DS.hotels)                       # each hotel is the base in turn
    assert {len(t["must_visit"]) for t in trips} == {0, 1, 2, 3}
    assert sum(t["order"] != sorted(t["order"]) for t in trips) > 100            # input order is not sorted (and not geographic)
    assert min(t["budget_factor"] for t in trips if t["budget_kind"] == "strict") < 0.7  # some budgets are deliberately tight


def test_committed_scenario_file_matches_the_generator_for_its_own_seed():
    path = BACKEND.parent / "docs" / "eval-scenarios.json"
    if not path.exists():
        pytest.skip("docs/eval-scenarios.json has not been generated yet")
    doc = json.loads(path.read_text())
    assert path.read_text() == generator.scenario_file_text(doc["seed"], doc["n"], DS)


def test_same_seed_gives_identical_metrics_excluding_wall_clock_fields():
    trips = generator.generate_trips(SEED, 6, DS)
    runs = []
    for _ in range(2):
        with deterministic_limits():
            tr = [metrics.evaluate_trip(t, DS) for t in trips]
            hv = [metrics.evaluate_hotel_value(t, DS, vots=(1000,)) for t in trips[:2]]
        runs.append(strip_wall_clock({"trips": tr, "hotel": hv, "agg": metrics.aggregate(tr, hv, None)}))
    assert runs[0] == runs[1]
    assert json.dumps(runs[0], sort_keys=True) == json.dumps(runs[1], sort_keys=True)


# ---------------------------------------------------------------------------------------------------------------------
# Nearest-neighbour baseline
# ---------------------------------------------------------------------------------------------------------------------
POOL = ["tokyo_national_museum", "tokyo_skytree", "senso_ji"]  # deliberately not in nearest-first order


def test_nn_hand_computed_route_times_and_travel_total():
    # Wed, hotel Asakusa (matrix: hotel->senso_ji 12, senso_ji->skytree 16, skytree->tnm 23, tnm->hotel 18; senso_ji nearest at 12 vs 16 / 18).
    p = Preferences(days=1, must_visit=POOL, start_weekday=2)
    r = nearest_neighbour(p, ASA, DS, order=POOL)
    assert ids(r) == ["senso_ji", "tokyo_skytree", "tokyo_national_museum"]
    assert [s.start for s in r.days[0].stops] == ["09:12", "10:58", "12:51"]   # 09:00+12; 10:42+16; 12:28+23 (museum already open)
    assert r.totals.travel_minutes == 12 + 16 + 23 + 18 == 69 and r.days[0].hotel_return == "15:39"
    assert r.status == "feasible" and validate(r, p).overall != "fail"


def test_nn_is_deterministic_and_ignores_pool_order_except_for_ties():
    p = Preferences(days=2, must_visit=POOL, start_weekday=2)
    a = nearest_neighbour(p, ASA, DS, order=POOL)
    assert nearest_neighbour(p, ASA, DS, order=POOL).days == a.days
    assert nearest_neighbour(p, ASA, DS, order=list(reversed(POOL))).days == a.days


def test_nn_respects_opening_hours_day_window_pace_and_budget():
    mon = Preferences(days=1, must_visit=[], start_weekday=0)  # Monday: museum closed
    r = nearest_neighbour(mon, ASA, DS, order=POOL)
    assert "tokyo_national_museum" not in ids(r) and r.status == "feasible"        # closed nice-to-have is simply not placed
    must = Preferences(days=1, must_visit=["tokyo_national_museum"], start_weekday=0)
    r = nearest_neighbour(must, ASA, DS, order=POOL)
    assert r.status == "infeasible" and any(c.code == "nn_not_placed" and c.attraction_ids == ["tokyo_national_museum"] for c in r.conflicts)
    assert "tokyo_national_museum" not in ids(r)                                    # reported, never faked

    short = Preferences(days=1, must_visit=[], start_weekday=2, day_end="12:00")    # only senso_ji (back at 10:54) fits before noon
    r = nearest_neighbour(short, ASA, DS, order=POOL)
    assert ids(r) == ["senso_ji"] and validate(r, short).overall != "fail"

    relaxed = Preferences(days=2, must_visit=[], start_weekday=2, pace="relaxed")   # cap 2/day
    r = nearest_neighbour(relaxed, ASA, DS, order=POOL)
    assert [len(d.stops) for d in r.days] == [2, 1] and ids(r)[:2] == ["senso_ji", "tokyo_skytree"]
    assert r.days[1].stops[0].start == "09:30"                                      # waits for the 09:30 opening
    assert validate(r, relaxed).overall != "fail"

    tight = Preferences(days=1, must_visit=[], start_weekday=2, budget=320, strict_budget=True)  # 160 there + 160 back = 320: only senso_ji
    r = nearest_neighbour(tight, ASA, DS, order=POOL)
    assert ids(r) == ["senso_ji"] and r.totals.total_jpy == 320
    assert next(c for c in validate(r, tight).checks if c.id == "budget").status == "pass"


def test_nn_and_naive_baselines_never_break_a_hard_constraint_on_generated_trips():
    """Baselines are real plans, not strawmen: their only validator failure is a must-visit they report as not placed."""
    for t in generator.generate_trips(SEED, 25, DS):
        p = generator.prefs_for(t, DS)
        for res in (nearest_neighbour(p, t["hotel_id"], DS, order=t["order"]), baseline(p, t["hotel_id"], DS, order=t["order"])):
            failed = {c.id for c in validate(res, p, t["hotel_id"]).checks if c.status == "fail"}
            assert failed <= {"must_visits"}, (t["id"], failed)
            assert (res.status == "infeasible") == bool(set(t["must_visit"]) - set(ids(res))), t["id"]


# ---------------------------------------------------------------------------------------------------------------------
# Like-for-like comparison and percent maths (hand-computed)
# ---------------------------------------------------------------------------------------------------------------------
def fake_trip(tid, s_status="feasible", s_placed=("a", "b"), s_min=80, b_placed=("a", "b"), b_min=100, key="naive"):
    rec = {"status": "feasible", "placed": list(b_placed), "travel_minutes": b_min}
    return {"id": tid, "solver": {"status": s_status, "placed": list(s_placed), "travel_minutes": s_min}, key: rec}


def test_reduction_and_percent_maths():
    assert metrics.reduction(100, 80) == (20, 20.0)
    assert metrics.reduction(150, 120) == (30, 20.0)
    assert metrics.reduction(100, 110) == (-10, -10.0)      # negative = the solver is worse
    assert metrics.reduction(0, 5) == (-5, None)            # no percent when the baseline has zero travel
    d = metrics.describe([20.0, 20.0, -10.0])
    assert (d["n"], d["min"], d["max"], d["median"]) == (3, -10.0, 20.0, 20.0) and d["mean"] == pytest.approx(10.0)
    assert metrics.describe([])["n"] == 0 and metrics.describe([])["mean"] is None


def test_comparable_subset_excludes_and_counts_non_like_for_like_cases_with_reasons():
    trips = [
        fake_trip("same1", s_min=80, b_min=100),                                           # comparable, 20 min / 20.0 %
        fake_trip("same2", s_min=120, b_min=150),                                          # comparable, 30 min / 20.0 %
        fake_trip("worse", s_min=110, b_min=100),                                          # comparable, -10 min / -10.0 %
        fake_trip("dropped", s_placed=("a", "b", "c"), b_placed=("a", "b"), b_min=50),    # baseline dropped a sight
        fake_trip("solver_dropped", s_placed=("a",), b_placed=("a", "b")),                 # solver placed fewer
        fake_trip("different", s_placed=("a", "c"), b_placed=("a", "b")),                  # neither contains the other
        fake_trip("infeasible", s_status="infeasible", s_placed=(), s_min=None),           # nothing to compare
        fake_trip("zero", s_placed=(), b_placed=(), s_min=0, b_min=0),                     # equal sets but 0 baseline travel
    ]
    c = metrics.compare_travel(trips, "naive")
    assert c["n_trips"] == 8 and c["n_comparable"] == 3
    assert c["excluded"] == {"n": 5, "reasons": {"baseline_dropped_sights": 1, "solver_dropped_sights": 1, "different_sets": 1,
                                                 "solver_infeasible": 1, "zero_baseline_travel": 1}}
    assert [r["reduction_minutes"] for r in c["rows"]] == [20, 30, -10]
    assert [r["reduction_percent"] for r in c["rows"]] == [20.0, 20.0, -10.0]
    assert (c["better"], c["equal"], c["worse"]) == (2, 0, 1)
    assert c["percent_reduction"]["mean"] == pytest.approx(10.0) and c["percent_reduction"]["median"] == 20.0
    assert c["pooled"] == {"solver_minutes": 310, "baseline_minutes": 350, "percent": pytest.approx(100 * 40 / 350)}
    # terciles of the baseline's own travel minutes (100, 150, 100): ordered 100 (same1), 100 (worse), 150 -> one trip each
    assert [t["n"] for t in c["terciles"]] == [1, 1, 1] and [t["baseline_minutes_range"] for t in c["terciles"]] == [[100, 100], [100, 100], [150, 150]]


def test_the_two_baselines_are_compared_separately_never_merged():
    both = [dict(fake_trip("t1", s_min=90, b_min=100), nn={"status": "feasible", "placed": ["a", "b"], "travel_minutes": 85})]
    naive, nn = metrics.compare_travel(both, "naive"), metrics.compare_travel(both, "nn")
    assert naive["percent_reduction"]["mean"] == 10.0 and nn["percent_reduction"]["mean"] == pytest.approx(-100 * 5 / 85)
    assert naive["baseline"] == "naive" and nn["baseline"] == "nn"


def test_conservative_rounding_helpers():
    assert claims.down(12.39) == "12.3" and claims.up(12.31) == "12.4"          # never toward the flattering value
    assert claims.down(-3.01) == "-3.1" and claims.up(-3.01) == "-3.0"
    assert claims.down(5.0) == "5.0" and claims.up(5.0) == "5.0"                  # exact values are unchanged
    assert claims.pct(1, 3, True) == "33.3%" and claims.pct(1, 3, False) == "33.4%" and claims.pct(0, 0, True) == "n/a"
    assert claims.down(1234.9, 0) == "1,234"


# ---------------------------------------------------------------------------------------------------------------------
# Claims generator, on a fixed fake metrics dict
# ---------------------------------------------------------------------------------------------------------------------
def fake_cmp(baseline_key, pcts, n_trips=8, excluded=None):
    excluded = excluded if excluded is not None else {"different_sets": 2, "solver_infeasible": 1}
    return {"baseline": baseline_key, "n_trips": n_trips, "n_comparable": len(pcts), "excluded": {"n": sum(excluded.values()), "reasons": excluded},
            "percent_reduction": metrics.describe(pcts), "better": sum(p > 0 for p in pcts), "equal": sum(p == 0 for p in pcts), "worse": sum(p < 0 for p in pcts)}


def fake_metrics(naive_pcts=(10.0, 20.0, 30.9, 0.0, -5.0), nn_pcts=(2.0, 4.0, 3.0)):
    v = lambda **o: {"n_trips": 10, "pass": 0, "pass_with_warnings": 8, "fail": 0, "no_plan": 2, "failed_checks": {}, "fail_hard_constraint_beyond_missing_must_visit": 0, **o}
    side = lambda fp, wb, nf: {"feasible_plans": fp, "within_budget": wb, "no_feasible_plan": nf}
    mv = lambda a, r, d: {"all_placed": a, "reported_infeasible": r, "silently_dropped": d}
    return {
        "travel": {"naive": fake_cmp("naive", list(naive_pcts)), "nn": fake_cmp("nn", list(nn_pcts))},
        "placement": {"n_trips": 8, "requested": 51, "visitable": 47, "solver": {"placed": 38}, "naive": {"placed": 35}, "nn": {"placed": 36}},
        "validation": {"solver": v(), "naive": v(fail=3, pass_with_warnings=7, no_plan=0, fail_hard_constraint_beyond_missing_must_visit=1), "nn": v(fail=4, no_plan=0)},
        "budget": {"n_strict_trips": 7, "solver": side(7, 7, 0), "naive": side(6, 6, 1), "nn": side(5, 4, 2)},
        "must_visit": {"n_trips_with_must": 6, "solver": mv(4, 2, 0), "naive": mv(3, 3, 0), "nn": mv(2, 4, 0),
                       "infeasible_audit": {"n_infeasible": 2, "all_stated_conflicts_confirmed": 2, "brute_force_agrees_no_schedule": 2,
                                            "brute_force_found_a_schedule": 0, "brute_force_not_decided": 0, "by_code": {}}},
        "hotel_value": {"vots": {"1000": {"n_eligible": 8, "cheapest_by_rate_not_best": 3, "gap_jpy": metrics.describe([0, 0, 0, 0, 0, 100, 250.9, 300])},
                                 "500": {"n_eligible": 8, "cheapest_by_rate_not_best": 0}, "2000": {"n_eligible": 8, "cheapest_by_rate_not_best": 5}},
                        "flip": {"n_analysed": 8, "n_with_flip": 3, "first_flip_vot": metrics.describe([800.0, 1293.4, 2700.0]), "exact_crossing": 2, "bisection_estimate": 1}},
        "replan": {"n_scenarios": 4, "n_both_applied": 3, "aware": {"moved": [0, 0, 0]}, "naive": {"moved": [3, 5, 4]}, "aware_fewer_moves": 3, "same_moves": 0,
                   "aware_more_moves": 0, "extra_cost_of_stability_jpy": [0, 600, 1353]},
        "solver_status": {"trip_solves": {"OPTIMAL": 8, "NOT_RUN": 2}},
    }


def claim(m, cid):
    return dict(claims.generate_claims(m))[cid]


def test_positive_travel_claim_has_exact_floor_rounded_numbers_n_and_caveat():
    # mean of [10, 20, 30.9, 0, -5] = 11.18 -> floored to 11.1; median 10.0; range -5.0 to 30.9; better 3, equal 1, worse 1
    assert claim(fake_metrics(), "travel_vs_input_order") == (
        "Across 5 comparable trips (of 8; 3 excluded (2 different sets, 1 solver infeasible)), optimised routes used 11.1% less total travel time than the "
        "input-order baseline (mean of per-trip percentages; median 10.0%, range -5.0% to 30.9%, where positive means less travel; better on 3, equal on 1, worse on 1 of 5). "
        + claims.CAVEAT)


def test_null_result_is_stated_plainly():
    s = claim(fake_metrics(naive_pcts=(0.0, 0.0, 0.0)), "travel_vs_input_order")
    assert "used the same total travel time on average as the input-order baseline" in s
    assert "did not reduce travel time against this baseline" in s and "fewer than half (0 of 3)" in s and "less total travel time" not in s


def test_negative_result_says_more_travel_and_rounds_against_the_product():
    s = claim(fake_metrics(nn_pcts=(-10.0, -20.0, 5.0)), "travel_vs_nearest_neighbour")   # mean -8.333... -> 8.4% MORE (rounded up)
    assert "used 8.4% MORE total travel time than the nearest-neighbour baseline on average" in s
    assert "did not reduce travel time" in s and "better on 1, equal on 0, worse on 2 of 3" in s and "fewer than half (1 of 3)" in s


def test_secondary_analysis_claims_are_labelled_as_such_and_only_present_when_measured():
    m = fake_metrics()
    assert "secondary_phase2_objective_vs_input_order" not in dict(claims.generate_claims(m))
    m["secondary_objectives"] = {"variants": {"phase2_default": {"naive": fake_cmp("naive", [-14.6, -14.5, 3.0]), "nn": fake_cmp("nn", [-7.0, 0.0, -1.9])}}}
    d = dict(claims.generate_claims(m))
    s = d["secondary_phase2_objective_vs_input_order"]
    assert "optimised routes (using the Phase 2 weighted objective, which no API route uses; a secondary analysis added after seeing the results)" in s
    assert "MORE total travel time" in s and claims.CAVEAT in s and "Across 3 comparable trips" in s   # unflattering secondary results are stated too
    assert "nearest-neighbour baseline" in d["secondary_phase2_objective_vs_nearest_neighbour"]
    assert list(d).index("secondary_phase2_objective_vs_input_order") == 6   # after the headline travel/placement/validator/budget/must-visit claims


def test_small_average_and_empty_comparisons_are_labelled():
    assert "small average difference (under 5%)" in claim(fake_metrics(nn_pcts=(2.0, 4.0, 3.0)), "travel_vs_nearest_neighbour")
    none = claim(fake_metrics(naive_pcts=()), "travel_vs_input_order")
    assert none.startswith("No travel-time comparison against the input-order baseline could be made") and "(2 different sets, 1 solver infeasible)" in none


def test_every_generated_claim_carries_n_and_a_caveat_and_never_overclaims():
    for cid, text in claims.generate_claims(fake_metrics()):
        assert claims.CAVEAT in text, cid
        assert re.search(r"(?:of|N\s*=|Across|Among)\s+\d+", text, re.I), f"{cid} has no N"
        assert "significan" not in text.lower() and "verified" not in text.lower() and "proves" not in text.lower(), cid
    assert [c for c, _ in claims.generate_claims(fake_metrics())] == [
        "travel_vs_input_order", "travel_vs_nearest_neighbour", "placement", "validator", "budget", "must_visits",
        "hotel_value_1", "hotel_value_2", "hotel_value_3", "replanning", "solver_status"]


def test_other_claims_use_the_counts_in_the_metrics_and_flag_failures():
    m = fake_metrics()
    assert "passed 8 of 8 solver itineraries (8 with data-quality warnings) and failed 0" in claim(m, "validator")
    assert "the input-order baseline: 3 of 10 plans failed validation (1 of them for a reason beyond a missing must-visit)" in claim(m, "validator")
    assert "nearest-neighbour baseline 4 of 5 feasible plans within budget" in claim(m, "budget")
    assert "silently dropped one in 0" in claim(m, "must_visits") and "found a schedule in 0" in claim(m, "must_visits")
    h = claim(m, "hotel_value_1")
    assert "not the best by true cost in 3 of 8 trips" in h and "(37.5%)" in h and "FICTIONAL" in h and "mean 81 JPY" in h   # 650.9/8 = 81.36 -> floored
    assert "500 JPY/h: 0 of 8; 2000 JPY/h: 5 of 8" in claim(m, "hotel_value_2")
    assert "about 1,293 JPY/h (2 exact crossings, 1 bisection estimates, so approximate)" in claim(m, "hotel_value_3")
    r = claim(m, "replanning")
    assert "fewer activities than a naive full re-solve in 3, the same number in 0 and more in 0" in r and "0.0 (aware, N=3) vs 4.0 (naive, N=3)" in r and "mean 651 JPY higher" in r
    m["validation"]["solver"]["fail"] = 2
    assert "failed 2" in claim(m, "validator")                                   # a solver failure would be stated, not hidden
    m["hotel_value"]["vots"]["1000"]["cheapest_by_rate_not_best"] = 0
    assert "also the best by true cost in all 8 trips" in claim(m, "hotel_value_1")


# ---------------------------------------------------------------------------------------------------------------------
# Independent recomputation (audits the solver's claims)
# ---------------------------------------------------------------------------------------------------------------------
def test_brute_force_on_hand_built_cases():
    wed = dict(start_weekday=2)
    assert independent.musts_schedulable(DS, Preferences(days=1, must_visit=["senso_ji", "tokyo_skytree"], **wed), ASA) is True
    assert independent.musts_schedulable(DS, Preferences(days=1, must_visit=["ghibli_museum"], start_weekday=1), ASA) is False          # closed Tuesday
    assert independent.musts_schedulable(DS, Preferences(days=1, day_end="13:00", pace="packed", must_visit=["tokyo_national_museum", "senso_ji", "tokyo_skytree"], **wed), ASA) is False
    assert independent.musts_schedulable(DS, Preferences(days=1, must_visit=["tokyo_skytree", "shibuya_sky"], budget=1000, strict_budget=True, **wed), ASA) is False
    assert independent.musts_schedulable(DS, Preferences(days=1, must_visit=["akihabara"], **wed), ASA) is False                          # unknown hours not allowed
    assert independent.musts_schedulable(DS, Preferences(days=1, must_visit=["akihabara"], allow_unknown_hours=True, **wed), ASA) is True
    assert independent.musts_schedulable(DS, Preferences(days=1, **wed), ASA) is True                                                      # no must-visits
    assert independent.musts_schedulable(DS, Preferences(days=1, must_visit=["nope"], **wed), ASA) is None


def test_confirm_conflict_agrees_with_the_documented_scenarios():
    def check(prefs, hotel=ASA):
        r = solve(prefs, hotel, DS, with_alternatives=False)
        assert r.status == "infeasible"
        order = [a for a in DS.attractions if a not in prefs.skip]
        return [(c.code, independent.confirm_conflict(c.code, c.attraction_ids, DS, prefs, hotel, order)) for c in r.conflicts]
    assert check(Preferences(days=1, must_visit=["ghibli_museum"], start_weekday=1)) == [("closed_all_days", True)]
    assert check(Preferences(days=1, must_visit=["akihabara"], start_weekday=2)) == [("unknown_hours_not_allowed", True)]
    assert check(Preferences(days=1, day_end="12:00", must_visit=["ghibli_museum"], start_weekday=2), "hotel_harbour_odaiba") == [("does_not_fit_day", True)]
    assert check(Preferences(days=1, pace="packed", day_end="13:00", must_visit=["tokyo_national_museum", "senso_ji", "tokyo_skytree"], start_weekday=2)) == [("not_enough_time", True)]
    assert check(Preferences(days=1, must_visit=["tokyo_skytree", "shibuya_sky"], budget=1000, strict_budget=True, start_weekday=2)) == [("budget_too_low", True)]
    # a wrong claim is contradicted: Senso-ji is open and fits, so "closed on all days" must come back False
    assert independent.confirm_conflict("closed_all_days", ["senso_ji"], DS, Preferences(days=1, start_weekday=2), ASA, list(DS.attractions)) is False
    assert independent.confirm_conflict("solver_timeout", [], DS, Preferences(days=1), ASA, list(DS.attractions)) is None


def test_solver_feasibility_and_brute_force_agree_on_generated_trips():
    with deterministic_limits():
        for t in generator.generate_trips(SEED, 24, DS):
            p = generator.prefs_for(t, DS)
            r = solve(p, t["hotel_id"], DS, with_alternatives=False)
            brute = independent.musts_schedulable(DS, p, t["hotel_id"])
            assert brute is (r.status == "feasible"), (t["id"], r.status, brute, [c.code for c in r.conflicts])
            if r.status != "feasible":
                for c in r.conflicts:
                    assert independent.confirm_conflict(c.code, c.attraction_ids, DS, p, t["hotel_id"], t["order"]) is True, (t["id"], c.code)


# ---------------------------------------------------------------------------------------------------------------------
# Import guards: no live LLM, independent module really independent
# ---------------------------------------------------------------------------------------------------------------------
def _imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, None
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                yield node.module or "", a.name


def test_evaluation_never_imports_the_live_llm_client():
    files = list((BACKEND / "app" / "eval").glob("*.py")) + [BACKEND / "scripts" / "run_evaluation.py"]
    allowed_from_client = {"LLMResponse", "LLMUnavailable"}   # plain data/exception types the mock needs; no client class, no factory
    for f in files:
        for module, name in _imports(f):
            assert module.split(".")[0] != "anthropic", f"{f.name} imports the SDK"
            if module == "app.llm.client":
                assert name in allowed_from_client, f"{f.name} imports {name} from app.llm.client"
            assert not (module == "app.llm" and name == "client"), f.name
        text = f.read_text()
        assert "get_llm_client" not in text and "OpenRouterLLMClient" not in text and "OPENROUTER_API_KEY" not in text, f.name


def test_independent_module_imports_nothing_from_the_optimiser_or_validator():
    mods = {m for m, _ in _imports(BACKEND / "app" / "eval" / "independent.py")}
    assert not any(m.startswith(("app.optimiser", "app.validator", "app.llm")) for m in mods), mods


# ---------------------------------------------------------------------------------------------------------------------
# The script, end to end in --quick mode
# ---------------------------------------------------------------------------------------------------------------------
def test_quick_mode_runs_end_to_end_offline_in_under_a_minute_and_writes_every_output(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)          # any attempt to import the SDK raises ImportError (scenarios would FAIL)
    for var in ("OPENROUTER_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    sys.path.insert(0, str(BACKEND / "scripts"))
    try:
        script = importlib.import_module("run_evaluation")
    finally:
        sys.path.remove(str(BACKEND / "scripts"))
    repo_docs = BACKEND.parent / "docs"
    before = {p.name: p.stat().st_mtime_ns for p in repo_docs.glob("eval-*")} | {"README-claims.md": (repo_docs / "README-claims.md").stat().st_mtime_ns if (repo_docs / "README-claims.md").exists() else 0}

    t0 = time.perf_counter()
    rc = script.main(["--quick", "--out-dir", str(tmp_path)])
    elapsed = time.perf_counter() - t0
    assert rc == 0 and elapsed < 60, f"quick mode took {elapsed:.1f} s"

    for name in ("eval-results.md", "eval-results.json", "eval-scenarios.json", "README-claims.md", "eval-travel-minutes.csv", "eval-hotel-gaps.csv", "tests-results/evaluation-scenarios.md"):
        assert (tmp_path / name).stat().st_size > 0, name
    raw = json.loads((tmp_path / "eval-results.json").read_text())
    assert {"meta", "parameters", "limits", "scenarios", "trips", "hotel_value", "secondary_objective_solvers", "metrics"} <= set(raw)
    assert len(raw["scenarios"]) == 15 and len(raw["trips"]) == 10 == len(raw["hotel_value"]) and all(s["passed"] for s in raw["scenarios"])
    assert {"generation", "travel", "placement", "validation", "budget", "must_visit", "hotel_value", "replan", "secondary_objectives", "solver_status", "machine_dependent"} <= set(raw["metrics"])
    assert set(raw["metrics"]["travel"]) == {"naive", "nn"} and raw["meta"]["mode"] == "quick" and raw["limits"]["solver_workers"] == 1
    # the script ran the SAME scenario definitions the tests use: same numbers, same observations
    assert [(s["num"], s["passed"], s["observed"]) for s in raw["scenarios"]] == [(r.num, r.passed, r.observed) for r in run_all(DS)]
    assert (tmp_path / "eval-scenarios.json").read_text() == generator.scenario_file_text(script.DEFAULT_SEED, 10, DS)

    md = (tmp_path / "eval-results.md").read_text()
    for heading in ("## The 15 scenarios", "## Generated trips", "## A. Travel time", "## A2. Secondary analysis (post-hoc)", "## B. Constraint satisfaction", "## C. Budget compliance", "## D. Must-visit handling",
                    "## E. Hotel value", "## F. Replanning", "## G. Explanation and parsing", "## H. Solver status and runtime", "## Parameters and seeds",
                    "## WHAT THESE NUMBERS DO NOT SHOW"):
        assert heading in md, heading
    assert md.count("mocked LLM: verifies the pipeline, not the model") >= 3 and "FICTIONAL" in md and "machine-dependent" in md and "Code version:" in md
    assert "Sample data (12 attractions" in (tmp_path / "README-claims.md").read_text()
    assert (tmp_path / "eval-travel-minutes.csv").read_text().splitlines()[0].startswith("trip,hotel,days,requested")
    after = {p.name: p.stat().st_mtime_ns for p in repo_docs.glob("eval-*")}
    assert all(after.get(k) == v for k, v in before.items() if k != "README-claims.md"), "quick mode with --out-dir must not touch the repo's docs"
