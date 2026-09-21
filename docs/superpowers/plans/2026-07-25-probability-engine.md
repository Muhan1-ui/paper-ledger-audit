# Auditable Football Probability Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add auditable xG-derived score distributions, market blending, and
calibration diagnostics to the offline tracker while preserving legacy records.

**Architecture:** `model.py` owns deterministic Poisson distributions and
derived football markets. `cli.py` builds frozen snapshots from the market and
optional model output. `ledger.py` validates the richer immutable structures,
and `report.py` calculates probability-quality diagnostics from linked records.

**Tech Stack:** Python standard library, `Decimal`, JSONL, `unittest`.

---

### Task 1: Score distribution primitives

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_model.py`
- Modify: `outputs/tai_cai_tracker/model.py`

- [ ] **Step 1: Write failing model tests**

```python
from outputs.tai_cai_tracker.model import poisson_score_matrix

def test_poisson_score_matrix_derives_normalised_1x2_probabilities(self):
    result = poisson_score_matrix(Decimal("1.2"), Decimal("0.8"))
    self.assertEqual(sum(result["one_x_two"].values(), Decimal(0)), Decimal(1))
    self.assertGreater(result["one_x_two"]["home_win"], result["one_x_two"]["away_win"])
    self.assertEqual(len(result["top_scores"]), 5)
```

- [ ] **Step 2: Verify the model test fails**

Run: `python -m unittest work/tai_cai_tracker_tests/test_model.py -v`

Expected: `ImportError` because `poisson_score_matrix` does not exist.

- [ ] **Step 3: Implement the minimal Poisson matrix API**

```python
def poisson_score_matrix(home_xg, away_xg, max_goals=10):
    """Return normalised score and derived-market probabilities from xG."""
    # Validate finite non-negative Decimal inputs.
    # Calculate each marginal recursively from exp(-lambda).
    # Build, normalise, and derive the matrix values defined in the test.
```

- [ ] **Step 4: Verify the model test passes**

Run: `python -m unittest work/tai_cai_tracker_tests/test_model.py -v`

Expected: all model tests pass.

### Task 2: Blend auditable market and xG probabilities

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_model.py`
- Modify: `outputs/tai_cai_tracker/model.py`

- [ ] **Step 1: Write failing blend tests**

```python
from outputs.tai_cai_tracker.model import blend_probabilities

def test_blend_endpoints_return_the_named_input_distribution(self):
    market = {"home_win": Decimal("0.2"), "draw": Decimal("0.3"), "away_win": Decimal("0.5")}
    model = {"home_win": Decimal("0.5"), "draw": Decimal("0.3"), "away_win": Decimal("0.2")}
    self.assertEqual(blend_probabilities(market, model, Decimal(0)), market)
    self.assertEqual(blend_probabilities(market, model, Decimal(1)), model)
```

- [ ] **Step 2: Verify the blend test fails**

Run: `python -m unittest work/tai_cai_tracker_tests/test_model.py -v`

Expected: `ImportError` because `blend_probabilities` does not exist.

- [ ] **Step 3: Implement the minimal blend function**

```python
def blend_probabilities(market, model, model_weight):
    """Blend validated home/draw/away distributions with a fixed weight."""
    # Require exactly the three outcome labels and a finite Decimal in [0, 1].
    # Return Decimal values summing exactly to one.
```

- [ ] **Step 4: Verify all model tests pass**

Run: `python -m unittest work/tai_cai_tracker_tests/test_model.py -v`

Expected: all model tests pass.

### Task 3: Freeze enhanced snapshots without breaking legacy input

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `work/tai_cai_tracker_tests/test_ledger.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `outputs/tai_cai_tracker/ledger.py`

- [ ] **Step 1: Write failing enhanced-snapshot tests**

```python
def test_snapshot_freezes_market_model_and_blended_probabilities(self):
    payload["xg_inputs"] = {
        "source_url": "https://stats.example.test/match",
        "home_xg": "1.2",
        "away_xg": "0.8",
        "model_version": "poisson-v1",
        "model_weight": "0.40",
    }
    record = prediction_from_input(payload, [named_capture])
    self.assertEqual(record["model"]["version"], "poisson-v1")
    self.assertEqual(record["outcomes"], record["model"]["blended_outcomes"])
```

- [ ] **Step 2: Verify enhanced-snapshot tests fail**

Run: `python -m unittest work/tai_cai_tracker_tests/test_cli.py work/tai_cai_tracker_tests/test_ledger.py -v`

Expected: a missing `model` key or rejected xG shape.

- [ ] **Step 3: Implement the new optional input shape**

```python
# xg_inputs accepts source_url, home_xg, away_xg, model_version, model_weight.
# Named European odds remain required when model_weight is non-zero.
# Persist market_outcomes, model.one_x_two, score distribution summaries,
# and blended_outcomes. Legacy xg_inputs remain accepted as context only.
```

- [ ] **Step 4: Repair the weekday identifier validation with Unicode escapes**

```python
WEEKDAY_SEQUENCE_MATCH_ID = re.compile(r"^[\u5468][\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5][0-9]{3}$")
```

- [ ] **Step 5: Verify snapshot and ledger tests pass**

Run: `python -m unittest work/tai_cai_tracker_tests/test_cli.py work/tai_cai_tracker_tests/test_ledger.py -v`

Expected: all selected tests pass.

### Task 4: Add settlement scores and calibration diagnostics

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_report.py`
- Modify: `outputs/tai_cai_tracker/ledger.py`
- Modify: `outputs/tai_cai_tracker/report.py`
- Modify: `outputs/tai_cai_tracker/cli.py`

- [ ] **Step 1: Write failing report tests**

```python
def test_calibration_at_fifty_records_reports_log_loss_and_ece(self):
    report = calibration_report(records)
    self.assertEqual(report["log_loss_average"], -Decimal("0.50").ln())
    self.assertIn("expected_calibration_error", report)
    self.assertEqual(report["phase"], "observational")
```

- [ ] **Step 2: Verify the report test fails**

Run: `python -m unittest work/tai_cai_tracker_tests/test_report.py -v`

Expected: missing report fields.

- [ ] **Step 3: Implement settlement score support and diagnostics**

```python
def log_loss(prediction, settlement):
    """Return negative natural log probability of the settled 1X2 outcome."""

def expected_calibration_error(predictions_and_settlements, bin_count=10):
    """Return fixed-bin confidence calibration error for top-class confidence."""
```

Settlement accepts an optional `final_score` object with integer `home` and
`away` fields. The report adds scoreline log score only for records carrying
both a model score matrix and final score.

- [ ] **Step 4: Verify report tests pass**

Run: `python -m unittest work/tai_cai_tracker_tests/test_report.py -v`

Expected: all report tests pass.

### Task 5: Document and verify the complete workflow

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`
- Modify: `outputs/tai_cai_tracker/example_input.json`

- [ ] **Step 1: Document enhanced input and its evidence limits**

```markdown
`xg_inputs` is optional context unless it also has `model_version` and
`model_weight`. A non-zero weight requires a named-bookmaker odds baseline.
The tracker records paper predictions only and makes no network requests.
```

- [ ] **Step 2: Run the complete regression suite**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -v`

Expected: all tests pass with no failures or errors.

- [ ] **Step 3: Exercise the documented CLI workflow in a temporary ledger**

Run: `python -m outputs.tai_cai_tracker.cli snapshot outputs/tai_cai_tracker/example_input.json <temporary-ledger>`

Expected: one frozen prediction JSON object and no network access.
