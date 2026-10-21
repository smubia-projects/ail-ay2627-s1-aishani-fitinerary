"""Addendum B: the live gold-set run covers all 48 prompts within its cap, stamps the run, writes a NEW timestamped file every time and never overwrites an
earlier one. Mock model only (the Oracle from test_gold_set); no network."""
import importlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import config

sys.path.insert(0, str(Path(__file__).resolve().parent))
tgs = importlib.import_module("test_gold_set")
sg = tgs.sg


def test_the_call_cap_covers_all_48_prompts_with_one_retry_each():
    assert len(sg.load_gold()) == 48
    assert sg.worst_case_calls(sg.load_gold()) == 96 <= config.GOLD_MAX_CALLS


def test_labels_are_made_safe_and_paths_are_timestamped():
    assert sg.label_arg(["--live", "--label", "First Run!"]) == "first-run"
    assert sg.label_arg(["--live", "--label", "../../etc/x"]) == "etc-x"
    assert sg.label_arg(["--live"]) == "run"
    t = datetime(2026, 10, 17, 14, 5, 9, tzinfo=timezone.utc)
    assert sg.live_path(Path("d"), "first-run", t).name == "gold-results-live-20261017T140509Z-first-run.md"


def _run_live(monkeypatch, tmp_path, label):
    monkeypatch.setenv("GOLD_OUT_DIR", str(tmp_path))
    monkeypatch.setattr(sg.lc, "gate", lambda *a: None)
    monkeypatch.setattr(sg.lc, "load_env_file", lambda: None)
    monkeypatch.setattr(sg.lc, "endpoint_stamp", lambda c: "Endpoint: relay.example.test (served via a third-party relay).")
    monkeypatch.setattr("app.llm.client.OpenRouterLLMClient.from_env", staticmethod(lambda: tgs.Oracle()))
    monkeypatch.setattr(sys, "argv", ["score_gold.py", "--live", "--label", label])
    return sg.main()


def test_every_live_run_writes_a_new_file_and_the_first_run_is_never_overwritten(monkeypatch, tmp_path):
    assert _run_live(monkeypatch, tmp_path, "first-run") == 0
    first = next(tmp_path.glob("gold-results-live-*-first-run.md"))
    before = first.read_text()
    # a second run at the very same second must not reuse the name
    fixed = datetime.strptime(re.search(r"live-(\d{8}T\d{6}Z)", first.name).group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(sg, "datetime", Frozen)
    with pytest.raises(FileExistsError):
        _run_live(monkeypatch, tmp_path, "first-run")
    assert first.read_text() == before
    monkeypatch.setattr(sg, "datetime", datetime)
    assert _run_live(monkeypatch, tmp_path, "after-fix") == 0
    assert len(list(tmp_path.glob("gold-results-live-*.md"))) == 2 and first.read_text() == before


def test_the_report_stamps_the_run_and_lists_failure_categories_per_city(monkeypatch, tmp_path):
    _run_live(monkeypatch, tmp_path, "first-run")
    text = next(tmp_path.glob("gold-results-live-*.md")).read_text()
    for needle in ("Run label: `first-run`", "Model: `oracle`", "relay.example.test", "PROMPT_VERSION", "Date:", "Git:", "Calls used: ",
                   "Failure categories", "small sample, one model"):
        assert needle in text, needle
    assert text.count("| 12 |") == 4 and "(LIVE model)" in text      # four cities, 12 prompts each, never pooled
    assert "gold-results-live.md" not in text


def test_refuses_to_run_when_the_worst_case_exceeds_the_cap(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "GOLD_MAX_CALLS", 95)
    assert _run_live(monkeypatch, tmp_path, "x") == 0
    assert "Refusing to run" in capsys.readouterr().out and list(tmp_path.iterdir()) == []
