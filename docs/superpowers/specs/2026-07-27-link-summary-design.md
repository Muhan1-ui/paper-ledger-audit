# Link Summary Design

## Scope

Add a read-only helper that extracts the three usable URLs from a `resolve-links`
result JSON:

- OddsPortal event URL
- home FotMob team stats URL
- away FotMob team stats URL

## Behavior

`link-summary <resolved.json>` reads a previously saved resolver result. If all
three required sections have `status: "candidate"`, it returns `ready: true`
with the three URLs and a `pregame_bundle_args` array. If any section is
skipped, ambiguous, or missing a URL, it returns `ready: false` and a clear
`missing` list.

## Safety

The command reads one local JSON file and prints derived JSON. It does not fetch
web pages, append ledger records, freeze predictions, settle results, or perform
purchase/fund operations.
