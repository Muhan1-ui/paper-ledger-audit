# Workflow Next Plain Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a human-readable `--plain` output mode to `workflow-next`.

**Architecture:** Add a pure formatter in `outputs/tai_cai_tracker.resolver`, then expose it through the existing CLI command.

**Tech Stack:** Python standard library, existing `outputs.tai_cai_tracker` CLI style, `unittest`.

---

### Task 1: Plain Formatter

**Files:**
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Test: `work/tai_cai_tracker_tests/test_resolver.py`

- [ ] **Step 1: Write failing test**

Add a test for `format_workflow_next` on a missing-candidate result.

- [ ] **Step 2: Run resolver tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: fail because `format_workflow_next` is missing.

- [ ] **Step 3: Implement formatter**

Add `format_workflow_next(result)` returning a concise multiline string.

- [ ] **Step 4: Run resolver tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: OK.

### Task 2: CLI Flag And Verification

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `outputs/tai_cai_tracker/README.md`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing CLI test**

Add a test that `workflow-next --help` includes `--plain`.

- [ ] **Step 2: Run targeted test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_workflow_next_command_is_exposed -v
```

Expected: fail until `--plain` is added.

- [ ] **Step 3: Implement CLI flag and docs**

Add `--plain`; use formatter when present. Document usage.

- [ ] **Step 4: Full verification**

Run:

```powershell
python -m unittest discover -s work\tai_cai_tracker_tests -t . -v
python -m outputs.tai_cai_tracker.cli report outputs\tai_cai_tracker\data\paper_ledger.jsonl --json
```

Expected: all tests pass and the report remains read-only.
