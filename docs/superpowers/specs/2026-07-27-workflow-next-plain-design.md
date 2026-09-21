# Workflow Next Plain Output Design

## Scope

Add `--plain` to `workflow-next` so users can read the next local workflow step
without parsing JSON.

## Behavior

- Default `workflow-next` output remains JSON.
- `workflow-next <match_id> --plain` prints:
  - current step;
  - missing fields when applicable;
  - next command to run.

## Safety

This only changes display formatting. It does not fetch websites, write files,
append ledger records, freeze predictions, settle results, purchase, or handle
funds.
