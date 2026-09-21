# Cycle Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute each scheduled tracker round as one logged cycle and run it every 30 minutes.

**Architecture:** The PowerShell runner owns a single mutex for the whole cycle and invokes the existing three CLI stages through a shared stage helper. Per-stage logs retain compatibility with status reporting, while one cycle summary drives the heartbeat receipt.

**Tech Stack:** PowerShell 7, Python standard-library tracker CLI, `unittest`, Codex heartbeat automation.

---

### Task 1: Cover Cycle Runner Contract

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_scheduler_script.py`
- Modify: `work/run-tai-cai-task.ps1`

- [ ] **Step 1: Write the failing test**

```python
def test_cycle_mode_runs_and_summarizes_all_required_stages(self):
    script = (Path(__file__).resolve().parents[1] / "run-tai-cai-task.ps1").read_text(
        encoding="utf-8"
    )

    self.assertIn('"cycle"', script)
    self.assertIn('("preflight", "prematch", "settle")', script)
    self.assertIn('"completed_with_failures"', script)
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `python -m unittest work.tai_cai_tracker_tests.test_scheduler_script -v`

Expected: FAIL because the runner has no `cycle` contract.

- [ ] **Step 3: Implement the cycle stage helper and summary**

Add `cycle` to the `Mode` validation list. Extract the existing mode branches
into a stage helper that returns a record with `mode`, `status`, and `output`.
In cycle mode, invoke `preflight`, `prematch`, and `settle` sequentially while
holding the mutex, append each stage record, then append a summary record whose
status is `completed` or `completed_with_failures`.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `python -m unittest work.tai_cai_tracker_tests.test_scheduler_script -v`

Expected: PASS.

### Task 2: Make The Heartbeat Use One Cycle

**Files:**
- Modify: active Codex automation `taicaitracker-3`

- [ ] **Step 1: Update the heartbeat**

Set the recurrence to 30 minutes. Replace the three independent PowerShell
commands with one `-Mode cycle` command. Require the reply to parse the last
cycle log entry and state all three phase statuses, scheduled-match counts,
freeze count, settlement count, pending count, and any failure reason.

- [ ] **Step 2: Verify the saved automation**

Read `C:\\Users\\User\\.codex\\automations\\taicaitracker-3\\automation.toml` and
confirm the active interval is 30 minutes and the prompt invokes `cycle`.

### Task 3: Regression Verification

**Files:**
- Test: `work/tai_cai_tracker_tests/test_scheduler_script.py`

- [ ] **Step 1: Run scheduler tests**

Run: `python -m unittest work.tai_cai_tracker_tests.test_scheduler_script -v`

Expected: PASS.

- [ ] **Step 2: Run full tracker tests**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -v`

Expected: PASS with no test failures.

- [ ] **Step 3: Parse the latest scheduler entries**

Run: `powershell.exe -NoProfile -NonInteractive -Command "$entries = Get-Content 'work\\logs\\scheduler.jsonl' -Encoding utf8 | ForEach-Object { $_ | ConvertFrom-Json }; $entries | Select-Object -Last 4 | Select-Object timestamp,mode,status | ConvertTo-Json"`

Expected: Existing and future per-phase entries remain valid JSON and can be read by status reporting.
