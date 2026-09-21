# Workflow Next Design

## Scope

Add a local-only `workflow-next` helper that tells the user the next command in
the candidate-link workflow for one `match_id`.

## Behavior

For a `match_id`, inspect:

- `work/candidates/<match_id>.json`
- `work/resolved/<match_id>.json`

Return:

1. If the candidate file is missing, next command is `candidate-template ... --output ...`.
2. If the candidate file exists but resolved file is missing, next command is `resolve-links ... --output ...`.
3. If resolved file exists and `link-summary` is not ready, return the missing link fields and ask the user to fix the candidate file.
4. If resolved file exists and ready, return the `pregame_bundle_command`.

## Safety

The helper only checks local files and prints JSON. It does not fetch websites,
write files, append ledger records, freeze predictions, settle results, or
handle purchase/fund operations.
