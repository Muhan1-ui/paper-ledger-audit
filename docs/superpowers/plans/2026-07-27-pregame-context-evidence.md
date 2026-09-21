# Pregame Context Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect and freeze auditable home/away xG, weather, and lineup context without changing first-phase prediction probabilities.

**Architecture:** Add one focused public-context module that validates a matched event, derives optional evidence objects, and always returns an explicit status. Extend the existing CLI and immutable prediction schema to accept a validated `pregame_context` object; keep model blending and all-market derivation unchanged.

**Tech Stack:** Python standard library, Decimal, existing JSON CLI, public event-provider JSON, Open-Meteo forecast API, unittest.

---

### Task 1: Define and test pure pregame-context evidence transformations

**Files:**
- Create: `outputs/tai_cai_tracker/context.py`
- Create: `work/tai_cai_tracker_tests/test_context.py`

- [ ] **Step 1: Write failing tests for identity and split xG**

```python
def test_build_split_xg_uses_current_season_home_and_away_samples(self):
    evidence = build_split_xg(EVENT, HOME_HOME, AWAY_AWAY)
    self.assertEqual(evidence["status"], "available")
    self.assertEqual(evidence["scope"], "home_away_split")
    self.assertEqual(evidence["home_xg"], Decimal("1.45"))

def test_build_split_xg_marks_incomplete_source_unavailable(self):
    self.assertEqual(build_split_xg(EVENT, HOME_HOME, {"xg": None})["status"], "unavailable")
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m unittest work.tai_cai_tracker_tests.test_context -v`

Expected: import failure because `context.py` does not exist.

- [ ] **Step 3: Implement pure validation and xG derivation**

```python
def build_split_xg(event, home_home, away_away):
    if not _valid_event(event):
        return _unavailable("event_identity_unverified")
    if not _complete_split(home_home, away_away):
        return _unavailable("home_away_xg_unavailable")
    home_xg = (_decimal(home_home["xg"]) / home_home["matches"]
               + _decimal(away_away["xga"]) / away_away["matches"]) / 2
    away_xg = (_decimal(away_away["xg"]) / away_away["matches"]
               + _decimal(home_home["xga"]) / home_home["matches"]) / 2
    return {"status": "available", "scope": "home_away_split", ...}
```

The returned evidence must contain `source_url`, timezone-aware `captured_at`,
competition, season, separate home/away sample counts, raw xG/xGA values,
formula, and Decimal `home_xg`/`away_xg` values.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `python -m unittest work.tai_cai_tracker_tests.test_context -v`

Expected: all split-xG tests pass.

### Task 2: Add weather and lineup status transformations

**Files:**
- Modify: `outputs/tai_cai_tracker/context.py`
- Modify: `work/tai_cai_tracker_tests/test_context.py`

- [ ] **Step 1: Write failing tests for evidence gates**

```python
def test_build_weather_selects_the_timezone_matched_kickoff_hour(self):
    result = build_weather(EVENT_WITH_COORDINATES, FORECAST)
    self.assertEqual(result["status"], "available")
    self.assertEqual(result["precipitation_mm"], Decimal("0.4"))

def test_build_lineup_requires_provider_confirmation_and_22_starters(self):
    self.assertEqual(build_lineup(CONFIRMED_LINEUP)["status"], "confirmed")
    self.assertEqual(build_lineup(REPORTED_LINEUP)["status"], "reported")
    self.assertEqual(build_lineup(PARTIAL_LINEUP)["status"], "unavailable")
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m unittest work.tai_cai_tracker_tests.test_context -v`

Expected: missing weather and lineup builders fail.

- [ ] **Step 3: Implement weather and lineup builders**

`build_weather` must reject missing/out-of-range coordinates, choose only the
forecast timestamp equal to the kickoff hour in the forecast timezone, and
retain request URL, capture time, temperature, precipitation, and wind speed.
`build_lineup` must return `confirmed` only for explicit provider confirmation
plus exactly 11 starters per side; otherwise return `reported` or unavailable
with a reason and direct event URL.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `python -m unittest work.tai_cai_tracker_tests.test_context -v`

Expected: all context transformation tests pass.

### Task 3: Add public collection and a review-only CLI command

**Files:**
- Modify: `outputs/tai_cai_tracker/context.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/tai_cai_tracker_tests/test_context.py`
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing command and collector tests**

```python
def test_collect_pregame_context_returns_unavailable_items_without_blocking(self):
    with patch("outputs.tai_cai_tracker.context._http_get", return_value=EVENT_FIXTURE):
        bundle = collect_pregame_context(IDENTITY, EVENT_URL)
    self.assertEqual(bundle["risk_summary"]["confidence_ceiling"], "medium")

def test_pregame_context_command_is_exposed(self):
    result = self.run_cli("pregame-context", "--help")
    self.assertEqual(result.returncode, 0)
```

- [ ] **Step 2: Run focused command tests and verify they fail**

Run: `python -m unittest work.tai_cai_tracker_tests.test_context work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_pregame_context_command_is_exposed -v`

Expected: the command and collector are absent.

- [ ] **Step 3: Implement collection and command wiring**

Implement event fetching through the existing no-proxy request style, validate
official teams and kickoff before using event data, request Open-Meteo only for
verified coordinates, and add:

```python
pregame_context_parser = commands.add_parser("pregame-context")
pregame_context_parser.add_argument("official_identity_path")
pregame_context_parser.add_argument("event_url")
```

The command prints JSON only and never writes the ledger or invokes purchase
operations.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `python -m unittest work.tai_cai_tracker_tests.test_context work.tai_cai_tracker_tests.test_cli -v`

Expected: context and CLI tests pass.

### Task 4: Freeze validated context with predictions

**Files:**
- Modify: `outputs/tai_cai_tracker/context.py`
- Modify: `outputs/tai_cai_tracker/ledger.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/tai_cai_tracker_tests/test_ledger.py`
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing freeze/round-trip tests**

```python
def test_snapshot_freezes_valid_pregame_context_without_changing_outcomes(self):
    prediction = prediction_from_input({**PAYLOAD, "pregame_context": CONTEXT})
    self.assertEqual(prediction["pregame_context"]["lineup"]["status"], "confirmed")
    self.assertEqual(prediction["outcomes"], BASELINE_OUTCOMES)

def test_ledger_rejects_tampered_or_invalid_pregame_context(self):
    record = freeze_prediction(PREDICTION_WITH_CONTEXT)
    record["pregame_context"]["weather"]["status"] = "invented"
    with self.assertRaises(ValueError):
        verify_prediction(record)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m unittest work.tai_cai_tracker_tests.test_ledger work.tai_cai_tracker_tests.test_cli -v`

Expected: context is not validated or preserved.

- [ ] **Step 3: Implement schema validation and snapshot preservation**

Add `validate_pregame_context` in `context.py`. It must require exactly
`split_xg`, `weather`, `lineup`, and `risk_summary`; validate all URLs,
timezone-aware times, legal statuses, and all numerical types. Call it in both
`ledger._validate_prediction` and `cli.prediction_from_input`, preserving the
normalized value under `prediction["pregame_context"]`. Do not change
`outcomes`, model weights, or market selection.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `python -m unittest work.tai_cai_tracker_tests.test_ledger work.tai_cai_tracker_tests.test_cli -v`

Expected: existing and new freeze tests pass.

### Task 5: Document and verify the upgrade

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`
- Modify: `docs/superpowers/specs/2026-07-27-pregame-context-evidence-design.md`

- [ ] **Step 1: Document command, source limits, and confidence rules**

Add the `pregame-context` invocation, the three statuses, the no-probability-
adjustment guarantee, and the explicit home/away fallback rule. State that the
public provider and Open-Meteo evidence are observations, not official lineup
or weather authorities.

- [ ] **Step 2: Run full verification**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -t . -v`

Expected: all tests pass.

Run: `python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl --json`

Expected: the existing ledger remains readable and its settled-record totals
are unchanged.
