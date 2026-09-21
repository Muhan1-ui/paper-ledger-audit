# Candidate Template Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--output` to `candidate-template` so a blank candidate JSON file can be saved directly.

**Architecture:** Keep template generation unchanged. Add a small CLI output branch that writes the already generated JSON to the explicit path and returns a concise status object.

**Tech Stack:** Python standard library, existing `outputs.tai_cai_tracker` CLI style, `unittest`.

---

### Task 1: CLI Output File Support

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing test**

Add a CLI test that patches `outputs.tai_cai_tracker.resolver.collect_schedule`,
runs `candidate-template 2040623 --output <tmp>/candidates/2040623.json`, and
asserts that the file contains the candidate template.

- [ ] **Step 2: Run targeted test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_candidate_template_writes_output_file -v
```

Expected: fail because `--output` is missing.

- [ ] **Step 3: Implement CLI option**

Add `--output` to the parser. In `main`, if present, create the parent
directory, write JSON, and print a JSON status object.

- [ ] **Step 4: Run targeted test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_candidate_template_writes_output_file -v
```

Expected: OK.

### Task 2: Docs And Verification

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`

- [ ] **Step 1: Document `--output`**

Add an example command beside `candidate-template`.

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
