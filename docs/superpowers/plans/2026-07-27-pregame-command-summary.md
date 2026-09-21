# Pregame Command Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Include a copyable `pregame-bundle` command in ready `link-summary` output.

**Architecture:** Keep the feature inside the pure `link_summary_from_resolved` helper. The CLI automatically benefits because it prints that helper's result.

**Tech Stack:** Python standard library, existing `outputs.tai_cai_tracker` CLI style, `unittest`.

---

### Task 1: Pure Summary Command Field

**Files:**
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Test: `work/tai_cai_tracker_tests/test_resolver.py`

- [ ] **Step 1: Write failing test**

Extend the ready `link_summary_from_resolved` test to assert
`pregame_bundle_command` equals the expected one-line command.

- [ ] **Step 2: Run resolver tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: fail because `pregame_bundle_command` is missing.

- [ ] **Step 3: Implement command field**

Add a helper to quote PowerShell arguments and include the command only when
`ready` is true.

- [ ] **Step 4: Run resolver tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: OK.

### Task 2: Docs And Verification

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`

- [ ] **Step 1: Document command field**

Mention that ready `link-summary` output contains `pregame_bundle_command`.

- [ ] **Step 2: Run full tests and read-only report**

Run:

```powershell
python -m unittest discover -s work\tai_cai_tracker_tests -t . -v
python -m outputs.tai_cai_tracker.cli report outputs\tai_cai_tracker\data\paper_ledger.jsonl --json
```

Expected: all tests pass and the report remains read-only.
