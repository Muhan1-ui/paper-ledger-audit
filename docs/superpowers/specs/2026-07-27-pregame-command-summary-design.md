# Pregame Command Summary Design

## Scope

Enhance `link-summary` so a ready resolved-link summary includes a copyable
PowerShell command for the next paper-only collection step.

## Behavior

When `link-summary` has all three required URLs, it already returns
`pregame_bundle_args`. Add `pregame_bundle_command`, a single-line command:

```powershell
python -m outputs.tai_cai_tracker.cli pregame-bundle <match_id> "<oddsportal_url>" "<home_fotmob_url>" "<away_fotmob_url>"
```

When the summary is not ready, omit `pregame_bundle_command` so users do not
accidentally run an incomplete bundle.

## Safety

This command text is only a convenience for a later public-data helper. It does
not execute the command, append ledger records, freeze predictions, settle
results, purchase, or handle funds.
