# Source Performance Breakdown

## Goal

Make provider selection measurable from frozen records: show collection
observations/failures by source and settled primary prediction diagnostics by
European odds provider, xG source domain, and frozen pregame-context ceiling.
The report is descriptive and never selects a provider, changes a model
weight, or claims a source is better before the calibration sample gate.

## Source Labels

- European odds label: `prediction.european_odds.provider`, otherwise
  `unlinked_market`.
- xG label: hostname of frozen `xg_inputs.evidence.source_url`, falling back to
  `xg_inputs.source_url`, otherwise `no_xg`.
- Context label: frozen `pregame_context.risk_summary.confidence_ceiling`,
  otherwise `no_context`.
- Evidence event label: `observed_facts.provider` for successful observations
  when present, otherwise source URL hostname; failures use `collector`.

## Report Contract

`source_performance` contains `evidence_sources` and three settled-primary
breakdowns. Each settled group has record count, Brier average, log-loss
average, top-pick accuracy, and `calibration_available`, which remains false
until both the overall report and that group have at least 50 settlements.
Evidence groups contain observation and failure counts by evidence kind.

Missing or malformed optional source fields fall into explicit fallback labels;
no provider name or outcome is inferred. The text report prints a compact
evidence-event summary only; full provider diagnostics remain in `--json`.
