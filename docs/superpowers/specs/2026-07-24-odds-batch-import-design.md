# Odds Batch Import Design

## Purpose

Make the paper-only tracker accept a batch of auditable European 1X2 odds
captures without requiring an API key, browser automation, or betting action.
European odds become the probability baseline. China Sports Lottery remains the
official source for match identity, fixed prizes, and settlement.

## Input

Add an `odds_import` CLI command that reads a JSON file containing a provider
name and one or more captures. Each capture contains a lottery match ID, an
ISO-8601 capture timestamp, a source URL, a named bookmaker, 1X2 decimal odds,
and a source-quality label.

Only `named_bookmaker` quality can serve as a European market baseline. The
other allowed value, `unattributed`, is retained as an observation but cannot
be used to create a frozen prediction.

## Storage

The command appends immutable `odds_capture` records to the existing JSONL
ledger. Each record has its own content hash. Captures do not alter prediction
or settlement records.

## Prediction Construction

`snapshot` gains an optional `european_odds` object. When present, it must cite
an imported capture hash with `named_bookmaker` quality. Its reciprocal 1X2
prices are normalized to the frozen prediction probabilities. The existing
`fixed_prizes` field is still mandatory and is retained as the official China
Sports Lottery evidence; it no longer sets probabilities when European odds
are supplied.

Without `european_odds`, legacy snapshots continue to use fixed prizes so
existing paper records remain readable and reproducible.

## Validation And Safety

- The importer accepts no API key and makes no HTTP requests.
- All source URLs must be HTTP(S), timestamps timezone-aware, and captures
  strictly before kick-off.
- Decimal odds must be finite and positive.
- A capture with no named bookmaker is observation-only.
- Snapshot validation verifies that the referenced capture exists, is named,
  matches the lottery ID, and predates kick-off.

## Testing

Tests cover an accepted named-bookmaker capture, rejection of invalid source
quality and invalid odds, observation-only handling of unattributed captures,
European-odds probability selection, and capture-reference validation.
