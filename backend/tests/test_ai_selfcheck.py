"""scripts/ai_selfcheck.py with a MOCK client only. These tests check the gate, the call cap, the abort-on-bad-key behaviour, the pass/fail logic
and the report. They do not measure any model."""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from app.data.loader import load_all_city_datasets
from app.llm.client import LLMResponse, LLMUnavailable
from llm_mock import extraction

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))
sc = importlib.import_module("ai_selfcheck")
lc = importlib.import_module("live_common")
ALL = load_all_city_datasets()
PROMPTS = sc._prompts()
KEY = "sk-test-NOT-A-REAL-KEY-123456"


class RoutedMock:
    """Answers each selfcheck request by looking at the user text. `mode` bends the behaviour for failure tests."""
    model = "mock-model"

    def __init__(self, mode="good"):
        self.mode, self.calls = mode, 0

    def _extraction(self, text):
        for city, ds in ALL.items():
            t = PROMPTS[city]
            if t["typical"]["text"] in text:
                names = [ds.attractions[i].name for i in t["typical"]["expect_resolved"]]
                return extraction(days=3, must_visit_names=[] if self.mode == "typical_empty" else names)
            if t["ambiguous"]["text"] in text:
                word = t["ambiguous"]["expect_ambiguous"][0]
                if self.mode == "ambiguous_as_interest":   # what the real model did: "the bay" came back as interests ["bay"], "the temple" as must "temple"
                    return extraction(days=2, interests=[word.replace("the ", "")])
                return extraction(days=2, must_visit_names=[word])
            if t["wrong_city"]["text"] in text:
                return extraction(days=3, destination_mentioned="Bangkok")
        sym = {"tokyo": ("¥", 25000), "seoul": ("₩", 250000), "singapore": ("$", 200), "kuala_lumpur": ("RM", 300)}
        for city, (case, _) in sc.CURRENCY_CASES.items():
            if case in text:
                return extraction(days=2, budget={"amount": sym[city][1], "currency": sym[city][0], "strict": False})
        raise AssertionError("unrecognised request")

    def complete(self, system, messages, max_tokens):
        self.calls += 1
        if self.mode == "auth":
            raise LLMUnavailable("authentication")
        body = messages[0]["content"]
        if "Kind:" in body:  # explanation request
            return LLMResponse(text="This compares the hotels for your trip." if self.mode != "bad_explain" else "It costs 999,999,999 in total.", input_tokens=5, output_tokens=5)
        return LLMResponse(text=json.dumps(self._extraction(body)), input_tokens=5, output_tokens=5)


def _run(mode="good", cap=config.SELFCHECK_MAX_CALLS):
    client = lc.Capped(RoutedMock(mode), cap)
    return client, sc.run_all(client, ALL, report=lambda *_: None)


def test_a_good_model_passes_every_check_within_the_cap():
    client, res = _run()
    assert len(res) == 20 and all(c.passed for c in res), [(c.city, c.name, c.reason) for c in res if not c.passed]
    assert client.total_calls <= config.SELFCHECK_MAX_CALLS


def test_a_real_models_rewording_of_the_ambiguous_phrase_still_passes_when_the_code_flags_it():
    """Observed live: the model returned interests ["bay"] / ["temples"] instead of must_visit_names ["the bay"]; the pipeline asks the user, so the check passes."""
    _, res = _run("ambiguous_as_interest")
    amb = [c for c in res if c.name == "ambiguous_name"]
    # cities whose phrase is an ambiguous place word as a plain word (Tokyo "museum", Singapore "bay", Seoul "palace", KL "temple")
    assert len(amb) == 4 and all(c.passed for c in amb), [(c.city, c.reason) for c in amb if not c.passed]


def test_a_model_that_resolves_nothing_fails_the_typical_check_with_a_reason():
    _, res = _run("typical_empty")
    bad = [c for c in res if c.name == "parse_typical"]
    assert bad and not any(c.passed for c in bad) and all("not resolved" in c.reason for c in bad)


def test_an_ungrounded_explanation_is_a_fail_with_the_safe_fallback_named():
    _, res = _run("bad_explain")
    ex = [c for c in res if c.name == "explain_real_result"]
    assert len(ex) == 4 and not any(c.passed for c in ex) and all("template used" in c.reason for c in ex)


def test_a_rejected_key_aborts_after_the_first_call_with_actionable_advice():
    client = lc.Capped(RoutedMock("auth"), 40)
    with pytest.raises(lc.RunAborted) as e:
        sc.run_all(client, ALL, report=lambda *_: None)
    assert client.total_calls == 1 and "API_KEY" in str(e.value)


def test_a_bad_model_name_message_names_the_model_variable_not_the_key():
    msg = str(lc.RunAborted("api_error", "not-a-model"))
    assert "MODEL_NAME" in msg and "not-a-model" in msg


def test_the_hard_cap_stops_the_run():
    client = lc.Capped(RoutedMock(), 3)
    with pytest.raises(lc.RunAborted) as e:
        sc.run_all(client, ALL, report=lambda *_: None)
    assert e.value.category == "call_cap" and client.total_calls == 3


def test_report_has_model_prompt_version_git_hash_and_n_and_no_key(tmp_path):
    client, res = _run()
    text = sc.write_report(res, client, ALL, tmp_path).read_text()
    for needle in ("mock-model", f"PROMPT_VERSION: `{sc.PROMPT_VERSION}`", "Git: `", "N = 20", "hard cap of 40", "20 of 20"):
        assert needle in text
    assert KEY not in text and "sk-" not in text
    assert all(PROMPTS[c][k]["text"] not in text for c in PROMPTS for k in PROMPTS[c])  # prompts are not stored in the report


def _cli(*args, env=None):
    base = {k: v for k, v in os.environ.items() if k not in ("RUN_LIVE_LLM_EVAL", "API_KEY", "OPENROUTER_API_KEY", "MODEL_NAME", "LLM_MODEL")}
    return subprocess.run([sys.executable, str(BACKEND / "scripts" / "ai_selfcheck.py"), *args], capture_output=True, text=True, cwd=BACKEND,
                          env={**base, "LIVE_ENV_FILE": "/nonexistent", **(env or {})}, timeout=120)


def test_cli_without_opt_in_makes_no_calls_and_writes_nothing(tmp_path):
    r = _cli(env={"SELFCHECK_OUT_DIR": str(tmp_path)})
    assert r.returncode == 0 and "opt-in" in r.stdout and list(tmp_path.iterdir()) == []


def test_cli_opt_in_without_key_or_model_says_exactly_what_is_missing(tmp_path):
    r = _cli(env={"RUN_LIVE_LLM_EVAL": "1", "SELFCHECK_OUT_DIR": str(tmp_path)})
    assert "API_KEY and MODEL_NAME are not set" in r.stdout and list(tmp_path.iterdir()) == []
    r = _cli(env={"RUN_LIVE_LLM_EVAL": "1", "OPENROUTER_API_KEY": KEY, "SELFCHECK_OUT_DIR": str(tmp_path)})
    assert "MODEL_NAME is not set" in r.stdout and KEY not in r.stdout + r.stderr


def test_cli_plan_is_zero_calls_and_fits_the_cap():
    r = _cli("--plan")
    assert "Zero calls were made" in r.stdout and "hard cap 40" in r.stdout


def test_a_model_that_could_not_be_reached_is_inconclusive_not_a_pass_and_not_a_model_failure(tmp_path):
    class Flaky(RoutedMock):
        def complete(self, system, messages, max_tokens):
            if self.calls == 0:
                self.calls += 1
                raise LLMUnavailable("timeout")
            return super().complete(system, messages, max_tokens)
    client = lc.Capped(Flaky(), config.SELFCHECK_MAX_CALLS)
    res = sc.run_all(client, ALL, report=lambda *_: None)
    first = res[0]
    assert first.inconclusive and not first.passed and "INCONCLUSIVE" in first.reason
    assert sum(c.passed for c in res) == 19 and not any(c.inconclusive for c in res[1:])
    text = sc.write_report(res, client, ALL, tmp_path).read_text()
    assert "1 inconclusive" in text and "INCONCLUSIVE" in text
