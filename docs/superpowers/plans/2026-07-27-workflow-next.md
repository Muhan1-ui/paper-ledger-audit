# Workflow Next Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `workflow-next` command that reports the next local workflow step for one match.

**Architecture:** Implement pure path-inspection logic in `outputs/tai_cai_tracker/resolver.py`. Add a thin CLI command in `outputs/tai_cai_tracker/cli.py`.

**Tech Stack:** Python standard library, existing `outputs.tai_cai_tracker` CLI style, `unittest`.

---

### Task 1: Pure Workflow State Helper

**Files:**
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Test: `work/tai_cai_tracker_tests/test_resolver.py`

- [ ] **Step 1: Write failing tests**

Add tests for missing candidate file, missing resolved file, not-ready resolved
file, and ready resolved file.

- [ ] **Step 2: Run resolver tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: fail because `workflow_next` is missing.

- [ ] **Step 3: Implement helper**

Add `workflow_next(match_id, base_dir="work")`.

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

Add a test for `workflow-next --help`.

- [ ] **Step 2: Run targeted test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_workflow_next_command_is_exposed -v
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

Expected: all tests pass and the report remains read-only.
