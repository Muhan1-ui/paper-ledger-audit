# Evidence Events And Walk-Forward Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append auditable collection observations or failures and expose a deterministic, read-only expanding-window blend evaluation.

**Architecture:** Ledger functions own strict schemas and content hashes for the two new evidence event types. The CLI validates and appends one event through the existing append path, while the report verifies and counts them without allowing them into calibration. A pure report helper evaluates each later enhanced primary prediction with weights selected from only earlier frozen records.

**Tech Stack:** Python standard library (`json`, `hashlib`, `datetime`, `decimal`), `unittest`, JSONL.

---

### Task 1: Add Immutable Evidence Event Schemas

**Files:**
- Modify: `outputs/tai_cai_tracker/ledger.py`
- Test: `work/tai_cai_tracker_tests/test_ledger.py`

- [ ] **Step 1: Write failing ledger tests**

Add a valid `source_observation` fixture and tests which assert a frozen event
has `record_type: "source_observation"`, `status: "observed"`, a 64-character
hash, and rejects tampering. Add a `collection_failure` fixture and test that
an unknown reason code is rejected.

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_ledger -v
```

Expected: import failure because `freeze_evidence_event` and
`verify_evidence_event` do not yet exist.

- [ ] **Step 3: Implement event validation and hashing**

Add `freeze_evidence_event(payload)` and `verify_evidence_event(record)`.
Require the exact observation/failure field sets described in the design,
validate URL/timestamps/non-empty strings and SHA-256 syntax, recursively
validate JSON observations, then attach status and a content hash.

- [ ] **Step 4: Verify ledger tests pass**

Run the Task 1 command again. Expected: all ledger tests pass.

### Task 2: Append And Read Evidence Events Through The CLI

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write a failing CLI test**

Use a temporary JSON input containing a valid source observation. Invoke
`evidence-record`, then assert one event line is appended and `read_records`
returns it. Tamper with the stored line and assert `read_records` raises
`ValueError`.

- [ ] **Step 2: Verify the test fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_evidence_record_appends_verified_observation -v
```

Expected: CLI rejects the unrecognised `evidence-record` command.

- [ ] **Step 3: Implement the CLI path**

Import the event helpers, have `read_records` verify both event record types,
add `evidence_record(input_path, ledger_path)`, register `evidence-record`,
and append it with `_append_record`.

- [ ] **Step 4: Verify the CLI test passes**

Run the Task 2 command again. Expected: one valid event is appended and read
back successfully.

### Task 3: Add Read-Only Walk-Forward Report Data

**Files:**
- Modify: `outputs/tai_cai_tracker/report.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Test: `work/tai_cai_tracker_tests/test_report.py`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing report tests**

Create 200 enhanced primary frozen predictions with sequential capture times
and linked outcomes. Assert the walk-forward output has 60 folds; its first
fold has 140 training hashes, excludes its test hash, and selects `1.0` when
the frozen model distribution is best. Add an event record and assert the
calibration settlement count is unchanged while event counts appear.

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_report -v
```

Expected: missing `walk_forward_weight_evaluation` or missing report key.

- [ ] **Step 3: Implement deterministic expanding-window evaluation**

Sort eligible enhanced primary pairs by `captured_at` then prediction hash.
Use the first 140 as training; for each later pair select from `0.0` through
`1.0` by training log loss with lower-weight tie-breaking, score the held-out
pair, and include fold hashes/times/model version. Return an unavailable
structured result below 200 pairs. Count and surface evidence event types in
`calibration_report`; render a concise text summary in `format_report`.

- [ ] **Step 4: Verify report tests pass**

Run the Task 3 command again. Expected: report tests pass with a deterministic
60-fold result.

### Task 4: Document And Verify The Complete Integration

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`
- Verify: `outputs/tai_cai_tracker/data/paper_ledger.jsonl`

- [ ] **Step 1: Update CLI documentation**

Document the evidence-event input shapes, `evidence-record` invocation, and
the 200-record read-only walk-forward gate. State explicitly that neither
changes a model weight or frozen prediction.

- [ ] **Step 2: Run the full suite**

Run:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

Expected: all tests pass.

- [ ] **Step 3: Run the existing-ledger report without writes**

Run:

```powershell
python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl --json
```

Expected: JSON report succeeds and the immutable ledger receives no line.
