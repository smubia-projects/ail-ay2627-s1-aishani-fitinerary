"""Phase 3: hotel comparison. No LLM involved.

Hand-computed reference case ("TINY"): 1 day, Wednesday, nights=1 (explicit), only two sights,
everything else skipped so there are no nice-to-have choices and the route cost does not depend on
visiting order (the matrix is symmetric). Matrix values (asserted first so data drift fails loudly):
  Asakusa hotel: ->Senso-ji 12 min / 160 JPY, ->Skytree 16 / 190;  Senso-ji<->Skytree 16 / 190
  Odaiba  hotel (Wakasu shore): ->Senso-ji 56 min / 460 JPY, ->Skytree 53 / 430
Route hotel -> A -> B -> hotel:
  ASA: 12+16+16 = 44 min, fares 160+190+190 = 540 JPY
  ODA: 53+16+56 = 125 min, fares 430+190+460 = 1080 JPY   (Skytree first, then Senso-ji)
Admissions: Senso-ji 0 + Skytree 2100 = 2100 JPY. No missed preferences (all others skipped).
At value of time 1000 JPY/h:
  ASA time cost = 44*1000/60 = 733.33 -> 733;   true = 10500 + 2100 + 540 + 733  = 13873
  ODA time cost = 125*1000/60 = 2083.33 -> 2083; true = 8500  + 2100 + 1080 + 2083 = 13763
Flip: K_asa = 10500+2100+540 = 13140, K_oda = 8500+2100+1080 = 11680.
  13140 + v*44/60 = 11680 + v*125/60  ->  1460 = v*81/60  ->  v = 60*1460/81 = 1081.48 JPY/h
"""
import pytest
from fastapi.testclient import TestClient

from app import config
from app.data.loader import load_dataset
from app.main import app
from app.optimiser.hotel_compare import ComparisonInputError, SolveCache, compare_hotels, sensitivity
from app.optimiser.solver import ObjectiveSpec, solve
from app.schemas import Preferences
from app.validator.feasibility_report import build_report

DS = load_dataset()
SHJ, ASA, ODA = "hotel_kitsune_shinjuku", "hotel_lantern_asakusa", "hotel_harbour_odaiba"
SIGHTS = ["senso_ji", "tokyo_skytree"]
OTHERS = [a for a in DS.attractions if a not in SIGHTS]


def tiny(days=1, vot=1000, **kw) -> Preferences:
    return Preferences(days=days, must_visit=SIGHTS, skip=OTHERS, start_weekday=2, value_of_time_per_hour=vot, **kw)


def res_of(c, hotel):
    return next(r for r in c.results if r.hotel_id == hotel)


def test_reference_matrix_values_are_what_the_hand_calculation_assumes():
    m, f = DS.minutes, DS.fare_jpy
    assert (m[ASA]["senso_ji"], f[ASA]["senso_ji"], m[ASA]["tokyo_skytree"], f[ASA]["tokyo_skytree"]) == (12, 160, 16, 190)
    assert (m[ODA]["senso_ji"], f[ODA]["senso_ji"], m[ODA]["tokyo_skytree"], f[ODA]["tokyo_skytree"]) == (56, 460, 53, 430)
    assert (m["senso_ji"]["tokyo_skytree"], f["senso_ji"]["tokyo_skytree"]) == (16, 190)
    assert DS.hotels[ASA].nightly_rate == 10500 and DS.hotels[ODA].nightly_rate == 8500
    assert DS.attractions["tokyo_skytree"].admission_cost == 2100 and DS.attractions["senso_ji"].admission_cost == 0


def test_hand_computed_true_cost_breakdown():
    c = compare_hotels(tiny(nights=1), [ASA, ODA], DS)
    a, o = res_of(c, ASA).breakdown, res_of(c, ODA).breakdown
    assert (a.rooms_jpy, a.admissions_jpy, a.transport_jpy, a.travel_minutes, a.time_cost_jpy, a.true_cost_jpy) == (10500, 2100, 540, 44, 733, 13873)
    assert (o.rooms_jpy, o.admissions_jpy, o.transport_jpy, o.travel_minutes, o.time_cost_jpy, o.true_cost_jpy) == (8500, 2100, 1080, 125, 2083, 13763)
    assert a.missed_preferences.count == 0 and o.unknown_price_items == []
    assert c.best_by_true_cost == ODA and c.cheapest_by_rate == ODA
    assert [r.hotel_id for r in c.ranking] == [ODA, ASA]
    assert c.ranking[1].gap_to_winner_jpy == 13873 - 13763
    assert c.ranking[1].gap_to_winner_travel_hours == round((44 - 125) / 60, 2)


def test_true_cost_equals_sum_of_displayed_lines():
    c = compare_hotels(Preferences(days=1, start_weekday=2, value_of_time_per_hour=1300), [SHJ, ASA, ODA], DS)
    for r in c.results:
        b = r.breakdown
        assert b.true_cost_jpy == b.rooms_jpy + b.admissions_jpy + b.transport_jpy + b.time_cost_jpy + b.missed_preferences.penalty_jpy


def test_solver_objective_matches_true_cost_units():
    spec = ObjectiveSpec("true_cost", 1000, config.MISSED_PREFERENCE_PENALTY_JPY)
    r = solve(tiny(nights=1), ASA, DS, with_alternatives=False, objective=spec)
    t = r.totals
    assert r.solver_meta.objective == 60 * (t.admissions_jpy + t.transport_jpy + 3000 * len(r.missed_preferences)) + 1000 * t.travel_minutes


@pytest.mark.parametrize("vot", [0, 1500, 3000])
def test_solver_objective_matches_true_cost_with_optional_sights(vot):
    p = Preferences(days=2, pace="balanced", start_weekday=2, skip=["teamlab_planets"], must_visit=["senso_ji"])
    r = solve(p, ASA, DS, with_alternatives=False, objective=ObjectiveSpec("true_cost", vot, 3000))
    t = r.totals
    assert r.solver_meta.solver_status == "OPTIMAL"
    assert r.solver_meta.objective == 60 * (t.admissions_jpy + t.transport_jpy + 3000 * len(r.missed_preferences)) + vot * t.travel_minutes


def test_default_objective_is_unchanged_by_the_spec_parameter():
    p = Preferences(days=2, must_visit=["senso_ji"], start_weekday=2)
    assert solve(p, ASA, DS).days == solve(p, ASA, DS, objective=ObjectiveSpec()).days


# ---- nights ----------------------------------------------------------------
def test_nights_default_is_days_minus_one_and_override_works():
    c = compare_hotels(tiny(days=3), [ASA, ODA], DS)
    assert c.nights_used == 2 and c.nights_source == "default (days - 1)"
    assert res_of(c, ASA).breakdown.rooms_jpy == 2 * 10500
    c2 = compare_hotels(tiny(days=3, nights=3), [ASA, ODA], DS)
    assert c2.nights_used == 3 and c2.nights_source == "user override"
    assert res_of(c2, ODA).breakdown.rooms_jpy == 3 * 8500
    assert compare_hotels(tiny(days=1), [ASA, ODA], DS).nights_used == 0  # 1-day trip: no room cost


# ---- value of time ----------------------------------------------------------
def test_changing_value_of_time_changes_the_ranking():
    low = compare_hotels(tiny(vot=0, nights=1), [ASA, ODA], DS)
    high = compare_hotels(tiny(vot=3000, nights=1), [ASA, ODA], DS)
    assert low.best_by_true_cost == ODA and not low.rate_winner_differs_from_true_winner
    assert high.best_by_true_cost == ASA and high.rate_winner_differs_from_true_winner
    assert "more per night" in high.insight and "less in total" in high.insight


def test_flip_point_matches_the_analytic_value_and_cache_saves_solves():
    cache = SolveCache()
    s = sensitivity(tiny(nights=1), [ASA, ODA], DS, (0, 3000), cache=cache)
    analytic = 60 * 1460 / 81  # 1081.48, derived in the module docstring
    assert len(s.flips) == 1 and s.flips[0].method == "exact_crossing"
    assert abs(s.flips[0].value_of_time_per_hour - analytic) < 0.5
    assert s.flips[0].from_winner == ODA and s.flips[0].to_winner == ASA
    assert not s.approximate
    assert [g.winner for g in s.grid][0] == ODA and [g.winner for g in s.grid][-1] == ASA
    assert s.solves == 4 and len(s.grid) == 7  # 2 hotels x 2 range ends; the other 5 grid points are not solved


def test_sensitivity_refines_where_plans_change_and_is_honest_about_it():
    p = Preferences(days=1, pace="packed", start_weekday=2, skip=["teamlab_planets"])
    s = sensitivity(p, [SHJ, ASA, ODA], DS, (0, 3000), cache=SolveCache(), deadline=__import__("time").monotonic() + 60)
    assert s.solves > 6  # more than just the two range ends x 3 hotels: some plans changed with value-of-time
    assert s.approximate and any("upper bounds" in w for w in s.warnings)  # unrefined segments are labelled, not hidden
    assert all(len(g.true_cost_jpy) == 3 for g in s.grid)


def test_no_flip_reported_when_one_hotel_dominates():
    s = sensitivity(tiny(nights=1), [ASA, ODA], DS, (0, 1000))
    assert s.flips == [] and "No flip within 0-1000" in s.flip_summary


def test_sensitivity_grid_costs_match_a_direct_solve():
    s = sensitivity(tiny(nights=1), [ASA, ODA], DS, (0, 3000))
    g = next(g for g in s.grid if g.value_of_time_per_hour == 1000)
    assert g.true_cost_jpy[ASA] == 13873 and g.true_cost_jpy[ODA] == 13763  # same as the hand computation


# ---- scenario 6: hotel change recalculates ----------------------------------------
def test_s06_hotel_change_recalculates_itinerary():
    p = Preferences(days=1, start_weekday=2, value_of_time_per_hour=1000)
    c = compare_hotels(p, [SHJ, ASA, ODA], DS)
    mins = {r.hotel_id: r.itinerary.totals.travel_minutes for r in c.results}
    assert len(set(mins.values())) == 3
    assert all(r.itinerary.hotel_id == r.hotel_id for r in c.results)


# ---- infeasible hotel, unknown prices, comparability --------------------------------
def test_hotel_that_breaks_a_must_visit_is_reported_not_dropped():
    p = Preferences(days=1, must_visit=["ghibli_museum"], day_start="09:00", day_end="13:55", start_weekday=2, nights=1)
    c = compare_hotels(p, [SHJ, ASA, ODA], DS)
    assert len(c.results) == 3
    assert res_of(c, SHJ).feasible
    for h in (ASA, ODA):
        r = res_of(c, h)
        assert r.status == "infeasible" and r.breakdown is None and r.conflicts[0].code == "does_not_fit_day"
    assert c.best_by_true_cost == SHJ and c.cheapest_by_rate == ODA and c.rate_winner_differs_from_true_winner
    assert "no feasible plan" in c.insight
    assert any("cannot host a feasible plan" in w for w in c.warnings)


def test_all_hotels_infeasible_is_handled():
    p = Preferences(days=1, must_visit=["ghibli_museum"], day_start="09:00", day_end="11:00", start_weekday=2)
    c = compare_hotels(p, [ASA, ODA], DS)
    assert c.best_by_true_cost is None and c.ranking == [] and "No hotel can host" in c.insight


def test_unknown_price_stays_out_of_totals_and_is_warned():
    p = Preferences(days=1, must_visit=["teamlab_planets"], skip=[a for a in DS.attractions if a != "teamlab_planets"], start_weekday=2, nights=1)
    c = compare_hotels(p, [ASA, ODA], DS)
    for r in c.results:
        assert r.breakdown.unknown_price_items == ["teamlab_planets"] and r.breakdown.admissions_jpy == 0
    assert any("price unknown for teamlab_planets" in w and "excluded from totals" in w for w in c.warnings)


def test_different_nice_to_have_sets_trigger_a_comparability_warning():
    p = Preferences(days=1, start_weekday=2, skip=["teamlab_planets"], value_of_time_per_hour=1000)
    c = compare_hotels(p, [SHJ, ASA, ODA], DS)
    sets = {tuple(r.breakdown.missed_preferences.names) for r in c.results}
    assert len(sets) > 1  # the hotels really do schedule different optional sights
    assert any("Not like-for-like" in w for w in c.warnings)


def test_same_nice_to_have_sets_give_no_comparability_warning():
    c = compare_hotels(tiny(nights=1), [ASA, ODA], DS)
    assert not any("like-for-like" in w for w in c.warnings)


# ---- honesty about solver status ------------------------------------------------------
def test_non_optimal_solve_marks_comparison_approximate(monkeypatch):
    monkeypatch.setattr(config, "SOLVER_MAX_DETERMINISTIC_TIME", 0.2)  # forces a FEASIBLE (not proven) solve
    p = Preferences(days=4, pace="packed", start_weekday=2)
    c = compare_hotels(p, [ASA, ODA], DS, time_limit=60)
    assert all(r.solver_status == "FEASIBLE" and r.proven_optimal is False for r in c.results)
    assert c.approximate and any("not proven optimal" in w for w in c.warnings)
    s = sensitivity(p, [ASA, ODA], DS, (0, 3000), cache=SolveCache(), deadline=__import__("time").monotonic() + 60)
    assert s.approximate and any("not proven OPTIMAL" in w for w in s.warnings)


def test_optimal_solves_are_labelled_optimal_and_not_approximate():
    c = compare_hotels(tiny(nights=1), [ASA, ODA], DS)
    assert all(r.proven_optimal for r in c.results) and not c.approximate and not c.partial


# ---- validator, time limit, input validation -------------------------------------------
def test_every_comparison_itinerary_passes_the_independent_validator():
    p = Preferences(days=2, must_visit=["senso_ji"], start_weekday=2, value_of_time_per_hour=1500)
    c = compare_hotels(p, [SHJ, ASA, ODA], DS, time_limit=60)
    for r in c.results:
        assert r.validator_overall in ("pass", "pass_with_warnings")
        assert build_report(r.itinerary, DS, p, r.hotel_id).overall != "fail"


def test_time_limit_returns_labelled_partial_results_instead_of_hanging():
    c = compare_hotels(tiny(nights=1), [ASA, ODA], DS, time_limit=0.0, include_sensitivity=True)
    assert c.partial and c.approximate and c.best_by_true_cost is None
    assert all(r.status == "skipped" for r in c.results)
    assert any("PARTIAL" in w for w in c.warnings)
    assert c.sensitivity.grid == [] and "time limit" in c.sensitivity.flip_summary
    assert c.elapsed_seconds < 1.0


def test_input_validation():
    for bad in ([ASA], [ASA, ASA], [ASA, "hotel_nope"], [ASA, ODA, SHJ, "x"]):
        with pytest.raises(ComparisonInputError):
            compare_hotels(tiny(), bad, DS)


# ---- API --------------------------------------------------------------------------------
def test_api_compare_hotels_works_in_sample_mode(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    body = {"preferences": tiny(nights=1).model_dump(), "hotel_ids": [ASA, ODA], "include_sensitivity": True}
    with TestClient(app) as client:
        assert client.get("/health").json()["mode"] == "sample"
        r = client.post("/compare-hotels", json=body)
        assert r.status_code == 200
        j = r.json()
        assert j["best_by_true_cost"] == ODA and j["nights_used"] == 1
        assert j["sensitivity"]["flips"][0]["method"] == "exact_crossing"
        assert j["display_sgd"][ASA]["true_cost_sgd"] == round(13873 / config.SAMPLE_FX_JPY_PER_SGD, 2)
        assert "SAMPLE" in j["fx_label"]


def test_api_returns_clear_json_errors():
    with TestClient(app) as client:
        r = client.post("/compare-hotels", json={"preferences": tiny().model_dump(), "hotel_ids": [ASA]})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request" and "2 or 3" in r.json()["error"]["message"]
        r = client.post("/compare-hotels", json={"preferences": {"days": 99}, "hotel_ids": [ASA, ODA]})
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error" and r.json()["error"]["details"]
