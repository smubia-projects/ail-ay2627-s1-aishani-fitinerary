"""The live evaluation is opt-in and never runs here. These tests check its inputs, its gate, its call budget, and (with a MOCK client,
output redirected to a temp folder) that its mechanics work. They do not measure any model."""
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from app.data.loader import load_all_city_datasets
from app.llm.client import LLMResponse
from llm_mock import extraction

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "scripts" / "run_llm_live_eval.py"
FIX = BACKEND / "tests" / "fixtures"
ALL = load_all_city_datasets()
PER_CITY = json.loads((FIX / "live_eval_prompts_by_city.json").read_text())
KINDS = {"typical", "vague", "wrong_city", "mixed_language", "adversarial", "ambiguous"}


def test_the_default_cap_is_150():
    assert config.LIVE_EVAL_MAX_CALLS == 150


def test_each_city_has_six_prompts_covering_every_kind_and_the_expectations_are_real():
    ids = [p["id"] for p in PER_CITY]
    assert len(ids) == len(set(ids)) == 24
    for city in ALL:
        mine = [p for p in PER_CITY if p["city"] == city]
        assert {p["kind"] for p in mine} == KINDS and len(mine) == 6
        for p in mine:
            for aid in p.get("expect_resolved", []):
                assert aid in ALL[city].attractions, (p["id"], aid)
            if p.get("expect_wrong_city") not in (None, "unsupported"):
                assert p["expect_wrong_city"] in ALL and p["expect_wrong_city"] != city
            assert p["text"] and len(p["text"]) < 400
    assert sum(1 for p in PER_CITY if p["kind"] == "adversarial") == 4 and sum(1 for p in PER_CITY if p["kind"] == "wrong_city") == 4


def _run(*args, env=None):
    base = {k: v for k, v in os.environ.items() if k not in ("RUN_LIVE_LLM_EVAL", "OPENROUTER_API_KEY", "LLM_MODEL")}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=BACKEND, env={**base, **(env or {})}, timeout=120)


def test_plan_mode_makes_no_calls_and_fits_the_cap():
    r = _run("--plan")
    assert r.returncode == 0 and "Zero calls were made" in r.stdout and "hard cap 150" in r.stdout
    worst = int(r.stdout.split("= ")[1].split(";")[0])
    assert worst <= config.LIVE_EVAL_MAX_CALLS


def test_without_the_opt_in_nothing_runs_and_nothing_is_written(tmp_path):
    r = _run(env={"LIVE_EVAL_OUT_DIR": str(tmp_path)})
    assert r.returncode == 0 and "Nothing was run" in r.stdout and list(tmp_path.iterdir()) == []
    r2 = _run(env={"LIVE_EVAL_OUT_DIR": str(tmp_path), "RUN_LIVE_LLM_EVAL": "1"})  # opt-in flag alone is not enough: a key and a model are required
    assert "Nothing was run" in r2.stdout and list(tmp_path.iterdir()) == []


class FakeClient:
    """Stands in for the real client. Everything this produces is mock output and is only used to exercise the script."""
    model = "mock-model"

    def __init__(self):
        self.n = 0

    @classmethod
    def from_env(cls):
        return cls()

    def complete(self, system, messages, max_tokens):
        self.n += 1
        text = "The comparison is complete." if "<facts>" in messages[0]["content"] else json.dumps(extraction(days=3))
        return LLMResponse(text=text, input_tokens=100, output_tokens=20, latency_seconds=0.01)


def test_the_script_runs_end_to_end_with_a_mock_client_and_reports_every_city(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_LIVE_LLM_EVAL", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
    monkeypatch.setenv("LLM_MODEL", "mock-model")
    monkeypatch.setenv("LIVE_EVAL_OUT_DIR", str(tmp_path))
    monkeypatch.setattr("app.llm.client.OpenRouterLLMClient.from_env", FakeClient.from_env)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    runpy.run_path(str(SCRIPT), run_name="__main__")
    md = (tmp_path / "llm-live-eval.md").read_text()
    data = json.loads((tmp_path / "llm-live-eval.json").read_text())
    for name in ("Tokyo", "Seoul", "Singapore", "Kuala Lumpur", "All"):
        assert name in md
    assert data["calls_used"] <= config.LIVE_EVAL_MAX_CALLS and data["model"] == "mock-model"
    # the mechanics: explicit wrong-city prompts are stopped in code (0 calls); the others reach the (mock) model
    assert set(data["per_city"]) == {"tokyo", "seoul", "singapore", "kuala_lumpur"}
    for c, d in data["per_city"].items():
        assert d["Wrong-city / unsupported-city detected, no draft planned"].startswith(("1/1", "0/1"))
    assert "not-a-real-key" not in md
