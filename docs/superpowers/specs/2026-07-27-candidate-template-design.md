# Candidate Template Design

## Scope

Add a paper-only helper that builds an editable `resolve-links` candidate JSON
template from one official Sporttery match identity.

## Selected Approach

Use a conservative official-identity-only template. The command fetches the
current Sporttery schedule, finds the requested `match_id`, copies only official
identity fields, and leaves OddsPortal/FotMob candidate arrays empty for later
manual or separately verified filling.

## Out Of Scope

- No automatic OddsPortal or FotMob web search.
- No ledger writes.
- No odds, xG, prediction, purchase, or settlement action.

## Interface

```powershell
python -m outputs.tai_cai_tracker.cli candidate-template 2040623
```

Optional:

```powershell
python -m outputs.tai_cai_tracker.cli candidate-template 2040623 --method concern
```

## Output

```json
{
  "sporttery": {
    "match_id": 2040623,
    "match_num": "周一001",
    "competition": "K League 1",
    "home_team": "首尔",
    "away_team": "蔚山HD",
    "kickoff_at": "2026-07-27T18:30:00+08:00",
    "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623"
  },
  "oddsportal_candidates": [],
  "home_fotmob_candidates": [],
  "away_fotmob_candidates": []
}
```

## Safety

If the match is not found in the current official schedule payload, the command
raises a clear error instead of inventing identity fields.
