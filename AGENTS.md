# Tai Cai Tracker Instructions

## Purpose

This project is an auditable, paper-only tracker for China Sports Lottery
football matches. Work from this project may monitor public information,
freeze paper predictions, settle verified results, and review model outputs.
It must never purchase, place, fund, recommend funding, or allocate a lottery
bet.

## Start Of Every Task

1. Read this file and `outputs/tai_cai_tracker/README.md`.
2. Treat `outputs/tai_cai_tracker/data/paper_ledger.jsonl` as the immutable
   ledger. Do not edit existing JSONL lines.
3. Use the local `tai-cai-audit` skill when it is available. Its absence does
   not block the project rules in this file.
4. Work one match at a time unless the user explicitly requests a batch.

## Evidence Rules

- Use public China Sports Lottery pages for match identity, kickoff time,
  official fixed prizes, and official settlement.
- Preserve a direct source URL and visible source time beside each observed
  fact. Do not infer a missing publication time.
- An independent European 1X2 reference requires a named provider, named
  bookmaker, direct URL, capture time, and three decimal prices.
- xG evidence requires its source URL, competition, season, sample size,
  scope, raw values, formula, and either a publication time or a precise
  publication-time-unavailable reason. If publication time is unavailable,
  actual capture time must be traceable and strictly before kickoff.
  `league_total` evidence cannot be presented as a home/away split.
- Look for missing public evidence independently, but do not bypass a login,
  paywall, regional restriction, CAPTCHA, or other access control.
- Never invent odds, lineups, injuries, xG, weather, referee data, final
  scores, source URLs, or timestamps.

## Freeze And Settlement Rules

- Freeze at most one `primary` prediction for each match ID. Use a
  `comparison` record for an auditable research variant.
- An enhanced/model-derived five-market snapshot requires complete official
  SPF, HHAD, CRS, TTG, and HAFU tables, a pre-kickoff official source/time for
  each, traceable named independent 1X2, and traceable xG. xG with no visible
  publication time is allowed only when actual capture was before kickoff and
  its unavailable-time reason is retained.
- When xG alone is unavailable, an `official_market_baseline` five-market
  snapshot may be auto-frozen only with all five complete pre-kickoff official
  tables and traceable named independent 1X2. It must use the explicit
  `official_market_baseline` label, never be called xG-derived, enhanced, or
  stable, and must be reported separately from model-derived snapshots.
- If evidence is incomplete, record an observation or a skipped market with a
  concrete reason. Do not fill gaps with untraceable data.
- Settle only from a traceable official China Sports Lottery final-result URL
  that shows the result. Include the final score and map all frozen markets.
- Append through `python -m outputs.tai_cai_tracker.cli`. Never overwrite a
  frozen prediction, snapshot, or settlement.

## Model And Risk Rules

- A prediction is a paper model output, never a guarantee or a purchase
  instruction.
- Do not label a match a "stable pick" when a verified starting lineup is
  unavailable or the xG evidence is only `league_total`.
- Treat a calibration result as unavailable below 50 settled primary records.
  Do not automatically alter model weights before the tracker threshold is met.
- When current official results are not yet published, keep the record pending
  even when a third-party score display is visible.

## Required Match Report

Separate observed facts from model output. For every monitored match, include:

1. Identity: match ID, teams, kickoff time, and official URL.
2. Evidence: official market URLs/times, independent odds reference, and xG
   source or a precise missing-data reason.
3. Ledger action: frozen primary/comparison, observation only, or skipped.
4. Model output: 1X2, handicap, score TOP3, total goals, and HAFU only when
   supported by frozen model evidence.
5. Risk: source gaps, timing conflicts, and lineup uncertainty.
6. Review: pending settlement, official settlement URL, or the calibration
   sample-gate status.

Use concise Chinese for user-facing reports. Do not use betting, purchase, or
money-allocation language.
