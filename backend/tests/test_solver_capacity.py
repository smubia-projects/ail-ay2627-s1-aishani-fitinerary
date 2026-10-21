"""Final hardening: solver capacity is explicit and non-blocking."""
import threading
import time

from fastapi.testclient import TestClient

from app.data.loader import load_dataset
from app.hardening.config import HConfig
from app.main import create_app
from app.optimiser.solver import solve as real_solve
from app.schemas import Preferences

DS = load_dataset()
ASA = "hotel_lantern_asakusa"


def tiny():
    return Preferences(days=1, must_visit=["senso_ji", "tokyo_skytree"], start_weekday=2)


def test_solver_busy_returns_structured_423_and_health_stays_responsive(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_solve(*args, **kwargs):
        entered.set()
        release.wait(timeout=5)
        return real_solve(*args, **kwargs)

    import app.main as main_mod

    monkeypatch.setattr(main_mod, "solve", slow_solve)
    app = create_app(HConfig(solver_concurrency=1, solver_busy_retry_after_seconds=3, rate_standard_per_min=100))
    with TestClient(app) as client:
        body = {"preferences": tiny().model_dump(), "hotel_id": ASA}
        first = {}

        def run_first():
            first["response"] = client.post("/optimise", json=body)

        thread = threading.Thread(target=run_first)
        thread.start()
        assert entered.wait(timeout=3)

        busy = client.post("/optimise", json=body)
        assert busy.status_code == 423
        assert busy.headers["retry-after"] == "3"
        err = busy.json()["error"]
        assert err["code"] == "solver_busy"
        assert err["details"] == {"retry_after_seconds": 3, "active_solves": 1, "capacity": 1}

        health = client.get("/health")
        assert health.status_code == 200 and health.json()["status"] == "ok"

        release.set()
        thread.join(timeout=5)
        assert first["response"].status_code == 200


def test_solver_capacity_releases_after_exception(monkeypatch):
    import app.main as main_mod

    calls = {"n": 0}

    def flaky_solve(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(main_mod, "solve", flaky_solve)
    app = create_app(HConfig(solver_concurrency=1, rate_standard_per_min=100))
    with TestClient(app, raise_server_exceptions=False) as client:
        body = {"preferences": tiny().model_dump(), "hotel_id": ASA}
        assert client.post("/optimise", json=body).status_code == 500
        assert client.post("/optimise", json=body).status_code == 200


def test_time_limit_env_values_are_loaded():
    cfg = HConfig.from_env({
        "OPTIMISE_TIME_LIMIT_SECONDS": "4",
        "HOTEL_COMPARE_TIME_LIMIT_SECONDS": "6",
        "REPLAN_TIME_LIMIT_SECONDS": "2",
        "ALTERNATIVES_BUDGET_SECONDS": "1",
        "SOLVER_CONCURRENCY": "2",
        "SOLVER_BUSY_RETRY_AFTER_SECONDS": "9",
    })
    assert cfg.optimise_time_limit_seconds == 4
    assert cfg.hotel_compare_time_limit_seconds == 6
    assert cfg.replan_time_limit_seconds == 2
    assert cfg.alternatives_budget_seconds == 1
    assert cfg.solver_concurrency == 2
    assert cfg.solver_busy_retry_after_seconds == 9
