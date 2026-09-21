# Odds Evidence Template

## Goal

Turn a successfully collected public OddsPortal 1X2 response into a valid,
reviewable `source_observation` input without automatically appending it.

## Design

`collect_oddsportal` hashes the exact decoded market API response bytes and
returns `market_raw_content_sha256`. `build_pregame_bundle` emits one
`evidence_event_inputs` item only when that hash is present and a complete
named-bookmaker capture was selected. The item uses the official weekday match
sequence when available, otherwise the supplied match ID; it contains the
direct event URL, capture time, no-visible-publication-time reason, collector
and parser identifiers, raw market hash, provider, bookmaker, and three raw
prices.

The bundle remains read-only. A reviewer chooses whether to run
`evidence-record` with the generated item. Hashing an exact received response
is not treated as evidence that a current page will remain unchanged.
