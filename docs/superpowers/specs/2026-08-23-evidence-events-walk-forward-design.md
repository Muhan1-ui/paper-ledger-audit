# Evidence Events And Walk-Forward Evaluation

## Goal

Add a ledger-backed audit trail for public-source collection attempts and a
read-only, expanding-window evaluation of frozen enhanced predictions. Neither
feature changes a frozen prediction, settlement, model weight, or the existing
ledger lines.

## Scope

The work has two bounded components:

1. Evidence events record either a successful public-source observation or a
   concrete collection failure for one Sports Lottery match.
2. Walk-forward evaluation scores only already frozen enhanced primary
   predictions and their linked official settlements. It is descriptive and
   never changes a future model weight.

The current fixed 70/30 `temporal_weight_evaluation` remains available for
backward-compatible output. The new walk-forward result is the preferred
time-ordered diagnostic.

## Evidence Event Records

Two hashed ledger record types are added. They are appended only through a new
CLI command and do not affect prediction uniqueness, settlement linkage, or
calibration counts.

### `source_observation`

A successful capture contains exactly:

- `match_id`, `evidence_kind`, `source_url`, and timezone-aware `captured_at`;
- either a timezone-aware, source-visible `published_at` or a non-empty
  `published_at_unavailable_reason`;
- non-empty `collector` and `parser_version` identifiers;
- a SHA-256 `raw_content_sha256` of the captured public response or page text;
- a non-empty `observed_facts` JSON object containing the raw values relied on
  by the collector.

`evidence_kind` is limited to `official_market`, `european_1x2`, `xg`, and
`pregame_context`. The source URL must be HTTP(S). `observed_facts` is checked
recursively to accept only JSON scalar, list, and object values, so it can be
stored as immutable evidence without custom objects.

### `collection_failure`

A failed attempt contains exactly:

- `match_id`, `evidence_kind`, `attempted_at`, `collector`, and
  `parser_version`;
- an optional HTTP(S) `attempted_source_url` when an exact URL was reached;
- a controlled `reason_code` and a non-empty human-readable `reason`.

Allowed reason codes are `access_restricted`, `not_found`, `identity_mismatch`,
`missing_publication_time`, `incomplete_data`, `parse_error`, `network_error`,
and `other`. This captures the distinction between “not yet collected” and
“collected but unusable”, without fabricating any missing evidence.

Both records receive `record_type`, status (`observed` or `unavailable`), and
their content hash in the same manner as other ledger records. `read_records`
verifies them. The report ignores them for calibration metrics but exposes
their counts by type and evidence kind.

## CLI And Data Flow

`evidence-record <input.json> <ledger.jsonl>` accepts one input object whose
`record_type` is either `source_observation` or `collection_failure`, validates
and hashes it, appends exactly one JSONL line, and prints the frozen record.
Its JSON input intentionally mirrors the event contract so external collectors
can pass their direct URL, capture time, parser version, raw-content hash, and
facts without a second lossy transformation.

This command is the only write path for the new records. Existing collection
commands remain read-only; callers can review their JSON output and then
append a successful observation or a concrete failure as appropriate.

## Walk-Forward Evaluation

`walk_forward_weight_evaluation` is a pure report function that considers only
settled `primary` enhanced predictions with both frozen market and model
probability maps. It requires 200 eligible records, matching the existing
model-selection gate.

Eligible pairs are ordered by frozen `prediction.captured_at`, with prediction
content hash as a deterministic tie-breaker. The first 140 records form the
initial training window. For each later record:

1. Evaluate fixed candidate blend weights from `0.0` to `1.0` in `0.1` steps
   using only earlier ordered pairs.
2. Select the minimum training log-loss weight, using the lower weight to
   break an exact tie.
3. Score that one future prediction with the selected weight.
4. Expand the training window by that future pair and repeat.

Each fold includes the training endpoint timestamp, the training-pair snapshot
hash, test prediction hash, test timestamp, frozen model version, selected
weight, and test log loss. The aggregate output includes the candidate set,
initial-window size, fold count, selected-weight frequency, walk-forward log
loss, and market/model endpoint log loss. These hashes make the exact
time-ordered sample auditable even if later records are appended.

The evaluation boundary is the prediction capture timestamp. Existing
settlement records do not contain an official result publication timestamp, so
the diagnostic makes no claim that a historic outcome was available at the
same instant. It only prevents features or predictions frozen after a test
prediction from entering that test prediction's training window.

## Error Handling

- Invalid event inputs fail before any ledger append.
- Invalid or tampered evidence-event lines cause `read_records` to fail, like
  existing frozen records.
- Unknown record types remain rejected.
- Fewer than 200 eligible enhanced primary settlements produce a structured
  unavailable walk-forward result rather than a partial score.
- Empty or malformed frozen model distributions are excluded by the existing
  eligibility checks; no probability is inferred.

## Tests And Verification

Tests are added before implementation for:

- valid and tamper-rejected observation/failure records;
- CLI append and subsequent `read_records` verification;
- evidence-event report counts that leave calibration counts unchanged;
- an expanding-window fold whose training hash excludes its future test pair;
- the 200-record availability gate and deterministic tie-breaking.

The full unit suite and a read-only `report --json` over the existing ledger
must pass. The project has no Git repository, so this design cannot be
committed here.
