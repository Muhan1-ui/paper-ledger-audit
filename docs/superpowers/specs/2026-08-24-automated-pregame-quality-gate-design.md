# Automated Pregame Quality Gate

## Goal

Reduce the time required to prepare one auditable match bundle and make the
evidence limitations visible before any paper snapshot is frozen. This feature
does not promise or optimize a hit rate: the current ledger has fewer than 50
settled primary records, so a measured model improvement is unavailable.

## Scope

`pregame-bundle` receives three changes:

1. It concurrently collects independent official fixed prizes, official
   schedule identity, OddsPortal 1X2, and FotMob xG data.
2. Once official identity is available, it automatically attempts the existing
   strict SofaScore context-event resolver. An adopted event is passed to the
   existing pregame-context collector; a skipped resolver result becomes an
   explicit unavailable context without guessing an event URL.
3. It returns a deterministic `quality_gate` that states whether the bundle is
   ready for paper review, constrained to observation, or blocked, with every
   source gap listed.

## Collection Flow

The first four collectors have no data dependency and run with a bounded
`ThreadPoolExecutor(max_workers=4)`. Results are read in a deterministic order:
official fixed prizes first, then schedule/manual identity, OddsPortal, and
FotMob. Exceptions retain the current CLI failure behavior; no partial
prediction input is frozen or appended.

The auto context step runs only after a schedule or explicitly supplied manual
identity has been established. It calls `resolve_context_event(identity)`. If
the resolver returns `adopted`, the exact `event_url` is passed to
`collect_pregame_context`; otherwise the bundle uses a schema-valid unavailable
context and retains the resolver's source and reason in `context_resolution`.

## Quality Gate

`quality_gate` has one `status` and an ordered list of `reasons`:

- `review_ready`: official SPF, named complete 1X2 capture, traceable xG, and
  all timestamps are before kickoff. It remains a paper review result.
- `observation_only`: baseline inputs are present but xG is `league_total`, a
  verified starting lineup is unavailable, or optional context is unresolved.
  This status must not be described as a stable result.
- `blocked`: a required input is missing, timestamp is invalid/late, or the
  generated bundle is at or after kickoff.

The gate lists exact reasons such as `official_spf_unavailable`,
`named_european_1x2_unavailable`, `xg_evidence_unavailable`,
`pre_kickoff_capture_required`, `league_total_xg`,
`starting_lineups_unconfirmed`, and `context_event_unresolved`.

When an adopted context returns `split_xg: available` and a confirmed lineup,
the quality gate may be `review_ready` only if its primary xG evidence is also
traceable and pre-kickoff. No model values or blend weights change in this
feature.

## Output And Tests

The bundle includes `context_resolution`, `pregame_context`, and
`quality_gate`; the prediction template includes `pregame_context` only when
the context event was adopted. Tests cover parallel collector composition via
patched collectors, adopted context inclusion, skipped-context observation
classification, lineup/xG limitations, and the blocked late-capture case.

The full unit suite and read-only existing-ledger report must pass. No current
ledger line is modified or appended by this functionality.
