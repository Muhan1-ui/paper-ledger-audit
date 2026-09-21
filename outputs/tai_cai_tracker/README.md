# China Sports Lottery Football Calibration Tracker

This is an offline, paper-only tracker for recording pre-kickoff three-outcome
predictions and checking their later calibration. It does not scrape websites,
place bets, purchase lottery products, or recommend any purchase.

## Source hierarchy

Use sources in this order when preparing a real paper record:

1. **Match identity and settlement:** [China Sports Lottery official website](https://www.lottery.gov.cn/)
   is the authority for the 11-digit match ID and final result URL.
2. **European market baseline:** import an offline JSON export that records a
   named bookmaker, exact source URL, time, and 1X2 prices. Store that exact
   source page as `S2`. The tracker makes no HTTP requests and does not scrape
   websites.
3. **Optional xG context:** cite the exact public statistics page used, for
   example [FBref's expected-goals data](https://fbref.com/en/comps/9/stats/Premier-League-Stats).
   Store it in `xg_inputs.source_url`. A context-only record is preserved but
   does not affect probabilities. An enhanced Poisson record requires the
   xG source, both expected-goals values, `model_version: "poisson-v1"`, and
   a fixed `model_weight`. A non-zero model weight also requires a referenced
   named-bookmaker European odds capture. The snapshot freezes market, model,
   and blended probabilities together with a bounded scoreline matrix.

`S1` and `S2` must be HTTP(S) URLs. The snapshot must provide either an
11-digit official match ID or the official weekday sequence (for example,
`周五201`), a timezone-aware capture time before kick-off, and fixed prizes for
`home_win`, `draw`, and `away_win`. Optional xG fields are `source_url`,
`home_xg`, and `away_xg`. To activate the model, also provide
`model_version`, `model_weight`, and optionally a whole-number
`home_handicap`. The model stores 1X2, scoreline TOP5, total-goal buckets,
half-time/full-time proxy, and the supplied handicap 1X2 result. It makes no
claim that a model probability is a recommendation or a guaranteed outcome.

New snapshots default to `"prediction_role": "primary"`. Only one primary
prediction may be frozen for one official match identity. The 11-digit official
match ID is unique; because official weekday sequences such as `周五201` repeat,
a sequence-based record is identified by its weekday sequence plus official
kickoff time. Use `"prediction_role": "comparison"` for a same-match research
variant; comparison records remain visible in the strategy breakdown but are
excluded from aggregate calibration metrics. Legacy records without this field
are treated as primary for continuity.

An enhanced Poisson input may include a fully traceable `xg_inputs.evidence`
object. Its fields are `source_url`, either timezone-aware `published_at` or a
precise `published_at_unavailable_reason` plus timezone-aware `captured_at`,
`competition`, `season`, either positive integer `sample_matches` or the pair
`home_sample_matches` and `away_sample_matches`, `scope`, four raw xG/xGA
fields, and a non-empty `formula`. `scope` is one of `home_away_split`,
`league_total`, or `source_match_model`. The last form records a source's
explicit pre-match xG model and its reported sample basis; it is never relabeled
as a home/away split. `league_total` and `source_match_model` evidence receive
a frozen `confidence_ceiling` of `medium`. Evidence publication cannot be later than
the snapshot capture time. When publication time is unavailable, snapshot
capture time must be strictly before kickoff and the missing-time reason is
frozen instead. A missing publication time is never inferred.

```json
{
  "xg_inputs": {
    "source_url": "https://stats.example.test/match",
    "home_xg": "1.20",
    "away_xg": "0.80",
    "model_version": "poisson-v1",
    "model_weight": "0.40",
    "evidence": {
      "source_url": "https://stats.example.test/season",
      "published_at": "2026-07-26T17:53:00+08:00",
      "competition": "K League 1",
      "season": "2026",
      "sample_matches": 19,
      "scope": "home_away_split",
      "home_attack_xg": "31.4",
      "home_defense_xga": "23.3",
      "away_attack_xg": "34.3",
      "away_defense_xga": "26.1",
      "formula": "mean(home_attack_xg/sample_matches, away_defense_xga/sample_matches)"
    }
  }
}
```

Optional `match_context` must contain non-empty `competition`, `home_team`,
and `away_team` strings. When enough records are settled, `report --json`
includes descriptive `competition_breakdown` statistics for records that carry
this frozen context.

An odds import batch contains a provider and one or more captures. A capture
has a match ID, kickoff/capture timestamps, source URL, source quality, and
three decimal odds. Only `named_bookmaker` captures, which also require a
bookmaker name, can become a probability baseline. `unattributed` captures
remain observation-only. A snapshot that uses European odds adds
`"european_odds": {"capture_hash": "..."}` and must use the same S2 URL as the
imported capture.

## Paper workflow

The included input is explicitly hypothetical and uses only `.example.test`
URLs. It is not real sports or lottery data.

```powershell
python -m outputs.tai_cai_tracker.cli odds-import `
  outputs/tai_cai_tracker/example_odds_import.json `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl

python -m outputs.tai_cai_tracker.cli snapshot `
  outputs/tai_cai_tracker/example_input.json `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl

python -m outputs.tai_cai_tracker.cli settle `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl `
  <prediction-content-hash> home_win <official-result-url>

python -m outputs.tai_cai_tracker.cli settle `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl `
  <prediction-content-hash> home_win <official-result-url> `
  --final-score 2 0

python -m outputs.tai_cai_tracker.cli settlement-invalidate `
  <identity-mismatch-invalidation.json> `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl

python -m outputs.tai_cai_tracker.cli settlement-queue `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl

python -m outputs.tai_cai_tracker.cli report `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl

python -m outputs.tai_cai_tracker.cli report `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl `
  --json
```

`odds-import` appends one or more immutable odds-capture records and prints
their hashes. Put the printed hash in a real snapshot's `european_odds` object
to use that capture as the probability baseline. `snapshot` appends one frozen
JSONL prediction. `settle` appends one linked official-result record and refuses
a second active settlement for the same prediction. If an already appended
settlement is proven to reference a different official fixture, use
`settlement-invalidate` with the prior settlement hash, frozen prediction hash,
capture time, direct official result URL, and the official result identity
(`match_id`, weekday sequence, date, home team, away team). It appends a hashed
`settlement_invalidation` record instead of changing history. The invalidated
settlement no longer affects reports and the prediction returns to the official
settlement queue. `settlement-queue` is read-only:
it lists frozen predictions awaiting a traceable official result URL, their
official match pages, and any linked full-market snapshot status. It never
uses a third-party score display or appends a settlement. `report` reads the linked ledger
and deliberately reports calibration as unavailable until at least 50 settled
predictions exist. At 50 to 199 settlements it reports Brier score, multiclass
log loss, fixed-bin expected calibration error, and confidence buckets as
observational diagnostics. It does not tune weights automatically; 200
settlements and temporal validation are required before model-selection work.
The report groups settled records by their frozen strategy, such as
`market_baseline` or `poisson-v1@0.40`. It also reports five-market snapshots
by their immutable source tier: legacy and xG/model-derived snapshots are
`model_derived`; an official-only five-market baseline is
`official_market_baseline`. These groups are descriptive only: they do not
choose a model or alter any future prediction weight.

## Auditable Collection Events

Collector commands print reviewable evidence but do not write the ledger by
themselves. After reviewing a public response, append either one successful
`source_observation` or one concrete `collection_failure` through the CLI. Both
forms are hashed immutable records; they do not count as predictions or
settlements and cannot change frozen model weights.

```powershell
python -m outputs.tai_cai_tracker.cli evidence-record `
  <evidence-event-input.json> `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl
```

A successful observation records the match ID, `official_market`,
`european_1x2`, `xg`, or `pregame_context` evidence kind, direct source URL,
capture time, source-visible publication time (or a precise reason it is not
visible), collector/parser version, raw-content SHA-256, and the raw observed
facts used. A failure records the same match/evidence identity, attempt time,
collector/parser version, optional reached URL, and a controlled reason code.

```json
{
  "record_type": "source_observation",
  "match_id": "2040643",
  "evidence_kind": "european_1x2",
  "source_url": "https://www.example.test/event",
  "captured_at": "2026-08-24T10:30:00+08:00",
  "published_at_unavailable_reason": "page does not show a publication time",
  "collector": "oddsportal-extract",
  "parser_version": "2026.08.24",
  "raw_content_sha256": "<64 lowercase hexadecimal characters>",
  "observed_facts": {"provider": "Example", "bookmaker": "ExampleBook"}
}
```

```json
{
  "record_type": "collection_failure",
  "match_id": "2040643",
  "evidence_kind": "xg",
  "attempted_at": "2026-08-24T10:35:00+08:00",
  "collector": "fotmob-xg-evidence",
  "parser_version": "2026.08.24",
  "reason_code": "incomplete_data",
  "reason": "public page has no verified home/away split"
}
```

Allowed failure codes are `access_restricted`, `not_found`,
`identity_mismatch`, `missing_publication_time`, `incomplete_data`,
`parse_error`, `network_error`, and `other`.

After 200 settled enhanced records, the report also performs a chronological
70/30 holdout evaluation across fixed blend weights from 0.0 through 1.0. Its
selected weight is a paper recommendation only; it is never applied to future
snapshots automatically.

Both temporal evaluations count only settled `primary` enhanced predictions.
`comparison` records remain research variants and cannot satisfy a calibration
or model-selection sample gate. Before the gate is met, JSON and plain-text
reports show the current enhanced-settlement count and required minimum.

At the same 200-record gate, `report --json` also includes an expanding-window
`walk_forward_weight_evaluation`: each later frozen prediction is scored once
with a weight selected only from earlier frozen prediction hashes. Its folds
include the training snapshot hash, test prediction hash, timestamps, selected
weight, and loss for audit. The result is descriptive only and never writes a
new weight to a snapshot.

`report --json` also includes `source_performance`. It groups settled primary
records by frozen European odds provider, xG source domain, and pregame-context
confidence ceiling, and groups evidence events by the observed provider or
failure collector. Each group reports count, Brier score, log loss, and top
outcome accuracy. A group remains descriptive until both it and the overall
primary ledger have at least 50 settled records; it never automatically picks
a source or changes a model weight.

The same report adds derived `source_families` and `comparable_cohorts` without
editing any frozen record. Source families coalesce known legacy labels such as
OddsPortal screenshots and FotMob subdomains, while the raw provider and source
domain breakdowns remain available for audit. A comparable cohort is always
split by European source family, xG source family, xG evidence scope, and frozen
strategy, so `league_total`, `home_away_split`, and `source_match_model` are
never compared as one sample. A cohort needs 20 settled primary records before
it is marked `comparison_ready`; its metrics remain descriptive and still need
the existing 50-record calibration gate before they are calibration-available.

## Full-market paper snapshots

For a future enhanced prediction that contains a frozen full Poisson score
matrix, a manually prepared, auditable JSON input can freeze all five official
markets: SPF, HHAD, correct score, total goals, and half-time/full-time.

```powershell
python -m outputs.tai_cai_tracker.cli all-market-snapshot `
  <all-market-snapshot-input.json> `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl

python -m outputs.tai_cai_tracker.cli all-market-settle `
  <all-market-result-input.json> `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl
```

The snapshot records every highest-probability option, full option
distribution, ties, official line, and official prizes before kickoff. A
missing official line or outcome is skipped or pending; third-party results
are never substituted. These commands are paper-only and do not scrape,
purchase, place bets, or make financial decisions.

The all-market input requires a complete official fixed-prize table for every
market, with a separate official source URL and publication time per market.
The table must include all declared options: 3 for SPF, 3 for HHAD, 31 for
correct score, 8 for total goals, and 9 for half-time/full-time.
When an official table is unavailable, use exactly
`{"status": "skipped", "reason": "..."}` for that market instead of
substituting a third-party table or an incomplete prize map.

## Official Sporttery Collector

The CLI can read the public JSON endpoints used by the official mobile pages.
These commands only collect public evidence and print JSON; they do not append
ledger records, place purchases, or fill missing independent odds/xG evidence.

```powershell
python -m outputs.tai_cai_tracker.cli sporttery-scan --method concern

python -m outputs.tai_cai_tracker.cli sporttery-fixed-bonus 2040641
```

`sporttery-scan` returns official match identity, kickoff time, sale status, and
visible SPF fixed prizes from the official schedule endpoint. `sporttery-fixed-bonus`
returns the latest published fixed-prize row for SPF, HHAD, CRS, TTG, and HAFU,
including each market's official page URL and `published_at` timestamp when the
official history table contains one.

Use the public official result endpoint to settle every currently pending paper
record without opening individual match-detail pages:

```powershell
python -m outputs.tai_cai_tracker.cli sporttery-settle-batch `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl `
  --match-date 2026-07-26
```

The command reads the current official results once plus each optional historical
date, appends only entries whose official status is `11` and whose final score is
present, and preserves the exact public API URL in every settlement. It never
uses a third-party score fallback or bypasses access controls. A missing official
half-time score leaves a linked HAFU settlement pending while the SPF settlement
can still be appended.

For each date, the collector follows only the official response's `nextPage`
field, retaining the direct URL and raw-response SHA-256 for every fetched page.
It rejects a repeated, malformed, or over-limit page chain instead of treating a
partial page as complete. `sporttery-settle-batch` exposes its cumulative
`official_result_page_count` and duplicate-result count so the official scan is
auditable when a current result page overlaps a dated result page.

```json
{
  "prediction_hash": "<frozen-prediction-hash>",
  "captured_at": "2026-07-26T18:30:00+08:00",
  "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=<id>",
  "official_markets": {
    "spf": {
      "published_at": "2026-07-26T18:00:00+08:00",
      "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=<id>",
      "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"}
    },
    "hhad": {
      "published_at": "2026-07-26T18:01:00+08:00",
      "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=<id>",
      "home_handicap": "-1",
      "fixed_prizes": {"home_win": "4.00", "draw": "3.00", "away_win": "2.00"}
    },
    "crs": {"published_at": "...", "official_source_url": "https://...", "fixed_prizes": {"...all 31 official options...": "..."}},
    "ttg": {"published_at": "...", "official_source_url": "https://...", "fixed_prizes": {"...all 8 official options...": "..."}},
    "hafu": {"published_at": "...", "official_source_url": "https://...", "fixed_prizes": {"...all 9 official options...": "..."}}
  }
}
```

## Public 433 And Free xG Pregame Bundle Helpers

The CLI can also prepare reviewable public evidence inputs without appending to
the ledger. These commands are paper-only. They do not purchase, place bets, or
settle records.

```powershell
python -m outputs.tai_cai_tracker.cli european-433-extract `
  "https://www.433.cool/zqzhishu/data/<match-id>.html"

python -m outputs.tai_cai_tracker.cli fotmob-team-stats `
  "https://www.fotmob.com/teams/<id>/stats/<slug>/teams"

python -m outputs.tai_cai_tracker.cli fotmob-xg-evidence `
  "<home FotMob stats URL>" `
  "<away FotMob stats URL>"

python -m outputs.tai_cai_tracker.cli pregame-bundle `
  <sporttery-match-id> `
  "https://www.433.cool/zqzhishu/data/<match-id>.html" `
  "<home FotMob stats URL>" `
  "<away FotMob stats URL>"

python -m outputs.tai_cai_tracker.cli auto-pregame-bundle `
  <sporttery-match-id>

python -m outputs.tai_cai_tracker.cli rapid-freeze `
  <rapid-match.json> `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl

python -m outputs.tai_cai_tracker.cli rapid-freeze-batch `
  <rapid-batch.json> `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl `
  --max-workers 6

python -m outputs.tai_cai_tracker.cli schedule-driven-freeze `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl `
  --min-lead-minutes 30 `
  --max-lead-minutes 60 `
  --max-workers 2 `
  --sales-cutoff-policy work/sales-cutoff-policy.json

python -m outputs.tai_cai_tracker.cli identity-preflight `
  work/logs/identity-review-queue.json `
  --min-lead-minutes 120 `
  --max-lead-minutes 180 `
  --sales-cutoff-policy work/sales-cutoff-policy.json

python -m outputs.tai_cai_tracker.cli identity-coverage-report `
  --min-lead-minutes 90 `
  --max-lead-minutes 240 `
  --sales-cutoff-policy work/sales-cutoff-policy.json

python -m outputs.tai_cai_tracker.cli fotmob-source-prewarm `
  --hours-ahead 48 `
  --max-workers 2 `
  --source-alias-registry outputs/tai_cai_tracker/data/source_aliases.json

python -m outputs.tai_cai_tracker.cli wikidata-source-prewarm `
  --hours-ahead 48 `
  --max-workers 2

python -m outputs.tai_cai_tracker.cli daily-freeze-status `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl `
  work/logs/scheduler.jsonl

python -m outputs.tai_cai_tracker.cli source-access-check

python -m outputs.tai_cai_tracker.cli match-preflight `
  <resolved-links.json>
```

`pregame-bundle` combines:

1. Sporttery official five-market fixed prizes;
2. a 433 detail-page named-bookmaker immediate 1X2 capture; and
3. automatically discovered SofaScore same-competition home/away xG evidence
   when its event identity passes the strict resolver; and
4. if SofaScore is inaccessible, ambiguous, or mismatched, TheSportsDB may
   supply a separately audited event identity, venue coordinates, and weather
   context. It is accepted only when both verified team-name anchors, competition,
   and the timezone-aware kickoff instant match exactly. It never supplies xG
   evidence or a confirmed lineup; an empty or unverified lineup remains
   `unavailable`.
5. FotMob league-total xG/xGA, then Understat league-total xG/xGA, only when
   the verified SofaScore split is unavailable. When the current xG sample is
   below eight, eligible Understat leagues also attempt the constrained
   same-league season bridge described below.
6. When the earlier sources cannot produce comparable evidence, xGscore's
   public, explicitly displayed pre-match model may be used only after the
   strict discovery and detail-page checks described below.

The official, schedule, and 433 requests run concurrently. Context resolution
starts after official identity is available. A verified SofaScore split becomes
the xG model input directly; FotMob is not requested in that case. An ambiguous,
missing, inaccessible, or timestamp-mismatched context source remains an explicit
unavailable result; the command never attaches a guessed event URL. TheSportsDB
context is recorded with its own provider and collector label, never as SofaScore
evidence.

`auto-pregame-bundle` reads the official match identity and resolves a 433
detail page only when kickoff and both teams are uniquely matched. Its public
433 discovery reads at most three list pages, then applies the same single
team-and-kickoff match requirement across the combined candidates. It defers
FotMob team discovery until the SofaScore split is unavailable. When both teams
already have strict FotMob provider identities, it may read the xGscore public
homepage once, accept only one preview slug matching both provider names, and
then re-check the xGscore detail-page teams and kickoff before use. Pass a
direct 433 detail URL as the optional second argument when the public resolver
cannot establish an unambiguous identity.

After a collection succeeds, `auto-pregame-bundle` may retain only verified
source links in `data/source_link_registry.json`: 433 and xGscore detail URLs
are tied to the exact official match ID, home team, away team, and kickoff; a
FotMob team URL is tied to the explicit official team name. On later runs the registry
skips link discovery, but it never supplies odds or xG values: the live 433
and xG pages are collected again and must still satisfy the normal identity,
timing, and `review_ready` quality gate. Ambiguous, unavailable, or failed
sources are not registered and cannot create a frozen record.

`identity-preflight` uses the same strict match check and, from its one official
schedule read, prewarms both each resolved 433 detail URL and each missing
FotMob team identity before the later freeze window. It de-duplicates a team
across the eligible matches, searches it at most once, and writes only a
strictly resolved public team identity to the mutable source registry. Its JSON
output exposes `source_link_cache_hit_count`,
`source_link_fresh_discovery_count`, `source_link_unresolved_count`,
`fotmob_cache_hit_team_count`, `fotmob_resolved_team_count`, and
`fotmob_unresolved_team_count` so source coverage and avoidable discovery work
remain measurable. Understat aliases remain optional xG-quality upgrades and
do not enter the blocking review queue. A cached URL is only an event locator;
the command still captures the current named-bookmaker row and current xG
evidence immediately before a paper record is frozen.

`identity-coverage-report` is a fast, read-only coverage panel for the current
official schedule. It does not call 433 discovery, collect public provider
pages, write a registry, or touch the immutable ledger. For every competition
it reports exact cached 433 event links, both-side FotMob team links, strict
Understat bilingual alias coverage where the competition is supported, and
official sale/time-window blocks. Its per-match `items` separates
`required_identity_blockers` (433 and FotMob locators needed for normal source
collection), `optional_xg_enhancements` (such as an Understat alias that would
enable a reused venue-split batch), and `operational_blockers` (sale status or
timing). `priority: preflight_required` identifies identity work worth doing
before the collection window; `ready_for_collection` and
`operationally_deferred` distinguish source readiness from the current sales
window. `Understat` `not_covered` and an absent Understat alias are enhancement
availability facts, not a freeze failure, because the normal xG cascade can
still use another traceable source. Use `block_reason_counts` for required and
operational blockers, and `optional_xg_gap_counts` to plan xG-quality upgrades.

`fotmob-source-prewarm` performs one official schedule scan and considers only
currently open fixtures within its bounded future horizon (48 hours by
default). It queries public FotMob team suggestions only for missing team links,
using the same high-confidence, non-ambiguous resolver as the normal collector.
Each accepted result adds only an official-name-to-FotMob-team-URL locator to
`data/source_link_registry.json`; it does not collect xG values, write the
immutable ledger, or freeze a prediction. Later pregame collection reuses those
locators but must still retrieve live xG evidence, validate it before kickoff,
and meet the normal quality gate. The JSON output distinguishes cache hits,
newly resolved teams, and explicit unresolved reasons so a failed lookup never
silently becomes evidence. When a name has already been adopted through the
strict source-alias workflow, the prewarm command uses that recorded provider
name only as its FotMob search term and still applies FotMob's own uniqueness
check; `--source-alias-registry` selects that local alias registry.

`wikidata-source-prewarm` uses the same bounded official schedule scan to cache
only a unique Chinese-name match from Wikidata together with its QID and English
label and exact English aliases. A generic visible `足球俱乐部`/`足球队` suffix
may differ from the official name. When same-name public search candidates exist,
the tracker accepts one only if exactly one candidate is explicitly described as
a football club; otherwise it remains unresolved. It is an identity anchor, not match,
odds, xG, official-market,
or official-result evidence, and it never writes the immutable ledger. A
duplicate exact Chinese-name match or a missing English label is retained as an
explicit unresolved result instead of being guessed. FotMob discovery selects a
search term in this strict order: an already reviewed source alias, a cached
Wikidata English label, then the official Sporttery name. In every case FotMob
still has to return its own unique high-confidence provider identity before the
tracker retains a source locator.

`historical-fotmob-source-prewarm` builds the same strict identity cache from
previously published official result dates. For every historical date slice it
first de-duplicates official Chinese team names and attempts a strict Wikidata
identity anchor. A unique exact Chinese-name result supplies the cached English
label for the later FotMob search; it never becomes match or xG evidence. A
Wikidata requests are globally spaced by at least five seconds and this
historical command attempts at most three uncached identities per invocation.
Wikidata `401`/`403`/`429` stops only the remaining Wikidata lookups in that
run and stores the affected name in a persistent retry queue. It honors a
public `Retry-After` header when present (otherwise it uses a one-hour
checkpoint cooldown); network failure uses a 12-hour cooldown and is queued
the same way. The next invocation processes eligible queued names before new
historical names, including after the date cursor has reached the end of its
range, so a rate-limited name is never lost solely because the cursor advanced.
Names first encountered while a network cooldown is open are likewise queued
instead of discarded. On upgrade, unresolved historical FotMob attempts that
never reached Wikidata are recovered into that queue before a completed date
cursor can hide them. Other HTTP failures remain visible failures and do not
open the circuit. It
initializes a fixed 18-month (`548` day) local-China date range, reads at most
seven official result dates per run, and resumes from
`data/historical_fotmob_prewarm_progress.json` on the next invocation. The
range remains fixed after its first run so a daily re-run cannot silently reset
or re-scan it. It de-duplicates official home/away names, skips validated cache
hits and prior attempted names, and searches each remaining name at most once
through the normal public FotMob strict resolver. Only a unique high-confidence
identity is retained; an ambiguous, missing, or access-restricted response is
recorded in the mutable progress file with its concrete reason. The command
never writes the immutable paper ledger or creates any observation, prediction,
snapshot, or settlement record.

The checkpoint also stores the exact FotMob search name used for every attempt.
An unresolved team is skipped only when that search name is unchanged. If a
later strictly adopted provider alias supplies a different search name, the
historical command retries the team once with that new name and still accepts
only FotMob's own unique high-confidence result.

Historical Wikidata attempts carry an identity-strategy version. A future
strict resolver improvement automatically retries unresolved identities made by
an older strategy once; it never changes a cached QID or treats a retry as a
verified provider identity.

```powershell
python -m outputs.tai_cai_tracker.cli historical-fotmob-source-prewarm `
  --days-back 548 `
  --max-dates-per-run 7 `
  --max-workers 2
```

For the faster 180-day identity graph, keep a separate checkpoint so it does
not mix with an existing historical range:

```powershell
python -m outputs.tai_cai_tracker.cli historical-fotmob-source-prewarm `
  --days-back 180 `
  --max-dates-per-run 2 `
  --max-workers 1 `
  --progress-path outputs/tai_cai_tracker/data/historical_identity_graph_180_progress.json
```

Use `--progress-path` only to select a different mutable checkpoint file. A
changed `--days-back` rejects the existing checkpoint instead of mixing date
ranges. Start a separate progress file to deliberately build another range.

For daily continuation, use the rolling command instead of re-running the
fixed historical range. It scans open fixtures in the next 72 hours and the
three official-result dates ending yesterday. For open fixtures it first
prewarms strict public Wikidata English-name anchors, then queries FotMob using
those anchors or an already reviewed alias when available; it never falls back
to a blind Chinese-name FotMob query in daily mode. It seeds its separate daily checkpoint from prior
historical attempts, so an unchanged unresolved Chinese name is not searched
again in the recent-result path. Only strict FotMob provider matches are added
to the mutable source-link registry; the command never writes the paper ledger.
Wikidata requests in this batch have a five-second bound. A network failure on
the first uncached lookup opens a 12-hour source-level circuit in
`data/daily_fotmob_identity_progress.json`: remaining names are reported as
skipped for that run, and subsequent daily runs make no Wikidata network
request until the circuit expires. Failed access never becomes an identity
mapping claim; after expiry, the normal strict lookup is retried.
After verifying a local transport repair, use `--retry-wikidata-now` once to
clear only this mutable network cooldown and retry strict lookup immediately.
It does not bypass source access controls, relax identity matching, or write
the paper ledger.

```powershell
python -m outputs.tai_cai_tracker.cli daily-fotmob-identity-map
```

Use `--result-days 1` through `7` to change the recent-result window and
`--hours-ahead` to change the upcoming-fixture horizon. The JSON result keeps
the Wikidata, upcoming-FotMob, and recent-results paths separately as
`wikidata`, `upcoming`, and `recent_results`, and states `ledger_action: "none"`.

### Reviewed FotMob search bridge

`historical-fotmob-alias-candidates` turns unresolved historical names into a
replaceable review queue. It reads the mutable historical checkpoint, gives
priority to names appearing in the current official schedule, then ranks by
observed occurrence count and recency. For each selected name it captures the
public FotMob search response, including the English query, direct search URL,
capture time, raw-response SHA-256, and returned team candidates. The queue is
not evidence of a resolved identity: it never writes `source_link_registry`,
the immutable ledger, a prediction, or an xG record.

```powershell
python -m outputs.tai_cai_tracker.cli historical-fotmob-alias-candidates `
  --max-teams 25
```

New historical scans preserve each returned official result-date URL, match
date, and available match identifiers beside the attempted official team name.
Older checkpoints that predate this field keep an explicit empty reference list;
the queue does not infer a missing match URL from it.

After a bilingual review, import only an auditable **search keyword**. This is
kept in the separate mutable `data/fotmob_search_aliases.json` registry, not in
the strict source-alias or provider-identity registries. The import does not
contain or create a FotMob team ID. Its `fotmob_search_url` and response hash
show exactly what was reviewed; later `historical-fotmob-source-prewarm` still
requires FotMob's normal unique, high-confidence resolution before it records a
team identity.

```json
{
  "aliases": [{
    "official_team": "AC米兰",
    "search_name": "AC Milan",
    "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2042001",
    "fotmob_search_url": "https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term=AC%20Milan",
    "captured_at": "2026-09-08T15:00:00+08:00",
    "raw_content_sha256": "<64 lowercase hexadecimal characters>"
  }]
}
```

```powershell
python -m outputs.tai_cai_tracker.cli fotmob-search-alias-import `
  work/reviewed-fotmob-aliases.json

python -m outputs.tai_cai_tracker.cli historical-fotmob-source-prewarm `
  --reviewed-alias-registry outputs/tai_cai_tracker/data/fotmob_search_aliases.json
```

`historical-fotmob-failure-report` is a read-only diagnosis panel for the
historical checkpoint. New attempts carry an explicit `failure_code`, category,
and remediation: public-search absence, strict mismatch, strict ambiguity,
public access restriction, transport failure, and parse failure remain distinct.
For entries created before structured codes existed, the report marks every
classification as `legacy_reason_text`; it is a conservative prioritization
signal, not newly collected FotMob evidence.

```powershell
python -m outputs.tai_cai_tracker.cli historical-fotmob-failure-report
```

`sporttery-settle-batch` first checks the current official result page, then
automatically checks the distinct local kickoff dates of every pending frozen
primary record. This closes the cross-day gap where a prior-day fixture is no
longer returned by the default result page. It still appends a settlement only
when the dated official response supplies the matching weekday sequence, a
completed status, an unambiguous final score, and a direct official result URL.
For recurring weekday sequences, it additionally requires the same kickoff
date and both official team names; sequence alone cannot settle a fixture.

### Rapid verified-source workflow

`rapid-freeze` is the operational path after a reviewed named-bookmaker row
has been collected. It fetches the official schedule once, then fetches the
official five-market table and both FotMob teams concurrently. Before it writes
anything, it applies the same canonical pregame quality gate, then builds and
validates a named-bookmaker capture, one SPF primary prediction, and one
full-market snapshot in memory. Any missing, late, or observation-only source
leaves the match out of the ledger without a partial primary prediction.

The command prints only the three frozen hashes and its quality gate, rather
than a full scoreline matrix. It does not read or click external bookmaker
links. A minimal input is:

```json
{
  "match_id": 2041050,
  "home_fotmob_url": "https://www.fotmob.com/teams/<home-id>/overview/<home-slug>",
  "away_fotmob_url": "https://www.fotmob.com/teams/<away-id>/overview/<away-slug>",
  "odds_capture": {
    "provider": "433足球网",
    "source_url": "https://www.433.cool/zqzhishu/data/<match-id>.html",
    "bookmaker": "<visible bookmaker name>",
    "captured_at": "2026-08-25T13:47:37+08:00",
    "odds": {"home_win": "2.33", "draw": "3.20", "away_win": "3.07"}
  }
}
```

`rapid-freeze-batch` accepts `{"jobs": [<rapid-match object>, ...]}`. It reads
the Sporttery schedule once, collects each job independently with bounded
concurrency, and appends only complete records. A blocked source affects only
that job and appears with its concrete reason in `items`; it never causes a
fallback to unnamed odds or inferred xG data.

`schedule-driven-freeze` is the automatic paper-only path. It reads the
official Sporttery schedule once per run, then considers only `已开售` matches
that do not already have a frozen primary prediction. An ordinary fixture uses
the configured pre-kickoff lead-time window. A fixture scheduled after the
local sales-day cutoff instead uses the earlier cutoff window from
`work/sales-cutoff-policy.json`: 60 to 30 minutes before cutoff by default.
The default local policy is 22:00 Monday through Friday and 23:00 Saturday and
Sunday in `Asia/Shanghai`, based on the task's local run date so a Friday-night
sale cutoff also governs a Saturday-after-midnight fixture. It passes the
already collected official identity into the pregame collector, so eligible
matches do not re-fetch the schedule. External 433, xG, and five-market
evidence is collected only for those eligible matches with bounded concurrency.

The sales-cutoff JSON is an operator-observed local scheduling policy, not an
official Sporttery market source or a substitute for any evidence. Status `2`
(`已开售`) remains required at collection time. A status of pending or suspended
sale creates a concrete scheduler skip with the observed status; it never
triggers external collection or a frozen primary. `identity-preflight` reads
the same policy and advances unresolved late-fixture identities from 180 to 120
minutes before the cutoff collection window. Scheduler and daily-status output retain the local
timing basis and cutoff alongside each operational skip.

When one schedule scan finds zero `已开售` matches, `schedule-driven-freeze`
also returns a read-only `sale_availability` object. The official open-sale
time is reported as `null` with
`not_published_by_current_official_schedule` unless the current official
schedule actually displays it. `next_system_check_at` is instead the next
local sales-cutoff collection-window start from the configured policy; its
`next_system_check_basis` explicitly states that it is a local scheduling
time, not an official Sporttery open-sale time. This operational output never
triggers external collection or writes to the ledger.

For one `schedule-driven-freeze` run, exact-identity 433 links found by an
earlier preflight are used first. Remaining cache misses read no more than
eight public list pages once for all eligible fixtures, then apply the same
per-fixture unique home-team, away-team, and kickoff check. Newly verified
links are retained for later runs. A fixture absent from that batch result is
blocked for the run rather than triggering another per-fixture list scan. The
normal direct `auto-pregame-bundle` command retains its bounded three-page
discovery for a single user-requested fixture.

The same batch run reads the public xGscore homepage at most once for all
eligible fixtures. It first uses two cached, strict FotMob provider identities.
When either FotMob identity is unavailable, it may instead use both teams'
cached, exact Wikidata English labels and aliases as discovery anchors.
Wikidata is only an identity bridge: the homepage still needs one unique preview
slug, and the xGscore detail page must still verify both teams and the exact
kickoff before use or retention. Its JSON output reports
`xgscore_cache_hit_count`, `xgscore_candidate_count`, and
`xgscore_unavailable_count`, plus the FotMob and Wikidata anchor fixture
counts; an unavailable or ambiguous candidate never triggers per-fixture
homepage retries.

The same schedule run can reuse one Understat league response for every
eligible fixture in the same `league + season`. A strict SofaScore event may
create exact provider-name aliases, or a user may import an Understat-specific
alias after a manual bilingual review with the official and Understat URLs,
capture time, team ID, and response SHA-256. Those explicit aliases take
priority. When an alias is missing, a freshly captured Understat roster may
resolve the official team only if exactly one roster title exactly matches one
cached Wikidata English label or alias. This temporary roster match is not
stored as a provider alias. Missing anchors, ambiguous roster matches,
unsupported competitions, identity mismatches, and batch failures remain
explicit unavailable evidence; the tracker neither transliterates names nor
retries a failed batch one match at a time.

The command appends an odds capture and one primary SPF market-baseline
prediction when the official SPF table and a traceable named-bookmaker 1X2
capture are both available before kickoff. This baseline has no xG model,
scoreline, total-goals, or half-time/full-time output. A five-market snapshot
is appended only when the pregame quality gate is `review_ready`: traceable
named-bookmaker 1X2 and complete pre-kickoff official markets are always
required. Home/away-split xG plus confirmed lineups is the high-confidence
path. Traceable `league_total` or `source_match_model` xG is also eligible only
when its reported sample is at least eight matches (or both reported home and
away sample counts meet that floor); it remains medium confidence and preserves
scope and lineup risks.
The same gate accepts a traceable Understat season bridge only with at least two
current-season venue matches per side, at least twelve prior-season venue
matches per side, the same named competition, and exact provider team-name
continuity. It is always medium confidence. All other bridge attempts remain
observation-only.
When xG is the only unresolved enhanced-evidence condition, and all five
official tables plus a named European 1X2 capture are complete before kickoff,
the schedule command appends an `official_market_baseline` v3 snapshot. Its
SPF probabilities remain the frozen named-bookmaker baseline; HHAD, CRS, TTG,
and HAFU are normalized reciprocal probabilities from their own official fixed
prizes. It contains no xG, Poisson score matrix, lineup, or context claim. The
summary reports `freeze_level: "official_market_five_market"`, and all-market
reporting keeps it separate from `model_derived` snapshots.

For any other incomplete-enhanced case where the official SPF table and a
traceable named-bookmaker 1X2 capture are available, the JSON summary reports
`freeze_level: "market_baseline"`, retains the unresolved evidence reasons,
and marks the five-market snapshot `not_frozen`. Incomplete baseline evidence
remains `observation_only` or `blocked`; it never creates a frozen primary
record. This preserves the immutable ledger without forcing untraceable
predictions for every scheduled match.

Every `schedule-driven-freeze` item also includes read-only
`collection_diagnostics`: the quality gate, missing required inputs, current
`xg_source_cascade`, verified locator URLs, and, when collected, official
market statuses. This lets an operator distinguish an identity gap, an xG
source gap, or an official-market gap without treating diagnostics as frozen
evidence or appending them to the paper ledger.

`match-preflight` is the single-match shortcut after `resolve-links`: it reads
the resolved candidate JSON, requires all three accepted source URLs, preserves
the official kickoff and team identity as fallbacks, and runs the same
read-only pregame bundle. It never writes the ledger or freezes a prediction.

It prints:

- `odds_import_input`: feed this to `odds-import`;
- `prediction_input_template`: fill the printed odds-capture hash, then feed it
  to `snapshot`;
- `all_market_snapshot_input_template`: fill the printed prediction hash, then
  feed it to `all-market-snapshot`;
- `model_preview`: read-only Poisson preview for review before freezing.
- `quality_gate`: `review_ready`, `observation_only`, or `blocked`, with exact
  evidence and timing reasons.
- `xg_source_cascade`: read-only xG selection audit. It identifies the selected
  source, its scope, reported sample and quality tier, plus sources that were
  available but not selected or deliberately not collected because a stronger
  already-captured home/away split was available. It is runtime audit metadata,
  not frozen ledger evidence.
- `evidence_event_inputs`: reviewed `evidence-record` inputs for collected
  sources whose exact response hash and ledger-compatible match identity are
  available. It can contain one template per official frozen market, one
  named-bookmaker European 1X2 template, one template per FotMob xG stat
  response, and one strictly matched SofaScore event-context template.

`review_ready` means the captured evidence is internally complete enough for
paper review, not that an outcome is assured. An adequately sampled
`league_total` or `source_match_model` record is `review_ready` with a `medium`
confidence ceiling; its scope, unavailable lineup, and unresolved context remain
explicit risk reasons. A qualifying season bridge is also `review_ready` with
the fixed `medium` ceiling; its unavailable publication time is retained with
the actual pre-kickoff capture time and explicit reason. `observation_only` is
returned for low-sample or otherwise limited evidence. `blocked` means a
required source is missing or one of the relevant publication/capture timestamps
is not strictly before kickoff. The command does not alter a frozen model
weight or append a prediction automatically.
`evidence_event_inputs` is also read-only: review its direct URL, timestamp,
raw response hash, provider, bookmaker, and prices, then append a selected item
with `evidence-record`. Official, FotMob, and SofaScore collectors preserve a
SHA-256 hash of each exact raw HTTP response used for these templates; they
never combine different responses into one hash. A template is absent for a
source that lacks a compatible official match sequence or its own raw response
hash. No collector automatically appends a ledger record.

If the match is no longer in the current Sporttery schedule, provide auditable
manual identity fields:

```powershell
python -m outputs.tai_cai_tracker.cli pregame-bundle `
  <sporttery-match-id> "<OddsPortal URL>" "<home FotMob URL>" "<away FotMob URL>" `
  --kickoff-at "2026-07-27T18:30:00+08:00" `
  --home-team "FC Seoul" `
  --away-team "Ulsan HD FC" `
  --competition "K League 1"
```

SofaScore xG is `scope: "home_away_split"` only after the strict event match
and both same-competition home/away statistic responses are available. FotMob
and the single-match Understat fallback remain `scope: "league_total"`, so
their confidence ceiling remains medium. The separately verified Understat
batch and season-bridge paths retain their own venue-split evidence rules.

### Understat xG fallback and same-league batches

Understat is a public league-total team xG/xGA fallback automatically used by
`pregame-bundle` when SofaScore is unavailable and FotMob fails or has no
current-season sample, when the competition is covered. It exposes no publication time in its league response, so the
collector preserves the direct data URL, response SHA-256, and actual capture
time with a `published_at_unavailable_reason`. Use it only before kickoff; it
remains `league_total` with a `medium` confidence ceiling.

When a same-action Understat batch result has already produced a traceable,
adequately sampled `home_away_split`, `pregame-bundle` reuses it before asking
xGscore or FotMob for a lower-tier fallback. It records the decision in
`xg_source_cascade`; unselected sources do not become frozen xG evidence.

For an eligible early-season fixture, the collector may replace sub-eight-sample
current evidence with a same-league Understat `season_bridge` venue split. It
fetches the current and immediately preceding seasons separately, keeps both
direct URLs, response SHA-256 values, capture times, raw xG/xGA totals, venue
sample counts, and blending formula in the frozen evidence. The bridge requires
at least `2 + 12` venue matches per side (current + prior), the same competition,
and exact Understat team-name continuity. Promotion, relegation, cross-league
data, a renamed or missing provider team, or an insufficient source sample
rejects the bridge rather than guessing a match. Current-season influence is
`min(0.70, current_effective / (current_effective + prior_effective))`; the
remainder is prior-season influence. It is never above medium confidence.

```powershell
python -m outputs.tai_cai_tracker.cli understat-league-xg `
  Serie_A 2026 Bologna Lazio
```

For several fixtures in one Understat-covered league, use the read-only batch
collector. It makes one `getLeagueData` request for the whole `league + season`,
then derives each fixture from the home team's `h_a: "h"` history rows and the
away team's `h_a: "a"` rows. A reviewed exact Understat alias is used first;
otherwise an exact cached Wikidata English label or alias may locate exactly
one title in this freshly captured provider roster. The anchor is never stored
as an Understat identity. This creates `home_away_split` evidence only when
both venue samples are positive; a missing or ambiguous team match, or missing
venue sample, remains an explicit per-fixture unavailable result. Each result retains the direct data
URL, capture time, no-publication-time reason, response SHA-256, raw venue
totals, sample counts, and formula. It does not write the ledger.

Use `--rolling-matches 8` for a rolling form window. In that mode every fixture
must include the official, timezone-aware `kickoff_at`. Understat history only
exposes an unzoned calendar date, so the collector uses only rows dated strictly
before the kickoff calendar date and excludes all same-day rows rather than
guessing their time order. The evidence records the requested window, cutoff
date, and effective first/last date for each team's sample.

```json
{
  "league": "Serie_A",
  "season": "2026",
  "fixtures": [
    {"match_id": "2041001", "home_team": "Bologna", "away_team": "Lazio", "kickoff_at": "2026-08-30T20:45:00+08:00"},
    {"match_id": "2041002", "home_team": "Roma", "away_team": "Inter", "kickoff_at": "2026-08-31T20:45:00+08:00"}
  ]
}
```

```powershell
python -m outputs.tai_cai_tracker.cli understat-batch-xg `
  work/understat-serie-a-2026.json `
  --rolling-matches 8
```

### xGscore strict event fallback

`xGscore` is a public fallback for fixtures whose other free xG sources cannot
form comparable evidence. The current public layout exposes an explicit source
pre-match score model, two raw team aggregate xG/xGA blocks, source impact
weights, and home/away sample rows. The collector preserves these fields as
`scope: "source_match_model"`, never calls them a venue split, and fixes the
confidence ceiling at `medium`. It requires an exact detail-page team and
kickoff match; missing season metadata is retained as `source_not_exposed`, not
inferred. Automatic discovery is limited to a unique public homepage preview
whose slug matches two previously verified FotMob provider names, or, only when
those are unavailable, two cached exact Wikidata English-name sets. Wikidata
remains a candidate locator only. Ambiguous previews or failed exact detail-page
team and kickoff verification are discarded. Capture time and the
missing-publication-time reason are always retained.

```powershell
python -m outputs.tai_cai_tracker.cli xgscore-extract `
  "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore" `
  Molde KFUM "2026-09-06T23:00:00+08:00" "Eliteserien"

python -m outputs.tai_cai_tracker.cli auto-pregame-bundle 2041310 `
  --xgscore-url "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore"
```

To make a reviewed bilingual mapping reusable without SofaScore, import a
strict Understat alias file. This writes only the alias registry, never the
paper ledger or a prediction.

```powershell
python -m outputs.tai_cai_tracker.cli understat-alias-import `
  work/understat-aliases.json
```

Each input has `provider: "Understat"`,
`confirmation_method: "manual_bilingual_review"`, and one or more entries with
the official Chinese team and Sporttery URL, exact Understat team name and ID,
Understat `getLeagueData` URL, timezone-aware capture time, and the 64-character
SHA-256 of that response. The command rejects guessed names, non-Understat
source URLs, missing source IDs, duplicate official teams, and conflicts with
an existing Understat identity.

Set `TAICAI_HTTP_PROXY` when a public provider needs an explicitly configured
proxy; it has priority over every other proxy setting. On Windows only, when
that variable is absent, collectors may use the enabled Internet Settings
proxy only when it is a loopback endpoint (`127.0.0.1`, `::1`, or `localhost`)
with an HTTP(S) URL and valid port. This supports local proxy clients without
silently routing through a remote or ambient `HTTP_PROXY` value. Collectors
otherwise ignore ambient proxy variables.

`source-access-check` is read-only. It checks the public JSON endpoints used by
Sporttery, 433, Understat, and SofaScore before a batch run. It reports each
source as `available`, `access_restricted` (HTTP 401, 403, or 429),
`network_error`, or `invalid_response`; it never creates evidence or ledger
records. When a proxy is needed, configure the actual listening endpoint
explicitly. The report shows only its scheme, host, and port, never credentials.

```powershell
$env:TAICAI_HTTP_PROXY = "http://127.0.0.1:<actual-listening-port>"
python -m outputs.tai_cai_tracker.cli source-access-check
```

### Collection throughput and evidence freshness

Public collectors coordinate concurrent requests inside one CLI process. An
identical URL requested at the same time is fetched once and its exact response
is shared with waiting collectors. Requests to different URLs remain parallel,
but each provider has a bounded in-flight limit: Sporttery and FotMob use four,
Understat, Open-Meteo, and 433 use two, and SofaScore uses one. This
keeps a batch from flooding a rate-sensitive source while allowing independent
sources to run together.

The coordinator intentionally does not reuse a completed response for a later
collection action. Every later action gets a fresh capture time and raw-content
hash, preserving evidence freshness. HTTP `403`, `429`, login, CAPTCHA, or
regional restriction responses are not retried as a workaround; callers report
the source as unavailable with the relevant evidence reason.

`understat-batch-xg` is the explicit same-action exception to per-fixture
fetching: every fixture in its input is derived from the one freshly captured
league response, so a same-league list avoids repeated identical downloads
without carrying a response into a later collection action.

The design follows the public scheduling and page/context-cap guidance in
[scrapy-playwright](https://github.com/scrapy-plugins/scrapy-playwright) and
the queue, configurable retry, and concurrency patterns documented by
[Crawlee](https://github.com/apify/crawlee). The tracker keeps a small
standard-library implementation rather than adding either framework to its
Windows runtime.

## Optional Pregame Context

The review-only `pregame-context` command accepts a local JSON identity with
`competition`, `home_team`, `away_team`, and timezone-aware `kickoff_at`, plus
a public event JSON URL. It verifies the event identity before attempting
home/away xG, Open-Meteo weather at the provider-supplied venue coordinates,
and the provider's lineup state.

Automatic pregame collection can use previously verified team-name anchors
when the official schedule uses a different language. The accepted order is a
strictly adopted SofaScore alias, then a complete cached FotMob team identity,
then cached Wikidata English labels. These anchors are collection-only identity
bridges: they are not frozen as context facts or retroactively added to ledger
records. The SofaScore event detail must still match one verified name for
each side, the official competition, and the exact official kickoff before a
`pregame_context` can be retained.

```powershell
python -m outputs.tai_cai_tracker.cli pregame-context `
  work/official-identity.json `
  "https://www.sofascore.com/api/v1/event/<event-id>"
```

Each item is `available`, `reported`, `confirmed`, or `unavailable` with a
reason and source. It never writes the ledger. A returned `pregame_context`
may be included in a snapshot input and is frozen unchanged after schema
validation. This first phase does not alter Poisson xG values, blend weights,
or market selections from weather or lineup observations; missing context
automatically remains a risk flag rather than a guessed value.

## Match Link Resolver

`candidate-template` prints a blank `resolve-links` input file from one current
official Sporttery `match_id`. It copies only official identity fields and
leaves independent candidate links empty.

```powershell
python -m outputs.tai_cai_tracker.cli candidate-template 2040623

python -m outputs.tai_cai_tracker.cli candidate-template 2040623 `
  --output work/candidates/2040623.json
```

`resolve-links` ranks user-supplied OddsPortal and FotMob candidate links for
one Sporttery match. It is conservative: ambiguous or low-confidence candidates
are skipped and must be manually confirmed. Before scoring, it accepts only
HTTPS URLs on the expected provider domain (`oddsportal.com` for market links;
`fotmob.com/teams/...` for team-stat links). Rejected inputs remain in the JSON
result with a precise reason such as `source_url_domain_mismatch`, so a source
adapter can be added without silently retrying an unsafe link. The command only
prints JSON and does not append to the ledger.

```powershell
python -m outputs.tai_cai_tracker.cli resolve-links candidates.json

python -m outputs.tai_cai_tracker.cli resolve-links candidates.json `
  --alias-registry work/aliases.json `
  --output work/resolved/2040623.json

python -m outputs.tai_cai_tracker.cli resolve-context-event official-identity.json `
  --alias-registry work/aliases.json

python -m outputs.tai_cai_tracker.cli link-summary work/resolved/2040623.json

python -m outputs.tai_cai_tracker.cli workflow-next 2040623

python -m outputs.tai_cai_tracker.cli workflow-next 2040623 --plain
```

When all three links are ready, `link-summary` also prints
`pregame_bundle_command`, a copyable next-step command for `pregame-bundle`.
`workflow-next` checks local candidate/resolved files and prints the next
command to run for that match.

An alias registry is optional. It is written only after `resolve-context-event`
uniquely adopts a strict SofaScore event, and records the Sporttery team name
against that source's exact team name and ID. Passing the same registry to
`resolve-links` improves future cross-language candidate matching without
lowering the match-score, date, or ambiguity thresholds. A missing or invalid
registry is ignored; it never produces a guessed candidate or a ledger entry.

Input JSON shape:

```json
{
  "sporttery": {
    "match_id": "2040623",
    "home_team": "首尔",
    "away_team": "蔚山HD",
    "kickoff_at": "2026-07-27T18:30:00+08:00"
  },
  "oddsportal_candidates": [
    {
      "home_team": "FC Seoul",
      "away_team": "Ulsan HD FC",
      "kickoff_at": "2026-07-27T18:30:00+08:00",
      "source_url": "https://www.oddsportal.com/football/h2h/..."
    }
  ],
  "home_fotmob_candidates": [
    {"team_name": "FC Seoul", "source_url": "https://www.fotmob.com/teams/..."}
  ],
  "away_fotmob_candidates": [
    {"team_name": "Ulsan HD FC", "source_url": "https://www.fotmob.com/teams/..."}
  ]
}
```

## Verification

Run the complete tracker test suite from the workspace root:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```
