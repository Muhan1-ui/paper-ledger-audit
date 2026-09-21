# 体彩竞彩足球全玩法冻结与结算 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze, settle, and report all five official China Sports Lottery football markets per eligible paper prediction without altering historical records.

**Architecture:** Keep the existing `prediction` and `settlement` records as the immutable SPF baseline. Add a linked `all_market_snapshot` record for derived market distributions and a linked `all_market_settlement` record for official outcomes. A new market-derivation module maps the frozen Poisson score matrix to the five official market option sets, while report code keeps legacy SPF statistics separate from all-market statistics.

**Tech Stack:** Python standard library, `decimal.Decimal`, JSONL, `unittest`, existing offline CLI.

---

## File Structure

- Create: `outputs/tai_cai_tracker/markets.py` - official option constants, probability derivation, deterministic selection, and market-result consistency helpers.
- Modify: `outputs/tai_cai_tracker/model.py` - retain complete half/full-time probabilities and exact total-goal probabilities in frozen model output.
- Modify: `outputs/tai_cai_tracker/ledger.py` - immutable all-market snapshot and all-market settlement records with hash validation.
- Modify: `outputs/tai_cai_tracker/cli.py` - JSON-input commands for appending all-market snapshots and all-market settlements.
- Modify: `outputs/tai_cai_tracker/report.py` - independent all-market accuracy and probability metrics.
- Modify: `outputs/tai_cai_tracker/README.md` - paper-only workflow and command examples.
- Modify: `work/tai_cai_tracker_tests/test_model.py` - model derivation tests.
- Modify: `work/tai_cai_tracker_tests/test_ledger.py` - hash and validation tests.
- Modify: `work/tai_cai_tracker_tests/test_cli.py` - CLI workflow tests.
- Modify: `work/tai_cai_tracker_tests/test_report.py` - market-separated report tests.

The workspace has no `.git` directory, so no commit steps are possible. Every task instead ends with the exact focused test command and expected passing result.

### Task 1: Derive the Official Option Distributions

**Files:**
- Create: `outputs/tai_cai_tracker/markets.py`
- Modify: `outputs/tai_cai_tracker/model.py`
- Test: `work/tai_cai_tracker_tests/test_model.py`

- [ ] **Step 1: Write failing derivation tests**

```python
from outputs.tai_cai_tracker.markets import (
    CORRECT_SCORE_OPTIONS,
    HALF_FULL_OPTIONS,
    TOTAL_GOAL_OPTIONS,
    derive_official_markets,
    select_top_option,
)

def test_derives_normalised_official_markets_and_stable_top_choice(self):
    matrix = poisson_score_matrix(Decimal("1.2"), Decimal("0.8"), home_handicap=Decimal("-1"))
    markets = derive_official_markets(matrix, Decimal("-1"))
    self.assertEqual(set(markets), {"hhad", "crs", "ttg", "hafu"})
    self.assertEqual(sum(markets["hhad"].values(), Decimal(0)), Decimal(1))
    self.assertEqual(sum(markets["crs"].values(), Decimal(0)), Decimal(1))
    self.assertEqual(sum(markets["ttg"].values(), Decimal(0)), Decimal(1))
    self.assertEqual(sum(markets["hafu"].values(), Decimal(0)), Decimal(1))
    self.assertEqual(tuple(markets["ttg"]), TOTAL_GOAL_OPTIONS)
    self.assertEqual(tuple(markets["hafu"]), HALF_FULL_OPTIONS)
    self.assertEqual(select_top_option({"a": Decimal("0.5"), "b": Decimal("0.5")}, ("a", "b")), ("a", Decimal("0.5"), ("a", "b")))
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `python -m unittest work.tai_cai_tracker_tests.test_model -v`

Expected: import failure for `outputs.tai_cai_tracker.markets`.

- [ ] **Step 3: Add the derivation module and complete model output**

Implement these public constants and functions in `markets.py`:

```python
TOTAL_GOAL_OPTIONS = ("0", "1", "2", "3", "4", "5", "6", "7+")
HALF_FULL_OPTIONS = ("HH", "HD", "HA", "DH", "DD", "DA", "AH", "AD", "AA")
CORRECT_SCORE_OPTIONS = (
    "1:0", "2:0", "2:1", "3:0", "3:1", "3:2", "4:0", "4:1", "4:2", "5:0", "5:1", "5:2", "H_OTHER",
    "0:0", "1:1", "2:2", "3:3", "D_OTHER",
    "0:1", "0:2", "1:2", "0:3", "1:3", "2:3", "0:4", "1:4", "2:4", "0:5", "1:5", "2:5", "A_OTHER",
)

def select_top_option(probabilities, option_order):
    maximum = max(probabilities.values())
    tied = tuple(option for option in option_order if probabilities[option] == maximum)
    return tied[0], maximum, tied

def derive_official_markets(score_matrix, home_handicap):
    if not isinstance(home_handicap, Decimal) or home_handicap != home_handicap.to_integral_value():
        raise ValueError("home_handicap must be a whole-number Decimal")
    hhad = {option: Decimal(0) for option in ("H", "D", "A")}
    crs = {option: Decimal(0) for option in CORRECT_SCORE_OPTIONS}
    ttg = {option: Decimal(0) for option in TOTAL_GOAL_OPTIONS}
    for line in score_matrix["scorelines"]:
        home, away, probability = line["home_goals"], line["away_goals"], line["probability"]
        hhad["H" if home + home_handicap > away else "A" if home + home_handicap < away else "D"] += probability
        score = f"{home}:{away}"
        score_option = score if score in crs else "H_OTHER" if home > away else "A_OTHER" if home < away else "D_OTHER"
        crs[score_option] += probability
        ttg[str(home + away) if home + away <= 6 else "7+"] += probability
    hafu = {option: Decimal(0) for option in HALF_FULL_OPTIONS}
    for key, probability in score_matrix["half_time_full_time"].items():
        half_time, full_time = key.split(":")
        hafu[{"home_win": "H", "draw": "D", "away_win": "A"}[half_time] + {"home_win": "H", "draw": "D", "away_win": "A"}[full_time]] += probability
    return {"hhad": hhad, "crs": crs, "ttg": ttg, "hafu": hafu}
```

Modify `poisson_score_matrix` to return all nine half/full-time entries instead of only the top two, while retaining `half_time_full_time_proxy` for backward compatibility:

```python
"half_time_full_time": {
    f"{half_time}:{full_time}": +probability
    for (half_time, full_time), probability in half_time_full_time.items()
},
"half_time_full_time_proxy": [
    {"half_time": half_time, "full_time": full_time, "probability": +probability}
    for (half_time, full_time), probability in sorted(
        half_time_full_time.items(), key=lambda item: (-item[1], item[0])
    )[:2]
],
```

Use `score_matrix["scorelines"]` as the single source for score and total-goal aggregation. Map unlisted legitimate correct scores into `H_OTHER`, `D_OTHER`, and `A_OTHER`; map totals of seven or greater into `"7+"`; map outcomes as `H`, `D`, and `A` in `HALF_FULL_OPTIONS`.

- [ ] **Step 4: Run focused model tests**

Run: `python -m unittest work.tai_cai_tracker_tests.test_model -v`

Expected: all model tests pass, including the new normalization and tie-order tests.

### Task 2: Add Immutable All-Market Ledger Records

**Files:**
- Modify: `outputs/tai_cai_tracker/ledger.py`
- Test: `work/tai_cai_tracker_tests/test_ledger.py`

- [ ] **Step 1: Write failing record validation tests**

```python
from outputs.tai_cai_tracker.ledger import (
    freeze_all_market_snapshot,
    settle_all_market_snapshot,
    verify_all_market_settlement,
    verify_all_market_snapshot,
)

def test_all_market_snapshot_and_settlement_are_hashed_and_linked(self):
    snapshot = freeze_all_market_snapshot(VALID_ALL_MARKET_SNAPSHOT)
    self.assertEqual(snapshot["record_type"], "all_market_snapshot")
    settlement = settle_all_market_snapshot(snapshot, VALID_ALL_MARKET_RESULTS)
    self.assertTrue(settlement["markets"]["ttg"]["hit"])
    settlement["markets"]["ttg"]["actual_option"] = "7+"
    with self.assertRaises(ValueError):
        verify_all_market_settlement(settlement)

def test_rejects_late_snapshot_and_invalid_hhad_line(self):
    late = deepcopy(VALID_ALL_MARKET_SNAPSHOT)
    late["captured_at"] = late["kickoff_at"]
    with self.assertRaises(ValueError):
        freeze_all_market_snapshot(late)
    invalid = deepcopy(VALID_ALL_MARKET_SNAPSHOT)
    invalid["markets"]["hhad"]["home_handicap"] = Decimal("-0.5")
    with self.assertRaises(ValueError):
        freeze_all_market_snapshot(invalid)
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `python -m unittest work.tai_cai_tracker_tests.test_ledger -v`

Expected: import failure for the all-market ledger functions.

- [ ] **Step 3: Implement snapshot and settlement schemas**

Add the following public functions to `ledger.py` and reuse `_content_hash`, `_parse_timestamp`, `_is_url`, and `_validated_final_score`:

```python
def freeze_all_market_snapshot(snapshot):
    _validate_all_market_snapshot(snapshot)
    record = copy.deepcopy(dict(snapshot))
    record["record_type"] = "all_market_snapshot"
    record["status"] = "frozen"
    record["content_hash"] = _content_hash(record)
    return record

def settle_all_market_snapshot(snapshot, result):
    _validate_all_market_result(snapshot, result)
    record = {
        "record_type": "all_market_settlement",
        "status": "settled",
        "snapshot_hash": snapshot["content_hash"],
        "official_result_url": result["official_result_url"],
        "final_score": result["final_score"],
        "markets": _market_hits(snapshot["markets"], result["markets"]),
    }
    record["content_hash"] = _content_hash(record)
    return record
```

Require a snapshot to contain `prediction_hash`, `match_id`, `kickoff_at`, `captured_at`, `official_source_url`, and `markets`. Each non-skipped market must contain `status: "frozen"`, an exactly normalized `probabilities` map using its official option order, `selected_option`, `selected_probability`, `tied_options`, and `official_fixed_prizes`; HHAD must also contain an integral `home_handicap`. A skipped market must contain exactly `status: "skipped"` and non-empty `reason`.

Require settlement results for frozen markets to use valid official options, derive `hit` from the frozen `selected_option`, and reject any caller-supplied hit flag. Add `verify_all_market_snapshot` and `verify_all_market_settlement`. Extend `read_records` later rather than changing historical records.

- [ ] **Step 4: Run focused ledger tests**

Run: `python -m unittest work.tai_cai_tracker_tests.test_ledger -v`

Expected: all ledger tests pass, including tamper detection and pre-kickoff validation.

### Task 3: Append Linked Snapshots and Official Results Through the CLI

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing end-to-end CLI tests**

```python
def test_all_market_snapshot_and_settle_append_linked_records(self):
    # Create an enhanced base prediction with a frozen score matrix first.
    snapshot = self.run_cli("all-market-snapshot", str(all_market_input), str(ledger_path))
    self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
    settlement = self.run_cli("all-market-settle", str(all_market_result), str(ledger_path))
    self.assertEqual(settlement.returncode, 0, settlement.stderr)
    records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    self.assertEqual(records[-2]["record_type"], "all_market_snapshot")
    self.assertEqual(records[-1]["record_type"], "all_market_settlement")

def test_all_market_snapshot_rejects_base_prediction_without_full_model(self):
    result = self.run_cli("all-market-snapshot", str(spf_only_input), str(ledger_path))
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("full score matrix", result.stderr)
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests -v`

Expected: CLI rejects unknown `all-market-snapshot` command.

- [ ] **Step 3: Implement JSON input commands and read support**

Add parser commands:

```python
all_market_snapshot_parser = commands.add_parser("all-market-snapshot")
all_market_snapshot_parser.add_argument("input_path")
all_market_snapshot_parser.add_argument("ledger_path")

all_market_settle_parser = commands.add_parser("all-market-settle")
all_market_settle_parser.add_argument("input_path")
all_market_settle_parser.add_argument("ledger_path")
```

`all-market-snapshot` input must name a prior frozen `prediction_hash`, repeat the match identity and timestamps, provide one official fixed-prize page URL, and provide market fixed-prize maps plus the official HHAD line. Load the linked prediction, require `prediction["model"]["scorelines"]` and complete `half_time_full_time`, call `derive_official_markets`, select a stable maximum for each market, build the validated snapshot mapping, and append the record returned by `freeze_all_market_snapshot(snapshot_mapping)`.

`all-market-settle` input must name a frozen `snapshot_hash`, provide an official result URL, `final_score`, and a `markets` map of official outcomes. Validate that the linked snapshot exists and is not already settled, append the record returned by `settle_all_market_snapshot(snapshot, result_mapping)`, and preserve the existing SPF `settle` command unchanged.

Extend `read_records` with two branches:

```python
elif record.get("record_type") == "all_market_snapshot":
    verify_all_market_snapshot(record)
elif record.get("record_type") == "all_market_settlement":
    verify_all_market_settlement(record)
```

After reading all records, verify each new snapshot links to a preceding prediction and each new settlement links to one preceding snapshot; reject duplicate all-market settlements.

- [ ] **Step 4: Run focused CLI tests**

Run: `python -m unittest work.tai_cai_tracker_tests.test_cli -v`

Expected: all CLI workflow tests pass with temporary JSONL ledgers only.

### Task 4: Report Independent Per-Market Performance

**Files:**
- Modify: `outputs/tai_cai_tracker/report.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Test: `work/tai_cai_tracker_tests/test_report.py`
- Test: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Write failing report tests**

```python
def test_report_keeps_legacy_spf_and_all_market_denominators_separate(self):
    report = calibration_report([self.prediction, self.settlement, self.all_market_snapshot, self.all_market_settlement])
    self.assertEqual(report["settled_records"], 1)
    self.assertEqual(report["all_market_breakdown"]["hhad"]["settled_records"], 1)
    self.assertEqual(report["all_market_breakdown"]["hhad"]["hit_rate"], Decimal(1))
    self.assertEqual(report["all_market_breakdown"]["ttg"]["skipped_records"], 0)
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `python -m unittest work.tai_cai_tracker_tests.test_report -v`

Expected: missing `all_market_breakdown` key.

- [ ] **Step 3: Implement descriptive all-market breakdowns**

Add `all_market_breakdown(records)` to `report.py`. Group linked all-market snapshot/settlement pairs by `hhad`, `crs`, `ttg`, and `hafu`; calculate exactly these values:

```python
{
    "frozen_records": frozen_count,
    "settled_records": settled_count,
    "skipped_records": skipped_count,
    "hits": hit_count,
    "hit_rate": Decimal(hit_count) / Decimal(settled_count) if settled_count else None,
    "average_selected_probability": sum(selected_probabilities, Decimal(0)) / Decimal(settled_count) if settled_count else None,
    "brier_average": average_multiclass_brier_if_settled else None,
    "log_loss_average": average_negative_log_probability_if_settled else None,
}
```

Return `all_market_breakdown` even below the legacy 50-SPF threshold, with `all_market_phase: "observational"` until every market has 50 settled records. Do not alter the existing `available` gate or use all-market records in legacy SPF calibration. Extend `format_report` to print one concise line per all-market code, preserving the JSON report as the detailed audit surface.

- [ ] **Step 4: Run report and CLI tests**

Run: `python -m unittest work.tai_cai_tracker_tests.test_report work.tai_cai_tracker_tests.test_cli -v`

Expected: all report tests pass; legacy SPF sample counts remain unchanged.

### Task 5: Document the Paper Workflow and Run Full Verification

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`
- Test: `work/tai_cai_tracker_tests/test_model.py`
- Test: `work/tai_cai_tracker_tests/test_ledger.py`
- Test: `work/tai_cai_tracker_tests/test_cli.py`
- Test: `work/tai_cai_tracker_tests/test_report.py`

- [ ] **Step 1: Update the README with concrete paper-only commands**

Add this command sequence after the existing `snapshot` example:

```powershell
python -m outputs.tai_cai_tracker.cli all-market-snapshot `
  outputs/tai_cai_tracker/data/example_all_market_snapshot.json `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl

python -m outputs.tai_cai_tracker.cli all-market-settle `
  outputs/tai_cai_tracker/data/example_all_market_result.json `
  outputs/tai_cai_tracker/data/paper_ledger.jsonl
```

Document that the two JSON files are manual, auditable public-data inputs; a missing official line or result causes a skipped or pending market; commands never scrape, purchase, place bets, or perform financial actions.

- [ ] **Step 2: Run the complete test suite**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -v`

Expected: every test passes with no network access.

- [ ] **Step 3: Validate the existing production ledger is backward compatible**

Run: `python -c "from outputs.tai_cai_tracker.cli import read_records; r=read_records(r'outputs\\tai_cai_tracker\\data\\paper_ledger.jsonl'); print(len(r))"`

Expected: the command prints the current record count and raises no validation error; no historical record is edited.

## Self-Review

- Spec coverage: Tasks 1-4 cover all five official markets, per-market maximum selection, source/line validation, immutable settlement, and independent statistics. Task 5 documents the paper-only workflow and verifies legacy compatibility.
- Scope: no automation of web scraping, purchasing, recommendations, BTTS official reporting, historical backfill, or model-weight tuning is included.
- Consistency: `all_market_snapshot` always links to `prediction_hash`; `all_market_settlement` always links to `snapshot_hash`; all report paths use those exact names.
- Placeholder scan: this plan contains no deferred implementation markers or unspecified validation behavior.
