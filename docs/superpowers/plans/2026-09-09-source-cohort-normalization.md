# Comparable Source Cohorts Plan

**Goal:** Make source-performance reporting compare settled paper predictions
only within stable, auditable source cohorts, without changing immutable ledger
records or selecting a model automatically.

## Rules

1. Report-time canonicalization preserves the frozen provider and URL; it only
   emits a derived source-family label. Known legacy provider variants map to
   `433`, `OddsPortal`, or `sgodds`; all other non-empty providers remain their
   trimmed original label.
2. xG source family is the source hostname with known FotMob subdomains merged
   to `fotmob`. Its evidence scope is retained independently. Missing xG is
   `no_xg` with scope `no_xg`.
3. A comparable cohort key contains European source family, xG source family,
   xG scope, and frozen strategy. Distinct xG scopes never share a cohort.
4. Cohort metrics are descriptive. A cohort is only marked `comparison_ready`
   at 20 settled primary records and only marked `calibration_available` when
   the existing overall 50-record gate and the cohort 50-record gate both pass.
5. The change neither writes the ledger nor changes any model weight, source
   priority, capture record, or evidence requirement.

## Implementation

1. Add report tests for legacy odds-label coalescing, FotMob-domain coalescing,
   scope separation, and the cohort readiness flags.
2. Add pure report helpers and return `source_families` plus
   `comparable_cohorts` under `source_performance` while retaining current raw
   source breakdowns for audit.
3. Document the derived-only grouping and verify the complete test suite plus a
   read-only report whose ledger hash is unchanged.
