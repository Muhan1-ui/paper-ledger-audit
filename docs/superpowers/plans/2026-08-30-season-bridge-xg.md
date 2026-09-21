# Season-Bridge xG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add traceable, same-league prior-season venue xG to early-season paper records and ensure every automatic freeze path honors one quality gate.

**Architecture:** Extend `understat.py` with a collector that loads the current and immediately preceding league season, validates provider team and competition continuity, and produces a medium-confidence `home_away_split` bridge input. `pregame.py` chooses this input only when current season evidence is below eight samples. `cli.py` obtains the canonical pregame bundle before staging rapid freezes, so `rapid-freeze`, batch freezes, and schedule-driven freezes share the same `review_ready` decision.

**Tech Stack:** Python standard library, `Decimal`, `unittest`, public Understat and existing Sporttery/433/FotMob collectors.

---

### Task 1: Define season-bridge evidence behavior

**Files:**
- Modify: `D:\TaiCaiTracker\work\tai_cai_tracker_tests\test_understat.py`
- Modify: `D:\TaiCaiTracker\outputs\tai_cai_tracker\understat.py`

- [ ] **Step 1: Write failing tests for the bridge threshold and traceability**

Add fixtures with two teams that have two current venue rows and twelve prior
venue rows. Assert the desired output shape before adding production code:

```python
result = build_season_bridge_xg(current_payload, prior_payload, fixture)

self.assertEqual(result["xg_inputs"]["confidence_ceiling"], "medium")
self.assertEqual(result["xg_inputs"]["evidence"]["scope"], "home_away_split")
self.assertEqual(result["xg_inputs"]["evidence"]["current_season"]["sample_matches"], 2)
self.assertEqual(result["xg_inputs"]["evidence"]["prior_season"]["sample_matches"], 12)
self.assertIn("season_bridge", result["xg_inputs"]["evidence"]["formula"])
self.assertIn("raw_content_sha256", result["xg_inputs"]["evidence"]["current_season"])
self.assertIn("raw_content_sha256", result["xg_inputs"]["evidence"]["prior_season"])
```

- [ ] **Step 2: Run the new test to verify it fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_understat.UnderstatTests.test_build_season_bridge_xg_requires_two_current_and_twelve_prior_venue_samples -v
```

Expected: failure because `build_season_bridge_xg` does not exist.

- [ ] **Step 3: Implement the smallest bridge builder**

In `understat.py`, add a pure `build_season_bridge_xg(current, prior, fixture,
model_weight="0.40")` helper. It must:

```python
if current["competition"] != prior["competition"]:
    raise ValueError("season bridge requires the same competition")
if min(current["home_sample_matches"], current["away_sample_matches"]) < 2:
    raise ValueError("season bridge current sample is below two matches")
if min(prior["home_sample_matches"], prior["away_sample_matches"]) < 12:
    raise ValueError("season bridge prior sample is below twelve matches")
```

Calculate each current and prior venue rate from raw totals and samples. Use
`current_weight = min(Decimal("0.70"), current_effective / (current_effective + prior_effective))`,
then derive home and away expected goals from the blended home attack/away
defence and away attack/home defence rates. Retain both payload URLs, hashes,
capture times, unavailable-publication-time reasons, raw values, sample sizes,
and the complete formula in `xg_inputs.evidence`.

- [ ] **Step 4: Run the focused bridge test**

Run the Step 2 command.

Expected: `OK`.

- [ ] **Step 5: Add rejection tests and run the Understat module tests**

Add independent tests for different competition names, a current sample below
two, and a prior sample below twelve. Each must assert the specific
`ValueError` message. Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_understat -v
```

Expected: `OK`.

### Task 2: Collect and validate both Understat seasons

**Files:**
- Modify: `D:\TaiCaiTracker\work\tai_cai_tracker_tests\test_understat.py`
- Modify: `D:\TaiCaiTracker\outputs\tai_cai_tracker\understat.py`

- [ ] **Step 1: Write a failing collection test**

Use the existing injected public-request test seam to supply two deterministic
league payloads. Assert that `collect_season_bridge_xg` requests exactly the
current and immediately previous season, retains both response hashes, and
excludes same-day rows using the fixture kickoff calendar date.

```python
result = collect_season_bridge_xg(
    {"competition": "Premier League", "season": "2026", "home_team": "A", "away_team": "B", "kickoff_at": "2026-08-30T20:00:00+08:00"}
)

self.assertEqual(requested_seasons, ["2026", "2025"])
self.assertEqual(result["xg_inputs"]["evidence"]["prior_season"]["season"], "2025")
```

- [ ] **Step 2: Run the collection test to verify it fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_understat.UnderstatTests.test_collect_season_bridge_xg_reads_current_and_prior_same_league_payloads -v
```

Expected: failure because `collect_season_bridge_xg` does not exist.

- [ ] **Step 3: Implement the public collection wrapper**

Add `collect_season_bridge_xg(fixture, model_weight="0.40")`. Derive the
previous integer season only when the configured league season is numeric.
Fetch the two public league payloads with the existing transport helper; do
not retry an access-restricted response. Reuse current Understat parsing for
each venue split, then call `build_season_bridge_xg`.

Return a clear unavailable error for a nonnumeric season, absent team,
cross-league payload, or incomplete venue history. Do not add aliases or
translate a provider team name without an exact payload match.

- [ ] **Step 4: Run the collection test and the whole Understat module**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_understat -v
```

Expected: `OK`.

### Task 3: Route low-sample xG through the bridge

**Files:**
- Modify: `D:\TaiCaiTracker\work\tai_cai_tracker_tests\test_pregame.py`
- Modify: `D:\TaiCaiTracker\outputs\tai_cai_tracker\pregame.py`

- [ ] **Step 1: Write failing routing tests**

Mock the existing FotMob result with one current-season sample and the bridge
collector with a valid `2 + 12` payload. Assert `collect_pregame_bundle` uses
the bridge input. Add a separate test showing an eight-sample FotMob result
does not call the bridge collector.

```python
self.assertEqual(bundle["evidence"]["xg"]["provider"], "Understat season bridge")
self.assertEqual(bundle["quality_gate"]["status"], "review_ready")
self.assertEqual(bundle["quality_gate"]["confidence_ceiling"], "medium")
bridge.assert_not_called()
```

- [ ] **Step 2: Run the new routing test to verify it fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_pregame.PregameBundleTests.test_collect_bundle_uses_season_bridge_for_low_sample_current_xg -v
```

Expected: failure because the bridge collector is not selected.

- [ ] **Step 3: Implement narrowly scoped fallback routing**

In `pregame.py`, add a helper that reads current xG sample sizes. After a
successful FotMob or Understat current-season collection, call the bridge only
when the current evidence is below the existing eight-sample floor. Preserve
the current evidence when bridge collection fails and return
`observation_only`, not a guessed replacement.

Add `season_bridge_xg` to quality-gate reasons. Keep the pre-kickoff evidence
time checks and force `confidence_ceiling: medium` for a bridge input.

- [ ] **Step 4: Run the focused pregame tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_pregame -v
```

Expected: `OK`.

### Task 4: Make rapid freezes honor the common quality gate

**Files:**
- Modify: `D:\TaiCaiTracker\work\tai_cai_tracker_tests\test_cli.py`
- Modify: `D:\TaiCaiTracker\outputs\tai_cai_tracker\cli.py`

- [ ] **Step 1: Write failing single and batch freeze tests**

Create one low-sample `league_total` rapid job and one review-ready bridge
job. Assert the low-sample job appends zero records while the bridge job
appends three linked records.

```python
result = tracker_cli.rapid_freeze_batch(manifest_path, ledger_path, max_workers=2)

self.assertEqual(result["frozen_count"], 1)
self.assertEqual(result["blocked_count"], 1)
self.assertEqual(len(read_records(ledger_path)), 3)
self.assertEqual(result["items"][0]["reason"], "xg_sample_below_minimum")
```

- [ ] **Step 2: Run the freeze test to verify it fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_rapid_freeze_batch_keeps_observation_only_job_out_of_ledger -v
```

Expected: failure because rapid freezing currently stages records without the
canonical quality gate.

- [ ] **Step 3: Refactor rapid staging to consume a canonical bundle**

In `cli.py`, build the exact official/odds/xG bundle before freezing, invoke
`quality_gate`, and immediately return a no-record stage for any status other
than `review_ready`. Preserve the existing atomic staging behavior: only
append the odds capture, primary prediction, and five-market snapshot after
all three have validated.

Use the gate result in the summary instead of the old ad hoc
`xg_inputs_is_league_total` label. Do not alter an already frozen ledger line.

- [ ] **Step 4: Run focused CLI tests**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli -v
```

Expected: `OK`.

### Task 5: Document and verify end-to-end behavior

**Files:**
- Modify: `D:\TaiCaiTracker\outputs\tai_cai_tracker\README.md`
- Modify: `D:\TaiCaiTracker\work\tai_cai_tracker_tests\test_cli.py`

- [ ] **Step 1: Add a failing end-to-end bridge freeze test**

Use public-collector fakes to stage a valid bridge evidence bundle, named
bookmaker capture, and five complete official markets. Assert exactly three
append-only records, `confidence_ceiling: medium`, and preserved current/prior
source blocks in the frozen prediction.

- [ ] **Step 2: Run it to verify it fails before documentation changes**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_rapid_freeze_persists_traceable_medium_confidence_season_bridge -v
```

Expected: failure until Task 4 routes and preserves the bridge fields.

- [ ] **Step 3: Update the README with the completed contract**

Document the `2 + 12` threshold, same-league restriction, time-decay formula,
medium confidence ceiling, source provenance fields, and the fact that every
automatic freeze command requires `review_ready`.

- [ ] **Step 4: Run the complete suite**

Run:

```powershell
python -m unittest discover -s work\tai_cai_tracker_tests -t . -v
```

Expected: all tests pass.

- [ ] **Step 5: Perform a read-only ledger report**

Run:

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python -m outputs.tai_cai_tracker.cli report outputs\tai_cai_tracker\data\paper_ledger.jsonl --json
```

Expected: existing frozen records remain readable and no calibration threshold
claim changes solely because this feature was implemented.
