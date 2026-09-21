# Identity Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an early, read-only identity exception queue that lets the local scheduler and Codex resolve new team mappings before the normal freeze window.

**Architecture:** `identity-preflight` reuses the official schedule and batch 433 resolver, then consults the existing strict Understat alias registry. It writes a replaceable local JSON snapshot; the immutable ledger remains owned solely by the freeze and settlement commands.

**Tech Stack:** Python standard library, existing tracker collectors, PowerShell Task Scheduler, unittest.

---

### Task 1: Queue Builder

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_identity_preflight_writes_only_unresolved_matches(self):
    summary = tracker_cli.identity_preflight(queue_path, now=now)
    self.assertEqual(summary["review_count"], 1)
    self.assertEqual(json.loads(queue_path.read_text())["items"][0]["reasons"], [
        "understat_home_alias_unresolved"
    ])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.TrackerCliTests.test_identity_preflight_writes_only_unresolved_matches -v`
Expected: `ERROR` because `identity_preflight` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
def identity_preflight(output_path, min_lead_minutes=120, max_lead_minutes=180, now=None,
                       source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH):
    # Select open official fixtures, discover 433 candidates, and queue only
    # unresolved provider identities with a traceable capture time.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.TrackerCliTests.test_identity_preflight_writes_only_unresolved_matches -v`
Expected: `OK`.

### Task 2: CLI And Scheduler

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/run-tai-cai-task.ps1`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write the failing CLI forwarding test**

```python
tracker_cli.main(["identity-preflight", "queue.json", "--min-lead-minutes", "120"])
preflight.assert_called_once_with("queue.json", min_lead_minutes=120, max_lead_minutes=180)
```

- [ ] **Step 2: Run it and confirm the parser rejects the unknown command**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.TrackerCliTests.test_identity_preflight_command_forwards_options -v`
Expected: `FAIL`.

- [ ] **Step 3: Add parser and PowerShell `preflight` mode**

```powershell
& $Python -m outputs.tai_cai_tracker.cli identity-preflight $IdentityQueue `
  --min-lead-minutes 120 --max-lead-minutes 180
```

- [ ] **Step 4: Run targeted tests**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli -v`
Expected: `OK`.

### Task 3: Verification

**Files:**
- Verify: `work/run-tai-cai-task.ps1`
- Verify: `work/logs/identity-review-queue.json`

- [ ] **Step 1: Parse the PowerShell script**

Run: `[System.Management.Automation.Language.Parser]::ParseFile(...)`
Expected: no parser errors.

- [ ] **Step 2: Run an actual preflight**

Run: `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\work\run-tai-cai-task.ps1 -Mode preflight`
Expected: scheduler log contains `completed`; the ledger is unchanged.

- [ ] **Step 3: Run complete tracker tests**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -t . -v`
Expected: all tests pass.
