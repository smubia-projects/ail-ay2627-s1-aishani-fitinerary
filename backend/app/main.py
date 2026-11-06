"""FastAPI app factory. All hardening (limits, rate tiers, deadlines, circuit breaker, leakage control) is wired here."""
import logging
import re
import time
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config
from app.city_guard import reject_foreign_ids
from app.money import missed_preference_penalty, empty_day_penalty, spread_penalty
from app.config import FX_LABEL, SAMPLE_FX_JPY_PER_SGD, get_settings
from app.data.loader import DEFAULT_CITY_ID, Dataset, load_all_city_datasets, load_dataset
from app.hardening.config import HConfig
from app.hardening.errors import ApiError, error_response
from app.hardening.limiter import SlidingWindowLimiter
from app.hardening.llm_scope import LLMBudget, llm_for_request
from app.hardening.logging_setup import configure_logging
from app.hardening.middleware import HardeningMiddleware
from app.evidence import FAIL_CLOSED, structured_evidence, verify_evidence
from app.fingerprint import stamp, plan_fingerprint
from app.hardening.disconnect import run_cancellable
from app.optimiser.cost_summary import summarise_costs
from app.hardening.solver_capacity import SolverCapacity
from app.llm.client import OPENROUTER_HOST, LLMClient
from app.llm.explain import OptimiseView, explain
from app.llm.extract import extract_preferences
from app.llm.parse_replan import parse_replan
from app.llm.copilot import Intent, Operation as CopilotOperation, answer as copilot_answer, classify as classify_copilot
from app.llm.profile import parse_profile
from app.llm.repair import parse_repair
from app.repair import CONFIRMED, RepairAction, RepairError, apply_actions, base_fingerprint
from app.llm.sanitize import EmptyInput, InputTooLong
from app.optimiser.alternatives import explore
from app.optimiser.hotel_compare import ComparisonInputError, compare_hotels
from app.optimiser.itinerary_compare import ItineraryInputError, check_itinerary, evaluate_itinerary
from app.optimiser.replan import Operation, ReplanError, replan
from app.optimiser.solver import ObjectiveSpec, solve
from app.schemas import Preferences, SolveResult, Verification, HotelVerification, nights_for
from app.verification import count_dataset
from app.untrusted import rebuild_client_schedule
from app.validator.feasibility_report import build_report

log = logging.getLogger("app.startup")
router = APIRouter()


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected on every request model


# ---- cheap routes -------------------------------------------------------------------------------------------
# What this server can do. The page compares this with what it needs, so an OLDER server (started before a feature existed) is reported plainly instead of as "Not found" on one button.
API_FEATURES = ("check-itinerary", "evaluate-itinerary", "explore", "copilot", "parse-repair", "apply-repair", "parse-profile", "min-active-days", "plan-verification")


@router.get("/version")
def version_route():
    from app.fingerprint import SOLVER_VERSION
    return {"api": "fitinerary", "features": list(API_FEATURES), "solver_version": SOLVER_VERSION}


@router.get("/health")
def health(request: Request):
    st = get_settings()  # never echoes the key
    limit = request.app.state.llm_budget.exhausted()
    mode = "llm" if (st.llm_enabled and not limit) else "sample"
    out = {"status": "ok", "mode": mode, "llm_configured": st.llm_enabled, "llm_mode": mode, "llm_limit_reached": bool(st.llm_enabled and limit)}
    if st.llm_enabled:  # the host only: never the key, never the full URL
        out["llm_endpoint"] = OPENROUTER_HOST
        out["llm_relay"] = True  # OpenRouter is a third-party gateway between this app and the model provider
        out["llm_model"] = st.llm_model  # the model name only: never the key, never the URL
    return out


def _dataset(request: Request, city_id: str = DEFAULT_CITY_ID) -> Dataset:
    try:
        return request.app.state.datasets[city_id]
    except KeyError:
        raise ApiError(422, "unknown_city", f"Unknown city id '{city_id[:64]}'.")


def _check_city_ids(request: Request, ds: Dataset, *, hotels=(), prefs: Preferences | None = None, op_list=(), part: str = "all") -> None:
    """Reject ids that belong to another city. On the replan routes the preference lists (must_visit, skip) are checked after the
    supplied schedule has been validated (part="late"), so a whole schedule from another city is still reported as invalid_schedule."""
    attractions: list[str] = []
    if prefs and part in ("all", "early"):
        attractions += [l.attraction_id for l in prefs.locked]
    if prefs and part in ("all", "late"):
        attractions += [*prefs.must_visit, *prefs.skip]
    for op in op_list if part != "late" else ():
        if getattr(op, "attraction_id", None):
            attractions.append(op.attraction_id)
        if getattr(op, "hotel_id", None):
            hotels = [*hotels, op.hotel_id]
    reject_foreign_ids(ds, request.app.state.datasets, hotels=hotels if part != "late" else (), attractions=attractions)


def _display_money(ds: Dataset, value: int) -> float:
    return round(value * ds.city.sample_fx_to_display_currency, 2)


def _city_payload(ds: Dataset) -> dict:
    atts = list(ds.attractions.values())
    return {
        "id": ds.city.id,
        "display_name": ds.city.display_name,
        "country": ds.city.country,
        "currency_code": ds.city.currency_code,
        "display_currency_code": ds.city.display_currency_code,
        "sample_fx_label": ds.city.sample_fx_label,
        "default_value_of_time_per_hour": ds.city.default_value_of_time_per_hour,
        "provenance": ds.city.provenance,
        "counts": {"attractions": len(atts), "hotels": len(ds.hotels), "matrix_nodes": len(ds.minutes)},
    }


@router.get("/data/cities")
def data_cities(request: Request):
    return {"cities": [_city_payload(ds) for ds in request.app.state.datasets.values()]}


@router.get("/data/summary")
def data_summary(request: Request, city_id: str = Query(DEFAULT_CITY_ID, max_length=32)):
    ds = _dataset(request, city_id)
    atts = list(ds.attractions.values())
    return {
        "city_id": ds.city.id,
        "counts": {"attractions": len(atts), "hotels": len(ds.hotels), "matrix_nodes": len(ds.minutes)},
        "provenance": {
            "attractions": dict(Counter(a.provenance.source_status for a in atts)),
            "hotels": dict(Counter(h.provenance.source_status for h in ds.hotels.values())),
            "travel_matrix": ds.matrix_meta["source_status"],
        },
        "data_quality": {
            "attractions_with_unknown_hours": sum(any(v == "unknown" for v in a.opening_hours.values()) for a in atts),
            "attractions_with_unknown_cost": sum(a.admission_cost is None for a in atts),
            "verified_records": sum(a.provenance.source_status == "verified" for a in atts),
        },
        "verification": count_dataset(ds),   # per-field verification states, computed from the data
        "disclaimer": "Sample data, not live prices. Hotels are fictional. Travel times are estimates.",
        "fx": {"jpy_per_sgd": SAMPLE_FX_JPY_PER_SGD, "label": FX_LABEL},
        "city": _city_payload(ds),
        "display_fx": {"currency_code": ds.city.display_currency_code, "rate": ds.city.sample_fx_to_display_currency, "label": ds.city.sample_fx_label},
    }


@router.get("/data/places")
def data_places(request: Request, city_id: str = Query(DEFAULT_CITY_ID, max_length=32)):
    """Read-only reference data for the UI (map markers, wishlist cards). Deliberately omits `description`: it is untrusted free text
    and nothing in the interface needs it."""
    ds = _dataset(request, city_id)
    src = lambda p: p.source_status
    return {
        "city_id": ds.city.id,
        "attractions": [
            {"id": a.id, "name": a.name, "area": a.area, "lat": a.lat, "lon": a.lon, "category": a.category, "priority": a.priority,
             "duration_minutes": a.duration_minutes, "admission_cost": a.admission_cost, "indoor": a.indoor,
             "advance_booking_required": a.advance_booking_required, "opening_hours": a.opening_hours,
             "source": {"record": src(a.provenance), "hours": src(a.hours_provenance), "cost": src(a.cost_provenance)},
             "verification": (a.verification or Verification()).model_dump()}
            for a in ds.attractions.values()],
        "hotels": [{"id": h.id, "name": h.name, "area": h.area, "lat": h.lat, "lon": h.lon, "nightly_rate": h.nightly_rate,
                    "source": {"record": src(h.provenance)}, "verification": (h.verification or HotelVerification()).model_dump()} for h in ds.hotels.values()],
        "travel_times_source": ds.matrix_meta["source_status"],
        "fx": {"jpy_per_sgd": SAMPLE_FX_JPY_PER_SGD, "label": FX_LABEL},
        "city": _city_payload(ds),
        "display_fx": {"currency_code": ds.city.display_currency_code, "rate": ds.city.sample_fx_to_display_currency, "label": ds.city.sample_fx_label},
        "disclaimer": "Sample data, not live prices. Hotels are fictional. Travel times are estimates.",
    }


# ---- standard routes -------------------------------------------------------------------------------------------
class CompareRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    preferences: Preferences
    hotel_ids: list[str] = Field(max_length=3)
    include_sensitivity: bool = False
    vot_min: int | None = Field(default=None, ge=0, le=100_000)
    vot_max: int | None = Field(default=None, ge=0, le=100_000)
    explain: bool = False  # optional explanation, generated server-side from the result computed here


@router.post("/compare-hotels")
async def compare_hotels_route(req: CompareRequest, request: Request, client: LLMClient | None = Depends(llm_for_request)):
    ds, cfg = _dataset(request, req.city_id), request.app.state.hconf
    _check_city_ids(request, ds, hotels=req.hotel_ids, prefs=req.preferences)
    return await run_cancellable(request, lambda: _compare_work(req, request, client, ds, cfg))


def _compare_work(req: CompareRequest, request: Request, client, ds, cfg):
    with request.app.state.solver_capacity.claim():
        # The default sensitivity range is 0 to 3x the CITY's own default value of time (Tokyo: 0-3000 JPY/h, as before; Seoul: 0-45000 KRW/h).
        vot_range = (req.vot_min if req.vot_min is not None else config.VOT_RANGE_DEFAULT[0],
                     req.vot_max if req.vot_max is not None else 3 * ds.city.default_value_of_time_per_hour)
        comp = compare_hotels(req.preferences, req.hotel_ids, ds, include_sensitivity=req.include_sensitivity,
                              vot_range=vot_range, time_limit=cfg.hotel_compare_time_limit_seconds, balanced=True)
    out = comp.model_dump()
    # Presentation layer only: SGD display values from the labelled SAMPLE FX rate.
    out["fx_label"] = FX_LABEL
    out["display_sgd"] = {
        r.hotel_id: {"true_cost_sgd": round(r.breakdown.true_cost_jpy / SAMPLE_FX_JPY_PER_SGD, 2),
                     "rooms_sgd": round(r.breakdown.rooms_jpy / SAMPLE_FX_JPY_PER_SGD, 2)}
        for r in comp.results if r.breakdown
    }
    out["city_id"] = ds.city.id
    stamp(out, req.preferences, req.hotel_ids, ds)
    out["city"] = _city_payload(ds)
    out["display_currency"] = ds.city.display_currency_code
    out["display_fx_label"] = ds.city.sample_fx_label
    out["display_values"] = {
        r.hotel_id: {"true_cost": _display_money(ds, r.breakdown.true_cost_jpy),
                     "rooms": _display_money(ds, r.breakdown.rooms_jpy)}
        for r in comp.results if r.breakdown
    }
    if req.explain:
        out["explanation"] = explain(comp, ds, *_explain_args(client, request, comp.best_by_true_cost is not None), request.app.state.datasets).model_dump()
    return out


class ReplanRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    preferences: Preferences
    current_result: SolveResult
    operations: list[Operation] = Field(max_length=config.MAX_REPLAN_OPERATIONS)
    confirm_remove_must_visit: bool = False  # only ever set by the client; the server never sets it
    explain: bool = False


@router.post("/replan")
async def replan_route(req: ReplanRequest, request: Request, client: LLMClient | None = Depends(llm_for_request)):
    """Stateless: nothing from the request is stored. The supplied schedule is UNTRUSTED: it is rebuilt and validated first."""
    ds, cfg = _dataset(request, req.city_id), request.app.state.hconf
    _check_city_ids(request, ds, prefs=req.preferences, op_list=req.operations, part="early")
    return await run_cancellable(request, lambda: _replan_work(req, request, client, ds, cfg))


def _replan_work(req: ReplanRequest, request: Request, client, ds, cfg):
    with request.app.state.solver_capacity.claim():
        current = rebuild_client_schedule(req.preferences, req.current_result, ds, request.app.state.datasets,
                                          after_structure=lambda: _check_city_ids(request, ds, prefs=req.preferences, part="late"))
        result = replan(req.preferences, current, req.operations, ds,
                        confirm_remove_must_visit=req.confirm_remove_must_visit,
                        max_seconds=cfg.replan_time_limit_seconds, balanced=True)
    out = result.model_dump()
    out["city_id"] = ds.city.id
    out["cost_summary"] = summarise_costs(result.schedule, ds, result.hotel_id, result.nights_used)
    stamp(out, result.new_preferences or req.preferences, result.hotel_id, ds)
    if req.explain:  # built from the result just computed here, never from client-supplied results
        out["explanation"] = explain(result, ds, *_explain_args(client, request, result.status == "applied"), request.app.state.datasets).model_dump()
    return out


class OptimiseRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    preferences: Preferences
    hotel_id: str = Field(max_length=64)
    explain: bool = False


@router.post("/optimise")
async def optimise_route(req: OptimiseRequest, request: Request, client: LLMClient | None = Depends(llm_for_request)):
    """Best feasible itinerary for ONE hotel (true-cost objective, same as the hotel comparison), always validated."""
    ds, cfg = _dataset(request, req.city_id), request.app.state.hconf
    _check_city_ids(request, ds, hotels=[req.hotel_id], prefs=req.preferences)
    return await run_cancellable(request, lambda: _optimise_work(req, request, client, ds, cfg))


def _optimise_core(p: Preferences, hotel_id: str, ds, cfg):
    """The ONE place a request becomes a plan response: /optimise and an applied repair both come through here (caller holds the solver slot)."""
    if hotel_id not in ds.hotels:
        raise ApiError(422, "unknown_hotel", f"Unknown hotel id '{hotel_id[:64]}'.")
    spec = ObjectiveSpec("true_cost", p.value_of_time_per_hour, missed_preference_penalty(ds), empty_day_penalty_jpy=empty_day_penalty(ds), spread_penalty_jpy=spread_penalty(ds))
    res = solve(p, hotel_id, ds, objective=spec, max_seconds=cfg.optimise_time_limit_seconds, alternatives_budget=cfg.alternatives_budget_seconds)
    rep = build_report(res, ds, p, hotel_id)  # a failing report is returned as-is, never hidden
    nights, src = nights_for(p)
    approximate = res.status == "feasible" and res.solver_meta.solver_status != "OPTIMAL"
    out = {"city_id": ds.city.id, "result": res.model_dump(), "feasibility_report": rep.model_dump(), "approximate": approximate, "nights_used": nights, "nights_source": src}
    out["cost_summary"] = summarise_costs(res, ds, hotel_id, nights)
    stamp(out, p, hotel_id, ds)
    if approximate:
        out["approximate_note"] = "The solver stopped at its time limit without proving optimality: this plan is good but may not be the best."
    return res, rep, out, approximate, nights


def _optimise_work(req: OptimiseRequest, request: Request, client, ds, cfg):
    with request.app.state.solver_capacity.claim():
        res, rep, out, approximate, nights = _optimise_core(req.preferences, req.hotel_id, ds, cfg)
    if req.explain:
        out["explanation"] = explain(OptimiseView(res, rep, req.hotel_id, approximate, nights), ds, *_explain_args(client, request, res.status == "feasible" and rep.overall != "fail"),
                                     request.app.state.datasets).model_dump()
    return out


NO_VALID_PLAN_NOTE = "No model-written text is used when there is no valid plan: this explanation is written by code from the validator's result."


def _explain_args(client, request: Request, ok: bool):
    """The model may only word an explanation of a VALID result. For a plan that does not exist (infeasible, rejected by the validator, nothing applied) the model is not asked at all:
    otherwise its prose could say "this plan is valid" next to a failing report. The decision is made here, in code, outside the model's reach."""
    return (client, request.state.llm_note) if ok else (None, NO_VALID_PLAN_NOTE)


class EvaluateItineraryRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    preferences: Preferences
    hotel_id: str = Field(max_length=64)
    days: list[list[str]] = Field(max_length=7)      # the user's own days: attraction ids in visiting order


@router.post("/check-itinerary")
def check_itinerary_route(req: EvaluateItineraryRequest, request: Request):
    """Live feedback for a DRAFT itinerary (placement, independent validator, modelled cash). No optimiser run, no storage."""
    ds = _dataset(request, req.city_id)
    _check_city_ids(request, ds, hotels=[req.hotel_id], prefs=req.preferences)
    if any(len(d) > 8 or any(len(a) > 64 for a in d) for d in req.days):
        raise ApiError(422, "invalid_itinerary", "Each day can hold at most 8 attractions.")
    _check_city_ids(request, ds, prefs=req.preferences.model_copy(update={"must_visit": [a for d in req.days for a in d], "skip": [], "locked": []}))
    try:
        out = check_itinerary(req.preferences, req.hotel_id, ds, req.days)
    except ItineraryInputError as e:
        raise ApiError(422, e.code, e.message)
    out["city_id"] = ds.city.id
    return stamp(out, req.preferences, req.hotel_id, ds)


@router.post("/evaluate-itinerary")
async def evaluate_itinerary_route(req: EvaluateItineraryRequest, request: Request):
    """Your itinerary vs the optimiser for the SAME sights (and vs a full plan that may leave optional sights out). Stateless; no model."""
    ds, cfg = _dataset(request, req.city_id), request.app.state.hconf
    _check_city_ids(request, ds, hotels=[req.hotel_id], prefs=req.preferences)
    if any(len(d) > 8 or any(len(a) > 64 for a in d) for d in req.days):
        raise ApiError(422, "invalid_itinerary", "Each day can hold at most 8 attractions.")
    _check_city_ids(request, ds, prefs=req.preferences.model_copy(update={"must_visit": [a for d in req.days for a in d], "skip": [], "locked": []}))

    def work():
        with request.app.state.solver_capacity.claim():
            try:
                out = evaluate_itinerary(req.preferences, req.hotel_id, ds, req.days, max_seconds=cfg.optimise_time_limit_seconds)
            except ItineraryInputError as e:
                raise ApiError(422, e.code, e.message)
        out["city_id"] = ds.city.id
        return stamp(out, req.preferences, req.hotel_id, ds)
    return await run_cancellable(request, work)


class ExploreRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    preferences: Preferences
    hotel_ids: list[str] = Field(min_length=1, max_length=3)


@router.post("/explore")
async def explore_route(req: ExploreRequest, request: Request):
    """Bounded budget/time alternatives (real optimiser runs, dominance-filtered). Stateless; no model."""
    ds, cfg = _dataset(request, req.city_id), request.app.state.hconf
    _check_city_ids(request, ds, hotels=req.hotel_ids, prefs=req.preferences)
    if any(h not in ds.hotels for h in req.hotel_ids):
        raise ApiError(422, "unknown_hotel", "Unknown hotel id.")

    def work():
        with request.app.state.solver_capacity.claim():
            out = explore(req.preferences, list(dict.fromkeys(req.hotel_ids)), ds, max_seconds=cfg.optimise_time_limit_seconds)
        out["city_id"] = ds.city.id
        return stamp(out, req.preferences, req.hotel_ids, ds)
    return await run_cancellable(request, work)


# ---- expensive (LLM) routes -----------------------------------------------------------------------------------------
class ParseRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    text: str = Field(max_length=10_000)
    start_weekday: int | None = Field(default=None, ge=0, le=6)  # optional trip context supplied by the client (0=Mon .. 6=Sun)


@router.post("/parse")
def parse_route(req: ParseRequest, request: Request, client: LLMClient | None = Depends(llm_for_request)):
    """Free text -> Preferences DRAFT (a proposal). In sample mode returns status llm_unavailable (HTTP 200, never a 500)."""
    r = extract_preferences(req.text, _dataset(request, req.city_id), client, start_weekday=req.start_weekday,
                            all_datasets=request.app.state.datasets,
                            unavailable_note=request.state.llm_note)
    return {**r.model_dump(), "city_id": req.city_id}


class ParseRepairRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    text: str = Field(max_length=10_000)
    preferences: Preferences
    hotel_id: str = Field(max_length=64)
    keep_hotel: bool = False          # the PERSON's lock, set by the client from their own control; the model can only make a request more cautious


@router.post("/parse-repair")
def parse_repair_route(req: ParseRepairRequest, request: Request, client: LLMClient | None = Depends(llm_for_request)):
    """Free text -> a PROPOSED repair, tried on a copy by the normal optimiser and validator. Nothing is applied; /apply-repair needs the person's confirmation."""
    ds, cfg = _dataset(request, req.city_id), request.app.state.hconf
    _check_city_ids(request, ds, hotels=[req.hotel_id], prefs=req.preferences)
    if req.hotel_id not in ds.hotels:
        raise ApiError(422, "unknown_hotel", "Unknown hotel id.")
    out = parse_repair(req.text, req.preferences, req.hotel_id, req.keep_hotel, ds, client, unavailable_note=request.state.llm_note,
                       solve_seconds=cfg.optimise_time_limit_seconds, solve_slot=request.app.state.solver_capacity.claim).model_dump()
    return {**out, "city_id": ds.city.id}


class ApplyRepairRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    preferences: Preferences
    hotel_id: str = Field(max_length=64)
    keep_hotel: bool = False
    base_fingerprint: str = Field(max_length=32)
    actions: list[RepairAction] = Field(min_length=1, max_length=8)
    confirmed_ids: list[str] = Field(default=[], max_length=8)    # set only by the person's own ticks in the confirmation step


def _ensure_repair_feasible(out: dict) -> None:
    """A preview is not authority to install an invalid apply-time result."""
    if out["result"]["status"] != "feasible" or out["feasibility_report"]["overall"] == "fail":
        raise ApiError(422, "repair_not_feasible", "These changes did not produce a validated plan. Your current plan is unchanged. Preview a different repair.")


@router.post("/apply-repair")
async def apply_repair_route(req: ApplyRepairRequest, request: Request):
    """Apply a confirmed repair. Everything is re-checked NOW: the plan must be the one the proposal was made for, every action must still match its expected previous value, the allowlist and
    the hotel lock are enforced, and each action that gives something up must be individually confirmed. The result comes from the normal optimiser and the independent validator."""
    ds, cfg = _dataset(request, req.city_id), request.app.state.hconf
    _check_city_ids(request, ds, hotels=[req.hotel_id], prefs=req.preferences)
    reject_foreign_ids(ds, request.app.state.datasets,
                       hotels=[a.proposed for a in req.actions if a.action == "switch_hotel" and isinstance(a.proposed, str)],
                       attractions=[a.target for a in req.actions if a.action in ("remove_optional", "promote_to_must", "demote_must_visit")])
    if req.hotel_id not in ds.hotels:
        raise ApiError(422, "unknown_hotel", "Unknown hotel id.")
    if req.base_fingerprint != base_fingerprint(req.preferences, req.hotel_id, req.keep_hotel, ds):
        raise ApiError(409, "stale_proposal", "The plan changed since this proposal was made. Ask again for a fresh proposal.")
    missing = [a.id for a in req.actions if a.action in CONFIRMED and a.id not in req.confirmed_ids]
    if missing:
        raise ApiError(422, "confirmation_required", "Each change that gives something up or touches a hard rule must be confirmed individually.", [{"action_id": i} for i in missing])

    def work():
        try:
            p, h, keep = apply_actions(req.preferences, req.hotel_id, req.keep_hotel, req.actions, ds)
        except RepairError as e:
            raise ApiError(e.status, e.code, e.message)
        with request.app.state.solver_capacity.claim():
            _, _, out, _, _ = _optimise_core(p, h, ds, cfg)
        _ensure_repair_feasible(out)
        out.update(new_preferences=p.model_dump(), hotel_id=h, keep_hotel=keep, applied=[a.model_dump() for a in req.actions])
        return out
    return await run_cancellable(request, work)


class ParseProfileRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    text: str = Field(max_length=10_000)
    current: dict = Field(default={}, max_length=12)          # the form's current pace / maxActivities / maxDailyTravel / valueOfTime / budget (display and "kept yours" only)
    user_overrides: list[str] = Field(default=[], max_length=8)


@router.post("/parse-profile")
def parse_profile_route(req: ParseProfileRequest, request: Request, client: LLMClient | None = Depends(llm_for_request)):
    """Free text -> PROPOSED travel-style settings, each labelled explicit or inferred. Nothing is applied or solved."""
    ds = _dataset(request, req.city_id)
    cur = {k: v for k, v in req.current.items() if k in ("pace", "maxActivities", "maxDailyTravel", "valueOfTime", "budget") and isinstance(v, (str, int, bool, type(None)))}
    out = parse_profile(req.text, cur, [u for u in req.user_overrides if u in ("pace", "maxActivities", "maxDailyTravel", "valueOfTime", "budget")], ds, client, unavailable_note=request.state.llm_note).model_dump()
    return {**out, "city_id": ds.city.id}


class ParseReplanRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    text: str = Field(max_length=10_000)
    preferences: Preferences
    current_result: SolveResult


@router.post("/parse-replan")
def parse_replan_route(req: ParseReplanRequest, request: Request, client: LLMClient | None = Depends(llm_for_request)):
    """Free text -> PROPOSED operations. Nothing is executed; the client must confirm and call /replan."""
    ds = _dataset(request, req.city_id)
    _check_city_ids(request, ds, prefs=req.preferences, part="early")
    current = rebuild_client_schedule(req.preferences, req.current_result, ds, request.app.state.datasets,
                                      after_structure=lambda: _check_city_ids(request, ds, prefs=req.preferences, part="late"))
    out = parse_replan(req.text, current, req.preferences, ds, client, unavailable_note=request.state.llm_note,
                       all_datasets=request.app.state.datasets).model_dump()
    return {**out, "city_id": ds.city.id}


class CopilotRequest(_Req):
    city_id: str = Field(default=DEFAULT_CITY_ID, max_length=32)
    preferences: Preferences
    current_result: SolveResult
    fingerprint: str = Field(min_length=16, max_length=16)
    text: str = Field(default="", max_length=10_000)
    operation: CopilotOperation | None = None  # explicit quick question; read-only operations only


@router.post("/copilot")
async def copilot_route(req: CopilotRequest, request: Request, client: LLMClient | None = Depends(llm_for_request)):
    ds, cfg = _dataset(request, req.city_id), request.app.state.hconf
    _check_city_ids(request, ds, hotels=[req.current_result.hotel_id], prefs=req.preferences)
    if req.current_result.hotel_id not in ds.hotels:
        raise ApiError(422, "unknown_hotel", "Unknown hotel id.")
    if req.fingerprint != plan_fingerprint(req.preferences, req.current_result.hotel_id, ds):
        raise ApiError(409, "stale_scenario", "This question refers to an older scenario. Ask again on the current plan.")

    def work():
        # Client prose, conflicts, prices, totals and validity assertions are never evidence.
        if req.current_result.status == "feasible":
            current = rebuild_client_schedule(req.preferences, req.current_result, ds, request.app.state.datasets)
        else:
            with request.app.state.solver_capacity.claim():
                current = solve(req.preferences, req.current_result.hotel_id, ds,
                                max_seconds=cfg.optimise_time_limit_seconds, with_alternatives=False)
        intent, note, calls = (Intent(operation=req.operation), None, 0) if req.operation else classify_copilot(req.text, client, get_settings().max_input_chars)
        if intent and intent.attraction_name and intent.attraction_name.casefold() not in req.text.casefold():
            intent = Intent(operation="CLARIFY")  # the model may only COPY a name the person actually wrote
        if intent and any(n is not None and not re.search(rf"(?<!\d){n}(?!\d)", req.text) for n in (intent.day, intent.target_day)) and not req.operation:
            intent = Intent(operation="CLARIFY")  # ditto for day numbers
        alternatives = None
        if intent and intent.operation in ("COMPARE_ALTERNATIVES", "ANALYSE_TRADEOFF") and current.status == "feasible":
            with request.app.state.solver_capacity.claim():
                alternatives = explore(req.preferences, [current.hotel_id], ds,
                                       max_seconds=min(2.0, cfg.optimise_time_limit_seconds), budget_seconds=4.0)
        out = copilot_answer(intent or Intent(operation="CLARIFY"), req.preferences, current, ds, alternatives)
        rep = build_report(current, ds, req.preferences, current.hotel_id)
        out["evidence"].extend(structured_evidence(current, rep))
        problems = verify_evidence(out, current, rep, req.preferences, ds, nights_for(req.preferences)[0])
        if problems:                                    # fail closed: an answer whose evidence does not re-derive is not shown at all
            log.warning("copilot_evidence_rejected", extra={"problems": len(problems)})
            out.update(message=FAIL_CLOSED, evidence=[], handoff=None, proposal=None, verification="none")
        out.update(fingerprint=req.fingerprint, city_id=ds.city.id, call_count=calls,
                   mode="llm" if calls else "deterministic", status=("evidence_rejected" if problems else "ok" if intent else "fallback"))
        if not req.operation and intent:
            out["interpretation"] = "AI-selected intent; explanation and evidence constructed by code."
        elif req.operation:
            out["interpretation"] = "Explicit quick question; explanation and evidence constructed by code."
        else:
            out["interpretation"] = "No AI interpretation accepted; use the deterministic controls."
        if note:
            out["message"] = note
        return out
    return await run_cancellable(request, work)


# ---- app factory ------------------------------------------------------------------------------------------------------
def create_app(cfg: HConfig | None = None, data_loader=load_all_city_datasets) -> FastAPI:
    cfg = cfg or HConfig.from_env()  # raises ConfigError on invalid configuration: fail fast
    cfg.validate()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        t0 = time.perf_counter()
        loaded = data_loader()  # fail fast with a clear error if data is bad
        app.state.datasets = loaded if isinstance(loaded, dict) else {DEFAULT_CITY_ID: loaded}
        app.state.dataset = app.state.datasets[DEFAULT_CITY_ID]  # legacy test/helper compatibility
        log.info("startup", extra={"startup_seconds": round(time.perf_counter() - t0, 3), **cfg.summary(get_settings().mode)})
        if get_settings().llm_enabled:
            log.info("llm_endpoint", extra={"host": OPENROUTER_HOST, "relay": True, "model": get_settings().llm_model})  # host and model only
        yield

    docs = {} if cfg.docs_enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Fitinerary API (multi-city sample data)", lifespan=lifespan, **docs)
    app.state.hconf = cfg
    app.state.limiter = SlidingWindowLimiter(
        {"expensive": cfg.rate_expensive_per_min, "standard": cfg.rate_standard_per_min, "cheap": cfg.rate_cheap_per_min},
        window=cfg.rate_window_seconds)
    app.state.llm_budget = LLMBudget(cfg.llm_daily_call_cap)
    app.state.solver_capacity = SolverCapacity(cfg.solver_concurrency, cfg.solver_busy_retry_after_seconds)
    app.include_router(router)
    app.add_middleware(HardeningMiddleware, cfg=cfg, limiter=app.state.limiter)
    app.add_middleware(CORSMiddleware, allow_origins=list(cfg.allowed_origins), allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])  # added last = outermost, so error responses carry CORS headers too

    def rid(request: Request) -> str | None:
        return getattr(request.state, "request_id", None)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(x) for x in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()[:20]]
        return error_response(422, "validation_error", "Request body is invalid.", details, rid(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, "Not found." if exc.status_code == 404 else "Request cannot be handled.", None, rid(request))

    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.details, rid(request), exc.headers)

    @app.exception_handler(ReplanError)
    async def _replan(request: Request, exc: ReplanError):
        return error_response(422, exc.code, exc.message, exc.details, rid(request))

    @app.exception_handler(ComparisonInputError)
    async def _cmp(request: Request, exc: ComparisonInputError):
        return error_response(400, "invalid_request", str(exc), None, rid(request))

    @app.exception_handler(InputTooLong)
    async def _long(request: Request, exc: InputTooLong):
        return error_response(422, "input_too_long", str(exc), {"limit": exc.limit}, rid(request))

    @app.exception_handler(EmptyInput)
    async def _empty(request: Request, exc: EmptyInput):
        return error_response(422, "empty_input", str(exc), None, rid(request))

    return app


app = create_app()
