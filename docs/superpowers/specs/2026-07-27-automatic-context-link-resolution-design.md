# Automatic Context Link Resolution Design

## Goal

Automatically discover and verify public event URLs needed by pregame context
collection, while preserving the tracker rule that an uncertain source is
skipped rather than attached to the wrong Sporttery match.

## Scope

Extend the existing resolver with one public event source for the context
module and a persisted local alias registry. The feature discovers candidates,
scores them against the official Sporttery identity, and returns a verified
event URL suitable for `pregame-context` only when the match is unique.

## Decision Rules

- Auto-adopt only when the top candidate score is at least `0.90` and exceeds
  the second-best candidate by at least `0.08`.
- Score home team, away team, competition, and timezone-normalized kickoff.
- A candidate must have direct source URL, event ID, and source capture time.
- A missing or ambiguous result returns `status: skipped` with candidates and
  evidence; it never guesses an event ID.
- Existing manual candidates remain supported and retain their current
  conservative ranking behavior.

## Alias Registry

The registry is a versioned JSON file under `outputs/tai_cai_tracker/data`.
Each entry maps a normalized Sporttery team name to a source-standard name and
optional source team ID. It also stores the direct source URL, first-seen time,
and evidence score.

Only a newly auto-adopted high-confidence event may create or update an alias.
Conflicting aliases, lower-confidence candidates, and candidates without a
team ID are not written. Registry data is evidence assistance, not an
authority: the current event still must pass team and kickoff validation.

## Data Flow

1. Read official Sporttery identity from the schedule or an existing candidate
   template.
2. Query the public event source for the official match date.
3. Normalize source and Sporttery team names using static aliases plus the
   registry; rank candidates using the strict threshold.
4. On adoption, return a `context_event` object with URL, event ID, score,
   margin, source capture time, and matching evidence. Persist any eligible
   aliases atomically.
5. Feed the exact URL to `pregame-context`; its independent identity check is
   retained as a second gate.

## Error Handling And Testing

Network, decode, no-result, low-score, tie, and alias-conflict paths return
auditable skipped objects. Fixture tests cover Chinese/English aliases, time
zone conversion, strict score and margin gates, atomic registry updates,
conflict rejection, CLI output, and unchanged manual resolver behavior.
