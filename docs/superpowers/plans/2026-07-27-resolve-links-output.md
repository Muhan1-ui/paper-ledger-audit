# Resolve Links Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--output` to `resolve-links` so resolved candidate rankings can be saved directly.

**Architecture:** Keep `outputs.tai_cai_tracker.resolver.resolve_links` unchanged. Add a CLI output branch that writes the existing resolver result to an explicit path and returns a concise status object.

**Tech Stack:** Python standard library, existing `outputs.tai_cai_tracker` CLI style, `unittest`.

---

### Task 1: CLI Output File Support

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing test**

Add a CLI test that writes a temporary candidate JSON file, runs
`resolve-links <candidate.json> --output <tmp>/resolved/2040623.json`, and
asserts that the output file contains the resolved candidate result.

- [ ] **Step 2: Run targeted test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_resolve_links_writes_output_file -v
```

Expected: fail because `--output` is missing.

- [ ] **Step 3: Implement CLI option**

Add `--output` to `resolve-links`. In `main`, if present, create the parent
directory, write JSON, and print a JSON status object.

- [ ] **Step 4: Run targeted test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_resolve_links_writes_output_file -v
```

Expected: OK.

### Task 2: Docs And Verification

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`

- [ ] **Step 1: Document `--output`**

Add an example command beside `resolve-links`.

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
