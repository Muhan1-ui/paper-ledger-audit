# Sales-Cutoff-Aware Freeze Design

## Goal

Prevent automatic paper freezes from missing late fixtures because the normal
pre-kickoff collection window starts after the official Sporttery sale status
has changed from open to suspended.

## Scope

This changes local task timing only. It does not loosen the evidence gate,
alter immutable ledger records, infer official data, or treat a local timing
policy as Sporttery evidence.

## Time Policy

The policy uses `Asia/Shanghai` and the local calendar date on which the task
runs (the sales day), not the fixture's kickoff date. This handles a Friday
night sale cutoff for a Saturday-after-midnight fixture.

- Monday through Friday: 22:00 sale cutoff.
- Saturday and Sunday: 23:00 sale cutoff.
- The policy is explicit, local, and marked as operator-observed. It is
  configurable outside the immutable paper ledger.

For each fixture, the normal target remains the configured pre-kickoff window.
When that target would occur after the sales cutoff, the task instead uses a
sale-cutoff window ending fifteen minutes before cutoff and beginning forty-five
minutes before cutoff. A fixture may only be collected while Sporttery reports
status `2` (open for sale) and before kickoff.

Examples:

- A weekday 23:55 fixture uses the 21:15-21:45 collection window.
- A weekday 21:00 fixture keeps its normal pre-kickoff window.
- A weekend late fixture uses the corresponding 23:00 cutoff window.

## Components

1. Add a small scheduling helper that derives the sales-day cutoff and assigns
   each official fixture one timing basis: `kickoff_window` or
   `sales_cutoff_window`.
2. Make `schedule-driven-freeze` use that helper while retaining the current
   official-open-status filter and evidence quality gate.
3. Make `identity-preflight` enter identity work early enough for fixtures that
   must freeze before a sales cutoff.
4. Record concrete local reasons in scheduler results, including
   `official_sale_pending`, `official_sales_suspended`, and
   `outside_freeze_window`, with timing basis and cutoff where relevant.
5. Extend the daily status report so a skipped late fixture is visible with its
   precise official sales status and local timing reason.
6. Update the PowerShell runner and documentation to use the new default policy
   without adding a separate polling task.

## Data and Audit Rules

The immutable `paper_ledger.jsonl` remains append-only and contains a frozen
prediction only when the existing `review_ready` gate passes. Scheduler logs
may contain the local policy, computed cutoff, and observed official sale
status, but they are operational records, not evidence substitutions.

If Sporttery reports pending or suspended sales, the task records the reason
and does not collect external evidence or freeze a prediction. If the regular
window is still feasible, it remains preferred because it is closer to kickoff.

## Error Handling

Invalid cutoff configuration must fail before a collection run. Missing or
unparseable official kickoff data remains a concrete skip reason. An ambiguous
or unavailable official sale status remains ineligible; no status is inferred
from the local cutoff policy.

## Verification

Regression coverage must prove that:

- a weekday late fixture is eligible before the 22:00 cutoff window;
- the same fixture is excluded after cutoff or when official status is
  suspended;
- weekend cutoff timing is 23:00;
- an ordinary fixture keeps the normal kickoff-based window;
- the command, runner, and status report preserve auditable timing reasons.

Run the full tracker suite with:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```
