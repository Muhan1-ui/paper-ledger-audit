# Evaluation Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose auditable overall and per-strategy probability diagnostics in
the report API and CLI.

**Architecture:** `report.py` classifies frozen prediction records and derives
per-strategy statistics from already-linked settlements. `cli.py` renders the
existing unavailable response or the enriched report in text and JSON forms.

**Tech Stack:** Python standard library, `Decimal`, JSONL, `unittest`.

---

### Task 1: Add descriptive strategy breakdowns

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_report.py`
- Modify: `outputs/tai_cai_tracker/report.py`

- [ ] **Step 1: Write the failing test**

```python
report = calibration_report(records)
assert report["strategy_breakdown"]["market_baseline"]["settled_records"] == 50
assert report["strategy_breakdown"]["poisson-v1@0.40"]["settled_records"] == 1
```

- [ ] **Step 2: Verify it fails**

Run: `python -m unittest work/tai_cai_tracker_tests/test_report.py -v`

Expected: missing `strategy_breakdown`.

- [ ] **Step 3: Implement classification and metric aggregation**

```python
def strategy_label(prediction):
    if not isinstance(prediction.get("model"), Mapping):
        return "market_baseline"
    return f"{prediction['model']['version']}@{prediction['model']['weight']}"
```

Aggregate Brier, Log Loss, top-pick accuracy, and optional exact-score Log
Loss without changing the top-level 50-record gate.

- [ ] **Step 4: Verify report tests pass**

Run: `python -m unittest work/tai_cai_tracker_tests/test_report.py -v`

Expected: all report tests pass.

### Task 2: Render enriched report output

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_cli.py`
- Modify: `outputs/tai_cai_tracker/cli.py`

- [ ] **Step 1: Write failing CLI-format tests**

```python
text = format_report(available_report)
assert "Log loss" in text
assert "market_baseline" in text
```

- [ ] **Step 2: Verify it fails**

Run: `python -m unittest work/tai_cai_tracker_tests/test_cli.py -v`

Expected: missing `format_report`.

- [ ] **Step 3: Implement text and JSON render paths**

```python
report_parser.add_argument("--json", action="store_true")
if arguments.as_json:
    print(json.dumps(result, default=_json_default, sort_keys=True))
else:
    print(format_report(result))
```

- [ ] **Step 4: Verify CLI tests pass**

Run: `python -m unittest work/tai_cai_tracker_tests/test_cli.py -v`

Expected: all CLI tests pass.

### Task 3: Verify the complete workflow

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`

- [ ] **Step 1: Document `report --json` and descriptive-only strategy groups**

```markdown
Strategy groups are descriptive. They do not select a model or update a
weight; temporal validation remains a separate step after sufficient records.
```

- [ ] **Step 2: Run the full suite**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -v`

Expected: all tests pass.
