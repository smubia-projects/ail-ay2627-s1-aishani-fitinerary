#!/usr/bin/env python3
"""Score the INDEPENDENT holdout set (docs/eval/holdout-prompts.json, written by someone other than the builder) with the gold-set scorer.

  python scripts/score_holdout.py                              mock baseline (a rule-based stand-in, NOT a model): zero model calls
  RUN_LIVE_LLM_EVAL=1 python scripts/score_holdout.py --live --label holdout1     the real model, capped (config.GOLD_MAX_CALLS; worst case prompts x 2 calls)

Refuses to run while any "FORMAT ONLY" row remains, if a row's place names are not in its city's data, or if a prompt is a copy of a gold prompt. Reports per city (never pooled),
split into prompts that code handled before the model (no call) and prompts that called the model. Writes a NEW timestamped docs/eval/holdout-results-<UTC>-<label>.md stamped with
model, gateway host, PROMPT_VERSION, date and git hash. The key and the prompts' replies are never printed. This script writes no prompts and no expected answers.
Output folder override (tests): HOLDOUT_OUT_DIR; prompts file override: HOLDOUT_PROMPTS."""
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend" / "scripts"))
sys.path.insert(0, str(REPO / "backend"))
import live_common as lc  # noqa: E402
import score_gold as sg  # noqa: E402
from app import config  # noqa: E402
from app.llm.prompts import PROMPT_VERSION  # noqa: E402

PROMPTS = REPO / "docs" / "eval" / "holdout-prompts.json"
CITIES = ("tokyo", "singapore", "seoul", "kuala_lumpur")
PACES = (None, "relaxed", "balanced", "packed")


class HoldoutError(ValueError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s)).strip().lower()


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"\w+", _norm(s)))


def near_copy(text: str, gold_texts: list[str]) -> str | None:
    t, tt = _norm(text), _tokens(text)
    for g in gold_texts:
        gg = _tokens(g)
        if t == _norm(g) or (tt and gg and len(tt & gg) / len(tt | gg) >= 0.8):
            return g
    return None


def _place_id(name: str, ds) -> str | None:
    n = _norm(name)
    for a in ds.attractions.values():
        if n == _norm(a.name) or n in {_norm(x) for x in a.aliases}:
            return a.id
    return None


def to_gold(row: dict, all_ds: dict) -> dict:
    """The holdout row's simple expected answer, converted to the gold scorer's expected fields. Raises HoldoutError for anything invalid."""
    where = row.get("id", "?")
    city = row.get("city")
    if city not in CITIES:
        raise HoldoutError(f"{where}: city must be one of {', '.join(CITIES)}")
    ds = all_ds[city]
    e = row["expected"]
    out: dict = {}
    wc = e.get("wrong_city")
    if wc:
        if wc != "unsupported" and (wc not in CITIES or wc == city):
            raise HoldoutError(f"{where}: wrong_city must be another city's id or 'unsupported'")
        return {"draft": False, "destination": "unsupported" if wc == "unsupported" else f"other_city:{wc}"}
    out["draft"], out["destination"] = True, "ok"
    if e.get("days") is not None:
        if not isinstance(e["days"], int) or not 1 <= e["days"] <= 7:
            raise HoldoutError(f"{where}: days must be 1 to 7 or null")
        out["days"] = e["days"]
    if e.get("pace") not in PACES:
        raise HoldoutError(f"{where}: pace must be relaxed, balanced, packed or null")
    if e.get("pace"):
        out["pace"] = e["pace"]
    b = e.get("budget")
    if b:
        cur, amt = str(b.get("currency", "")).upper(), b.get("amount")
        if not isinstance(amt, (int, float)) or amt <= 0:
            raise HoldoutError(f"{where}: budget amount must be a positive number")
        if cur == ds.city.currency_code:
            out["budget"] = round(amt)
        elif cur == ds.city.display_currency_code:
            out["budget"] = round(amt / ds.city.sample_fx_to_display_currency)
        else:
            raise HoldoutError(f"{where}: budget currency must be {ds.city.currency_code} or {ds.city.display_currency_code} for {city}")
    for key, field in (("must_visit_names", "must_visit"), ("skip_names", "skip")):
        ids = []
        for nm in e.get(key) or []:
            pid = _place_id(nm, ds)
            if pid is None:
                raise HoldoutError(f"{where}: '{nm}' is not a place in {city}'s data (write names exactly as the app shows them)")
            ids.append(pid)
        if ids:
            out[field] = sorted(ids)
    if e.get("ambiguous"):
        out["ambiguous"] = list(e["ambiguous"])
    return out


def load_holdout(path: Path, all_ds: dict, gold_texts: list[str]) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["prompts"]
    if any("FORMAT ONLY" in json.dumps(r) for r in rows) or not rows:
        raise HoldoutError("the file still holds FORMAT ONLY rows (or no rows). Replace them with real prompts written by someone other than the builder: docs/eval/HOLDOUT-INSTRUCTIONS.md")
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise HoldoutError("row ids must be unique")
    out = []
    for r in rows:
        if not str(r.get("author_role", "")).strip():
            raise HoldoutError(f"{r['id']}: author_role is required")
        try:
            if date.fromisoformat(r.get("written_on", "")) > date.today():
                raise ValueError
        except ValueError:
            raise HoldoutError(f"{r['id']}: written_on must be a real date (YYYY-MM-DD) that is not in the future") from None
        if not str(r.get("text", "")).strip():
            raise HoldoutError(f"{r['id']}: text is required")
        g = near_copy(r["text"], gold_texts)
        if g:
            raise HoldoutError(f"{r['id']}: this prompt is a copy or near-copy of a gold prompt; write your own words")
        out.append({"id": r["id"], "city": r["city"], "kind": "holdout", "text": r["text"], "expected": to_gold(r, all_ds), "author_role": r["author_role"], "written_on": r["written_on"]})
    return out


def score_split(client, all_ds, prompts: list[dict]) -> dict:
    """Per city: prompts that code handled before the model (no call) and prompts that called it, each exact/total, plus field accuracy."""
    res = {c: {"n": 0, "code": [0, 0], "model": [0, 0], "fields": [0, 0], "failed": []} for c in CITIES}
    for p in prompts:
        r = sg.score(client, all_ds, prompts=[p])[p["city"]]
        t = res[p["city"]]
        called = r["calls"] > 0
        t["n"] += 1
        bucket = t["model" if called else "code"]
        bucket[1] += 1
        bucket[0] += r["exact"]
        t["fields"][0] += r["fields_ok"]
        t["fields"][1] += r["fields"]
        t["failed"] += r["failed"]
    return res


def render(res: dict, prompts: list[dict], all_ds, *, label: str, model: str, calls: int | None, stamp: str = "") -> str:
    live = calls is not None
    roles = sorted({p["author_role"] for p in prompts})
    dates = sorted({p["written_on"] for p in prompts})
    head = ["# Independent holdout, per city " + ("(LIVE model)" if live else "(MOCK rule-based baseline: not a model)"), ""]
    head.append(f"Run label: `{label}`. " + (f"Model: `{model}`. {stamp} " if live else f"Scorer: `{model}` (a deterministic stand-in, NOT a measurement of any language model). ")
                + f"PROMPT_VERSION: `{PROMPT_VERSION}`. LLM code fingerprint: `{lc.llm_fingerprint()}`. Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. Git: `{lc.git_hash()}`." + (f" Calls used: {calls} of a hard cap of {config.GOLD_MAX_CALLS}." if live else " Zero model calls."))
    head += ["", f"**N = {len(prompts)} prompts, written by: {'; '.join(roles)}** (on {', '.join(dates)}), before any run. Small sample: indicative, not a benchmark. Reported per city, never pooled; a prompt is exact only if every scored field is right. "
             "\"Handled by code\" prompts were settled by the code-side destination check before any model call (a request about another city); \"called the model\" prompts used at least one model call.", ""]
    rows = ["| City | Prompts (N) | Field accuracy | Exact: handled by code before the model | Exact: called the model |", "|---|---|---|---|---|"]
    for c in CITIES:
        t = res[c]
        if not t["n"]:
            continue
        rows.append(f"| {all_ds[c].city.display_name} | {t['n']} | {sg.pct(*t['fields'])} | {sg.pct(*t['code'])} | {sg.pct(*t['model'])} |")
    miss = ["", "## Prompts with at least one wrong field", "", "| Prompt | Wrong fields |", "|---|---|"]
    miss += [f"| {i} | {', '.join(f)} |" for c in CITIES for i, f in res[c]["failed"]] or ["| none | |"]
    return "\n".join(head + rows + miss) + "\n"


def main(argv: list[str]) -> int:
    from app.data.loader import load_all_city_datasets
    live = "--live" in argv
    all_ds = load_all_city_datasets()
    gold_texts = [p["text"] for p in sg.load_gold()]
    try:
        prompts = load_holdout(Path(os.environ.get("HOLDOUT_PROMPTS") or PROMPTS), all_ds, gold_texts)
    except (HoldoutError, KeyError) as e:
        print(f"Refusing to run: {e}")
        return 1
    out_dir = Path(os.environ.get("HOLDOUT_OUT_DIR") or REPO / "docs" / "eval")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not live:
        from llm_mock_baseline import RuleBasedBaseline
        res = score_split(RuleBasedBaseline(all_ds), all_ds, prompts)
        path = out_dir / f"holdout-results-{stamp}-mock.md"
        path.write_text(render(res, prompts, all_ds, label="mock", model="rule-based baseline v1", calls=None), encoding="utf-8")
        print(f"Wrote {path}. Live scoring was not run (use --live --label NAME with RUN_LIVE_LLM_EVAL=1).")
        return 0
    lc.load_env_file()
    worst = len(prompts) * config.LLM_MAX_CALLS_PARSE
    if worst > config.GOLD_MAX_CALLS:
        print(f"Refusing to run: the worst case ({worst} calls) is above the cap {config.GOLD_MAX_CALLS}.")
        return 0
    msg = lc.gate("scripts/score_holdout.py --live", config.GOLD_MAX_CALLS)
    if msg:
        print(msg)
        return 0
    label = sg.label_arg(argv)
    from app.llm.client import OpenRouterLLMClient
    client = lc.Capped(OpenRouterLLMClient.from_env(), config.GOLD_MAX_CALLS)
    try:
        res = score_split(client, all_ds, prompts)
    except lc.RunAborted as e:
        print(f"\nStopped after {client.total_calls} call(s): {e}\nNo results were written.")
        return 2
    path = out_dir / f"holdout-results-{stamp}-{label}.md"
    with open(path, "x", encoding="utf-8") as f:
        f.write(render(res, prompts, all_ds, label=label, model=client.model, calls=client.total_calls, stamp=lc.endpoint_stamp(client)))
    print(f"\nWrote {path} ({client.total_calls} model call(s) used). Earlier result files are untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
