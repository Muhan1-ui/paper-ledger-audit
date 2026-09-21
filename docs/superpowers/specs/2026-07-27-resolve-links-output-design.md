# Resolve Links Output Design

## Scope

Extend `resolve-links` with an optional `--output` path so resolved candidate
rankings can be saved directly after the user fills a candidate JSON file.

## Behavior

- Without `--output`, keep existing behavior: print resolved candidate ranking
  JSON to stdout.
- With `--output`, create parent directories as needed, write UTF-8 pretty JSON
  to the explicit file path, and print a compact status object.
- The saved JSON is exactly the resolver result, including skipped/ambiguous
  entries for auditability.

## Safety

This command reads a user-supplied candidate file and writes only to the
explicit output path. It does not fetch new sources, freeze predictions, append
ledger records, settle results, or perform purchase/fund operations.
