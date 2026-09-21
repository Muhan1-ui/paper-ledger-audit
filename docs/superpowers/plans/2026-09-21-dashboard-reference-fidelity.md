# Dashboard Reference Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the local read-only dashboard so its desktop layout closely follows the approved reference image while rendering only auditable local tracker data.

**Architecture:** `dashboard.py` will derive two extra read-only view-model collections from validated ledger records: daily calibration points grouped by the captured date of predictions that were later settled, and reliability points projected from the existing report confidence buckets. `dashboard.html` will consume the expanded `/api/summary` response and render the reference layout with inline SVG chart primitives, avoiding external assets, packages, and invented data.

**Tech Stack:** Python 3 standard library, existing `outputs.tai_cai_tracker.report` metric functions, `unittest`, HTML, CSS, vanilla JavaScript, inline SVG.

---

## File Structure

- Modify: `outputs/tai_cai_tracker/dashboard.py` — add safe, read-only visual projections to the summary response.
- Modify: `outputs/tai_cai_tracker/static/dashboard.html` — implement the reference-aligned desktop visual layout and SVG chart renderers.
- Modify: `work/tai_cai_tracker_tests/test_dashboard.py` — add API projection, static safety, and product-title regression tests.
- Modify: `docs/superpowers/specs/2026-09-21-dashboard-reference-fidelity-design.md` — correct the trend source to prediction capture dates before code work begins.

### Task 1: Correct the Approved Data Definition

**Files:**
- Modify: `docs/superpowers/specs/2026-09-21-dashboard-reference-fidelity-design.md`

- [ ] **Step 1: Record the timestamp constraint**

Replace references to grouping by settlement date with this rule:

```markdown
Standard settlement records do not carry a timestamp. The trend groups each
validated primary prediction that has a non-invalidated settlement by the local
calendar date of the prediction's `captured_at` timestamp.
```

- [ ] **Step 2: Commit the corrected data definition**

```powershell
git add docs/superpowers/specs/2026-09-21-dashboard-reference-fidelity-design.md
git commit -m "docs: clarify dashboard trend timestamp"
```

Expected: one documentation-only commit; no ledger files staged.

### Task 2: Add Read-Only Visual Summary Tests

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_dashboard.py`
- Modify: `outputs/tai_cai_tracker/dashboard.py`

- [ ] **Step 1: Write a failing trend and reliability projection test**

Add this test to `DashboardSummaryTests`. Use the existing two frozen predictions, set their `captured_at` values to two different dates, settle both, and retain the original ledger bytes before calling `build_summary`.

```python
def test_build_summary_projects_sorted_capture_date_trend_and_reliability(self):
    summary = build_summary(
        ledger_path,
        now=datetime(2099, 1, 4, tzinfo=timezone.utc),
    )

    self.assertEqual(
        [item["date"] for item in summary["calibration"]["trend"]],
        ["2099-01-01", "2099-01-02"],
    )
    self.assertEqual(
        summary["calibration"]["trend"][0]["settled_records"], 1,
    )
    self.assertEqual(
        summary["calibration"]["reliability"],
        [{"confidence": "0.5", "accuracy": "1", "records": 2}],
    )
    self.assertEqual(ledger_path.read_bytes(), original_ledger)
```

Use a deterministic prediction fixture where both top picks are correct and have a top probability of `Decimal("0.50")`; the expected reliability point must come from actual report buckets rather than a hard-coded chart sample.

- [ ] **Step 2: Run the focused test and verify red**

```powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DashboardSummaryTests.test_build_summary_projects_sorted_capture_date_trend_and_reliability -v
```

Expected: FAIL because `calibration.trend` and `calibration.reliability` do not exist yet.

- [ ] **Step 3: Implement the smallest visual projection helpers**

In `dashboard.py`, import `brier_score` and `log_loss` beside `calibration_report`. Add these helpers:

```python
def _visual_pairs(records):
    predictions = {
        record["content_hash"]: record
        for record in records
        if record.get("record_type") == "prediction"
    }
    invalidated = {
        record["settlement_hash"]
        for record in records
        if record.get("record_type") == "settlement_invalidation"
    }
    pairs = []
    for settlement in records:
        if (
            settlement.get("record_type") != "settlement"
            or settlement.get("content_hash") in invalidated
        ):
            continue
        prediction = predictions.get(settlement.get("prediction_hash"))
        if prediction and prediction.get("prediction_role", "primary") == "primary":
            pairs.append((prediction, settlement))
    return pairs

def _visual_calibration_summary(records, report):
    groups = {}
    for prediction, settlement in _visual_pairs(records):
        captured_at = _timestamp_for_sort(prediction.get("captured_at"))
        if captured_at is not None:
            groups.setdefault(captured_at.date().isoformat(), []).append((prediction, settlement))
    trend = []
    for date in sorted(groups):
        pairs = groups[date]
        count = Decimal(len(pairs))
        trend.append({
            "date": date,
            "settled_records": len(pairs),
            "brier_average": sum((brier_score(*pair) for pair in pairs), Decimal(0)) / count,
            "log_loss_average": sum((log_loss(*pair) for pair in pairs), Decimal(0)) / count,
        })
    reliability = []
    for bucket in report.get("confidence_buckets", {}).values():
        if bucket.get("records", 0) and "accuracy" in bucket:
            reliability.append({
                "confidence": bucket["average_confidence"],
                "accuracy": bucket["accuracy"],
                "records": bucket["records"],
            })
    return _json_value({"trend": trend, "reliability": reliability})
```

Extend `_calibration_summary(report, records)` to merge `_visual_calibration_summary(records, report)` into its returned dictionary. Update `build_summary()` to call `_calibration_summary(report, records)`.

- [ ] **Step 4: Run the focused test and verify green**

Run the command from Step 2. Expected: `OK` and byte-identical ledger content.

- [ ] **Step 5: Add empty-state coverage**

Add:

```python
def test_build_summary_omits_visual_points_without_valid_settlements(self):
    summary = build_summary(empty_ledger_path)
    self.assertEqual(summary["calibration"]["trend"], [])
    self.assertEqual(summary["calibration"]["reliability"], [])
```

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DashboardSummaryTests -v
```

Expected before the fallback implementation: failure from missing visual keys. After the helper returns two empty lists for unavailable reports: `OK`.

- [ ] **Step 6: Commit the read-only summary projection**

```powershell
git add outputs/tai_cai_tracker/dashboard.py work/tai_cai_tracker_tests/test_dashboard.py
git commit -m "feat: add dashboard calibration visual data"
```

### Task 3: Lock the Reference Layout Contract

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_dashboard.py`
- Modify: `outputs/tai_cai_tracker/static/dashboard.html`

- [ ] **Step 1: Write a failing static page contract test**

Extend `DashboardPageTests` with assertions for the reference-aligned visible regions and SVG drawing hooks:

```python
for label in (
    "体彩数据追踪与校准平台",
    "近 7 天",
    "近 30 天",
    "全部",
    "模型校准趋势",
    "校准可靠性",
    "策略表现对比",
    "最近台账活动",
    "智能体流程",
):
    self.assertIn(label, page)
for hook in ("trend-chart", "reliability-chart", "renderTrend", "renderReliability"):
    self.assertIn(hook, page)
```

Retain the existing assertions for `/api/summary`, Chinese headings, and no `http://` or `https://` text.

- [ ] **Step 2: Run the static test and verify red**

```powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DashboardPageTests -v
```

Expected: FAIL because the current page uses summary bars rather than the named SVG chart hooks.

- [ ] **Step 3: Rebuild the desktop page around the reference structure**

Replace `dashboard.html` with these semantic regions:

```html
<header class="app-header">...</header>
<aside class="sidebar">...</aside>
<main class="workspace">
  <section class="page-heading">...</section>
  <section class="metric-grid">...</section>
  <section class="chart-grid">
    <article class="panel chart-panel"><svg id="trend-chart" viewBox="0 0 620 250"></svg></article>
    <article class="panel chart-panel"><svg id="reliability-chart" viewBox="0 0 360 250"></svg></article>
  </section>
  <section class="panel">策略表现对比表</section>
  <section class="panel">最近台账活动表</section>
</main>
<aside class="right-rail">运行状态、证据状态、智能体流程</aside>
```

Use the reference proportions: `196px` left navigation, fixed `250px` right rail, 16px gutters, 8px maximum panel radius, white panels on `#f6f8f8`, deep teal primary state, blue/orange chart series, and no external image, icon, font, CSS, or script. Implement the top segmented control as visible disabled buttons with `全部` selected, because the API has no range-filter parameter.

- [ ] **Step 4: Implement SVG renderers with empty states**

Add browser functions that clear their SVG before every render:

```javascript
function renderTrend(points) {
  var chart = ids("trend-chart");
  clearSvg(chart);
  if (points.length < 2) {
    svgText(chart, 310, 125, "预测采集日期不足，暂不生成趋势图", "chart-empty");
    return;
  }
  drawChartAxes(chart, 620, 250, {left:48, right:16, top:34, bottom:36});
  drawLineSeries(chart, points, "brier_average", "#079a74");
  drawLineSeries(chart, points, "log_loss_average", "#1477df");
  drawLineSeries(chart, points, "expected_calibration_error", "#ea7c21");
}

function renderReliability(points) {
  var chart = ids("reliability-chart");
  clearSvg(chart);
  if (!points.length) {
    svgText(chart, 180, 125, "当前样本不足，暂不生成可靠性图", "chart-empty");
    return;
  }
  drawReliabilityAxes(chart, 360, 250);
  drawIdealCalibrationLine(chart);
  points.forEach(drawReliabilityPoint);
}
```

`expected_calibration_error` is absent from individual daily records by design. Populate it only as `null` in each trend item and have `drawLineSeries` skip null points; do not compute a non-equivalent daily ECE. The chart legend labels it “ECE（整体见上方指标）” rather than implying a daily value.

- [ ] **Step 5: Render only actual status data**

Render metric card secondary text as static metric explanations, not invented changes. In the right rail, map stale scheduler state to `数据待刷新`; show the server-supplied age. Keep evidence counts and warning messages from the summary response. Do not hard-code dates, match names, numbers, report deltas, or success labels.

- [ ] **Step 6: Run the static page test and dashboard tests**

```powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard -v
```

Expected: `OK`.

- [ ] **Step 7: Commit the reference-aligned visual layout**

```powershell
git add outputs/tai_cai_tracker/static/dashboard.html work/tai_cai_tracker_tests/test_dashboard.py
git commit -m "feat: align dashboard with approved reference"
```

### Task 4: Verify With the Real Ledger

**Files:**
- Modify only when this verification demonstrates a defect.

- [ ] **Step 1: Run the complete tracker suite**

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

Expected: exit code `0` with all tests passing.

- [ ] **Step 2: Confirm the existing ledger remains readable**

```powershell
python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl --json > $null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

Expected: exit code `0`; do not edit `paper_ledger.jsonl`.

- [ ] **Step 3: Start a temporary local server and inspect both viewports**

```powershell
python -m outputs.tai_cai_tracker.dashboard --ledger-path outputs/tai_cai_tracker/data/paper_ledger.jsonl --scheduler-log-path work/logs/scheduler.jsonl --port 8767
```

At `http://127.0.0.1:8767`, inspect `1440px` desktop and a `600px` narrow viewport. Verify real metric values, visible SVGs or explicit empty states, no overlap, no horizontal desktop overflow, Chinese labels, and no browser console errors. Stop the temporary server after inspection.

- [ ] **Step 4: Check the static network boundary and staged scope**

```powershell
rg -n "https?://" outputs/tai_cai_tracker/static/dashboard.html
git status --short
```

Expected: the `rg` command has no matches. Stage only `dashboard.py`, `dashboard.html`, `test_dashboard.py`, and the two dashboard planning/design documents; exclude the user-owned progress JSON files.
