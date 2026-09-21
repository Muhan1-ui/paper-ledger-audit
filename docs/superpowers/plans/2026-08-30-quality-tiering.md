# Quality Tiering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow complete, pre-kickoff `league_total` xG evidence with at least eight samples to create a medium-confidence paper snapshot.

**Architecture:** Keep `quality_gate` as the single eligibility decision. It will inspect the existing xG evidence sample fields and return `review_ready` for adequate evidence, while retaining risk reasons for league-total xG and unavailable lineups. The automatic stage remains unchanged and relies on the returned status.

**Tech Stack:** Python standard library, `unittest`, existing tracker CLI.

---

### Task 1: Define the adequate league-total behavior

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_pregame.py`

- [ ] **Step 1: Write a failing test**

```python
def test_quality_gate_allows_adequately_sampled_league_total_xg_as_medium_confidence(self):
    result = quality_gate(
        {"kickoff_at": "2026-07-27T18:30:00+08:00"},
        {"official_markets": {"spf": {"status": "frozen", "published_at": "2026-07-27T10:00:00+08:00"}}},
        {"captured_at": "2026-07-27T10:10:00+08:00"},
        {"xg_inputs": {"evidence": {"published_at": "2026-07-27T10:15:00+08:00", "scope": "league_total", "sample_matches": 8}}},
        {"split_xg": {"status": "unavailable"}, "lineup": {"status": "unavailable"}},
        "2026-07-27T10:20:00+08:00",
    )
    self.assertEqual(result["status"], "review_ready")
    self.assertEqual(result["confidence_ceiling"], "medium")
    self.assertEqual(result["reasons"], ["league_total_xg", "starting_lineups_unconfirmed", "context_event_unresolved"])
```

- [ ] **Step 2: Run the focused test and verify that it fails**

Run: `python -m unittest work.tai_cai_tracker_tests.test_pregame.PregameTests.test_quality_gate_allows_adequately_sampled_league_total_xg_as_medium_confidence -v`

Expected: failure because the existing gate returns `observation_only`.

- [ ] **Step 3: Write a failing low-sample guard test**

```python
def test_quality_gate_keeps_low_sample_league_total_xg_observation_only(self):
    result = quality_gate(
        {"kickoff_at": "2026-07-27T18:30:00+08:00"},
        {"official_markets": {"spf": {"status": "frozen", "published_at": "2026-07-27T10:00:00+08:00"}}},
        {"captured_at": "2026-07-27T10:10:00+08:00"},
        {"xg_inputs": {"evidence": {"published_at": "2026-07-27T10:15:00+08:00", "scope": "league_total", "sample_matches": 7}}},
        {"split_xg": {"status": "unavailable"}, "lineup": {"status": "unavailable"}},
        "2026-07-27T10:20:00+08:00",
    )
    self.assertEqual(result["status"], "observation_only")
    self.assertIn("xg_sample_below_minimum", result["reasons"])
```

- [ ] **Step 4: Run the focused test and verify that it fails**

Run: `python -m unittest work.tai_cai_tracker_tests.test_pregame.PregameTests.test_quality_gate_keeps_low_sample_league_total_xg_observation_only -v`

Expected: failure because the existing gate has no sample threshold.

### Task 2: Implement the gate policy

**Files:**
- Modify: `outputs/tai_cai_tracker/pregame.py`

- [ ] **Step 1: Add a sample-count helper**

```python
def _xg_has_minimum_sample(evidence, minimum=8):
    counts = [evidence.get("sample_matches"), evidence.get("home_sample_matches"), evidence.get("away_sample_matches")]
    present = [count for count in counts if isinstance(count, int) and not isinstance(count, bool)]
    if evidence.get("sample_matches") is not None:
        return len(present) == 1 and present[0] >= minimum
    return len(present) == 2 and all(count >= minimum for count in present)
```

- [ ] **Step 2: Make low samples observation-only and adequate league totals review-ready**

```python
if scope == "league_total":
    reasons.append("league_total_xg")
    if not _xg_has_minimum_sample(evidence):
        reasons.append("xg_sample_below_minimum")
        return {"status": "observation_only", "reasons": reasons}
...
return {
    "status": "review_ready",
    "reasons": reasons,
    "confidence_ceiling": "medium" if scope == "league_total" else "high",
}
```

- [ ] **Step 3: Run both focused tests and verify they pass**

Run: `python -m unittest work.tai_cai_tracker_tests.test_pregame -v`

Expected: all pregame tests pass.

### Task 3: Preserve automatic-freeze safety

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write a failing integration test**

```python
def test_schedule_driven_freeze_accepts_review_ready_medium_confidence_bundle(self):
    bundle = _review_ready_bundle(match)
    bundle["quality_gate"] = {"status": "review_ready", "reasons": ["league_total_xg"], "confidence_ceiling": "medium"}
    # Patch collection with bundle, invoke schedule_driven_freeze, then assert three records append.
```

- [ ] **Step 2: Run the test and verify it fails only if the stage rejects the new status shape**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.TrackerCliTests.test_schedule_driven_freeze_accepts_review_ready_medium_confidence_bundle -v`

- [ ] **Step 3: Run the existing stage without production changes**

The stage already accepts `quality_gate.status == "review_ready"`; no new branch is needed. Confirm the integration test passes.

### Task 4: Document and verify

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`

- [ ] **Step 1: Document the eight-sample medium-confidence policy**

Add a concise paragraph in the pregame quality-gate section describing `review_ready` league-total evidence, its eight-sample minimum, and retained lineup/scope risk flags.

- [ ] **Step 2: Run the full suite**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -t . -v`

Expected: exit code 0 with all tests passing.
