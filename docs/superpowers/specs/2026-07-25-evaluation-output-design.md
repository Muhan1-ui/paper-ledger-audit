# Evaluation Output Design

## Goal

Make the tracker report probability-quality diagnostics in a form that lets a
user compare market-only and enhanced Poisson records without treating small
samples as evidence of a superior model.

## Record Classification

Settled predictions without a `model` object are classified as
`market_baseline`. Enhanced predictions are classified as
`<model version>@<weight>`, for example `poisson-v1@0.40`. Classification
uses only frozen prediction fields and never infers a strategy from the
settled outcome.

## Report Structure

When the existing 50-settlement visibility gate is reached, the report adds a
`strategy_breakdown` object. Each strategy contains its settled count,
three-class Brier average, multiclass log-loss average, and top-pick accuracy.
Any available exact-score log-loss is reported only for that strategy's
scoreline-evaluated records.

The existing overall metrics remain the primary summary. A strategy group is
always descriptive. It has no automatic selection, no comparison verdict, and
no automatic change to a model weight. The top-level `phase` continues to
communicate whether the dataset is observational or eligible for later
temporal validation.

## CLI Output

The default `report` command prints the overall Brier, Log Loss, ECE, phase,
and one concise line per strategy. `report --json` writes the complete report
object, including nested confidence buckets and strategy breakdown, using the
existing Decimal JSON serializer.

Below 50 settlements the command preserves its concise unavailable message.

## Tests

Tests create frozen predictions representing both record classes, append
linked settlements, and assert the strategy counts and metrics. CLI tests
assert that human output includes the new aggregate fields and that JSON is
parseable. Legacy report output tests remain unchanged.
