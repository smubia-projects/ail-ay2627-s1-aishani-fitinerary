# Fitinerary

**Choose a hotel based on the trip it makes possible, then build a feasible itinerary around it.**

> **Data notice.** Four cities (Tokyo, Singapore, Seoul, Kuala Lumpur) of **sample data**: made-up demo hotels, estimated travel times and fares, not live prices, public holidays not modelled. Singapore: 7 fields source-checked, 0 rows human-verified, 75 unchecked; other cities unchecked; all data is still sample data. (Generated: `docs/data-verification/counts.md`.)

[PLACEHOLDER: live app URL] · [PLACEHOLDER: backend URL] · [PLACEHOLDER: demo video link] · Source: https://github.com/smubia-projects/ail-ay2627-s1-aishani-fitinerary (original: https://github.com/Aish710/AI-lodge)

## 1. Problem Statement

Planning a city trip is not just picking attractions. The hotel changes the travel time, the transport cost and what can realistically fit into each day, and a hotel that looks cheap per night can cost more once travel time and fares are counted. Most planners treat the hotel and the itinerary as two separate decisions. Fitinerary chooses them together and shows its working.

**Who it is for and why it matters.** The target user is a traveller, often budget-conscious or a student, who plans a short city trip on their own and has to choose a hotel before knowing how the days will go. **The app tells you whether your hotel choice makes the trip workable and shows the arithmetic** (room, admissions, estimated transport, the value of your travel time, and a charge for sights that did not fit); it does not claim that cheap hotels are usually traps. How often the cheapest-rate hotel is not the best varies by city, measured on seeded synthetic trips with made-up hotels (per city, never pooled; `docs/README-claims.md`, `docs/worked-example.md`):

| City | Best-value hotel differed from the cheapest-rate hotel | Mean extra true cost of choosing by rate | Travel time vs an input-order baseline (N comparable) | Plans the validator passed |
|---|---|---|---|---|
| Tokyo | 4 of 52 trips | 39.5 JPY | 14.1% less (N = 15 comparable, 60 generated) | 53 of 53 |
| Singapore | 7 of 18 trips | 166.3 SGD | 7.6% less (N = 15 comparable, 20 generated) | 18 of 18 |
| Seoul | 0 of 15 trips | 0.0 KRW | 0.0% less (N = 1 comparable, 20 generated) | 15 of 15 |
| Kuala Lumpur | 8 of 15 trips | 116.9 MYR | 5.7% less (N = 14 comparable, 20 generated) | 15 of 15 |

For Tokyo the figures are at the default value of time (1,000 JPY an hour); for the other cities at the evaluation's middle tested value of time (1000 local units an hour, the same number in every currency, so Seoul's 1000 KRW is far below its own default and says little there). Samples are 20 to 60 seeded trips: indicative, not a claim about real trips or real hotel markets. What a traveller gets: a hotel comparison by true cost, a day-by-day itinerary that an independent validator has checked, and a plain statement of what could not fit and why. The honest limits: the data is sample data, the hotels are made up, and no user study has been run.

**User test (informal, small N):** Not run yet.

**Human red team (informal, small N):** Not run yet.

## 2. Solution Overview

Fitinerary is a Vue frontend plus a FastAPI backend. The pipeline is:

```
 your words (optional)        sanitise -> LLM extracts a draft -> code validates it -> you edit the form
 your form / choices  ---->   OR-Tools CP-SAT optimiser (fixed seed) ---->  independent validator
                                                                              |
                          LLM explains the computed facts (optional) <---- computed result
                          code checks every number and name in the explanation, else a template is shown
```

- The **optimiser** (OR-Tools CP-SAT) makes every planning decision: which hotel, which sights, in what order. It is never the language model.
- An **independent validator** re-checks every rule on the finished plan (opening hours, travel gaps, overlaps, budget, locked stops, must-visits).
- The **language model** only works at the edges, only when you click: it reads your words into an editable form, proposes changes that you must apply yourself, and words short explanations of numbers the code already computed.
- Without an AI key the whole app works (form, comparison, plan, replanning, template explanations): the header says "AI off".

## 3. Key Features

- **One click from choices to plan.** "Plan my trip" compares the hotels, picks the best by true cost, plans the days and lands on "Your trip" (2 clicks from the example, measured in `docs/ux-metrics.md`; machine-dependent).
- **Hotel comparison by true cost**, with a plain verdict, the full breakdown, and a sensitivity analysis of the value of an hour of your time (runs only when you click).
- **Itinerary with a feasibility report** and a cost panel (admissions, estimated transport, rooms, unknown prices listed, never counted as zero), day cards, travel connectors labelled "estimated", and a filterable map.
- **Your own limits.** Set a maximum number of activities per day (independent of the pace presets) and a maximum of estimated travel minutes per day. Both are enforced by the planner and re-checked by the independent validator; if they cannot be met, the plan says which limit conflicts instead of quietly breaking it. Travel is estimated from straight-line distances in the sample data, so there is no walking-distance, transport-mode or accessibility setting.
- **A trade-off explorer on "Your trip".** Choose how much an hour of your time is worth (or "Lower spending / Balanced / Less travel time" presets, which only set that value) and re-run the planner for real. Three plans sit side by side (cheapest hotel first, convenience first, joint optimisation) with rooms, admissions, estimated transport, estimated travel time, optional sights left out and the score shown separately, plus a plain reason for the recommended hotel. Your locked stops and limits are kept, "Keep my hotel" is respected, and every earlier valid plan can be shown again from memory.
- **Build your own itinerary.** Drag sights between days (or use the keyboard-accessible arrows) and see at once, from the independent validator, whether the draft works, each stop's time, the active days and the modelled spending including rooms; then compare it with the optimiser's plan for the same sights. A minimum-active-days preference (a hard rule) keeps a plan from squeezing everything into the first days.
- **Honest costs.** "Estimated spending" includes the demo hotel's rooms and shows its parts; the "generalised cost" (valued travel time and missed-sight charges included) is labelled "not money". Hotel claims say they are among a few demo hotels. Anything unverified (hours, prices, bookings) is listed once under "To check before you go".
- **AI-assisted repair.** When a plan is blocked you can type what you can live with ("keep my hotel but make it work", "I can finish an hour later"). The AI only picks from a fixed list of changes and copies names as you wrote them. Code resolves the names, computes every number, derives which changes need your individual confirmation, tries the proposal on a copy with the normal optimiser and the independent validator, and shows what was understood, what changes, what stays, and whether it was solver-verified. Nothing is applied until you press Apply, and applying re-checks everything against the current plan (a stale or tampered proposal is refused). A hotel you keep and locked stops can never be changed by a repair. If the AI is off or fails, the deterministic repair cards still work.
- **Travel style in your own words.** The AI only tags which supported preferences your text expresses. Numbers count as stated by you only if they are in your text; an inferred suggestion is labelled and needs your tick; "limited budget" without an amount becomes a question; unsupported wishes are listed, not enforced; values you set yourself are kept.
- **Conflict resolver.** When a plan cannot work, the planner names the conflicts it can establish and offers repairs that were actually solved and re-checked by the independent validator: relax a limit, add a day, extend the day, change pace, switch hotel, or (only if one change is not enough) a bounded pair of changes. Options are ordered by how little they change, which is a convenience and not a proof of minimality. Nothing is applied until you press a button.
- **Compare with my itinerary.** Arrange your own days with keyboard-accessible controls and see them beside the optimiser's plan for exactly the same sights, and beside a full plan that may leave optional sights out. A travel-time saving is only reported when both plans visit the same sights; otherwise the page says they differ.
- **Travel styles.** Budget-conscious, relaxed, sightseeing and convenience-focused styles propose values for pace, activities a day, estimated travel a day and the value of an hour. They are editable, show where each value came from, and never overwrite a value you typed.
- **Alternatives and what-if.** A bounded search shows non-dominated plans trading spending, estimated travel and optional sights (not claimed to be a complete Pareto frontier). A what-if panel previews a change on a copy of the plan; the plan you have is untouched until you press Apply, and earlier plans stay available.
- **Replanning** that queues edits, asks before removing a must-visit, and shows an explicit before/after diff.
- **Four cities**, one city per trip; switching asks first and clears everything.
- **An AI switch:** the header chip can switch the AI off for the session (nothing is then sent to a model, the form works the same, and a note says what the AI would have added).
- **AI that shows its working:** AI and "Calculated" markers, "What the AI understood", a "What the AI did" drawer, a header popover saying what the AI does and never does.
- **A Proof page** generated from repository files: evaluation, hardening, test results, the safety risk table, live AI evidence with its run history, a simulated red-team table, data-verification counts.
- Keyboard-accessible controls (Lighthouse accessibility 100 on all three audited pages).

## 4. AI Integration

The AI is used in three places, all optional, all on an explicit click (`docs/LLM_DESIGN.md`, "How it works" page):

1. **Ask the AI to set up your trip** (`/parse`): your words become a draft of the form. The model returns strict JSON; code then resolves place names, decides whether the request is about the selected city (another city only produces a "Switch to…" offer; nothing is planned), converts currencies (an ambiguous `$` is always confirmed with you) and fills defaults, listing every assumption.
2. **Ask the AI to change your plan** (`/parse-replan`): words become *proposed* operations that only join a pending list. Removing a must-visit needs your explicit confirmation, which the model can never set.
3. **Explain this plan** (`explain`): the model gets only computed facts (never place descriptions), each with an id (F1, F2, ...), and returns sentences that each name the facts they rest on. Code checks each sentence on its own: the ids exist, every number is in the cited facts, every name is in the facts, no claim about access, quiet, safety, children, diet, weather or queues that the facts do not state, nothing uncited is shown; failing sentences are dropped and with fewer than two left a template is shown. The sentences appear with citation chips (F5) that outline the cited number in the tables. Planner warnings are added by code, verbatim. A plain-text reply from the model still goes through the older number and name check and is labelled "numbers checked", not "every claim cited".

Around it: input sanitising, user text treated as data inside one delimited block, a leak guard on everything the model writes, per-call caps, a daily circuit breaker, rate limits. The key stays on the server.

**Historical live measurements from earlier development runs, and what was not measured** (all one model, `claude-haiku-4-5-20251001`, through a third-party gateway, `api.apiyi.com`; the cleaned text and computed facts pass through that gateway):

| What | Result | Read it as |
|---|---|---|
| Historical 20-check smoke test (`docs/ai-selfcheck.md`; history in `docs/ai-selfcheck-history.md`) | 19 of 20 on the last recorded prompt-v4 run | Tuned development checks, one model; not a result for the new copilot |
| Historical gold set, 12 prompts per city (`docs/eval/gold-results-live-20261018T043721Z-final4.md`) | Fields: 37 of 37 in Tokyo, Singapore and Seoul; 35 of 37 in Kuala Lumpur | Builder-written prompts and code-side name/currency checks; not unseen-request accuracy |
| Historical live red team (`docs/redteam-live.md`, 31 attacks on prompt version 4; first run in `docs/redteam-live-first-run.md`) | Raw reply tried to comply in 3 of 31; final result affected in 0 of 31. First run affected 2 of 27 before fixes | Parsing/explanation coverage on old AI code; no live coverage of the new copilot |

**Version note.** The live results above were produced before the new copilot and preference-clarification changes. The Proof page therefore shows the latest runs as "not run on this version" until the live scripts are re-run; the first runs stay shown as historical records. [PLACEHOLDER: delete this note after you re-run the live scripts on the final code]

**Current upgrade: deterministic/mock evidence only; paid reruns pending.** See [the local upgrade assessment](docs/QUALITY-UPGRADE-REPORT.md) for copilot, clarification, provenance, current verification and residual risks.

**Only tested with a mocked model on the new AI code:** the cited explanations (each sentence must name the facts it rests on; no live result exists yet for the new format), the AI on/off switch, the 840-run red team (`docs/redteam-results.md`, simulated models, not a real model), every pipeline test in `backend/tests`. Not measured at all: any second model, any user study, how the AI performs on requests it was not tuned on.

## 5. External Tools & Datasets

- Backend: Python 3.13, FastAPI, Pydantic v2 (strict), OR-Tools CP-SAT, httpx (OpenRouter), upstash-redis (shared AI Lodge rate limiter), pytest.
- Frontend: Vue 3, Vue Router, Vite, Bootstrap, Leaflet with OpenStreetMap tiles (a one-tile probe falls back to a plain background if the tile server refuses), Vitest, Playwright, Lighthouse.
- **Model and gateway:** all model calls go through OpenRouter (`https://openrouter.ai/api/v1`, OpenAI-compatible `/chat/completions`); the model is `MODEL_NAME` (default: the OpenRouter id for Claude Haiku, see `.env.example`). OpenRouter is a third-party gateway: it sees the cleaned request text and the computed facts (`docs/PRIVACY.md`). The live results in this README were produced with Claude Haiku 4.5 through an earlier gateway (`api.apiyi.com`); a different OpenRouter model needs the live scripts re-run.
- **Data:** hand-built sample attractions and made-up hotels for four cities (`backend/app/data/cities/`), with a travel-time matrix estimated from straight-line distance (`backend/scripts/build_travel_matrix.py`). Every record is labelled `sample`. Singapore has per-field verification records (7 fields checked against official pages, with URL and date, in `docs/data-verification/singapore-findings.json`); nothing is human-verified. Three Singapore opening-hours values were corrected from official pages (`docs/data-verification/CHANGELOG.md`).
- The AI-free evaluation method is in `docs/EVALUATION.md`.

## 6. Installation & Setup

Requires Python 3.13 and Node 22.

```bash
# backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --port 8000        # sample mode: no key needed

# frontend (second terminal)
cd frontend
npm install
printf 'VITE_API_BASE_URL=http://localhost:8000\n' > .env
npm run dev                                       # http://localhost:5173
```

To turn the AI on, export `API_KEY` (and optionally `MODEL_NAME`) before starting uvicorn; every variable is documented in the root `.env.example` (copy it to `.env`, which is gitignored; never commit it).

Checks:

```bash
cd backend && .venv/bin/python -m pytest              # full suite
cd frontend && npm test                               # Vitest
cd frontend && npm run e2e                            # Playwright (builds the app, starts a sample-mode backend)
cd backend && .venv/bin/python scripts/run_hardening_checks.py
cd backend && .venv/bin/python scripts/run_evaluation.py --full
cd frontend && npm run verify:clean-clone             # clone, build and test from a clean checkout
```

Opt-in live checks (each needs a key and spends model calls, capped): `RUN_LIVE_LLM_EVAL=1 .venv/bin/python scripts/ai_selfcheck.py`, `scripts/score_gold.py --live --label NAME`, `scripts/redteam_live.py`.

### Deployment (AI Lodge stack)

- **Backend → Google Cloud Run** via `.github/workflows/deploy.yml`: on push to `main` it builds `backend/` (Docker build context), pushes `docker.io/<DOCKERHUB_USERNAME>/ail-ay2627-s1-aishani-fitinerary`, and deploys the Cloud Run service `ail-ay2627-s1-aishani-fitinerary` (asia-southeast3, 1 vCPU / 1 GiB, 0-2 instances). Env vars come from the repo's `DOTENV` secret: fill `.env` from `.env.example`, then `gh secret set DOTENV --repo smubia-projects/ail-ay2627-s1-aishani-fitinerary < .env` and `gh workflow run deploy.yml`. Health check: `GET /api/health` (alias of `/health`). The backend is API-only.
- **Frontend → Vercel**: root directory `frontend`, framework Vite (`frontend/vercel.json`: `npm run build`, output `dist`, SPA rewrite, security headers + CSP). Set `VITE_API_BASE` to the Cloud Run URL. The CSP's `connect-src` allows `https://*.run.app`; if the API ever moves to another host, update `frontend/vercel.json`.
- **CORS**: put the Vercel URL in `CORS_ORIGINS` (`.env` → re-upload `DOTENV` → re-run the workflow). `*` is refused at startup.
- **AI quota**: the shared AI Lodge rate limiter (Upstash) caps model calls per IP per action bucket (`parse`, `repair`, `profile`, `replan`, `copilot`, `explain`). Over the limit, every AI route answers 429 and the page shows the AI Lodge CTA, including the opt-in plan explanations (`explain: true` on `/optimise`, `/compare-hotels` and `/replan`); the plan already on screen stays. A paused demo (kill switch) answers 503 `demo_paused`; a busy solver answers with its own non-503 status and the code `solver_busy`, and keeps its own retry notice.

`docs/DEPLOY.md` describes the student's original Render + Netlify setup and is kept for history only.

## 7. AI Safety & Limitations

**Risk, safeguard, test** (146 risks in `docs/SAFETY_LOG.md`, each with its test; the main ones):

| Risk | Safeguard | Test |
|---|---|---|
| Prompt injection via the user's text | One delimited data block that text cannot close; instructions are a fixed constant; the model has no tools and cannot set safety flags | `backend/tests/test_llm_input.py`, `test_redteam.py` |
| The model leaks the system prompt, a key or another user's data | A leak guard drops any model string that quotes the prompt or holds a key, email or phone number | `test_redteam.py` (with a mutation check) |
| The model invents a number, place, price or claim | Strict schemas; names resolved in code; every number and name in an explanation checked against computed facts; a word-list guard on claims about access, quiet, safety, children, diet, weather and queues; template fallback | `test_llm_explain.py`, `test_llm_per_city.py`, `test_unsupported_claims.py` |
| A request about the wrong city is planned | A code-side destination check before any model call; only a switch offer, confirmed by you | `test_city_packs.py`, `test_city_isolation.py` |
| The model removes a must-visit | Only your confirmation dialog can set the flag | `test_replan.py`, `replan.spec.js` |
| Cost runaway or abuse | Per-call caps, a daily circuit breaker, rate limits (6, 30 and 120 a minute by tier), a solver slot that frees when a client leaves | `test_hardening.py`, `test_solver_disconnect.py` |
| Data looks verified when it is not | `source_status` is `sample` on every record; per-field verification records need an official URL and a date | `test_verification.py` |

**Results (all with the caveats above).** Simulated red team (`docs/redteam-results.md`, simulated models, not a real model): 60 attacks in English, Korean, Chinese and Malay, 840 runs, final result affected in 0 after 4 fixes found by the red team itself. Live red team: 0 of 31 final results affected in that historical run; the first run affected 2 of 27 before the fix (section 4). Hardening: 33 of 33 in-process abuse cases pass. Test counts, outcomes and run versions are recorded in [the backend summary](backend/tests/results/full-suite.json) and [the frontend summary](docs/test-summary-frontend.json); see [the local upgrade report](docs/QUALITY-UPGRADE-REPORT.md) for the dated verification snapshot.

**Tests that pin the four rules judges ask about** (`backend/tests/`):
- *Place descriptions are never sent to the model:* `test_extraction_prompt_never_contains_dataset_descriptions` (`test_llm_input.py`), `test_explain_prompts_never_contain_attraction_descriptions` (`test_llm_explain.py`), `test_description_text_never_reaches_any_prompt_even_if_poisoned` (`test_llm_api_safety.py`), `test_city_descriptions_never_reach_extraction_prompt` (`test_city_packs.py`).
- *Hotels are not representable in the model's output (the trip request):* the extraction schema has no hotel field and rejects extra keys: `test_fields_the_model_may_not_set_are_rejected_by_the_schema` (a hotel price, ids, nights and both safety flags), `test_a_fully_compliant_malicious_model_cannot_change_flags_ids_or_state`. When a change request names a hotel, code resolves the name against the city's data (`test_hotel_resolution`) and an unknown hotel is only asked about.
- *Prices come only from the data:* the model has no price field to fill, and any number in an explanation must be in the computed facts: `test_numbers_not_in_the_facts_are_rejected_including_derived_ones`, `test_explanation_with_a_fabricated_number_is_rejected_and_replaced_by_the_template`, `test_invented_percentage_is_rejected` (`test_llm_explain.py`).
- *Grounding on numbers and names:* `test_name_not_in_facts_is_rejected_and_alias_of_a_fact_name_is_accepted`, `test_attraction_not_in_the_facts_is_rejected` (`test_llm_explain.py`), `test_an_explanation_that_names_another_citys_place_is_rejected_for_every_city_pair`.

**Known gaps, stated plainly.** (1) The grounding check looks at numbers and names. A separate word-list guard rejects explanations that claim accessibility, quiet or crowds, safety, suitability for children, dietary suitability, weather or queue times (English, Malay, Korean, Chinese) unless the facts state the term, but it is a word list: a claim worded another way can still pass (`docs/LLM_DESIGN.md`). (2) Preferences about walking, quiet, accessibility, children or diet cannot be acted on: the sample data has no such information, so the app lists them as "heard, not applied" and never turns them into constraints. (3) A suggestion such as a relaxed pace is a stand-in the user must accept, not a walking limit.

**Evaluation (no language model involved, seeded synthetic trips on sample data).** Across 15 comparable trips of 60, optimised routes used a mean of 14.1% less travel time than an input-order baseline (8.4% less than nearest-neighbour on 18 trips). The independent validator passed 53 of 53 plans. The lowest-rate hotel was not the best by true cost in 4 of 52 trips at the default value of time, and in 10 of 52 when an hour of time is valued at 2,000 yen. The hotels and rates are fictional and were tuned for the demo; this measures the model's behaviour on sample data, not hotel markets (`docs/eval-results.md`).

**How to read the costs.** Monetary spending is rooms plus admissions plus estimated transport. Travel time is estimated, and is turned into money only inside the score, at the value of an hour that you choose. A sight with no known admission price is left out of every money figure (the optimiser values it with a placeholder inside the score only). A fixed charge per optional sight left out is also part of the score only. The optimisation score is the sum of those parts and is never what you pay.

**Fair baselines (`docs/baseline-results.md`).** Cheapest-hotel-first, convenience-first and joint optimisation use the same data, constraints, estimated travel times and objective. Per city, at each city's own default value of time: in Tokyo the joint choice differed from the cheapest hotel in 4 of 52 eligible trips, and in none of the eligible Singapore, Seoul or Kuala Lumpur trips, so for most sample trips the cheapest hotel is also the best overall choice. Seeded synthetic trips on sample data with made-up hotels, not user-tested.

**Itinerary planning, separate from the hotel (`docs/itinerary-study.md`).** With the hotel fixed, a listed-order plan that respects the opening hours and the optimiser were compared on the same trips. For exactly the same sights the optimiser's order had less estimated travel in most trips and never more, but against a careful nearest-feasible heuristic many trips tie and the median difference is small; the clearest verified benefit is keeping every must-visit. Per city, seeded synthetic trips on sample data, not user-tested. `docs/profile-results.md` reports how often a travel style changes the plan at all.

**Safety documentation.** `docs/safety/` holds the threat model and trust boundaries, ten tested safety invariants, safeguard-by-safeguard mutation testing (every disabled safeguard was noticed by at least one test; developer-written tests), a protocol for an independent attack suite (not yet run), a provenance audit and the contract a future AI repair feature must meet.

**Limitations.** Sample data only; hotels are made up; no live prices, holidays or transit; travel times are estimates (one transport mode is never named because the data has none). Two Singapore closing days (the Buddha Tooth Relic Temple on Mondays, the ArtScience Museum on Wednesdays) are unverified and probably wrong. No user study has been run. All live AI evidence is one model through a third-party gateway and is optimistic. Interests only highlight sights; the planner uses your Must / Include / Skip choices. Rate limits and the circuit breaker are per process.

**Privacy.** Nothing is stored: no database, cookies, analytics or browser storage. Server logs hold metadata only. Place descriptions are never sent to the model. If the AI is on, your cleaned request text and the computed facts leave the server for the gateway named above and the model provider (`docs/PRIVACY.md`).

## Screenshots

`docs/screenshots/steps/` (Kuala Lumpur and Seoul, desktop and mobile). Files named `*-ai-canned-*` show the interface with canned responses only; they are not evidence of model behaviour.

## Future improvements

A live measurement of the cited-explanation format with a real model, an AI on/off side-by-side view, click counts for all four cities, a user test with the kit in `docs/user-test/`, human verification of the data sheets in `docs/data-verification/`, a second model for the live checks.

## Licence

[PLACEHOLDER: licence]

## Next: from words to constraints

Today the AI turns your words into the fields the planner uses and lists, without acting on them, what it cannot use ("Heard, but the planner can't act on this yet"). The next step would be to turn some of those words into planning constraints. This is how I would do it, and why it is not built.

- **Walking tolerance** ("not much walking"): map it to a limit on walking minutes per leg and per day, and make the optimiser respect it. This needs walking distances per leg, which the sample data does not have (its travel times are estimated from straight-line distance, with no mode and no walking share).
- **Quiet places** ("somewhere calm"): rank or avoid sights by expected crowding at the planned hour. This needs crowd levels by hour or day, which no dataset here has.
- **Hotel convenience** ("close to a station", "easy to get back to late"): add a hotel-to-station distance and last-train time to the comparison. This needs station exits and timetables.
- **Accessibility, children, diet:** each needs facts about step-free access, facilities or menus that must come from a source, never from a model.

**Why it is not built.** The data to act on these preferences does not exist in the sample data, a model must not invent it, and turning a subjective preference into a hard constraint without data would make the plan look more considered than it is.

**How assumptions would be confirmed.** Each inferred constraint would be shown as a suggestion in plain words ("I assumed at most N minutes of walking a day because you wrote 'not much walking'"), with the plan that results shown beside the plan without it, an Accept button, and nothing applied unless it is clicked. It would be held in memory only, like everything else. The first step of this is already in the app: a relaxed pace offered as an explicit, labelled stand-in.
