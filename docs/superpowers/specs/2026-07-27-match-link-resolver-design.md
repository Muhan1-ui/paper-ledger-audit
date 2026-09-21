# Match Link Resolver Design

## Goal

Build a conservative resolver that takes a China Sports Lottery football match identity and returns auditable candidate links for OddsPortal and FotMob, with confidence scores and explicit skip reasons.

## Scope

This feature only prepares link candidates. It does not freeze predictions, append ledger records, settle results, place purchases, bypass access controls, or invent missing data.

## Approach

The resolver works in three layers:

1. Normalize team names from Sporttery, OddsPortal, and FotMob into comparable tokens.
2. Search public pages or known page structures for candidate matches/teams.
3. Score candidates with transparent evidence: team-name similarity, competition similarity, kickoff/date proximity, and source URL.

High-confidence candidates may feed `pregame-bundle`. Medium-confidence candidates are returned for user confirmation. Low-confidence or missing candidates are marked as skipped.

## Components

- `outputs/tai_cai_tracker/resolver.py`: pure matching and scoring helpers plus public collection wrapper.
- `work/tai_cai_tracker_tests/test_resolver.py`: unit tests for normalization, scoring, candidate selection, and skip behavior.
- `outputs/tai_cai_tracker/cli.py`: add a `resolve-links` command.
- `outputs/tai_cai_tracker/README.md`: document resolver usage and safety boundaries.

## Confidence Policy

- `high`: both teams strongly match and date/kickoff evidence is compatible.
- `medium`: one source matches strongly but another needs confirmation, or team names are plausible but date evidence is weak.
- `low`: ambiguous team matches or missing date/competition evidence.
- `skipped`: no candidate can be verified from public data.

The resolver never silently uses low-confidence candidates in a generated pregame bundle.

## Output Contract

The command returns JSON:

```json
{
  "match_id": "2040623",
  "sporttery": {
    "home_team": "FC Seoul",
    "away_team": "Ulsan HD FC",
    "kickoff_at": "2026-07-27T18:30:00+08:00",
    "competition": "K League 1"
  },
  "oddsportal": {
    "status": "candidate",
    "confidence": "high",
    "source_url": "https://www.oddsportal.com/football/h2h/...",
    "score": 0.94,
    "evidence": ["home name matched", "away name matched", "date compatible"]
  },
  "fotmob": {
    "home": {
      "status": "candidate",
      "confidence": "high",
      "source_url": "https://www.fotmob.com/teams/92630/stats/fc-seoul/teams"
    },
    "away": {
      "status": "candidate",
      "confidence": "high",
      "source_url": "https://www.fotmob.com/teams/133896/stats/ulsan-hd-fc/teams"
    }
  }
}
```

## Error Handling

If public search or a source page is unavailable, the resolver returns `status: "skipped"` with a reason instead of raising unless the input itself is invalid. Network failures are reported as source-specific skip reasons.

## Testing

Tests must cover:

- Chinese/English alias normalization such as `首尔` and `FC Seoul`.
- Korean club suffix normalization such as `Ulsan HD` and `Ulsan HD FC`.
- confidence thresholds for high, medium, and low matches.
- no candidate selected when two candidates tie too closely.
- CLI exposure without requiring live network access.

