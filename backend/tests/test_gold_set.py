"""The gold set and its scorer, with a MOCK only. The ORACLE below answers each prompt from the gold file's own expectations; it proves the expectations
are reachable through the real validation pipeline (so a perfect model could score 100%), not that any model does."""
import importlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from app.data.loader import load_all_city_datasets
from app.llm.client import LLMResponse
from app.llm.sanitize import sanitise_user_text
from llm_mock import extraction

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))
sg = importlib.import_module("score_gold")
ALL = load_all_city_datasets()
GOLD = sg.load_gold()
UNSUPPORTED = {"tokyo_wrong_city_2": "Paris", "seoul_wrong_city_2": "Busan", "singapore_wrong_city_2": "Bali", "kuala_lumpur_wrong_city_2": "Penang"}
SYMBOL = re.compile(r"(S\$|RM|[¥￥₩$])")
KINDS = {"typical", "vague", "wrong_city", "mixed_language", "ambiguous", "adversarial"}


class Oracle:
    model = "oracle"

    def __init__(self):
        self.total_calls = 0
        self.by_text = {sanitise_user_text(p["text"], 4000).text: p for p in GOLD}  # the pipeline cleans the text first (full-width punctuation, fake tags)

    def complete(self, system, messages, max_tokens):
        self.total_calls += 1
        body = messages[0]["content"]
        p = next(p for t, p in self.by_text.items() if t in body)
        ds, e = ALL[p["city"]], p["expected"]
        names = lambda ids: [ds.attractions[i].name for i in ids]  # noqa: E731
        ex = extraction(days=e.get("days"), pace=e.get("pace"), must_visit_names=names(e.get("must_visit", [])) + e.get("ambiguous", []), skip_names=names(e.get("skip", [])))
        if e.get("budget") is not None:
            ex["budget"] = {"amount": e["budget"], "currency": ds.city.currency_code, "strict": False}
        if e.get("currency_confirmation"):
            m = re.search(r"(S\$|RM|[¥￥₩$])\s?([\d,]+)", p["text"])
            ex["budget"] = {"amount": float(m.group(2).replace(",", "")), "currency": m.group(1), "strict": False}
        if p["id"] in UNSUPPORTED:
            ex["destination_mentioned"] = UNSUPPORTED[p["id"]]
        if e.get("draft") is False and p["kind"] == "vague":
            ex["days"] = None
        return LLMResponse(text=json.dumps(ex), input_tokens=1, output_tokens=1)


def test_structure_twelve_per_city_two_per_kind_ids_real_and_unique():
    assert len(GOLD) == 48 and len({p["id"] for p in GOLD}) == 48
    for city, ds in ALL.items():
        mine = [p for p in GOLD if p["city"] == city]
        assert len(mine) == 12 and Counter(p["kind"] for p in mine) == {k: 2 for k in KINDS}
        for p in mine:
            assert p["text"].strip() and p["expected"]
            for aid in p["expected"].get("must_visit", []) + p["expected"].get("skip", []):
                assert aid in ds.attractions, (p["id"], aid)
            assert set(p["expected"]) <= set(sg.FIELDS)
        assert any(re.search(r"[^\x00-\x7f]", p["text"]) for p in mine if p["kind"] == "mixed_language")  # at least one native-script prompt per city


def test_a_perfect_model_scores_100_percent_so_every_expectation_is_reachable():
    res = sg.score(Oracle(), ALL)
    for city in sg.CITIES:
        assert res[city]["exact"] == 12, (city, res[city]["failed"])


def test_scoring_can_fail_and_the_baseline_is_not_perfect():
    res = sg.score(sg.RuleBasedBaseline(ALL), ALL)
    assert any(res[c]["exact"] < 12 for c in sg.CITIES)
    seen = sg.observe(sg.extract_preferences("2 days in Tokyo", ALL["tokyo"], _days_only(), all_datasets=ALL), ALL["tokyo"])
    assert sg.compare({"days": 3, "pace": "relaxed"}, seen) == {"days": False, "pace": False}


def _days_only():
    class C:
        model = "x"

        def complete(self, s, m, t):
            return LLMResponse(text=json.dumps(extraction(days=2)))
    return C()


def test_results_are_per_city_and_never_pooled():
    res = sg.score(sg.RuleBasedBaseline(ALL), ALL)
    text = sg.render(res, ALL, label="mock", model="rule-based baseline v1", calls=None)
    assert set(res) == set(sg.CITIES) and "pooled" in text and "All" not in text.split("\n\n")[0]
    assert text.count("| 12 |") == 4 and "NOT a measurement of any language model" in text


def _cli(*args, env=None):
    base = {k: v for k, v in os.environ.items() if k not in ("RUN_LIVE_LLM_EVAL", "API_KEY", "OPENROUTER_API_KEY", "MODEL_NAME", "LLM_MODEL")}
    return subprocess.run([sys.executable, str(BACKEND / "scripts" / "score_gold.py"), *args], capture_output=True, text=True, cwd=BACKEND,
                          env={**base, "LIVE_ENV_FILE": "/nonexistent", **(env or {})}, timeout=300)


def test_default_run_uses_zero_model_calls_and_writes_the_mock_file_only(tmp_path):
    r = _cli(env={"GOLD_OUT_DIR": str(tmp_path)})
    assert r.returncode == 0 and [f.name for f in tmp_path.iterdir()] == ["gold-results-mock.md"]


def test_live_flag_without_opt_in_or_key_does_nothing(tmp_path):
    r = _cli("--live", env={"GOLD_OUT_DIR": str(tmp_path)})
    assert "opt-in" in r.stdout and list(tmp_path.iterdir()) == []
    r = _cli("--live", env={"GOLD_OUT_DIR": str(tmp_path), "RUN_LIVE_LLM_EVAL": "1"})
    assert "API_KEY and MODEL_NAME are not set" in r.stdout and list(tmp_path.iterdir()) == []
