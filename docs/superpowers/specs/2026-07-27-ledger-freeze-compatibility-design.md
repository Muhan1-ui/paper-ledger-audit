# Ledger Freeze Compatibility Design

## Goal

Allow a pre-kickoff prediction produced from current China Sports Lottery public data to remain readable for subsequent full-market freezing, settlement, and reporting.

## Scope

The repair is limited to the immutable-ledger reader and the automatic-freeze identity convention.

- A frozen league-total xG record may contain the derived field `confidence_ceiling: "medium"`.
- The reader must accept that field only when it is justified by `evidence.scope: "league_total"`.
- The operational workflow uses the official visible weekday sequence, such as `周一201`, as the ledger identity. The official `mid` remains in every Sporttery source URL.

## Non-goals

- Do not modify existing JSONL records.
- Do not broaden the ledger to accept arbitrary numeric IDs in this repair.
- Do not alter model probabilities, weights, or the official-market mapping.
- Do not make purchase recommendations or perform financial actions.

## Validation

1. A fixture with league-total xG evidence and `confidence_ceiling: "medium"` is readable.
2. An invalid confidence ceiling is rejected.
3. The existing `周一201` prediction can be read and receives one all-market snapshot.
4. The complete test suite and a read-only ledger report run successfully.
