# Candidate Template Output Design

## Scope

Extend `candidate-template` with an optional `--output` path so the generated
blank `resolve-links` JSON can be saved directly to disk.

## Behavior

- Without `--output`, keep existing behavior: print the template JSON.
- With `--output`, create parent directories as needed, write UTF-8 pretty JSON,
  and print a small JSON status containing the saved path and match identity.
- Do not write to the paper ledger and do not fetch independent odds or xG.

## Safety

The command writes only to the explicit file path supplied by the user. It does
not perform recursive deletion, mutation of ledger records, prediction freeze,
or settlement.
