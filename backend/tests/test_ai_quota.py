"""The shared AI Lodge rate-limit engine wired into this API (app/rate_limit.py): which routes count, the 429/503 response
contract the frontend routes on, including the opt-in explanation bucket. The engine's Redis calls are replaced by a fake limiter."""
import pytest
from fastapi.testclient import TestClient

from app import rate_limit
from app.data.loader import load_dataset
from app.llm.client import get_llm_client
from app.main import app
from app.rate_limit_engine import RateLimitStatus
from llm_mock import MockLLM, extraction

DS = load_dataset()
ASA = "hotel_lantern_asakusa"
ONLY = ["senso_ji", "tokyo_skytree"]


class FakeLimiter:
    """Stands in for the engine's RateLimiter: records each (bucket) hit and answers with a fixed state."""

    def __init__(self, state="ok", max_=5):
        self.state, self.max, self.hits = state, max_, []

    def status(self, ip, bucket):
        self.hits.append(bucket)
        return RateLimitStatus(self.state, self.max if self.state == "limited" else None)

    def enforce(self, request, bucket):
        st, val = self.status("x", bucket)
        if st == "ok":
            return
        from fastapi import HTTPException  # the same exceptions the real engine raises
        if st == "killed":
            raise HTTPException(503, detail={"message": "This demo is temporarily paused. Check back soon."})
        raise HTTPException(429, detail={"message": "Rate limit exceeded.", "queries_used": val})


@pytest.fixture
def setup(monkeypatch):
    def make(state="ok", mock=None):
        fake = FakeLimiter(state)
        monkeypatch.setattr(rate_limit, "limiter", fake)
        app.dependency_overrides[get_llm_client] = lambda: mock
        return fake
    yield make
    app.dependency_overrides.pop(get_llm_client, None)


def prefs():
    return {"days": 1, "must_visit": ONLY, "skip": [a for a in DS.attractions if a not in ONLY], "start_weekday": 2}


def test_parse_over_the_limit_returns_the_cta_contract(setup):
    fake = setup("limited", MockLLM([extraction(days=2)]))
    with TestClient(app) as c:
        r = c.post("/parse", json={"text": "two days in Tokyo"})
    assert r.status_code == 429 and fake.hits == ["parse"]
    j = r.json()
    assert j["detail"]["queries_used"] == 5                      # the AI Lodge CTA reads detail.queries_used
    assert j["error"]["code"] == "ai_quota_reached" and j["error"]["details"]["queries_used"] == 5


def test_kill_switch_returns_503_demo_paused(setup):
    setup("killed", MockLLM([extraction(days=2)]))
    with TestClient(app) as c:
        r = c.post("/parse", json={"text": "two days in Tokyo"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "demo_paused" and "paused" in r.json()["detail"]["message"]


def test_sample_mode_never_counts_against_the_quota(setup):
    fake = setup("limited", None)  # no model configured: nothing is called, so nothing is counted
    with TestClient(app) as c:
        assert c.post("/parse", json={"text": "two days in Tokyo"}).status_code == 200
    assert fake.hits == []


def test_explanation_over_the_limit_returns_429_like_every_other_ai_route(setup):
    mock = MockLLM(["This should never be used."])
    fake = setup("limited", mock)
    with TestClient(app) as c:
        r = c.post("/optimise", json={"preferences": prefs(), "hotel_id": ASA, "explain": True})
    assert r.status_code == 429 and r.json()["error"]["code"] == "ai_quota_reached" and r.json()["detail"]["queries_used"] == 5
    assert fake.hits == ["explain"] and mock.calls == []


def test_explanation_with_the_demo_paused_returns_503(setup):
    setup("killed", MockLLM(["This should never be used."]))
    with TestClient(app) as c:
        r = c.post("/optimise", json={"preferences": prefs(), "hotel_id": ASA, "explain": True})
    assert r.status_code == 503 and r.json()["error"]["code"] == "demo_paused"


def test_explanation_under_the_limit_may_use_the_model_and_solver_routes_are_not_counted_without_explain(setup):
    fake = setup("ok", MockLLM(["Plan explanation."] * 3))
    with TestClient(app) as c:
        assert c.post("/optimise", json={"preferences": prefs(), "hotel_id": ASA}).status_code == 200
        assert fake.hits == []
        assert c.post("/optimise", json={"preferences": prefs(), "hotel_id": ASA, "explain": True}).status_code == 200
    assert fake.hits == ["explain"]


def test_api_health_alias_and_solver_busy_is_not_503():
    with TestClient(app) as c:
        assert c.get("/api/health").json()["status"] == "ok"
    from app.hardening.solver_capacity import SOLVER_BUSY_STATUS
    assert SOLVER_BUSY_STATUS != 503
