"""scripts/score_holdout.py: a template only (no prompts written by the builder), refusal rules, per-city split into code-handled and model-called prompts, new timestamped files.
Test rows below are TEST FIXTURES for the harness (generic one-line requests), not the holdout set; the real holdout is written by someone else."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND / "scripts"))
spec = importlib.util.spec_from_file_location("score_holdout", ROOT / "scripts" / "score_holdout.py")
sh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sh)
from app.data.loader import load_all_city_datasets  # noqa: E402

ALL = load_all_city_datasets()
GOLD = [p["text"] for p in sh.sg.load_gold()]


def row(i, city, text, expected=None, **o):
    r = {"id": i, "city": city, "text": text, "expected": expected or {"days": 2, "pace": None, "budget": None, "must_visit_names": [], "skip_names": [], "ambiguous": [], "wrong_city": None},
         "author_role": "a friend, not the builder", "written_on": "2026-10-01"}
    r.update(o)
    return r


def write(tmp_path, rows):
    p = tmp_path / "h.json"
    p.write_text(json.dumps({"about": "t", "prompts": rows}))
    return p


def test_the_shipped_file_is_a_template_with_two_format_only_rows_and_no_real_prompts():
    d = json.loads((ROOT / "docs/eval/holdout-prompts.json").read_text())
    assert len(d["prompts"]) == 2 and all(p["author_role"] == "FORMAT ONLY" and "FORMAT ONLY" in p["text"] for p in d["prompts"])
    assert set(d["prompts"][0]) == {"id", "city", "text", "expected", "author_role", "written_on"}


def test_it_refuses_to_run_while_a_format_only_row_remains_in_both_modes(monkeypatch, capsys):
    monkeypatch.delenv("HOLDOUT_PROMPTS", raising=False)
    assert sh.main([]) == 1 and "FORMAT ONLY" in capsys.readouterr().out
    assert sh.main(["--live", "--label", "x"]) == 1


@pytest.mark.parametrize("bad,why", [
    (dict(author_role=""), "author_role"), (dict(written_on="2999-01-01"), "written_on"), (dict(written_on="yesterday"), "written_on"), (dict(text="  "), "text"),
    (dict(city="paris"), "city"), (dict(expected={"days": 9, "wrong_city": None}), "days"), (dict(expected={"days": 2, "pace": "fast", "wrong_city": None}), "pace"),
    (dict(expected={"days": 2, "budget": {"amount": 100, "currency": "EUR"}, "wrong_city": None}), "currency"),
    (dict(expected={"days": 2, "must_visit_names": ["Moon Base"], "wrong_city": None}), "not a place"), (dict(expected={"days": 2, "wrong_city": "tokyo"}), "wrong_city")])
def test_invalid_rows_are_refused_with_a_reason(tmp_path, bad, why):
    r = row("h01", "tokyo", "two days please")
    r.update(bad)
    p = write(tmp_path, [r])
    with pytest.raises((sh.HoldoutError, KeyError)):
        sh.load_holdout(p, ALL, GOLD)


def test_a_copy_or_near_copy_of_a_gold_prompt_is_refused(tmp_path):
    g = GOLD[0]
    for text in (g, g.upper(), g + " thanks"):
        with pytest.raises(sh.HoldoutError, match="copy"):
            sh.load_holdout(write(tmp_path, [row("h01", "tokyo", text)]), ALL, GOLD)
    assert sh.load_holdout(write(tmp_path, [row("h01", "tokyo", "two days in town please")]), ALL, GOLD)


def test_expected_answers_are_converted_to_the_gold_scorers_fields(tmp_path):
    tokyo_place = next(iter(ALL["tokyo"].attractions.values()))
    rows = [row("h01", "tokyo", "t1", {"days": 3, "pace": "relaxed", "budget": {"amount": 30000, "currency": "JPY"}, "must_visit_names": [tokyo_place.name], "skip_names": [], "ambiguous": [], "wrong_city": None}),
            row("h02", "seoul", "t2", {"days": None, "pace": None, "budget": None, "must_visit_names": [], "skip_names": [], "ambiguous": [], "wrong_city": "tokyo"}),
            row("h03", "singapore", "t3", {"days": 2, "pace": None, "budget": {"amount": 100, "currency": "USD"}, "must_visit_names": [], "skip_names": [], "ambiguous": ["the bay"], "wrong_city": None}),
            row("h04", "kuala_lumpur", "t4", {"days": 2, "pace": None, "budget": None, "must_visit_names": [], "skip_names": [], "ambiguous": [], "wrong_city": "unsupported"})]
    out = {r["id"]: r["expected"] for r in sh.load_holdout(write(tmp_path, rows), ALL, GOLD)}
    assert out["h01"] == {"draft": True, "destination": "ok", "days": 3, "pace": "relaxed", "budget": 30000, "must_visit": [tokyo_place.id]}
    assert out["h02"] == {"draft": False, "destination": "other_city:tokyo"} and out["h04"] == {"draft": False, "destination": "unsupported"}
    sg_fx = ALL["singapore"].city.sample_fx_to_display_currency
    assert out["h03"]["budget"] == round(100 / sg_fx) and out["h03"]["ambiguous"] == ["the bay"]


def _fixture(tmp_path, monkeypatch):
    rows = [row("h01", "tokyo", "3 days in Tokyo please", {"days": 3, "pace": None, "budget": None, "must_visit_names": [], "skip_names": [], "ambiguous": [], "wrong_city": None}),
            row("h02", "seoul", "2 days in Tokyo please", {"days": None, "pace": None, "budget": None, "must_visit_names": [], "skip_names": [], "ambiguous": [], "wrong_city": "tokyo"}),
            row("h03", "singapore", "2 days in Singapore", {"days": 2, "pace": None, "budget": None, "must_visit_names": [], "skip_names": [], "ambiguous": [], "wrong_city": None})]
    monkeypatch.setenv("HOLDOUT_PROMPTS", str(write(tmp_path, rows)))
    monkeypatch.setenv("HOLDOUT_OUT_DIR", str(tmp_path))


def test_the_mock_baseline_writes_a_timestamped_file_labelled_not_a_model_with_zero_calls(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    assert sh.main([]) == 0
    f = next(tmp_path.glob("holdout-results-*-mock.md"))
    t = f.read_text()
    assert "MOCK rule-based baseline: not a model" in t and "NOT a measurement of any language model" in t and "Zero model calls" in t and "N = 3 prompts, written by: a friend, not the builder" in t
    assert "Exact: handled by code before the model" in t and "Exact: called the model" in t


def test_the_live_run_splits_code_handled_from_model_called_per_city_and_never_overwrites(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    from llm_mock import extraction  # noqa: F401  (tests dir on path via conftest rootdir)
    monkeypatch.setattr(sh.lc, "gate", lambda *a: None)
    monkeypatch.setattr(sh.lc, "load_env_file", lambda: None)
    monkeypatch.setattr(sh.lc, "endpoint_stamp", lambda c: "Endpoint host: `relay.example.test`.")

    class Fake:
        model = "fake-model"

        def complete(self, system, messages, max_tokens):
            from app.llm.client import LLMResponse
            body = {"days": 3 if "3 days" in messages[0]["content"] else 2, "budget": None, "pace": None, "interests": [], "must_visit_names": [], "skip_names": [], "day_start": None,
                    "day_end": None, "value_of_time": None, "max_walking_minutes": None, "unclear": []}
            return LLMResponse(text=json.dumps(body), input_tokens=1, output_tokens=1, latency_seconds=0.0)
    monkeypatch.setattr("app.llm.client.OpenRouterLLMClient.from_env", staticmethod(lambda: Fake()))
    assert sh.main(["--live", "--label", "holdout1"]) == 0
    t = next(tmp_path.glob("holdout-results-*-holdout1.md")).read_text()
    for needle in ("(LIVE model)", "Model: `fake-model`", "relay.example.test", "PROMPT_VERSION", "Calls used: 2 of a hard cap of", "N = 3 prompts"):
        assert needle in t, needle
    row_seoul = [l for l in t.splitlines() if l.startswith("| Seoul")][0]
    assert "1/1 (100%)" in row_seoul and "n/a" in row_seoul          # settled by code: one prompt, no model call
    row_tokyo = [l for l in t.splitlines() if l.startswith("| Tokyo")][0]
    assert row_tokyo.split("|")[4].strip() == "n/a" and "1/1" in row_tokyo.split("|")[5]
    with pytest.raises(FileExistsError):                              # a second run in the same second can never overwrite the first file
        class Frozen(sh.datetime):
            @classmethod
            def now(cls, tz=None):
                return sh.datetime.strptime(next(tmp_path.glob("holdout-results-*-holdout1.md")).name.split("-")[2], "%Y%m%dT%H%M%SZ").replace(tzinfo=sh.timezone.utc)
        monkeypatch.setattr(sh, "datetime", Frozen)
        sh.main(["--live", "--label", "holdout1"])


def test_the_cap_is_checked_before_any_call(tmp_path, monkeypatch, capsys):
    _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(sh.config, "GOLD_MAX_CALLS", 4)
    monkeypatch.setattr(sh.lc, "load_env_file", lambda: None)
    assert sh.main(["--live", "--label", "x"]) == 0 and "above the cap" in capsys.readouterr().out and list(tmp_path.glob("holdout-results-*")) == []
