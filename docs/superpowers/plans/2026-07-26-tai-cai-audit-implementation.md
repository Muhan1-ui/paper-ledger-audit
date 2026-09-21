# Tai Cai Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the paper-only tracker freeze complete official China Sports Lottery market tables, preserve per-market evidence timing, and prevent same-match strategy variants from inflating aggregate calibration.

**Architecture:** Keep the immutable JSONL ledger. Add a versioned all-market snapshot shape whose frozen entries carry complete fixed-prize mappings plus each market's publication time and official source. Add a prediction role that separates one primary prediction from comparison observations, then have aggregate reporting use primary records only while strategy reporting keeps both roles visible.

**Tech Stack:** Python 3 standard library, `decimal.Decimal`, `unittest`, JSONL.

---

### Task 1: Make the test suite reproducible

**Files:**
- Create: `work/__init__.py`
- Create: `work/tai_cai_tracker_tests/__init__.py`
- Modify: `outputs/tai_cai_tracker/README.md`

- [ ] **Step 1: Write the expected one-command test invocation in the README**

Add this command under a `Verification` heading:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

- [ ] **Step 2: Run the command and verify it fails before package markers exist**

Run:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

Expected: `ImportError: Start directory is not importable`.

- [ ] **Step 3: Add empty package markers**

Create these files with no executable code:

```python
# work/__init__.py
```

```python
# work/tai_cai_tracker_tests/__init__.py
```

- [ ] **Step 4: Run the one-command suite and verify green**

Run:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

Expected: all existing tests run and pass without setting `PYTHONPATH`.

### Task 2: Add tests for complete official-market prize tables

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `work/tai_cai_tracker_tests/test_ledger.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `outputs/tai_cai_tracker/ledger.py`

- [ ] **Step 1: Write a failing CLI workflow test for all five markets**

Add a test that first freezes an enhanced prediction with a full score matrix,
then runs `all-market-snapshot` with this input shape:

```python
{
    "prediction_hash": prediction_hash,
    "captured_at": "2026-07-24T18:30:00+08:00",
    "official_markets": {
        "spf": {
            "published_at": "2026-07-24T18:00:00+08:00",
            "official_source_url": "https://lottery.example.test/match",
            "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "4"},
        },
        "hhad": {
            "published_at": "2026-07-24T18:01:00+08:00",
            "official_source_url": "https://lottery.example.test/match",
            "home_handicap": "-1",
            "fixed_prizes": {"home_win": "4", "draw": "3", "away_win": "2"},
        },
        "crs": {"published_at": "2026-07-24T18:02:00+08:00", "official_source_url": "https://lottery.example.test/match", "fixed_prizes": COMPLETE_CRS_PRIZES},
        "ttg": {"published_at": "2026-07-24T18:03:00+08:00", "official_source_url": "https://lottery.example.test/match", "fixed_prizes": COMPLETE_TTG_PRIZES},
        "hafu": {"published_at": "2026-07-24T18:04:00+08:00", "official_source_url": "https://lottery.example.test/match", "fixed_prizes": COMPLETE_HAFU_PRIZES},
    },
}
```

Define `COMPLETE_CRS_PRIZES`, `COMPLETE_TTG_PRIZES`, and
`COMPLETE_HAFU_PRIZES` by iterating the declared option tuples and assigning
unique positive `Decimal` values. Assert that all five entries are frozen and
their prize keys equal their official option sets.

- [ ] **Step 2: Run the focused test and verify the current CLI fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_all_market_snapshot_accepts_complete_official_prize_tables -v
```

Expected: failure because the current command expects `fixed_prizes` at the
root and calls `_odds()` for CRS, TTG, and HAFU.

- [ ] **Step 3: Write failing ledger validation tests**

Add tests proving that a version-2 frozen entry requires `published_at` and
`official_source_url`, rejects a post-kickoff publication time, and rejects a
CRS prize map with one missing option. Keep a legacy version-1 fixture and
assert it remains valid.

- [ ] **Step 4: Run the focused ledger tests and verify red**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_ledger.LedgerTests.test_version_two_market_entry_requires_complete_prizes -v
```

Expected: failure because version-2 fields and rules do not exist.

- [ ] **Step 5: Implement a generic option-mapped prize parser**

In `outputs/tai_cai_tracker/cli.py`, add a helper with this contract:

```python
def _prize_mapping(prizes, option_order, field_name):
    if not isinstance(prizes, Mapping) or set(prizes) != set(option_order):
        raise ValueError(f"{field_name} must cover exactly the declared options")
    return {option: _decimal(prizes[option], f"{field_name} {option}") for option in option_order}
```

Use `_odds()` only for three-outcome compatibility paths. Change
`all_market_snapshot()` to consume `official_markets`; derive each market's
prize map with `_prize_mapping()` and add the market-specific source and
publication values to the frozen entry.

- [ ] **Step 6: Implement versioned ledger validation**

In `outputs/tai_cai_tracker/ledger.py`, keep legacy snapshots valid when
`schema_version` is absent. For `schema_version: 2`, require each frozen entry
to include `published_at` and `official_source_url`; validate both and require
publication before that snapshot's kickoff. Preserve the existing probability,
selection, and complete-prize-map validation.

- [ ] **Step 7: Run focused tests and then the full suite**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_all_market_snapshot_accepts_complete_official_prize_tables -v
python -m unittest work.tai_cai_tracker_tests.test_ledger -v
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

Expected: the new all-market workflow and all prior tests pass.

### Task 3: Isolate primary and comparison predictions

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `work/tai_cai_tracker_tests/test_report.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `outputs/tai_cai_tracker/ledger.py`
- Modify: `outputs/tai_cai_tracker/report.py`

- [ ] **Step 1: Write failing snapshot tests for roles**

Add tests for these cases:

```python
# A first primary snapshot succeeds.
# A second primary snapshot with the same match_id raises ValueError.
# A comparison snapshot with the same match_id succeeds.
# Legacy records with no role remain readable.
```

Use two different valid capture timestamps before kickoff so the test proves
identity handling rather than timestamp collision handling.

- [ ] **Step 2: Run the role tests and verify red**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_rejects_second_primary_for_same_match -v
```

Expected: failure because prediction roles are not yet represented.

- [ ] **Step 3: Write a failing calibration isolation test**

Create one settled primary prediction and one settled comparison prediction for
the same match. Assert `calibration_report()` has `settled_records == 1` for
aggregate calibration and a strategy breakdown that reports both strategies
with their own denominators.

- [ ] **Step 4: Implement roles and primary uniqueness**

Allow optional `prediction_role` values `primary` and `comparison` in a new
snapshot input; default omitted new inputs to `primary`. In `snapshot()`, scan
prior prediction records for the same `match_id` with role `primary` or a
legacy missing role, and reject a new primary when one exists. Do not reject a
comparison record.

Treat missing roles as legacy primary in aggregate reporting so existing seven
settlements retain their historical denominator. Filter aggregate score and
calibration calculations to primary/legacy-primary records. Leave strategy
breakdowns able to show both roles and add the role to their labels.

- [ ] **Step 5: Run role and report regression tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli -v
python -m unittest work.tai_cai_tracker_tests.test_report -v
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

Expected: duplicate primaries are rejected, comparison records remain
observable, and aggregate metrics are not double-counted.

### Task 4: Preserve xG derivation evidence

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `outputs/tai_cai_tracker/README.md`

- [ ] **Step 1: Write a failing enhanced-snapshot evidence test**

Use `prediction_from_input()` with `xg_inputs.evidence` and assert the frozen
prediction contains the source URL, timezone-aware publication time,
competition, season, sample-match count, scope, source xG/xGA values, and
formula. Add a separate test that `scope: league_total` results in
`confidence_ceiling: medium` and that an invalid scope or naive timestamp is
rejected.

- [ ] **Step 2: Run the test and verify red**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_enhanced_snapshot_preserves_xg_evidence -v
```

Expected: failure because `_optional_xg()` currently rejects the `evidence`
field.

- [ ] **Step 3: Implement evidence validation and storage**

Extend `_optional_xg()` to accept `evidence` only on enhanced Poisson inputs.
Validate this exact evidence shape:

```python
{
    "source_url": "https://...",
    "published_at": "2026-07-26T17:53:00+08:00",
    "competition": "K League 1",
    "season": "2026",
    "sample_matches": 19,
    "scope": "home_away_split",
    "home_attack_xg": "31.4",
    "home_defense_xga": "23.3",
    "away_attack_xg": "34.3",
    "away_defense_xga": "26.1",
    "formula": "mean(home_attack_xg/sample_matches, away_defense_xga/sample_matches)",
}
```

Reject publication timestamps after capture time. Persist the evidence in the
frozen prediction and add `confidence_ceiling: "medium"` when the scope is
`league_total`.

- [ ] **Step 4: Document the evidence contract and run all tests**

Update `README.md` with the evidence example and the rule that no automatic
home advantage is invented for league-total data. Run:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl
```

Expected: all tests pass and the existing ledger remains readable.

### Task 5: Verify the delivered behavior

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`
- Modify: `docs/superpowers/specs/2026-07-26-tai-cai-audit-design.md` only if verification exposes a documented mismatch.

- [ ] **Step 1: Run a real-data read-only verification**

Run:

```powershell
python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl --json
```

Expected: existing immutable records validate; report keeps its 50-settlement
calibration gate.

- [ ] **Step 2: Check requirements against the approved design**

Confirm: complete prize maps, per-market publication times and sources,
skipped-market reasons, primary/comparison isolation, xG evidence, and a
reproducible test command are each covered by a passing regression test.

- [ ] **Step 3: Record the verification outcome in the final handoff**

Report the exact test count, ledger validation result, remaining sample-size
limit, and the fact that the tool remains paper-only.

**Note:** This workspace is not a Git repository, so no commit steps are
included.
