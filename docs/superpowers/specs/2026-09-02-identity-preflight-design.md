# Identity Preflight Design

## Goal

Resolve new provider team identities early enough that the existing local
freeze process can use verified mappings without guessing.

## Design

The CLI will add a read-only `identity-preflight` command. It reads the public
Sporttery schedule once, selects open matches 120 to 180 minutes before
kickoff, and writes a replaceable local JSON queue. It does not read or append
the paper ledger.

For each selected match it runs the existing batch 433 resolver and checks
whether both teams have strictly adopted Understat aliases when the competition
is covered. The queue contains only matches requiring review, with official
identity fields, direct source URLs, capture time, and explicit reasons. A
resolved 433 detail URL is retained as a candidate, never treated as a frozen
odds observation.

The existing `schedule-driven-freeze` path remains the only automated writer of
paper predictions and retains its `review_ready` gate. A local scheduler runs
preflight every 15 minutes, while a low-frequency Codex heartbeat reviews only
queue entries still due in the next two to four hours. Confirmed aliases must
continue to be written through the existing strict alias workflow.

## Failure Handling

If 433 discovery fails, each eligible match gets a concrete discovery failure
reason. If the queue cannot be written, the command fails and the scheduler
records the failure. Matches outside the preflight window disappear from the
next queue snapshot, so stale entries cannot be reviewed after kickoff.

## Tests

Tests cover queue filtering, 433/Understat reason generation, atomic queue
output, and CLI argument forwarding. Existing freeze tests verify the freezing
path has not changed.
