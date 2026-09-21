# Tai Cai Audit Design

## Goal

Make the paper-only tracker freeze and evaluate all five China Sports Lottery
football markets without corrupting calibration when more than one strategy is
observed for the same match.

## Scope

The five official markets are SPF, HHAD, CRS, TTG, and HAFU. The tracker will
remain paper-only. It will not place bets, purchase lottery products, or use
third-party prices as official fixed prizes.

## Canonical Match Version

Each frozen prediction will declare one of these roles:

- `primary`: the only prediction eligible for aggregate calibration.
- `comparison`: a same-match strategy observation, reported only in its own
  strategy breakdown.

Only one `primary` prediction may exist for a `match_id`. A later primary is
rejected. Existing immutable records are preserved and remain readable as
legacy records. Aggregate calibration uses primary predictions only; strategy
reports may include primary and comparison records with separate denominators.

## Official Market Snapshot

An all-market snapshot keeps one market entry for each of the five official
markets. A frozen entry must contain:

- The complete probability distribution in declared option order.
- The complete official fixed-prize mapping for the same option set.
- The selected highest-probability option and all tied leaders.
- The official `published_at` timestamp for that market, before kickoff.
- The official source URL for that market.

HHAD additionally stores the official home handicap. A market with incomplete,
late, or unavailable official prizes is recorded as `skipped` with a reason.

The option sets are:

- SPF and HHAD: `home_win`, `draw`, `away_win`.
- CRS: the 31 China Sports Lottery exact-score and other-result options.
- TTG: `0` through `6` and `7+`.
- HAFU: `HH`, `HD`, `HA`, `DH`, `DD`, `DA`, `AH`, `AD`, `AA`.

The all-market command accepts these complete mappings. It must not use the
three-outcome prize parser for CRS, TTG, or HAFU.

## xG Evidence

Enhanced Poisson inputs store an evidence object containing:

- `source_url`, `published_at`, `competition`, `season`, and `sample_matches`.
- `scope`: `home_away_split` or `league_total`.
- Team attacking xG and defensive xGA source values.
- The deterministic calculation used to derive `home_xg` and `away_xg`.

When a home/away split is unavailable, the prediction may use a documented
league-total calculation but is marked `league_total`; its rating cannot exceed
medium confidence. No automatic home advantage is invented.

## Reporting

Aggregate calibration, Brier score, log loss, confidence buckets, and the
50/200 sample gates use only settled primary predictions. Strategy and
all-market reports include their own clear denominators. Each market report
shows frozen, skipped, and settled counts.

## Compatibility And Tests

Existing ledger records remain valid. New fields are required only for new
all-market snapshots and new enhanced predictions. Tests live in a Python
package and run from one documented command without environment-specific
`PYTHONPATH` setup.

Regression coverage must prove that complete CRS, TTG, and HAFU prize mappings
freeze successfully, malformed mappings fail, two primary records for one
match are rejected, comparison records do not alter aggregate calibration, and
per-market publication timestamps must precede kickoff.
