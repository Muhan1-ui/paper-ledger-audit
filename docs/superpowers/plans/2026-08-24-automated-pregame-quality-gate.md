# Automated Pregame Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Concurrently prepare one match's evidence bundle and surface a conservative pre-freeze quality gate.

**Architecture:** Keep collection and quality classification in `pregame.py`; reuse the strict SofaScore resolver/context modules. The CLI keeps its one-match `pregame-bundle` command and only receives the richer JSON output.

**Tech Stack:** Python standard library `concurrent.futures`, existing collector modules, `unittest`.

---

### Task 1: Define And Test The Quality Gate

**Files:**
- Modify: `outputs/tai_cai_tracker/pregame.py`
- Test: `work/tai_cai_tracker_tests/test_pregame.py`

- [ ] Write failing tests for `review_ready`, `observation_only` with league-total xG/unconfirmed lineup, and a late capture `blocked` status.
- [ ] Run `python -m unittest work.tai_cai_tracker_tests.test_pregame -v` and confirm the missing quality-gate API fails.
- [ ] Implement timestamp-safe deterministic gate classification without changing probabilities or weights.
- [ ] Re-run the targeted test module.

### Task 2: Add Automatic Context And Concurrent Collection

**Files:**
- Modify: `outputs/tai_cai_tracker/pregame.py`
- Test: `work/tai_cai_tracker_tests/test_pregame.py`

- [ ] Write a failing test with patched collectors that asserts an adopted strict context is inserted into the bundle and a skipped resolver results in a schema-valid unavailable context.
- [ ] Run the targeted test and confirm failure.
- [ ] Run independent official/schedule/OddsPortal/FotMob calls in a bounded executor, then resolve and collect context only after identity exists.
- [ ] Re-run the targeted test module.

### Task 3: Document And Verify

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`
- Test: `work/tai_cai_tracker_tests`

- [ ] Document parallel collection and the three quality-gate states, including that no status is a guarantee.
- [ ] Run `python -m unittest discover -s work/tai_cai_tracker_tests -t . -v`.
- [ ] Run `python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl --json` and verify the ledger line count is unchanged.
