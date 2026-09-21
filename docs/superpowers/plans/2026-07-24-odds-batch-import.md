# Odds Batch Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add offline batch import of auditable European 1X2 odds and use a named-bookmaker capture as the optional frozen-prediction probability baseline.

**Architecture:** Add an immutable `odds_capture` record validator beside the existing ledger validators. The CLI imports a JSON batch into the same JSONL ledger and resolves a named capture hash when snapshot input includes `european_odds`; otherwise legacy fixed-prize behavior remains unchanged.

**Tech Stack:** Python standard library, `unittest`, JSONL, `Decimal`.

---

### Task 1: Add auditable odds-capture records

**Files:**
- Modify: `outputs/tai_cai_tracker/ledger.py`
- Test: `work/tai_cai_tracker_tests/test_ledger.py`

- [ ] **Step 1: Write failing capture-record tests**

```python
from outputs.tai_cai_tracker.ledger import freeze_odds_capture

VALID_CAPTURE = {
    "provider": "manual-export",
    "match_id": "周五201",
    "kickoff_at": "2026-07-25T00:00:00+08:00",
    "captured_at": "2026-07-24T14:00:00+08:00",
    "source_url": "https://odds.example.test/jaro-sjk",
    "bookmaker": "ExampleBook",
    "source_quality": "named_bookmaker",
    "odds": {"home_win": Decimal("2.88"), "draw": Decimal("3.50"), "away_win": Decimal("2.15")},
}

def test_freezes_named_bookmaker_capture():
    record = freeze_odds_capture(VALID_CAPTURE)
    assert record["record_type"] == "odds_capture"
    assert record["status"] == "captured"

def test_rejects_unattributed_capture_as_baseline_later():
    record = freeze_odds_capture({**VALID_CAPTURE, "source_quality": "unattributed"})
    assert record["source_quality"] == "unattributed"
```

- [ ] **Step 2: Run the focused ledger test and confirm the missing function fails**

Run: `python -m unittest work.tai_cai_tracker_tests.test_ledger -v`

Expected: failure importing `freeze_odds_capture`.

- [ ] **Step 3: Implement capture validation and freezing**

```python
def freeze_odds_capture(capture):
    _validate_odds_capture(capture)
    record = copy.deepcopy(dict(capture))
    record["record_type"] = "odds_capture"
    record["status"] = "captured"
    record["content_hash"] = _content_hash(record)
    return record
```

Implement `_validate_odds_capture` using the existing match-ID, timestamp, URL,
and `OUTCOME_LABELS` conventions. Allow only `named_bookmaker` and
`unattributed`; require positive finite `Decimal` odds; require capture time
strictly before kickoff.

- [ ] **Step 4: Re-run the focused ledger test**

Run: `python -m unittest work.tai_cai_tracker_tests.test_ledger -v`

Expected: PASS.

### Task 2: Import a no-network JSON batch

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write a failing CLI import test**

```python
def test_odds_import_appends_named_bookmaker_capture(self):
    payload = {
        "provider": "manual-export",
        "captures": [{
            "match_id": "周五201",
            "kickoff_at": "2026-07-25T00:00:00+08:00",
            "captured_at": "2026-07-24T14:00:00+08:00",
            "source_url": "https://odds.example.test/jaro-sjk",
            "bookmaker": "ExampleBook",
            "source_quality": "named_bookmaker",
            "odds": {"home_win": "2.88", "draw": "3.50", "away_win": "2.15"},
        }],
    }
    # Write payload to a temporary JSON path, run `odds-import`, then assert
    # one JSONL record with record_type == "odds_capture".
```

Use a complete capture with the Task 1 fields and no external URLs beyond the
literal test URL.

- [ ] **Step 2: Run the focused CLI test and confirm the unknown command fails**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_odds_import_appends_named_bookmaker_capture -v`

Expected: FAIL because `odds-import` is not registered.

- [ ] **Step 3: Implement batch parsing and the `odds-import` subcommand**

```python
def odds_import(input_path, ledger_path):
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    provider = payload["provider"]
    captures = payload["captures"]
    records = [freeze_odds_capture({**capture, "provider": provider}) for capture in captures]
    for record in records:
        _append_record(ledger_path, record)
    return records
```

Reject a non-object batch, an empty/non-list `captures`, or a capture that
tries to override the top-level provider. Convert `odds` values with existing
`_decimal` before freezing. Add `odds-import input_path ledger_path` to the
argparse subcommands and print imported records as JSON.

- [ ] **Step 4: Re-run the focused CLI test**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_odds_import_appends_named_bookmaker_capture -v`

Expected: PASS.

### Task 3: Use imported European odds as a snapshot baseline

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `work/tai_cai_tracker_tests/test_model.py`

- [ ] **Step 1: Write a failing snapshot test using a named capture hash**

```python
def test_snapshot_uses_named_european_odds_capture_for_probabilities(self):
    # Import a named capture, include its content_hash in snapshot input as
    # {"capture_hash": capture_hash, "source_url": "https://odds.example.test/jaro-sjk"}, then assert that outcomes
    # equal normalize_fixed_prizes(capture odds), not fixed_prizes.
```

Also write a test that an `unattributed` capture hash is rejected.

- [ ] **Step 2: Run the focused snapshot tests and confirm failure**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli -v`

Expected: FAIL because snapshot cannot resolve an imported capture.

- [ ] **Step 3: Implement capture resolution for snapshot**

```python
def prediction_from_input(payload, odds_captures=None):
    european_odds = payload.get("european_odds")
    if european_odds is None:
        probabilities = normalize_fixed_prizes(prizes)
    else:
        capture = _resolve_named_capture(european_odds, odds_captures, payload)
        probabilities = normalize_fixed_prizes([capture["odds"][label] for label in OUTCOME_LABELS])
```

Make `snapshot` read prior ledger records, select only `odds_capture` records,
and require a referenced capture to have the same match ID, a `named_bookmaker`
quality label, an earlier capture timestamp, and the same S2 URL declared in
the snapshot. Persist `european_odds` metadata with the frozen prediction.

- [ ] **Step 4: Re-run focused snapshot tests**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli -v`

Expected: PASS.

### Task 4: Document the offline workflow and verify regression coverage

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`
- Modify: `outputs/tai_cai_tracker/example_input.json`
- Create: `outputs/tai_cai_tracker/example_odds_import.json`

- [ ] **Step 1: Add a failing end-to-end test using example import and snapshot files**

```python
def test_example_odds_import_then_snapshot_uses_the_imported_capture(self):
    # Import the new hypothetical example, inject its printed capture hash into
    # a copied example snapshot, and assert a frozen prediction is appended.
```

- [ ] **Step 2: Run the complete suite and confirm the example test fails**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -v`

Expected: FAIL because the example import file and documentation do not exist.

- [ ] **Step 3: Add hypothetical examples and README instructions**

Document `odds-import` followed by `snapshot`, state that the tool does not
call an API or scrape a webpage, and explain that only named-bookmaker captures
can form a baseline. Keep all supplied URLs under `.example.test`.

- [ ] **Step 4: Run the full regression suite**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -v`

Expected: all existing and new tests PASS with zero failures.
