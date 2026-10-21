"""Phase 6: API hardening. Mock LLM only, network blocked, injectable clock (no sleeping)."""
import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import config, runtime
from app.data.loader import DataError, load_dataset
from app.hardening.config import ConfigError, HConfig
from app.hardening.limiter import client_ip
from app.hardening.logging_setup import redact
from app.llm.client import LLMUnavailable, get_llm_client
from app.main import app as shared_app
from app.main import create_app
from app.optimiser.schedule import assemble
from app.optimiser.solver import ObjectiveSpec, solve
from app.schemas import Preferences, SolveResult
from llm_mock import MockLLM, extraction

DS = load_dataset()
ASA, ODA = "hotel_lantern_asakusa", "hotel_harbour_odaiba"
KEY = "sk-ant-FAKE-KEY-for-leak-tests-0123456789"
SPEC = ObjectiveSpec("true_cost", 1000, config.MISSED_PREFERENCE_PENALTY_JPY)
ONLY = ["senso_ji", "tokyo_skytree"]


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(runtime, "now", c)
    return c


def tiny(**kw):
    return Preferences(days=1, must_visit=ONLY, skip=[a for a in DS.attractions if a not in ONLY], start_weekday=2, **kw)


def tiny_cur(p=None):
    p = p or tiny()
    return p, solve(p, ASA, DS, with_alternatives=False, objective=SPEC)


def mk(client_ip_="testclient", mock="UNSET", raise_exc=False, **cfg_kw):
    a = create_app(HConfig(**cfg_kw))
    if mock != "UNSET":
        a.dependency_overrides[get_llm_client] = lambda: mock
    c = TestClient(a, client=(client_ip_, 50000), raise_server_exceptions=raise_exc)
    c.__enter__()
    return a, c


@pytest.fixture
def clients():
    made = []

    def make(*a, **k):
        app_, c = mk(*a, **k)
        made.append(c)
        return app_, c

    yield make
    for c in made:
        c.__exit__(None, None, None)


def optimise_body(**kw):
    return {"preferences": tiny().model_dump(), "hotel_id": ASA, **kw}


# ---- rate limiting ---------------------------------------------------------------------------------------------------
def test_each_tier_limits_at_its_configured_number_with_retry_after(clients, clock):
    _, c = clients(rate_expensive_per_min=2, rate_standard_per_min=3, rate_cheap_per_min=4)
    for _ in range(4):
        assert c.get("/health").status_code == 200
    r = c.get("/health")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["code"] == "rate_limited" and r.json()["error"]["request_id"]
    for _ in range(3):
        assert c.post("/optimise", json=optimise_body()).status_code == 200
    assert c.post("/optimise", json=optimise_body()).status_code == 429
    for _ in range(2):
        assert c.post("/parse", json={"text": "3 days"}).status_code == 200
    r = c.post("/parse", json={"text": "3 days"})
    assert r.status_code == 429 and "retry-after" in r.headers


def test_tiers_are_independent(clients, clock):
    _, c = clients(rate_expensive_per_min=1, rate_standard_per_min=2, rate_cheap_per_min=5)
    assert c.post("/parse", json={"text": "3 days"}).status_code == 200
    assert c.post("/parse", json={"text": "3 days"}).status_code == 429
    assert c.get("/health").status_code == 200 and c.get("/data/summary").status_code == 200
    assert c.post("/optimise", json=optimise_body()).status_code == 200


def test_explain_true_stays_on_the_standard_tier(clients, clock):
    """The AI explanation is cost-controlled by the shared engine's 'explain' bucket (non-fatal, see test_ai_quota.py), so the
    in-memory flood guard no longer moves explain=true requests to the expensive tier."""
    _, c = clients(rate_expensive_per_min=1, rate_standard_per_min=5)
    assert c.post("/optimise", json=optimise_body(explain=True)).status_code == 200
    assert c.post("/optimise", json=optimise_body(explain=True)).status_code == 200


def test_different_ips_have_separate_buckets(clients, clock):
    a, c1 = clients("10.0.0.1", rate_cheap_per_min=2, rate_standard_per_min=2, rate_expensive_per_min=1)
    c2 = TestClient(a, client=("10.0.0.2", 1))
    c2.__enter__()
    assert [c1.get("/health").status_code for _ in range(3)] == [200, 200, 429]
    assert [c2.get("/health").status_code for _ in range(3)] == [200, 200, 429]
    c2.__exit__(None, None, None)


def test_spoofed_x_forwarded_for_is_ignored_at_zero_trusted_hops(clients, clock):
    _, c = clients(rate_cheap_per_min=3, rate_standard_per_min=3, rate_expensive_per_min=1, trusted_proxy_hops=0)
    codes = [c.get("/health", headers={"X-Forwarded-For": f"8.8.8.{i}"}).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_x_forwarded_for_honoured_from_the_right_at_one_trusted_hop(clients, clock):
    _, c = clients(rate_cheap_per_min=2, rate_standard_per_min=2, rate_expensive_per_min=1, trusted_proxy_hops=1)
    # the proxy appended the real client (last entry); anything the client prepended is ignored
    same = [c.get("/health", headers={"X-Forwarded-For": f"{fake}, 203.0.113.7"}).status_code for fake in ("1.1.1.1", "2.2.2.2", "3.3.3.3")]
    assert same == [200, 200, 429]
    other = c.get("/health", headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.8"})
    assert other.status_code == 200  # a different real client has its own bucket


def test_client_ip_function_edge_cases():
    sc = lambda xff, peer="9.9.9.9": {"client": (peer, 1), "headers": [(b"x-forwarded-for", xff.encode())] if xff is not None else []}
    assert client_ip(sc("1.1.1.1, 2.2.2.2"), 0) == "9.9.9.9"
    assert client_ip(sc("1.1.1.1, 2.2.2.2"), 1) == "2.2.2.2"
    assert client_ip(sc("1.1.1.1, 2.2.2.2, 3.3.3.3"), 2) == "2.2.2.2"
    assert client_ip(sc("2.2.2.2"), 2) == "9.9.9.9"          # fewer entries than trusted hops: fall back to the socket
    assert client_ip(sc("not-an-ip"), 1) == "9.9.9.9"        # garbage: fall back to the socket
    assert client_ip(sc(None), 1) == "9.9.9.9"
    assert client_ip({"client": None, "headers": []}, 0) == "unknown"


def test_window_resets_after_time_passes_without_sleeping(clients, clock):
    _, c = clients(rate_cheap_per_min=2, rate_standard_per_min=2, rate_expensive_per_min=1)
    assert [c.get("/health").status_code for _ in range(3)] == [200, 200, 429]
    r = c.get("/health")
    assert r.status_code == 429 and int(r.headers["retry-after"]) == 60
    clock.advance(30)
    assert c.get("/health").status_code == 429 and int(c.get("/health").headers["retry-after"]) == 30
    clock.advance(31)
    assert c.get("/health").status_code == 200


# ---- LLM circuit breaker ---------------------------------------------------------------------------------------------------
def test_circuit_breaker_counts_retries_disables_the_llm_and_health_reflects_it(clients, clock, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "mock-model")
    mock = MockLLM(["bad", "bad", extraction(days=2)])
    _, c = clients(mock=mock, llm_daily_call_cap=3, rate_expensive_per_min=20, rate_standard_per_min=20)
    assert c.get("/health").json()["llm_mode"] == "llm" and c.get("/health").json()["llm_limit_reached"] is False
    r1 = c.post("/parse", json={"text": "2 days"}).json()
    assert r1["status"] == "needs_clarification" and r1["call_count"] == 2  # two real calls (first + retry) counted
    r2 = c.post("/parse", json={"text": "2 days"}).json()  # third call allowed -> valid
    assert r2["status"] == "ok" and len(mock.calls) == 3
    h = c.get("/health").json()
    assert h["llm_mode"] == "sample" and h["mode"] == "sample" and h["llm_limit_reached"] is True and h["llm_configured"] is True
    r3 = c.post("/parse", json={"text": "2 days"}).json()
    assert r3["status"] == "llm_unavailable" and "daily limit" in r3["clarifications"][0] and len(mock.calls) == 3
    ex = c.post("/optimise", json=optimise_body(explain=True)).json()["explanation"]
    assert ex["source"] == "template" and "daily limit" in ex["reason"]
    clock.advance(24 * 3600 + 1)  # rolling 24 h window
    assert c.get("/health").json()["llm_limit_reached"] is False


def test_breaker_stops_a_request_mid_flight_when_the_cap_is_hit_by_its_own_retry(clients, clock):
    mock = MockLLM(["bad", extraction(days=2)])
    _, c = clients(mock=mock, llm_daily_call_cap=1)
    r = c.post("/parse", json={"text": "2 days"}).json()
    assert r["status"] == "llm_unavailable" and len(mock.calls) == 1  # the retry was refused by the breaker


# ---- request limits -----------------------------------------------------------------------------------------------------------
def asgi_post(application, path, headers, chunks):
    sent = []
    queue = list(chunks)

    async def receive():
        if queue:
            b = queue.pop(0)
            return {"type": "http.request", "body": b, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": "POST", "path": path, "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
             "client": ("1.2.3.4", 1), "query_string": b"", "http_version": "1.1", "scheme": "http", "server": ("t", 80), "root_path": ""}
    asyncio.run(application(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


def test_oversize_body_is_rejected_with_honest_and_lying_content_length(clients):
    a, c = clients(body_max_bytes=2048)
    big = json.dumps({"preferences": tiny().model_dump(), "hotel_id": ASA, "pad": "x" * 5000})
    r = c.post("/optimise", content=big, headers={"Content-Type": "application/json"})  # honest length
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    status, body = asgi_post(a, "/optimise", {"content-type": "application/json", "content-length": "20"}, [b'{"a": "' + b"x" * 1500, b"y" * 1500, b'"}'])
    assert status == 413 and body["error"]["code"] == "payload_too_large"  # lying length: enforced while reading
    status, body = asgi_post(a, "/optimise", {"content-type": "application/json"}, [b"x" * 1500, b"y" * 1500])  # chunked, no length at all
    assert status == 413


def test_wrong_content_type_unknown_fields_and_bad_json(clients):
    _, c = clients()
    assert c.post("/optimise", content=json.dumps(optimise_body()), headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/optimise", content=b"{not json", headers={"Content-Type": "application/json"}).json()["error"]["code"] == "invalid_json"
    r = c.post("/optimise", json={**optimise_body(), "surprise": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    nested = optimise_body()
    nested["preferences"]["allow_everything"] = True
    assert c.post("/optimise", json=nested).status_code == 422


def test_caps_on_lists_strings_depth_and_numbers(clients):
    _, c = clients(rate_cheap_per_min=500, rate_standard_per_min=500, rate_expensive_per_min=10)
    base = tiny().model_dump()

    def post(path, **over):
        body = {"preferences": {**base, **over.pop("prefs", {})}, **over}
        return c.post(path, json=body)

    ids = [f"id{i}" for i in range(51)]
    cases = {
        "must_visit>50": post("/optimise", hotel_id=ASA, prefs={"must_visit": ids}),
        "interests>20": post("/optimise", hotel_id=ASA, prefs={"interests": ["x"] * 21}),
        "interest string>60": post("/optimise", hotel_id=ASA, prefs={"interests": ["x" * 61]}),
        "hotel_id>64": post("/optimise", hotel_id="h" * 65),
        "hotels>3": post("/compare-hotels", hotel_ids=["a", "b", "c", "d"]),
        "ops>20": post("/replan", current_result={}, operations=[{"op": "unlock", "attraction_id": "senso_ji"}] * 21),
        "days 8": post("/optimise", hotel_id=ASA, prefs={"days": 8}),
        "negative budget": post("/optimise", hotel_id=ASA, prefs={"budget": -1}),
        "absurd budget": post("/optimise", hotel_id=ASA, prefs={"budget": 10 ** 12}),
        "absurd VoT": post("/optimise", hotel_id=ASA, prefs={"value_of_time_per_hour": 10 ** 9}),
        "nights 31": post("/optimise", hotel_id=ASA, prefs={"nights": 31}),
        "text>10000": c.post("/parse", json={"text": "x" * 10_001}),
    }
    for name, r in cases.items():
        assert r.status_code == 422, (name, r.status_code, r.text[:200])
    deep = '{"preferences": ' + "[" * 40 + "]" * 40 + ', "hotel_id": "x"}'
    assert c.post("/optimise", content=deep, headers={"Content-Type": "application/json"}).json()["error"]["code"] == "json_too_deep"
    for bad in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}'):
        r = c.post("/optimise", content=bad, headers={"Content-Type": "application/json"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_number"


def test_stop_and_string_caps_on_client_schedules(clients):
    _, c = clients()
    p, cur = tiny_cur()
    d = cur.model_dump()
    d["days"][0]["stops"] = d["days"][0]["stops"] * 7  # 14 stops in a day
    assert c.post("/replan", json={"preferences": p.model_dump(), "current_result": d, "operations": []}).status_code == 422
    d2 = cur.model_dump()
    d2["days"][0]["stops"][0]["attraction_id"] = "x" * 100
    assert c.post("/replan", json={"preferences": p.model_dump(), "current_result": d2, "operations": []}).status_code == 422


# ---- untrusted client schedules -------------------------------------------------------------------------------------------------
def replan_body(p, cur_dict, ops=None):
    return {"preferences": p.model_dump(), "current_result": cur_dict, "operations": ops or []}


def assert_rejected(r, check):
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_schedule", r.text[:300]
    assert any(check == d["check"] for d in r.json()["error"]["details"]), r.json()["error"]["details"]


def test_tampered_schedules_are_rejected_with_findings(clients):
    _, c = clients(rate_standard_per_min=500, rate_expensive_per_min=100)
    p, cur = tiny_cur()
    good = cur.model_dump()
    assert c.post("/replan", json=replan_body(p, good)).status_code == 200

    # overlap: second stop moved onto the first one's time (end adjusted so only the validator can catch it)
    d = cur.model_dump()
    s0, s1 = d["days"][0]["stops"][0], d["days"][0]["stops"][1]
    s1["start"] = s0["start"]
    h, m = map(int, s0["start"].split(":"))
    dur = DS.attractions[s1["attraction_id"]].duration_minutes
    s1["end"] = f"{(h * 60 + m + dur) // 60:02d}:{(h * 60 + m + dur) % 60:02d}"
    r = c.post("/replan", json=replan_body(p, d))
    assert r.status_code == 422 and {x["check"] for x in r.json()["error"]["details"]} & {"overlaps", "travel_gaps"}

    # missing must-visit
    d = cur.model_dump()
    d["days"][0]["stops"] = [s for s in d["days"][0]["stops"] if s["attraction_id"] != "senso_ji"]
    assert_rejected(c.post("/replan", json=replan_body(p, d)), "must_visits")

    # closed attraction (Tokyo National Museum is closed on Monday)
    p_mon = Preferences(days=1, start_weekday=0)
    days, totals = assemble(DS, p_mon, ASA, [[("tokyo_national_museum", 600)]])
    mon = SolveResult(status="feasible", hotel_id=ASA, days=days, totals=totals)
    assert_rejected(c.post("/replan", json=replan_body(p_mon, mon.model_dump())), "opening_hours")

    # negative / altered duration, bad time format, unknown id, bad day, infeasible status, unknown hotel
    d = cur.model_dump()
    d["days"][0]["stops"][0]["end"] = "00:10"
    assert_rejected(c.post("/replan", json=replan_body(p, d)), "durations")
    d = cur.model_dump()
    d["days"][0]["stops"][0]["start"] = "99:99"
    assert_rejected(c.post("/replan", json=replan_body(p, d)), "times")
    d = cur.model_dump()
    d["days"][0]["stops"][0]["attraction_id"] = "atlantis"
    assert_rejected(c.post("/replan", json=replan_body(p, d)), "known_items")
    d = cur.model_dump()
    d["days"][0]["day"] = 5
    assert_rejected(c.post("/replan", json=replan_body(p, d)), "days")
    d = cur.model_dump()
    d["status"] = "infeasible"
    assert c.post("/replan", json=replan_body(p, d)).status_code == 422
    d = cur.model_dump()
    d["hotel_id"] = "hotel_nope"
    assert c.post("/replan", json=replan_body(p, d)).status_code == 422

    # /parse-replan applies the same gate
    d = cur.model_dump()
    d["days"][0]["stops"][0]["end"] = "00:10"
    r = c.post("/parse-replan", json={"text": "x", "preferences": p.model_dump(), "current_result": d})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_schedule"


def test_fabricated_costs_and_totals_are_ignored_and_server_recomputes(clients):
    _, c = clients(rate_standard_per_min=500)
    p, cur = tiny_cur()
    honest = c.post("/replan", json=replan_body(p, cur.model_dump())).json()
    forged = cur.model_dump()
    for s in forged["days"][0]["stops"]:
        s.update(cost=999_999, transport_jpy_from_previous=888_888, travel_minutes_from_previous=1, cost_status="unknown", uncertainty_flags=["fake"])
    forged["totals"].update(total_jpy=1, admissions_jpy=1, transport_jpy=1, travel_minutes=1, unknown_cost_items=["senso_ji"])
    forged["missed_preferences"] = ["tokyo_tower"]
    forged["notes"] = ["trust me"]
    r = c.post("/replan", json=replan_body(p, forged))
    assert r.status_code == 200
    j = r.json()
    assert j["old_breakdown"] == honest["old_breakdown"]  # the 'before' side was recomputed from the dataset
    assert j["old_breakdown"]["admissions_jpy"] == 2100 and j["old_breakdown"]["true_cost_jpy"] == honest["old_breakdown"]["true_cost_jpy"]
    assert j["schedule"]["totals"] == honest["schedule"]["totals"]


# ---- deadlines and fallback ---------------------------------------------------------------------------------------------------------
class SlowMock(MockLLM):
    def __init__(self, replies, clock, seconds):
        super().__init__(replies)
        self.clock, self.seconds = clock, seconds

    def complete(self, system, messages, max_tokens):
        self.clock.advance(self.seconds)
        return super().complete(system, messages, max_tokens)


def test_slow_llm_uses_up_the_deadline_so_the_retry_is_skipped_and_sample_mode_answers(clients, clock):
    mock = SlowMock(["not json", extraction(days=2)], clock, 20.0)
    _, c = clients(mock=mock)  # deadline 25 s; model timeout 10 s
    r = c.post("/parse", json={"text": "2 days"})
    assert r.status_code == 200 and r.json()["status"] == "llm_unavailable" and len(mock.calls) == 1  # retry skipped, no hang, no 5xx


def test_deadline_too_short_for_a_model_call_skips_the_llm_entirely(clients, clock):
    mock = MockLLM([extraction(days=2)])
    _, c = clients(mock=mock, request_deadline_seconds=5.0)
    r = c.post("/parse", json={"text": "2 days"}).json()
    assert r["status"] == "llm_unavailable" and "deadline" in r["clarifications"][0] and mock.calls == []
    ex = c.post("/optimise", json=optimise_body(explain=True))
    assert ex.status_code == 200 and ex.json()["explanation"]["source"] == "template" and ex.json()["explanation"]["call_count"] == 0


def test_llm_timeout_error_falls_back_without_error_status(clients, clock):
    mock = MockLLM([LLMUnavailable("timeout")])
    _, c = clients(mock=mock)
    assert c.post("/parse", json={"text": "2 days"}).json()["status"] == "llm_unavailable"


# ---- errors and leakage ---------------------------------------------------------------------------------------------------------------
def test_internal_exception_returns_generic_500_with_request_id_and_no_internals(clients, monkeypatch, caplog):
    import app.main as m

    def boom(*a, **k):
        raise RuntimeError(f"secret detail /Users/someone/private.py {KEY}")

    monkeypatch.setattr(m, "solve", boom)
    _, c = clients()
    with caplog.at_level(logging.DEBUG):
        r = c.post("/optimise", json=optimise_body())
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    rid = r.json()["error"]["request_id"]
    assert rid and r.headers["x-request-id"] == rid
    for bad in ("Traceback", "/Users", "secret detail", KEY, "RuntimeError", "private.py"):
        assert bad not in r.text
    dump = caplog.text + "\n".join(str(x.__dict__) for x in caplog.records)
    assert "secret detail" not in dump and KEY not in dump and "unhandled_exception" in dump and rid in dump


def test_client_supplied_request_id_is_ignored(clients):
    _, c = clients()
    r = c.get("/health", headers={"X-Request-ID": "evil\nlog injection"})
    assert r.headers["x-request-id"] != "evil\nlog injection" and len(r.headers["x-request-id"]) == 16


def test_error_format_is_unified_including_404_and_405(clients):
    _, c = clients()
    for r in (c.get("/nope"), c.post("/health", json={}), c.get("/optimise")):
        assert set(r.json()) == {"error"} and {"code", "message", "request_id"} <= set(r.json()["error"]), r.text
    assert c.get("/nope").json()["error"]["code"] == "not_found"


def test_fake_key_never_appears_in_responses_or_logs_across_a_fuzz_run(clients, clock, monkeypatch, caplog):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "mock-model")

    class Leaky(MockLLM):
        def complete(self, system, messages, max_tokens):
            raise RuntimeError(f"upstream said: key={KEY}")  # an unexpected failure that mentions the key

    _, c = clients(mock=Leaky([]), rate_cheap_per_min=1000, rate_standard_per_min=1000, rate_expensive_per_min=1000)
    p, cur = tiny_cur()
    fuzz = [b"", b"{}", b"[]", b"null", b"\xff\xfe", b'{"text": 5}', b'{"preferences": {"days": "x"}}', b'{"text":' + b'"a"' * 3 + b"}",
            json.dumps({"text": "hello " + KEY}).encode(), json.dumps(optimise_body(hotel_id=KEY)).encode(),
            json.dumps({"text": "2 days"}).encode(), json.dumps(optimise_body(explain=True)).encode(),
            json.dumps({"preferences": p.model_dump(), "current_result": cur.model_dump(), "operations": [], "explain": True}).encode()]
    responses = [c.get(u, headers={"Authorization": f"Bearer {KEY}", "X-Api-Key": KEY}) for u in ("/health", "/data/summary", "/nope?k=" + KEY)]
    with caplog.at_level(logging.DEBUG):
        for path in ("/parse", "/parse-replan", "/optimise", "/compare-hotels", "/replan"):
            for body in fuzz:
                responses.append(c.post(path, content=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}))
    texts = "\n".join(r.text + str(dict(r.headers)) for r in responses)
    echoed_by_client_input = KEY in json.dumps(optimise_body(hotel_id=KEY))  # the client's OWN input may be reflected in a 422; the env value never is
    assert echoed_by_client_input
    dump = caplog.text + "\n".join(str(x.__dict__) for x in caplog.records)
    assert KEY not in dump
    assert not any(r.status_code >= 500 and KEY in r.text for r in responses)
    hits = [r for r in responses if KEY in r.text]
    assert all(r.status_code == 422 for r in hits)  # only validation errors reflecting the sender's own field values
    assert "sk-ant" not in "".join(r.text for r in responses if r.status_code >= 500)
    assert any(r.status_code == 500 for r in responses)  # the leaky client did produce generic 500s
    assert all(r.json()["error"]["code"] == "internal_error" for r in responses if r.status_code == 500)


def test_log_redaction_unit():
    assert KEY not in redact(f"key={KEY} and Authorization: Bearer abc.def-123 and x-api-key: {KEY} api_key=abcdef")
    assert "abc.def-123" not in redact("Authorization: Bearer abc.def-123")
    assert redact("plain text 12345") == "plain text 12345"


def test_unique_marker_never_reaches_the_logs_on_any_route(clients, clock, caplog, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "mock-model")
    MARK = "ZZMARKERZZ_8675309"
    mock = MockLLM([extraction(days=2, must_visit_names=[MARK]), extraction(days=2)] * 3 + ["text " + MARK] * 4)
    _, c = clients(mock=mock, rate_cheap_per_min=1000, rate_standard_per_min=1000, rate_expensive_per_min=1000)
    p, cur = tiny_cur()
    hdr = {"Content-Type": "application/json", "Authorization": f"Bearer {MARK}", "X-Api-Key": MARK, "User-Agent": MARK, "Cookie": f"a={MARK}"}
    with caplog.at_level(logging.DEBUG):
        c.get(f"/health?q={MARK}", headers=hdr)
        c.get(f"/data/summary?q={MARK}", headers=hdr)
        c.get(f"/{MARK}", headers=hdr)
        c.post(f"/optimise?q={MARK}", content=json.dumps(optimise_body(hotel_id=MARK)), headers=hdr)
        c.post("/optimise", content=json.dumps(optimise_body(explain=True)), headers=hdr)
        c.post("/compare-hotels", content=json.dumps({"preferences": tiny().model_dump(), "hotel_ids": [ASA, MARK]}), headers=hdr)
        c.post("/compare-hotels", content=json.dumps({"preferences": tiny().model_dump(), "hotel_ids": [ASA, ODA], "explain": True}), headers=hdr)
        c.post("/replan", content=json.dumps({**replan_body(p, cur.model_dump(), [{"op": "remove", "attraction_id": MARK}])}), headers=hdr)
        c.post("/replan", content=json.dumps({**replan_body(p, cur.model_dump()), "explain": True}), headers=hdr)
        c.post("/parse", content=json.dumps({"text": f"3 days {MARK}"}), headers=hdr)
        c.post("/parse", content=json.dumps({"text": f"3 days {MARK}"}), headers=hdr)
        c.post("/parse-replan", content=json.dumps({"text": f"remove {MARK}", "preferences": p.model_dump(), "current_result": cur.model_dump()}), headers=hdr)
        c.post("/parse", content=f"{MARK}".encode(), headers=hdr)
        c.post("/parse", content=f"{MARK}".encode(), headers={**hdr, "Content-Type": "text/plain"})
    # the TestClient's own HTTP library logs the request URL on the *client* side; only server-side loggers count here
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert any(r.name == "app.access" for r in records)
    dump = "\n".join(str(r.__dict__) for r in records) + "\n".join(r.getMessage() for r in records)
    assert "ZZMARKERZZ" not in dump and "8675309" not in dump
    routes = {getattr(r, "route", None) for r in records if r.name == "app.access"}
    assert "unmatched" in routes and "/optimise" in routes  # unknown paths are logged as 'unmatched', never verbatim


def test_access_log_has_metadata_fields_only(clients, caplog):
    _, c = clients()
    with caplog.at_level(logging.INFO):
        c.get("/health?secret=1")
    rec = next(r for r in caplog.records if r.name == "app.access")
    for k in ("request_id", "route", "status", "latency_ms", "tier", "rate_limited", "llm_calls", "llm_input_tokens", "llm_output_tokens", "outcome"):
        assert hasattr(rec, k)
    assert rec.route == "/health" and "secret" not in str(rec.__dict__)


# ---- CORS, headers, docs, config ------------------------------------------------------------------------------------------------------------
def test_cors_allows_configured_origins_only_and_error_responses_carry_cors_headers(clients, clock):
    _, c = clients(allowed_origins=("https://demo.example.com",), rate_cheap_per_min=1, rate_standard_per_min=1, rate_expensive_per_min=1)
    ok = c.get("/health", headers={"Origin": "https://demo.example.com"})
    assert ok.headers["access-control-allow-origin"] == "https://demo.example.com"
    assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": "https://evil.example"}).headers
    limited = c.get("/health", headers={"Origin": "https://demo.example.com"})
    assert limited.status_code == 429 and limited.headers["access-control-allow-origin"] == "https://demo.example.com"
    pre = c.options("/optimise", headers={"Origin": "https://demo.example.com", "Access-Control-Request-Method": "DELETE"})
    assert pre.status_code == 400  # method not allowed by CORS
    pre = c.options("/optimise", headers={"Origin": "https://demo.example.com", "Access-Control-Request-Method": "POST",
                                          "Access-Control-Request-Headers": "x-evil"})
    assert pre.status_code == 400  # header not allowed


def test_default_cors_is_localhost_dev_only():
    cfg = HConfig.from_env({})
    assert "http://localhost:5173" in cfg.allowed_origins and all("localhost" in o or "127.0.0.1" in o for o in cfg.allowed_origins)
    with TestClient(shared_app) as c:
        assert c.get("/health", headers={"Origin": "http://localhost:5173"}).headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": "https://evil.example"}).headers


def test_wildcard_and_bad_origins_and_bad_config_are_refused_at_startup():
    with pytest.raises(ConfigError, match=r"\*"):
        HConfig.from_env({"CORS_ORIGINS": "*"})
    with pytest.raises(ConfigError):
        create_app(HConfig(allowed_origins=("https://ok.example", "*")))
    with pytest.raises(ConfigError, match="exact origin"):
        HConfig.from_env({"CORS_ORIGINS": "https://x.example/path"})
    for env in ({"RATE_LIMIT_PER_MIN": "0"}, {"RATE_LIMIT_PER_MIN": "abc"}, {"TRUSTED_PROXY_HOPS": "9"}, {"BODY_MAX_BYTES": "5"},
               {"RATE_LIMIT_EXPENSIVE_PER_MIN": "50", "RATE_LIMIT_PER_MIN": "10"}, {"LLM_DAILY_CALL_CAP": "-1"}):
        with pytest.raises(ConfigError):
            HConfig.from_env(env)
    ok = HConfig.from_env({"CORS_ORIGINS": "https://a.example, https://b.example/", "TRUSTED_PROXY_HOPS": "1"})
    assert ok.allowed_origins == ("https://a.example", "https://b.example") and ok.trusted_proxy_hops == 1


def test_startup_fails_fast_on_missing_data_and_logs_a_secret_free_config_summary(caplog, monkeypatch):
    def bad_loader():
        raise DataError("attractions.json: cannot read file")

    a = create_app(HConfig(), data_loader=bad_loader)
    with pytest.raises(DataError):
        with TestClient(a):
            pass
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "some-model")
    with caplog.at_level(logging.INFO):
        with TestClient(create_app(HConfig())):
            pass
    rec = next(r for r in caplog.records if r.name == "app.startup")
    assert rec.mode == "llm" and rec.startup_seconds >= 0 and rec.rate_per_min == {"expensive": 6, "standard": 30, "cheap": 120}
    assert KEY not in caplog.text and KEY not in str(rec.__dict__) and "some-model" not in str(rec.__dict__)


def test_security_headers_on_every_kind_of_response(clients, clock):
    _, c = clients(rate_cheap_per_min=1, rate_standard_per_min=1, rate_expensive_per_min=1, body_max_bytes=1024)
    responses = [c.get("/health"), c.get("/health"), c.get("/nope"), c.post("/optimise", content=b"x" * 2000, headers={"Content-Type": "application/json"}),
                 c.post("/parse", json={"text": "hi"})]
    for r in responses:
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in r.headers["cache-control"] and r.headers["x-frame-options"] == "DENY" and r.headers["x-request-id"]


def test_docs_and_openapi_are_off_by_default_and_on_when_enabled(clients):
    _, c = clients()
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404 and c.get("/redoc").status_code == 404
    _, d = clients(docs_enabled=True)
    assert d.get("/openapi.json").status_code == 200


# ---- /optimise and sample-mode parity -------------------------------------------------------------------------------------------------------------
def test_optimise_feasible_passes_the_validator_and_explain_works_in_sample_mode(clients):
    _, c = clients()
    r = c.post("/optimise", json=optimise_body(explain=True))
    assert r.status_code == 200
    j = r.json()
    assert j["result"]["status"] == "feasible" and j["feasibility_report"]["overall"] in ("pass", "pass_with_warnings")
    assert j["approximate"] is False and j["nights_used"] == 0
    assert sorted(s["attraction_id"] for d in j["result"]["days"] for s in d["stops"])[:2] == sorted(ONLY)
    assert j["explanation"]["source"] == "template" and "Plan for" in j["explanation"]["text"]
    assert "Warnings:" in j["explanation"]["text"]  # validator data-quality warnings appended by code
    assert "explanation" not in c.post("/optimise", json=optimise_body()).json()


def test_optimise_infeasible_returns_conflicts_and_computed_alternatives_and_a_failing_report(clients):
    _, c = clients()
    p = Preferences(days=1, pace="packed", day_end="13:00", start_weekday=2, must_visit=["tokyo_national_museum", "senso_ji", "tokyo_skytree"])
    j = c.post("/optimise", json={"preferences": p.model_dump(), "hotel_id": ASA}).json()
    assert j["result"]["status"] == "infeasible" and j["result"]["conflicts"][0]["code"] == "not_enough_time"
    assert any(a["kind"] == "add_day" for a in j["result"]["alternatives"])
    assert j["feasibility_report"]["overall"] == "fail"  # failing report is returned, not hidden
    e = c.post("/optimise", json={"preferences": p.model_dump(), "hotel_id": ASA, "explain": True}).json()["explanation"]
    assert e["source"] == "template" and "No feasible plan" in e["text"]
    assert c.post("/optimise", json={**optimise_body(), "hotel_id": "hotel_nope"}).json()["error"]["code"] == "unknown_hotel"


def test_optimise_labels_time_limited_plans_as_approximate(clients, monkeypatch):
    monkeypatch.setattr(config, "SOLVER_MAX_DETERMINISTIC_TIME", 0.2)   # tight enough to stop before proving optimality, loose enough to find a first plan with the balance nudge on
    _, c = clients(optimise_time_limit_seconds=20.0)
    p = Preferences(days=4, pace="packed", start_weekday=2)
    j = c.post("/optimise", json={"preferences": p.model_dump(), "hotel_id": ASA}).json()
    assert j["result"]["status"] == "feasible" and j["result"]["solver_meta"]["solver_status"] == "FEASIBLE"
    assert j["approximate"] is True and "proving optimality" in j["approximate_note"]


def test_every_route_works_end_to_end_in_sample_mode_with_no_key(clients):
    _, c = clients(rate_standard_per_min=100, rate_expensive_per_min=50)
    p, cur = tiny_cur()
    calls = [
        ("get", "/health", None), ("get", "/data/summary", None),
        ("post", "/optimise", optimise_body()),
        ("post", "/compare-hotels", {"preferences": p.model_dump(), "hotel_ids": [ASA, ODA]}),
        ("post", "/replan", replan_body(p, cur.model_dump(), [{"op": "lock", "attraction_id": "senso_ji", "day": 1, "start_time": "14:00"}])),
        ("post", "/parse", {"text": "3 days in Tokyo"}),
        ("post", "/parse-replan", {"text": "drop it", "preferences": p.model_dump(), "current_result": cur.model_dump()}),
    ]
    for method, path, body in calls:
        r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 200, (path, r.text[:200])
    assert c.get("/health").json()["llm_mode"] == "sample"
    assert c.post("/parse", json={"text": "3 days"}).json()["status"] == "llm_unavailable"
