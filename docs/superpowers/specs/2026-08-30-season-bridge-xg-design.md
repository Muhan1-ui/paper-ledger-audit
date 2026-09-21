# Season-Bridge xG Design

## Goal

Allow a paper-only, medium-confidence pregame record for selected early-season
fixtures without treating one or two current-season matches as a sufficient xG
sample. The record must retain traceable current and prior-season source data,
and must never invent cross-league or unverified team continuity.

## Scope

This change applies only when the existing xG input is below the current
eight-match floor. It adds a read-only Understat-backed season-bridge source
and makes every automatic freeze entry point enforce the same quality gate.

It does not alter Poisson formulas for existing current-season-only records,
change model blend weights automatically, or make a purchase decision.

## Evidence Model

`season_bridge` evidence contains distinct current- and prior-season source
blocks. Each block retains its direct Understat data URL, capture time,
publication-time-unavailable reason, competition, season, home and away
venue-split raw xG/xGA values, sample sizes, and response SHA-256.

The bridge is eligible only if all of these conditions hold:

- Current and prior data identify the same team name from the provider data.
- Both teams have matches in the same named competition in both seasons.
- The current season has at least two complete venue-split matches per team.
- The prior season has at least twelve complete venue-split matches per team.
- Every included prior-season row is strictly before the fixture kickoff date.

Promotion, relegation, different competition names, missing venue data,
ambiguous provider team identity, unavailable source data, and insufficient
samples make the bridge unavailable. They do not fall back to a guessed
mapping or a mixed-league statistic.

## Formula And Confidence

For each attack or defence rate, calculate a current-season venue rate and a
prior-season venue rate from the retained raw totals and samples. Blend them
with the current season's effective sample weight divided by the combined
effective samples, capped at 70 percent, with the remainder assigned to the
prior season. Derive home and away expected goals from the blended venue
attack/defence rates using the existing Poisson input convention.

The formula text, source samples, and calculated weights are frozen with the
record. `season_bridge` is always `scope: home_away_split` in the existing
schema because each input is a verified venue split, but receives a fixed
`confidence_ceiling: medium` and a `season_bridge_xg` risk reason. It never
becomes high confidence merely because it has a larger combined sample.

## Quality Gate

The new bridge path returns `review_ready` only when the existing official SPF
and named European 1X2 requirements are met, all five official markets are
complete for a snapshot, all evidence is pre-kickoff, and the `2 + 12`
current/prior sample threshold is met.

Fixtures with current samples of eight or more retain the existing current
season path. Fixtures below the bridge threshold remain `observation_only`.

## Freeze Behavior

`rapid-freeze`, `rapid-freeze-batch`, and `schedule-driven-freeze` must all
use the same pregame quality gate before appending any records. A gate other
than `review_ready` appends no odds capture, prediction, or market snapshot.
This prevents a low-quality rapid job from creating a partial or inconsistent
paper record.

## Tests

Add regression coverage for:

- A same-league `2 + 12` bridge creates traceable medium-confidence input.
- Each below-threshold case remains observation-only.
- Cross-league, missing prior source, and ambiguous team identities are
  rejected explicitly.
- Existing current-season xG uses its unchanged formula.
- Rapid single and batch freezes append nothing for an observation-only gate.
- A qualifying bridge freeze appends exactly linked odds, primary prediction,
  and five-market snapshot records.
