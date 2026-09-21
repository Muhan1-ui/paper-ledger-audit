# Sales-Cutoff-Aware Freeze Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze late, review-ready paper predictions before the observed Sporttery sales cutoff without weakening the existing evidence gate.

**Architecture:** Add a focused standard-library timing module that reads an explicit local cutoff policy and classifies each fixture as either a normal kickoff-window candidate or an early sales-cutoff candidate. The CLI will use its structured assessment for freeze eligibility, identity preflight, and auditable scheduler outcomes; the PowerShell runner will keep its existing cadence and call the same CLI defaults.

**Tech Stack:** Python standard library (`datetime`, `zoneinfo`, `json`), `unittest`, PowerShell Scheduled Tasks.

---

## File Structure

- Create: `outputs/tai_cai_tracker/sales_cutoff.py` - validates local policy and derives a fixture timing assessment without network or ledger access.
- Create: `work/sales-cutoff-policy.json` - operator-observed, editable local timing policy; not paper evidence.
- Modify: `outputs/tai_cai_tracker/cli.py` - applies assessments in automatic freeze and identity preflight, exposes precise skipped outcomes, and passes a policy path through CLI arguments.
- Modify: `work/run-tai-cai-task.ps1` - passes the single policy path to both automatic collection modes.
- Modify: `outputs/tai_cai_tracker/README.md` - documents sales-cutoff timing, local-policy status, and command options.
- Modify: `work/tai_cai_tracker_tests/test_cli.py` - regression coverage for CLI integration and daily status output.
- Create: `work/tai_cai_tracker_tests/test_sales_cutoff.py` - isolated timing-policy tests.

### Task 1: Implement the Local Timing Policy Module

**Files:**
- Create: `work/tai_cai_tracker_tests/test_sales_cutoff.py`
- Create: `outputs/tai_cai_tracker/sales_cutoff.py`
- Create: `work/sales-cutoff-policy.json`

- [x] **Step 1: Write the failing weekday late-fixture test**

```python
def test_assess_freeze_timing_uses_weekday_sales_cutoff_window_for_late_fixture():
    policy = default_policy()
    now = datetime.fromisoformat("2099-01-05T21:20:00+08:00")
    kickoff = datetime.fromisoformat("2099-01-05T23:55:00+08:00")

    result = assess_freeze_timing(kickoff, now, 60, 75, policy)

    assert result["eligible"] is True
    assert result["timing_basis"] == "sales_cutoff_window"
    assert result["sales_cutoff_at"] == "2099-01-05T22:00:00+08:00"
    assert result["window_start_at"] == "2099-01-05T21:15:00+08:00"
    assert result["window_end_at"] == "2099-01-05T21:45:00+08:00"
```

- [x] **Step 2: Run the timing test to verify it fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_sales_cutoff -v
```

Expected: FAIL because `sales_cutoff` does not exist.

- [x] **Step 3: Implement the smallest policy API**

Create `outputs/tai_cai_tracker/sales_cutoff.py` with:

```python
DEFAULT_POLICY = {
    "version": 1,
    "timezone": "Asia/Shanghai",
    "sales_day_basis": "run_date",
    "weekday_cutoff": "22:00",
    "weekend_cutoff": "23:00",
    "cutoff_window_start_minutes": 45,
    "cutoff_window_end_minutes": 15,
}

def default_policy():
    return dict(DEFAULT_POLICY)

def load_policy(path):
    # Merge an on-disk JSON object with no implicit fields, validate every key,
    # and return normalized cutoff strings and positive integer offsets.
    ...

def assess_freeze_timing(kickoff, now, min_lead_minutes, max_lead_minutes, policy):
    # Convert both inputs to policy timezone. Build the cutoff from now.date(),
    # selecting weekend_cutoff only for Saturday/Sunday. When kickoff is after
    # that cutoff, assess [cutoff-45m, cutoff-15m]; otherwise assess the normal
    # [now+min_lead, now+max_lead] range. Return the named basis, exact bounds,
    # cutoff timestamp, eligible flag, and outside_freeze_window reason.
    ...
```

Write `work/sales-cutoff-policy.json` as:

```json
{
  "version": 1,
  "timezone": "Asia/Shanghai",
  "sales_day_basis": "run_date",
  "weekday_cutoff": "22:00",
  "weekend_cutoff": "23:00",
  "cutoff_window_start_minutes": 45,
  "cutoff_window_end_minutes": 15,
  "policy_source": "operator_observed_schedule"
}
```

- [x] **Step 4: Run the weekday timing test to verify it passes**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_sales_cutoff.SalesCutoffTests.test_assess_freeze_timing_uses_weekday_sales_cutoff_window_for_late_fixture -v
```

Expected: PASS.

- [x] **Step 5: Add boundary tests before extending the module**

Add isolated tests for these exact outcomes:

```python
assert assess_freeze_timing(
    datetime.fromisoformat("2099-01-09T21:00:00+08:00"),
    datetime.fromisoformat("2099-01-09T19:50:00+08:00"), 60, 75, default_policy()
)["timing_basis"] == "kickoff_window"

assert assess_freeze_timing(
    datetime.fromisoformat("2099-01-10T23:30:00+08:00"),
    datetime.fromisoformat("2099-01-10T22:20:00+08:00"), 60, 75, default_policy()
)["sales_cutoff_at"] == "2099-01-10T23:00:00+08:00"

with self.assertRaisesRegex(ValueError, "weekday_cutoff"):
    load_policy(invalid_policy_path)
```

- [x] **Step 6: Run the timing-module test file**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_sales_cutoff -v
```

Expected: PASS.

- [x] **Step 7: Commit checkpoint when version control is available**

Run:

```powershell
git rev-parse --is-inside-work-tree
```

Expected in this workspace: command reports it is not a Git worktree, so retain the files and proceed without a commit.

### Task 2: Use Timing Assessments in Automatic Freeze Selection

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `outputs/tai_cai_tracker/cli.py`

- [x] **Step 1: Write failing integration tests for a late weekday fixture**

Add a test that patches the schedule with an open `2099-01-05T23:55:00+08:00` fixture, invokes `schedule_driven_freeze(..., now=2099-01-05T21:20:00+08:00)`, and asserts:

```python
self.assertEqual(summary["eligible_count"], 1)
self.assertEqual(summary["items"][0]["timing_basis"], "sales_cutoff_window")
self.assertEqual(summary["items"][0]["sales_cutoff_at"], "2099-01-05T22:00:00+08:00")
```

Patch `collect_auto_pregame_bundle` with `_review_ready_bundle(match)` so the assertion tests selection rather than external sources.

Add a separate test for an otherwise matching fixture with `status: "3"` at `22:20`, asserting:

```python
self.assertEqual(summary["eligible_count"], 0)
self.assertEqual(summary["skipped_by_reason"]["official_sales_suspended"], 1)
self.assertEqual(summary["skipped_items"][0]["timing_basis"], "sales_cutoff_window")
```

- [x] **Step 2: Run the two integration tests to verify they fail**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_schedule_driven_freeze_uses_sales_cutoff_window_for_late_weekday_fixture work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_schedule_driven_freeze_reports_official_sales_suspension -v
```

Expected: FAIL because the current selector only returns `sale_not_open` and has no timing metadata.

- [x] **Step 3: Implement the selector integration**

In `outputs/tai_cai_tracker/cli.py`:

```python
from .sales_cutoff import assess_freeze_timing, load_policy

def _eligible_schedule_matches(schedule, existing_records, now, min_lead_minutes,
                               max_lead_minutes, sales_cutoff_policy):
    # Assess kickoff before official sale status. For status "1", record
    # official_sale_pending; for status "3", record official_sales_suspended;
    # otherwise use official_sale_status_unavailable. Keep aggregate counters
    # and a compact skipped_items list only for fixtures that have passed or are
    # in a sales-cutoff window. Never call external collectors for these items.
    ...
```

Attach `timing_basis`, `sales_cutoff_at`, `window_start_at`, and `window_end_at`
to every eligible match and copied freeze summary. Keep `primary_prediction_exists`,
`invalid_kickoff`, and `official_match_num_unavailable` as concrete skip reasons.

Change `schedule_driven_freeze` to load the policy once, pass it to selection,
and return:

```python
{
    "sales_cutoff_policy": policy_audit_view,
    "skipped_by_reason": skipped,
    "skipped_items": skipped_items,
    # existing fields unchanged
}
```

`policy_audit_view` excludes no data because the policy has no credentials and
must include `policy_source` only when supplied by the local JSON.

- [x] **Step 4: Run the selection tests to verify they pass**

Run the command from Step 2.

Expected: PASS.

- [x] **Step 5: Run the pre-existing automatic-freeze tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_schedule_driven_freeze_collects_only_open_unfrozen_matches_in_the_lead_window work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_schedule_driven_freeze_appends_three_records_only_for_a_review_ready_bundle work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_schedule_driven_freeze_leaves_observation_only_bundle_out_of_the_ledger -v
```

Expected: PASS; these preserve ordinary-window behavior and immutable append-only writes.

### Task 3: Advance Identity Preflight and Surface Missed Sales Windows

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `outputs/tai_cai_tracker/cli.py`

- [x] **Step 1: Write failing preflight and status tests**

Add a preflight test for an open weekday 23:55 fixture at 19:30. It must appear
in the read-only queue even though it is not in the normal 120-180 minute
window, because identity work must finish before its 21:15 sales-cutoff window.

Add a daily-status log fixture whose scheduled output contains:

```json
{
  "skipped_items": [{
    "match_id": 20990101021,
    "match_num": "周一021",
    "status": "skipped",
    "reasons": ["official_sales_suspended"],
    "timing_basis": "sales_cutoff_window",
    "sales_cutoff_at": "2099-01-05T22:00:00+08:00"
  }]
}
```

Assert that `daily_freeze_status` returns this fixture in `not_frozen` with the
same concrete reason and cutoff.

- [x] **Step 2: Run these tests to verify they fail**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_identity_preflight_advances_for_late_sales_cutoff_fixture work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_daily_freeze_status_includes_sales_cutoff_skip -v
```

Expected: FAIL because preflight uses only the normal lead window and daily
status ignores `skipped_items`.

- [x] **Step 3: Implement preflight and report integration**

Use the same `assess_freeze_timing` in `identity_preflight`. An open fixture is
eligible for identity review when either its normal preflight lead range matches
or its sales-cutoff freeze window starts within the configured preflight horizon.
For the default 120-180 minute preflight, the late-fixture test at 19:30 must be
selected because 21:15 is 105 minutes away; therefore calculate against the
freeze-window start and allow the final 15-minute lead slack required by the
existing fifteen-minute task cadence.

Extend `daily_freeze_status` to read `skipped_items` with `status: "skipped"`
as well as existing blocked and observation records. Preserve `timing_basis`,
`sales_cutoff_at`, and `official_status` fields in its `not_frozen` item. Key
entries by match ID where available so a later scan replaces an older status.

- [x] **Step 4: Run the new preflight and status tests to verify they pass**

Run the command from Step 2.

Expected: PASS.

- [x] **Step 5: Run existing preflight and status regressions**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_identity_preflight_writes_only_matches_with_unresolved_provider_identities work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_identity_preflight_reuses_a_verified_cached_433_link work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_daily_freeze_status_includes_same_day_blocked_scan_not_in_ledger -v
```

Expected: PASS.

### Task 4: Expose Policy Configuration and Update the Local Scheduler

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/run-tai-cai-task.ps1`
- Modify: `outputs/tai_cai_tracker/README.md`

- [x] **Step 1: Write a failing CLI forwarding test**

Extend the `schedule-driven-freeze` command test to invoke:

```python
"--sales-cutoff-policy", "work/sales-cutoff-policy.json"
```

and assert the mocked function receives:

```python
sales_cutoff_policy_path="work/sales-cutoff-policy.json"
```

Add the equivalent expectation for `identity-preflight`.

- [x] **Step 2: Run the command forwarding tests to verify they fail**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_schedule_driven_freeze_command_forwards_window_worker_and_sales_policy_options work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_identity_preflight_command_forwards_window_and_sales_policy_options -v
```

Expected: FAIL because neither parser exposes the policy option.

- [x] **Step 3: Implement CLI, runner, and README changes**

Add this parser option to both commands:

```python
parser.add_argument(
    "--sales-cutoff-policy",
    default="work/sales-cutoff-policy.json",
    help="local operator-observed sales cutoff policy; not source evidence",
)
```

Forward it as `sales_cutoff_policy_path`. In `work/run-tai-cai-task.ps1`, add:

```powershell
$SalesCutoffPolicy = "work\sales-cutoff-policy.json"
```

and pass `--sales-cutoff-policy $SalesCutoffPolicy` to both `identity-preflight`
and `schedule-driven-freeze`. Do not add polling frequency or a new scheduled
task.

Update the README commands and automatic-workflow section to state the normal
window versus cutoff window, all values in the local JSON, that `status=2` is
still required, and that cutoff policy is not official evidence.

- [x] **Step 4: Run the forwarding tests to verify they pass**

Run the command from Step 2.

Expected: PASS.

- [x] **Step 5: Run the entire tracker suite**

Run:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

Expected: exit code 0 with every test passing.

- [x] **Step 6: Run a read-only local status report**

Run:

```powershell
python -m outputs.tai_cai_tracker.cli daily-freeze-status outputs/tai_cai_tracker/data/paper_ledger.jsonl work/logs/scheduler.jsonl
```

Expected: JSON output only; it does not append or rewrite the immutable ledger.

- [x] **Step 7: Inspect implementation changes and commit only if possible**

Run:

```powershell
git status --short
```

Expected in this workspace: Git reports that `D:\TaiCaiTracker` is not a repository. Report modified files and test evidence without claiming a commit.
