# Historical FotMob Identity Prewarm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a resumable 18-month official-history scan that strictly maps previously observed China Sports Lottery team names to public FotMob team identities without changing the paper ledger.

**Architecture:** `historical-fotmob-source-prewarm` reads a bounded sequence of official result dates, extracts and de-duplicates official team names, and delegates every unknown name to the existing strict FotMob resolver. A small mutable progress file records only the completed date range and last successful date; mappings remain in the existing source-link registry and are accepted only by `record_fotmob_team_identity`.

**Tech Stack:** Python standard library, existing Sporttery public collector, existing FotMob public search resolver, JSON mutable registry/progress files, unittest.

---

### Task 1: Define historical scan behavior with failing tests

**Files:**
- Modify: `D:\TaiCaiTracker\work\tai_cai_tracker_tests\test_cli.py`

- [ ] **Step 1: Write failing tests for one bounded historical batch**

```python
def test_historical_fotmob_source_prewarm_scans_dates_deduplicates_teams_and_records_only_strict_matches(self):
    with patch("outputs.tai_cai_tracker.cli.collect_results") as collect, \
         patch("outputs.tai_cai_tracker.cli.discover_team_stats_url") as discover:
        summary = tracker_cli.historical_fotmob_source_prewarm(
            days_back=2, max_dates_per_run=2, now=now,
            source_link_registry_path=registry_path,
            progress_path=progress_path,
        )
    self.assertEqual(collect.call_args_list, [call(match_date="2099-01-01"), call(match_date="2098-12-31")])
    self.assertEqual(discover.call_count, 3)
    self.assertEqual(summary["resolved_team_count"], 2)
```

- [ ] **Step 2: Run the test to verify it fails because the public function does not exist**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_historical_fotmob_source_prewarm_scans_dates_deduplicates_teams_and_records_only_strict_matches`

Expected: `AttributeError` for `historical_fotmob_source_prewarm`.

### Task 2: Implement bounded, resumable historical identity prewarm

**Files:**
- Modify: `D:\TaiCaiTracker\outputs\tai_cai_tracker\cli.py`

- [ ] **Step 1: Add a progress-file loader and atomic writer**

```python
def _load_historical_prewarm_progress(progress_path):
    try:
        payload = json.loads(Path(progress_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}

def _write_historical_prewarm_progress(progress_path, payload):
    path = Path(progress_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
```

- [ ] **Step 2: Add `historical_fotmob_source_prewarm`**

```python
def historical_fotmob_source_prewarm(days_back=548, max_dates_per_run=7, max_workers=2, now=None, source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH, progress_path=DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH):
    # Validate 1..548 days and 1..7 dates per run.
    # Start at the newest uncompleted date in the requested 18-month range.
    # Call collect_results(match_date=YYYY-MM-DD) once per date.
    # De-duplicate official home_team and away_team names across the batch.
    # Skip identities that cached_fotmob_team_identity already validates.
    # Resolve remaining names once each with discover_team_stats_url.
    # Persist only strict candidates with record_fotmob_team_identity.
    # Atomically record the last successful date after each completed official date.
    # Return scanned dates, official match count, cache hits, resolved/unresolved names, and next resume date.
```

- [ ] **Step 3: Run the focused test to verify it passes**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_historical_fotmob_source_prewarm_scans_dates_deduplicates_teams_and_records_only_strict_matches`

Expected: `OK`.

### Task 3: Protect resume, failure, and CLI behavior

**Files:**
- Modify: `D:\TaiCaiTracker\work\tai_cai_tracker_tests\test_cli.py`
- Modify: `D:\TaiCaiTracker\outputs\tai_cai_tracker\cli.py`

- [ ] **Step 1: Write failing tests for progress resume and one official-date failure**

```python
def test_historical_fotmob_source_prewarm_resumes_after_the_last_completed_date(self):
    progress_path.write_text(json.dumps({"last_completed_date": "2098-12-31"}), encoding="utf-8")
    summary = tracker_cli.historical_fotmob_source_prewarm(days_back=3, max_dates_per_run=2, now=now, progress_path=progress_path)
    self.assertEqual(summary["scanned_dates"], ["2098-12-30", "2098-12-29"])

def test_historical_fotmob_source_prewarm_stops_without_advancing_progress_when_official_date_fails(self):
    with patch("outputs.tai_cai_tracker.cli.collect_results", side_effect=OSError("offline")):
        summary = tracker_cli.historical_fotmob_source_prewarm(days_back=2, now=now, progress_path=progress_path)
    self.assertEqual(summary["status"], "blocked")
    self.assertFalse(progress_path.exists())
```

- [ ] **Step 2: Implement date-order validation and stop-on-failure progress handling**

```python
# Resume only when the existing progress range exactly matches the requested
# start/end dates. On any official-date collection failure, return a controlled
# blocked status and leave the next uncompleted date unchanged.
```

- [ ] **Step 3: Add parser and main-dispatch support**

```python
historical_prewarm_parser = commands.add_parser(
    "historical-fotmob-source-prewarm",
    help="resumably cache strict FotMob team identities from bounded official result history",
)
historical_prewarm_parser.add_argument("--days-back", type=int, default=548)
historical_prewarm_parser.add_argument("--max-dates-per-run", type=int, default=7)
historical_prewarm_parser.add_argument("--max-workers", type=int, default=2)
historical_prewarm_parser.add_argument("--source-link-registry", default=str(DEFAULT_SOURCE_LINK_REGISTRY_PATH))
historical_prewarm_parser.add_argument("--progress-path", default=str(DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH))
```

- [ ] **Step 4: Run focused CLI tests**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_historical_fotmob_source_prewarm_resumes_after_the_last_completed_date work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_historical_fotmob_source_prewarm_stops_without_advancing_progress_when_official_date_fails`

Expected: `OK`.

### Task 4: Document and verify the non-ledger workflow

**Files:**
- Modify: `D:\TaiCaiTracker\outputs\tai_cai_tracker\README.md`

- [ ] **Step 1: Document default range, bounded date batches, progress file, strict resolver, and non-ledger constraint**

```markdown
`historical-fotmob-source-prewarm` scans up to seven official result dates per
run from a default 18-month range. It is resumable through its mutable progress
file, records only strict FotMob identities, and never reads or writes the
paper ledger.
```

- [ ] **Step 2: Verify the complete tracker**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -p "test_*.py"`

Expected: all tests pass.

- [ ] **Step 3: Verify command interface and immutable ledger hash**

Run: `python -m py_compile outputs/tai_cai_tracker/cli.py && python -m outputs.tai_cai_tracker.cli historical-fotmob-source-prewarm --help && Get-FileHash outputs/tai_cai_tracker/data/paper_ledger.jsonl -Algorithm SHA256`

Expected: compile and help exit with code 0; the ledger hash is unchanged.
