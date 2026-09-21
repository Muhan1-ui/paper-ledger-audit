# Cycle Scheduler Design

## Goal

Run each scheduled paper-monitoring round as one auditable cycle so a missing
agent reply cannot silently skip preflight, pre-match freezing, or official
settlement.

## Scope

This phase changes only scheduling orchestration. It does not change the
immutable ledger schema, evidence quality gate, model, source access rules, or
freeze criteria.

## Design

`work/run-tai-cai-task.ps1` gains a `cycle` mode. It acquires the existing
project mutex once, executes `preflight`, `prematch`, and `settle` in that
order, and writes one normal scheduler entry for each phase. It then appends a
cycle summary entry containing the three phase statuses.

Each phase remains independently runnable with its existing mode. A failed
preflight must not prevent the same cycle from attempting pre-match freezing
and settlement. A failed pre-match phase must not prevent official settlement.
The cycle result is `completed` only when every phase completes; otherwise it
is `completed_with_failures` and names the failed phases.

The active Codex heartbeat runs only the `cycle` command every 30 minutes. It
must always read the final cycle entry and produce a Chinese execution receipt,
including any stage failure or mutex contention. The cadence gives at least six
attempts within the 15-to-210-minute collection window while preserving the
existing per-provider request limits.

`daily-freeze-status` continues to read the per-phase `prematch` entries, so
it remains compatible without reconstructing operational state from a cycle
summary.

## Failure Handling

If the mutex is held, the script appends exactly one `skipped_busy` cycle entry
and exits successfully. No overlapping collection is attempted. Native-command
output is kept as UTF-8 in every scheduler entry. Each phase records its native
output or its concrete error before the cycle moves to the next phase.

## Validation

Regression tests assert that the runner supports `cycle`, keeps the 15-to-210
minute pre-match window, preserves UTF-8 output, and records all three stage
names. The full tracker test suite remains green. A manual status read verifies
that scheduler entries stay parseable by `daily-freeze-status`.
