# Auditable Football Probability Engine Design

## Goal

Extend the offline China Sports Lottery calibration tracker with a transparent
score-distribution model, without removing the existing immutable pre-kickoff
ledger or making network, purchase, or betting actions.

## Scope

The enhancement accepts referenced home and away expected-goals inputs and
derives a bounded scoreline probability matrix. It exposes derived 1X2,
total-goals, exact-score, half-time/full-time proxy, and Asian-handicap-style
probabilities alongside the existing named-bookmaker 1X2 baseline.

The prediction snapshot stores the market probabilities, model probabilities,
final blend, model inputs, and a version string. Existing snapshots without
the new fields remain readable. The final blend is frozen before kick-off and
is the only probability set used for settlement metrics.

## Probability Model

Use an optional `penaltyblog` dependency when installed. The first supported
model is an independent Poisson score distribution over 0 through 10 goals
per team; the bounded tail is recorded as residual mass. This deterministic
fallback requires no network and makes all current input fixtures executable.

When `penaltyblog` is available, a future model version may use its
Dixon-Coles implementation. That is deliberately not selected automatically:
the model version is an explicit input so a historical record remains
reproducible.

The xG model creates a complete score matrix, then derives:

- Home win, draw, and away win probabilities.
- The five highest-probability exact scores.
- Total-goal buckets 0-1, 2-3, and 4+.
- Handicap outcomes for a supplied home handicap.

The v1 half-time/full-time proxy splits each expected-goals value in half,
calculates independent first- and second-half score distributions, and reports
the two most likely outcome paths. It is explicitly labelled a proxy, not
historical half-time team data.

## Market Blend

Named-bookmaker European odds stay the market baseline. The user supplies a
fixed blend weight between 0 and 1 for xG model probability. The final three
outcome probability is:

`final = xg_weight * xg_model + (1 - xg_weight) * market_baseline`.

The default weight is zero, so the existing evidence-first behavior is
unchanged. No command auto-learns a weight. A later tuning command can only
be introduced after at least 200 settled records and temporal validation.

## Data and Traceability

Each enhanced snapshot requires a source URL for xG inputs, non-negative
finite xG values, and a `model_version`. Market probabilities retain their
capture hash, named bookmaker, capture time, and source URL. New structured
data is frozen into the existing content hash.

Input validation is implemented in the standard-library codebase first. A
future Pandera adapter is intentionally deferred: bringing Pandas into this
small JSONL tool provides little immediate value. Validation rules are covered
by unit tests instead.

## Evaluation

The report retains three-class Brier score and adds:

- multiclass log loss;
- expected calibration error using fixed probability bins;
- outcome accuracy only as a descriptive metric;
- scoreline log score when a final score was supplied at settlement;
- grouped outcomes by confidence bucket.

The report remains unavailable below 50 settlements. Between 50 and 199
records it is labelled observational; no model-selection result is produced.

## Compatibility and Error Handling

Legacy snapshots keep their current three-outcome settlement behavior. A
snapshot that requests xG blending but has no named-bookmaker baseline fails
validation. Scoreline inputs must be non-negative integers when supplied.

The malformed Chinese weekday-sequence regular expression is replaced with an
explicit Unicode character class, allowing the documented `周一001` through
`周日999` identifiers.

## Test Strategy

Tests are written first for each new public behavior: score-matrix
normalisation, derived markets, blend endpoints, invalid model input, frozen
snapshot structure, settlement with final score, metrics, and Chinese weekday
match IDs. Existing legacy tests must remain green.
