# Candidate Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CLI command that prints an editable `resolve-links` candidate JSON template for one official Sporttery match.

**Architecture:** Keep template construction in `outputs/tai_cai_tracker/resolver.py` beside link resolution. The live wrapper fetches the public Sporttery schedule and never writes to the ledger. CLI exposes a thin `candidate-template` command.

**Tech Stack:** Python standard library, existing `outputs.tai_cai_tracker` CLI style, `unittest`.

---

### Task 1: Pure Candidate Template Builder

**Files:**
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Test: `work/tai_cai_tracker_tests/test_resolver.py`

- [ ] **Step 1: Write the failing test**

Add a test that imports `candidate_template_from_match`, passes a schedule match dictionary, and asserts that official identity is copied while all candidate arrays are empty.

- [ ] **Step 2: Run resolver tests to verify failure**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: fail because `candidate_template_from_match` is missing.

- [ ] **Step 3: Implement pure builder**

Add `candidate_template_from_match(match)` to `resolver.py`.

- [ ] **Step 4: Run resolver tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: OK.

### Task 2: Live Schedule Wrapper And CLI

**Files:**
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing CLI help test**

Add a test that calls `candidate-template --help` and checks for `match_id`.

- [ ] **Step 2: Run targeted CLI test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_candidate_template_command_is_exposed -v
```

Expected: fail because command is missing.

- [ ] **Step 3: Implement wrapper and parser**

Add `collect_candidate_template(match_id, method="concern")` to `resolver.py`.
Add the `candidate-template` parser and `main` branch to `cli.py`.

- [ ] **Step 4: Run targeted CLI test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_candidate_template_command_is_exposed -v
```

Expected: OK.

### Task 3: Documentation And Verification

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`

- [ ] **Step 1: Document command**

Add `candidate-template` usage beside `resolve-links`.

- [ ] **Step 2: Run full tests**

Run:

```powershell
python -m unittest discover -s work\tai_cai_tracker_tests -t . -v
```

Expected: all tests pass.

- [ ] **Step 3: Run read-only ledger report**

Run:

```powershell
python -m outputs.tai_cai_tracker.cli report outputs\tai_cai_tracker\data\paper_ledger.jsonl --json
```

Expected: command exits 0 and does not append records.
