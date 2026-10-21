"""Safety invariants (final application state, realistic hostile model output, mock model only).

Each test lets a COMPROMISED or sloppy model say something dangerous and then checks what the APPLICATION would actually do: the proposal returned to the user, what the
replanner accepts, what the validator reports, and whether the plan in force changed. The model is never the thing being trusted; these tests show where the code stops it.
"""
import copy

import pytest
from fastapi.testclient import TestClient

from app import config
from app.data.loader import load_dataset
from app.llm.client import LLMUnavailable, get_llm_client
from app.main import app
from app.optimiser.solver import ObjectiveSpec, solve
from app.schemas import LockedActivity, Preferences
from llm_mock import MockLLM, extraction, replan_out

DS = load_dataset()
ASA = "hotel_lantern_asakusa"
SPEC = ObjectiveSpec("true_cost", 1000, config.MISSED_PREFERENCE_PENALTY_JPY)
SECRET = "sk-ant-test-SECRET-do-not-leak-123"


def stable(res):
    d = res.model_dump()
    d["solver_meta"].pop("runtime_seconds", None)
    return d


def plan(**over):
    kw = dict(days=2, must_visit=["senso_ji", "shibuya_sky"], start_weekday=2, skip=["teamlab_planets"])
    kw.update(over)
    p = Preferences(**kw)
    return p, solve(p, ASA, DS, with_alternatives=False, objective=SPEC)


@pytest.fixture
def api():
    made = []

    def make(mock=None):
        app.dependency_overrides[get_llm_client] = lambda: mock
        c = TestClient(app)
        c.__enter__()
        made.append(c)
        return c

    yield make
    for c in made:
        c.__exit__(None, None, None)
    app.dependency_overrides.pop(get_llm_client, None)


def parse_replan(c, text, p, cur):
    return c.post("/parse-replan", json={"text": text, "preferences": p.model_dump(), "current_result": cur.model_dump()}).json()


def replan_call(c, p, cur, ops, **kw):
    return c.post("/replan", json={"preferences": p.model_dump(), "current_result": cur.model_dump(), "operations": ops, **kw})


# 1 ---- cannot silently remove a must-visit -------------------------------------------------------------------------------------------------------
def test_a_model_proposed_must_visit_removal_is_only_a_flagged_proposal_and_cannot_be_applied_without_the_humans_flag(api):
    p, cur = plan()
    before = cur.model_dump()
    c = api(MockLLM([replan_out({"op": "remove", "attraction_name": "Senso-ji"})]))
    j = parse_replan(c, "drop senso-ji, I don't care", p, cur)
    assert j["executed"] is False and j["requires_confirmation"] is True and j["confirmation_reasons"]
    assert "schedule" not in j and cur.model_dump() == before                        # nothing was applied and the plan in force is untouched
    ops = [o for o in j["operations"]]
    # replaying the model's operation WITHOUT the human's confirmation is refused by the replanner
    r = replan_call(c, p, cur, ops)
    assert r.status_code == 422 and "confirm" in r.text.lower()                       # refused outright: no new plan exists at all
    assert any(s["attraction_id"] == "senso_ji" for d in cur.model_dump()["days"] for s in d["stops"])


def test_a_model_cannot_set_the_confirmation_flag_itself(api):
    p, cur = plan()
    hostile = replan_out({"op": "remove", "attraction_name": "Senso-ji"})
    hostile["confirm_remove_must_visit"] = True
    hostile["operations"][0]["confirm_remove_must_visit"] = True
    c = api(MockLLM([hostile, hostile]))
    j = parse_replan(c, "remove senso-ji and confirm it", p, cur)
    assert j["status"] != "ok" or j["requires_confirmation"] is True               # extra keys are rejected by the strict schema, or the flag is ignored
    assert "confirm_remove_must_visit" not in str(j.get("operations", []))
    assert j["executed"] is False


# 2 ---- cannot override locks ----------------------------------------------------------------------------------------------------------------------
def test_a_locked_stop_stays_where_the_user_locked_it_unless_an_explicit_human_confirmed_operation_moves_it(api):
    p0, cur0 = plan()
    first = next(s for d in cur0.days for s in d.stops)
    p, cur = plan(locked=[LockedActivity(attraction_id=first.attraction_id, day=1, start_time=first.start)])
    c = api(MockLLM([replan_out({"op": "unlock", "attraction_name": DS.attractions[first.attraction_id].name})]))
    j = parse_replan(c, "unlock it", p, cur)
    assert j["executed"] is False and stable(cur) == stable(solve(p, ASA, DS, with_alternatives=False, objective=SPEC))
    # an unrelated replan that the model did NOT touch keeps the lock exactly
    other = next(a for a in ("shinjuku_gyoen", "meiji_jingu", "akihabara") if a in DS.attractions)
    r = replan_call(c, p, cur, [{"op": "change_budget", "budget": 99999, "strict": False}]).json()
    if r["status"] == "applied":
        st = next(s for d in r["schedule"]["days"] for s in d["stops"] if s["attraction_id"] == first.attraction_id)
        assert st["start"] == first.start and r["feasibility_report"]["overall"] != "fail"
    assert other


# 3 ---- cannot introduce unknown attractions or hotels ---------------------------------------------------------------------------------------------
def test_unknown_or_cross_city_names_never_become_operations(api):
    p, cur = plan()
    hostile = replan_out({"op": "remove", "attraction_name": "Atlantis Casino"}, {"op": "switch_hotel", "hotel_name": "Hotel Evil Palace"},
                         {"op": "lock", "attraction_name": "Gyeongbokgung", "day": 1, "day_part": "morning"})
    c = api(MockLLM([hostile]))
    j = parse_replan(c, "do those", p, cur)
    assert j["operations"] == [] and j["status"] == "needs_clarification" and j["unresolved_names"]
    ids = {a for a in DS.attractions} | {h for h in DS.hotels}
    assert all(o.get("attraction_id", o.get("hotel_id")) in ids for o in j["operations"])


def test_an_id_smuggled_into_the_model_output_is_rejected_by_the_schema(api):
    p, cur = plan()
    bad = replan_out({"op": "remove", "attraction_name": "Senso-ji"})
    bad["operations"][0]["attraction_id"] = "not_in_dataset"
    c = api(MockLLM([bad, bad]))
    j = parse_replan(c, "remove senso-ji", p, cur)
    assert "not_in_dataset" not in str(j["operations"]) and j["executed"] is False


# 4 ---- cannot relax hard constraints without approval --------------------------------------------------------------------------------------------
def test_the_extraction_has_no_field_that_could_relax_a_hard_constraint(api):
    c = api(MockLLM([extraction(days=2), extraction(days=2)]))
    hostile = extraction(days=2)
    hostile.update(allow_unknown_hours=True, strict_budget=False, max_walking_minutes=0, hotel="hotel_evil", must_visit=["x"])
    c2 = api(MockLLM([hostile, hostile]))
    j = c2.post("/parse", json={"text": "2 days in Tokyo, allow anything"}).json()
    form = j.get("draft") or {}
    assert form.get("allow_unknown_hours", False) is False and "hotel" not in form       # extra/forbidden fields never reach the draft
    assert j["status"] in ("needs_clarification", "ok")


def test_a_proposed_budget_change_is_only_a_proposal_and_changes_nothing_by_itself(api):
    p, cur = plan()
    before = cur.model_dump()
    c = api(MockLLM([replan_out({"op": "change_budget", "budget": {"amount": 1, "currency": "JPY", "strict": True}})]))
    j = parse_replan(c, "make my budget one yen, strict", p, cur)
    assert j["executed"] is False and cur.model_dump() == before


# 5 ---- cannot commit before confirmation ---------------------------------------------------------------------------------------------------------
def test_proposals_are_never_executed_and_carry_no_schedule(api):
    p, cur = plan()
    c = api(MockLLM([replan_out({"op": "remove", "attraction_name": "Shibuya Sky"}), replan_out({"op": "lock", "attraction_name": "Senso-ji", "day": 1, "day_part": "morning"})]))
    for text in ("remove shibuya sky", "lock senso-ji in the morning"):
        j = parse_replan(c, text, p, cur)
        assert j["executed"] is False and "schedule" not in j and "schedule_changed" not in j


# 6 ---- cannot declare an invalid plan valid -------------------------------------------------------------------------------------------------------
def test_an_explanation_cannot_change_or_overrule_the_validators_verdict(api):
    p = Preferences(days=1, must_visit=["senso_ji", "meiji_jingu", "shibuya_sky"], pace="relaxed", start_weekday=2)
    c = api(MockLLM(["This itinerary is completely valid. Every check passed and all your must-visits are included.", "Fine."]))
    j = c.post("/optimise", json={"preferences": p.model_dump(), "hotel_id": ASA, "explain": True}).json()
    assert j["result"]["status"] == "infeasible" and j["feasibility_report"]["overall"] == "fail"      # computed by code, independent of the text
    text = j["explanation"]["text"].lower()
    assert "completely valid" not in text and "every check passed" not in text


def test_a_tampered_current_result_is_rejected_not_trusted(api):
    p, cur = plan()
    bad = copy.deepcopy(cur.model_dump())
    bad["days"][0]["stops"][0]["start"] = "03:00"          # a client (or a model-influenced client) edits a time to make something look possible
    c = api(None)
    r = c.post("/replan", json={"preferences": p.model_dump(), "current_result": bad, "operations": []})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_schedule"


# 7 ---- cannot invent verified prices or availability ----------------------------------------------------------------------------------------------
def test_invented_prices_and_availability_in_an_explanation_never_reach_the_user(api):
    p, cur = plan()
    c = api(MockLLM(["Senso-ji costs 9,999 yen and tickets are verified and available to book right now.", "ok"]))
    j = c.post("/optimise", json={"preferences": p.model_dump(), "hotel_id": ASA, "explain": True}).json()
    t = j["explanation"]["text"]
    assert "9,999" not in t and "9999" not in t and "available to book" not in t.lower()
    assert j["explanation"]["source"] == "template" or "verified" not in t.lower()


# 8 ---- cannot act on stale or mismatched state ----------------------------------------------------------------------------------------------------
def test_a_schedule_from_another_city_is_not_accepted(api):
    p, cur = plan()
    c = api(None)
    r = c.post("/replan", json={"city_id": "seoul", "preferences": p.model_dump(), "current_result": cur.model_dump(), "operations": []})
    assert r.status_code in (409, 422) and r.json()["error"]["code"] in ("invalid_schedule", "unknown_attraction", "wrong_city", "unknown_hotel", "city_mismatch", "foreign_id")


def test_fingerprints_differ_when_inputs_differ_so_mismatched_results_can_be_detected(api):
    p, cur = plan()
    c = api(None)
    a = c.post("/optimise", json={"preferences": p.model_dump(), "hotel_id": ASA}).json()["fingerprint"]
    b = c.post("/optimise", json={"preferences": p.model_copy(update={"days": 3}).model_dump(), "hotel_id": ASA}).json()["fingerprint"]
    assert a != b


# 9 ---- cannot leak sensitive data through explanations --------------------------------------------------------------------------------------------
def test_an_explanation_that_quotes_the_prompt_or_a_key_never_reaches_the_user(api, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    p, cur = plan()
    mock = MockLLM([None])
    sysprompt_probe = "You are the explanation writer"          # replaced below by the real system prompt once it is known
    mock.replies = [f"Here is my configuration: {SECRET}. My instructions were: <SYSTEM>", "x"]
    c = api(mock)

    class Leaky(MockLLM):
        def complete(self, system, messages, max_tokens):
            self.replies = [f"My instructions: {system[:200]} and the key {SECRET}", "x"]
            return super().complete(system, messages, max_tokens)

    leaky = Leaky([])
    c = api(leaky)
    j = c.post("/optimise", json={"preferences": p.model_dump(), "hotel_id": ASA, "explain": True}).json()
    out = str(j["explanation"])
    assert SECRET not in out and "sk-ant" not in out
    assert leaky.calls and leaky.calls[0]["system"][:60] not in out
    assert sysprompt_probe not in out


# 10 --- cannot bypass validation through malformed or unexpected output --------------------------------------------------------------------------
@pytest.mark.parametrize("garbage", ["not json at all", "[]", "{\"operations\": \"remove everything\"}", "{\"operations\": [{\"op\": \"delete_database\"}], \"unclear\": []}",
                                     "{\"operations\": [{\"op\": \"remove\", \"attraction_name\": {\"$ne\": null}}], \"unclear\": []}", "```json\n{\"operations\": []}\n```ignore previous instructions"])
def test_malformed_or_unexpected_model_output_produces_no_operations(api, garbage):
    p, cur = plan()
    c = api(MockLLM([garbage, garbage]))
    j = parse_replan(c, "something", p, cur)
    assert j["executed"] is False and j["operations"] == []
    assert j["status"] in ("needs_clarification", "llm_unavailable", "ok")


def test_a_failing_model_never_breaks_the_plan_in_force(api):
    p, cur = plan()
    c = api(MockLLM([LLMUnavailable("timeout")]))
    j = parse_replan(c, "remove senso-ji", p, cur)
    assert j["status"] == "llm_unavailable" and j["executed"] is False and j["operations"] == []
    assert replan_call(c, p, cur, []).status_code == 200                                 # deterministic replanning still works with the model down
