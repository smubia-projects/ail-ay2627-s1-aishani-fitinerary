"""A client that hangs up must not keep the single solver slot. These tests use a REAL uvicorn server and a REAL CP-SAT solve (no mocks of the solver):
the client times out mid-solve, which closes the connection, and the slot must be free again within moments, long before the solve's time limit."""
import socket
import threading
import time

import httpx
import pytest
import uvicorn

pytestmark = pytest.mark.loopback  # talks only to a server this file starts on 127.0.0.1

from app.hardening.config import HConfig
from app.main import create_app
from app.schemas import Preferences

LIMIT = 30  # the comparison's own time limit; the slot must free far sooner than this after a disconnect
BODY = {"city_id": "tokyo", "hotel_ids": ["hotel_kitsune_shinjuku", "hotel_lantern_asakusa", "hotel_harbour_odaiba"], "include_sensitivity": True,
        "preferences": Preferences(days=4, pace="packed", value_of_time_per_hour=1000).model_dump()}


@pytest.fixture
def live():
    app = create_app(HConfig(solver_concurrency=1, solver_busy_retry_after_seconds=3, rate_standard_per_min=1000, rate_cheap_per_min=1000,
                             hotel_compare_time_limit_seconds=LIMIT, request_deadline_seconds=60))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=10)


def _wait_for(cond, seconds):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        if cond():
            return time.monotonic() - t0
        time.sleep(0.05)
    return None


def test_a_disconnected_comparison_releases_the_slot_within_moments_not_at_its_time_limit(live):
    app, base = live
    cap = app.state.solver_capacity
    with pytest.raises(httpx.ReadTimeout):
        httpx.post(base + "/compare-hotels", json=BODY, timeout=1.5)  # the client gives up mid-solve and closes the connection
    # The solve was genuinely still running when the client left (otherwise this test proves nothing).
    assert cap.active == 1, "the comparison finished before the client left: make the request heavier"
    freed_after = _wait_for(lambda: cap.active == 0, 5)
    assert freed_after is not None, "the slot was still held 5 s after the client disconnected"
    assert freed_after < LIMIT / 3
    # and the slot is really usable again, immediately
    ok = httpx.post(base + "/optimise", json={"city_id": "tokyo", "hotel_id": "hotel_lantern_asakusa", "preferences": Preferences(days=1, must_visit=["senso_ji"]).model_dump()}, timeout=30)
    assert ok.status_code == 200


def test_a_request_that_stays_connected_still_completes_normally(live):
    app, base = live
    body = {**BODY, "include_sensitivity": False}
    r = httpx.post(base + "/compare-hotels", json=body, timeout=60)
    assert r.status_code == 200 and r.json()["best_by_true_cost"]
    assert app.state.solver_capacity.active == 0


def test_busy_is_still_reported_while_another_request_is_connected(live):
    app, base = live
    results = {}
    t = threading.Thread(target=lambda: results.update(first=httpx.post(base + "/compare-hotels", json=BODY, timeout=90)))
    t.start()
    assert _wait_for(lambda: app.state.solver_capacity.active == 1, 5) is not None
    busy = httpx.post(base + "/optimise", json={"city_id": "tokyo", "hotel_id": "hotel_lantern_asakusa", "preferences": Preferences(days=1).model_dump()}, timeout=10)
    assert busy.status_code == 423 and busy.json()["error"]["code"] == "solver_busy"
    t.join(timeout=90)
    assert results["first"].status_code == 200
