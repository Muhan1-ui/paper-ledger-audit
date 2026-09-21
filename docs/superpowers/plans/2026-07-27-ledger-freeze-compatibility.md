# Ledger Freeze Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make frozen league-total xG snapshots readable so full-market snapshots, settlement, and reporting continue to work.

**Architecture:** `cli._optional_xg` is the canonical parser used while reading frozen prediction records. It will accept only the derived `confidence_ceiling: "medium"` that the same parser produces for league-total evidence, rejecting an inconsistent value or scope. Ledger identity continues to use the documented official weekday sequence.

**Tech Stack:** Python standard library, `unittest`, append-only JSONL ledger.

---

### Task 1: Capture the frozen-record regression

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `outputs/tai_cai_tracker/cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_read_records_accepts_derived_medium_confidence_ceiling(self):
    payload = self.enhanced_payload_with_league_total_evidence()
    payload["xg_inputs"]["confidence_ceiling"] = "medium"
    prediction = prediction_from_input(payload)
    frozen = freeze_prediction(prediction)
    ledger_path.write_text(json.dumps(frozen, default=str) + "\n", encoding="utf-8")
    self.assertEqual(read_records(ledger_path)[0]["xg_inputs"]["confidence_ceiling"], "medium")
```

- [ ] **Step 2: Run the focused test and confirm failure**

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.TrackerCliTests.test_read_records_accepts_derived_medium_confidence_ceiling -v
```

- [ ] **Step 3: Implement the minimal parser compatibility rule**

```python
if "confidence_ceiling" in xg_inputs:
    if xg_inputs["confidence_ceiling"] != "medium" or evidence["scope"] != "league_total":
        raise ValueError("xg_inputs confidence_ceiling is inconsistent with evidence scope")
```

- [ ] **Step 4: Run the focused test and confirm success**

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.TrackerCliTests.test_read_records_accepts_derived_medium_confidence_ceiling -v
```

### Task 2: Verify the repaired workflow

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Add a negative test for inconsistent confidence metadata**

```python
payload["xg_inputs"]["confidence_ceiling"] = "high"
with self.assertRaisesRegex(ValueError, "confidence_ceiling"):
    prediction_from_input(payload)
```

- [ ] **Step 2: Run the complete suite**

```powershell
$env:PYTHONIOENCODING='utf-8'
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

- [ ] **Step 3: Run the previously blocked all-market snapshot and report**

```powershell
$env:PYTHONIOENCODING='utf-8'
python -m outputs.tai_cai_tracker.cli all-market-snapshot work\freeze_2040641_all_markets.json outputs\tai_cai_tracker\data\paper_ledger.jsonl
python -m outputs.tai_cai_tracker.cli report outputs\tai_cai_tracker\data\paper_ledger.jsonl --json
```

### Task 3: Preserve All-Market Validation After JSONL Serialization

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `outputs/tai_cai_tracker/ledger.py`

- [ ] **Step 1: Write a failing persistence test**

```python
result = self.run_cli("all-market-snapshot", str(input_path), str(ledger_path))
self.assertEqual(result.returncode, 0, result.stderr)
records = read_records(ledger_path)
self.assertEqual(records[-1]["record_type"], "all_market_snapshot")
```

- [ ] **Step 2: Run it and confirm correct-score validation fails after JSON key sorting**

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli -v -k all_market_snapshot_round_trip
```

- [ ] **Step 3: Validate each distribution in its declared option order**

```python
if sum((probabilities[option] for option in options), Decimal(0)) != Decimal(1):
    raise ValueError("all-market probabilities must sum to one")
```

- [ ] **Step 4: Run the complete suite and the live ledger report**

```powershell
$env:PYTHONIOENCODING='utf-8'
python -m unittest discover -s work\tai_cai_tracker_tests -t . -v
python -m outputs.tai_cai_tracker.cli report outputs\tai_cai_tracker\data\paper_ledger.jsonl --json
```
