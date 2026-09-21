# Quality Tiering Design

## Goal

Allow a traceable, adequately sampled `league_total` xG model to become a
medium-confidence paper snapshot while retaining strict blocks for incomplete,
late, or low-sample evidence.

## Decision

`quality_gate` will distinguish source completeness from confidence. Complete
official five-market data, named 1X2 odds, pre-kickoff evidence, and xG with a
minimum of eight samples per side are sufficient for `review_ready`.
`league_total` xG and unconfirmed lineups remain explicit risk reasons and keep
the frozen confidence ceiling at `medium`; they do not by themselves block a
paper record. A sample below eight remains `observation_only`.

## Scope

This changes only the eligibility classification used by the pregame bundle and
schedule-driven freeze flow. It does not change Poisson formulas, source
collection, settlement, calibration thresholds, or the immutable-ledger model.
