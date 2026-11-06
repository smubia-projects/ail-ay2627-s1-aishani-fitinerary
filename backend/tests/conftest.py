"""Writes pytest evidence with enough metadata to distinguish full and partial runs."""
import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

RESULTS = Path(__file__).parent / "results" / "latest.md"
FULL_RESULTS = Path(__file__).parent / "results" / "latest-full.md"
_rows: list[tuple[str, str, str]] = []
_deselected = 0


def pytest_runtest_logreport(report):
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        status = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[report.outcome]
        _rows.append((datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), status, report.nodeid))


def pytest_deselected(items):
    global _deselected
    _deselected += len(items)


def _git_revision() -> str:
    root = Path(__file__).resolve().parents[2]
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True).stdout.strip()
        dirty = subprocess.run(["git", "diff", "--quiet"], cwd=root).returncode != 0
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode != 0
        return rev + (" + uncommitted changes" if dirty or staged else "")
    except Exception:
        return "unknown"


def _is_full_suite(session) -> tuple[bool, list[str]]:
    opt = session.config.option
    reasons = []
    args = [str(a) for a in getattr(opt, "file_or_dir", [])]
    if args and args != ["backend/tests"] and args != ["tests"]:
        reasons.append("pytest path/test selection was used")
    if getattr(opt, "keyword", ""):
        reasons.append("-k selection was used")
    if getattr(opt, "markexpr", ""):
        reasons.append("-m selection was used")
    if _deselected:
        reasons.append(f"{_deselected} tests were deselected")
    return not reasons, reasons


def pytest_sessionfinish(session, exitstatus):
    passed = sum(r[1] == "PASS" for r in _rows)
    failed = sum(r[1] == "FAIL" for r in _rows)
    skipped = sum(r[1] == "SKIP" for r in _rows)
    full, reasons = _is_full_suite(session)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Latest test results",
        "",
        f"Generated {generated} by pytest (auto-written by tests/conftest.py).",
        f"Run type: {'full backend suite' if full else 'partial backend run'}.",
        f"Git revision: {_git_revision()}.",
        f"Pytest args: {' '.join(session.config.invocation_params.args) or '(none)'}",
        f"Collected: {session.testscollected}; deselected: {_deselected}; executed rows: {len(_rows)}.",
        f"Passed: {passed}, failed: {failed}, skipped: {skipped}, total: {len(_rows)}.",
    ]
    if reasons:
        lines.append("Partial-run reason: " + "; ".join(reasons) + ".")
    lines += [
        "",
        "| Time | Result | Test |",
        "|---|---|---|",
    ]
    lines += [f"| {t} | {s} | `{n}` |" for t, s, n in _rows]
    RESULTS.parent.mkdir(exist_ok=True)
    text = "\n".join(lines) + "\n"
    RESULTS.write_text(text)
    if full:
        FULL_RESULTS.write_text(text.replace("# Latest test results", "# Latest verified full backend suite", 1))
        # machine-readable counts for the evidence checks (scripts/check_doc_consistency.py, scripts/evidence_status.py)
        (FULL_RESULTS.parent / "full-suite.json").write_text(json.dumps({"generated": generated, "git_revision": _git_revision(), "collected": session.testscollected,
                                                                         "total": len(_rows), "passed": passed, "failed": failed, "skipped": skipped}, indent=1) + "\n")


@pytest.fixture(autouse=True)
def _no_network_no_keys(monkeypatch, request):
    """Tests must never reach the network or depend on an API key: block outbound sockets, clear LLM env vars.
    A test marked `loopback` may connect to 127.0.0.1 only (it talks to a server it started itself); everything else stays blocked."""
    real_connect, real_connect_ex, real_create = socket.socket.connect, socket.socket.connect_ex, socket.create_connection
    loopback_ok = request.node.get_closest_marker("loopback") is not None

    def _is_loopback(address):
        return isinstance(address, tuple) and address and address[0] in ("127.0.0.1", "localhost", "::1")

    def blocked(*a, **k):
        raise RuntimeError("network access is blocked in tests")

    def connect(self, address, *a, **k):
        if loopback_ok and _is_loopback(address):
            return real_connect(self, address, *a, **k)
        blocked()

    def connect_ex(self, address, *a, **k):
        if loopback_ok and _is_loopback(address):
            return real_connect_ex(self, address, *a, **k)
        blocked()

    def create_connection(address, *a, **k):
        if loopback_ok and _is_loopback(address):
            return real_create(address, *a, **k)
        blocked()

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    for var in ("API_KEY", "OPENROUTER_API_KEY", "MODEL_NAME", "LLM_MODEL", "API_BASE_URL", "OPENROUTER_BASE_URL", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "UPSTASH_REDIS_URL", "UPSTASH_REDIS_TOKEN"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_shared_app_state():
    """The module-level app keeps in-memory rate-limit and LLM-budget state; reset it so tests are independent."""
    from app.main import app
    app.state.limiter.reset()
    app.state.llm_budget.reset()
    yield
    app.dependency_overrides.clear()
