# Pregame Context Evidence Design

## Goal

Add auditable pregame context for home/away xG, weather, and starting lineups
without changing the current paper-only prediction probabilities in this phase.

## Scope

The tracker will collect three optional evidence objects for one verified match:

- `split_xg`: public home/away attack and defence xG/xGA samples for the
  current competition and season.
- `weather`: weather forecast at a verified stadium coordinate for the kickoff
  hour.
- `lineup`: a public event provider's lineup state, including whether it
  explicitly marks the lineup confirmed and whether each team has 11 starters.

The base Sporttery, named-bookmaker 1X2, FotMob league-total xG, model blend,
and immutable ledger flow remain unchanged.

## Source And Trust Rules

1. A public match-event provider is used only after its event identity matches
   the official teams and kickoff within a documented tolerance. Its direct
   event URL is stored in every derived evidence object.
2. Home/away xG is accepted only when the source provides current-season
   `home` and `away` samples and all four values required by the formula:
   home attack xG, home defence xGA, away attack xG, away defence xGA.
3. Open-Meteo is used for weather only after the matched event supplies valid
   venue latitude and longitude. The API request URL, capture time, forecast
   hour, temperature, precipitation, and wind speed are retained.
4. A lineup is `confirmed` only if the source explicitly reports confirmation
   and both teams have exactly 11 starters. A complete unconfirmed lineup is
   `reported`; everything else is `unavailable` with a reason.
5. No source response is inferred, transformed into an official claim, or used
   when its event identity is ambiguous.

## Data Flow

`pregame-context` accepts a verified official identity plus an event-provider
URL or ID. It collects, validates, and prints a reviewable bundle:

1. Resolve and validate the public event against the official identity.
2. Build `split_xg`; on absent or incomplete split data, return
   `status: unavailable` and preserve the existing FotMob `league_total` xG.
3. Build `weather`; on an absent venue or forecast point, return unavailable.
4. Build `lineup`; before publication it normally returns unavailable rather
   than guessing probable starters.
5. Return a `risk_summary` with the three statuses and a deterministic
   confidence ceiling:
   - `high` only when home/away xG is complete and lineups are confirmed;
   - `medium` when existing league-total xG is valid but either stronger input
     is absent;
   - `low` when no xG model evidence is available.

The command never appends a ledger record. The existing `snapshot` command
may receive the resulting context object and freezes it unchanged alongside
the primary prediction. Existing frozen records remain valid.

## xG Formula

For a verified split sample:

```
home_xg = mean(home_attack_xg / home_sample_matches,
               away_defence_xga / away_sample_matches)
away_xg = mean(away_attack_xg / away_sample_matches,
               home_defence_xga / home_sample_matches)
```

The evidence scope becomes `home_away_split`; it stores both sample counts and
the formula. When this computation cannot be made, no substitute home
advantage is invented.

## Failure Handling

- Network, access, parsing, and identity failures become an auditable
  unavailable result with a stable reason code.
- A missing optional context item cannot block a valid baseline prediction.
- An ambiguous event, bad coordinate, stale forecast, or incomplete lineup
  cannot be frozen as confirmed evidence.
- The first phase does not modify Poisson inputs, blend weights, or selected
  market options from weather or lineups.

## Testing

Fixture-driven tests cover event matching, split-xG computation, split-data
fallback, weather-hour selection, invalid coordinates, confirmed/reported/
unavailable lineup states, evidence serialization, and snapshot compatibility.
The full tracker suite and a read-only ledger report must pass after changes.
