# Source Performance Breakdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Report auditable source availability and source-stratified descriptive calibration diagnostics.

**Architecture:** Implement pure report helpers over already validated ledger records. Add the resulting object to both gated and available calibration reports without changing ledger schemas or snapshot behavior.

**Tech Stack:** Python standard library, Decimal metrics, unittest.

---

### Task 1: Source Labels And Performance Tests

**Files:**
- Modify: `outputs/tai_cai_tracker/report.py`
- Test: `work/tai_cai_tracker_tests/test_report.py`

- [ ] Write a failing test with one named European provider, a frozen FotMob xG URL, a high context ceiling, and source observation/failure events. Assert explicit source labels and that one settlement remains descriptive.
- [ ] Run `python -m unittest work.tai_cai_tracker_tests.test_report -v` and confirm the missing report key fails.
- [ ] Implement pure source-label, event aggregation, and settled-pair metrics helpers.
- [ ] Re-run the targeted report tests.

### Task 2: Document And Verify

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`
- Test: `work/tai_cai_tracker_tests`

- [ ] Document `source_performance` and its 50-settlement interpretation gate.
- [ ] Run the full unit suite and a read-only JSON report against the existing ledger.
