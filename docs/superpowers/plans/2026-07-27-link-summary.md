# Link Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `link-summary` to extract pregame-bundle-ready URLs from a saved `resolve-links` result.

**Architecture:** Keep extraction in `outputs/tai_cai_tracker/resolver.py` as pure JSON helpers. Add a thin CLI command that reads a local resolved JSON file and prints the summary.

**Tech Stack:** Python standard library, existing `outputs.tai_cai_tracker` CLI style, `unittest`.

---

### Task 1: Pure Summary Extraction

**Files:**
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Test: `work/tai_cai_tracker_tests/test_resolver.py`

- [ ] **Step 1: Write failing tests**

Add tests for `link_summary_from_resolved`: one ready case with three candidate
URLs, and one not-ready case where a candidate is skipped.

- [ ] **Step 2: Run resolver tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: fail because `link_summary_from_resolved` is missing.

- [ ] **Step 3: Implement helper**

Add `link_summary_from_resolved(resolved)` and `link_summary(input_path)`.

- [ ] **Step 4: Run resolver tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: OK.

### Task 2: CLI Command And Verification

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `outputs/tai_cai_tracker/README.md`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing CLI help test**

Add a test for `link-summary --help`.

- [ ] **Step 2: Run targeted test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_link_summary_command_is_exposed -v
```

Expected: fail because command is missing.

- [ ] **Step 3: Implement CLI command and docs**

Add parser and main branch. Document usage.

- [ ] **Step 4: Full verification**

Run:

```powershell
python -m unittest discover -s work\tai_cai_tracker_tests -t . -v
python -m outputs.tai_cai_tracker.cli report outputs\tai_cai_tracker\data\paper_ledger.jsonl --json
```

Expected: all tests pass and the report is read-only.
