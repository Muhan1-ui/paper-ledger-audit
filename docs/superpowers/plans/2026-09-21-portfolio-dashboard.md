# Portfolio Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only local dashboard and portable developer workflow that make TaiCaiTracker credible and runnable as a software-engineering portfolio project.

**Architecture:** `dashboard.py` will be a read-only adapter over `cli.read_records()` and `report.calibration_report()`. A standard-library localhost HTTP server will expose its normalized view model and serve a static HTML page. PowerShell launchers will resolve Python from an explicit argument, `TAICAI_PYTHON`, or `PATH`, never a machine-specific drive path.

**Tech Stack:** Python 3.10+ standard library (`http.server`, `json`, `pathlib`, `unittest`), PowerShell 5+, HTML/CSS/vanilla JavaScript.

---

## File Structure

- Create: `outputs/tai_cai_tracker/dashboard.py` — read-only summary builder and localhost HTTP server.
- Create: `outputs/tai_cai_tracker/static/dashboard.html` — local responsive dashboard UI.
- Create: `work/start-dashboard.ps1` — dashboard launcher with portable Python resolution.
- Create: `work/tai_cai_tracker_tests/test_dashboard.py` — summary and HTTP-server contract tests.
- Create: `work/tai_cai_tracker_tests/test_launcher_scripts.py` — launcher portability checks.
- Modify: `work/run-tai-cai-task.ps1` — use the same Python resolution contract.
- Create: `README.md` — recruiter-facing project entry point.
- Create: `docs/architecture.md` — architecture and agent-workflow diagram.
- Create: `.gitignore` — exclude generated artifacts and local caches while retaining source and canonical paper ledger.

### Task 1: Establish Version Control Baseline

**Files:**
- Create: `.gitignore`

- [ ] **Step 1: Create the initial ignore rules**

Create `.gitignore` with these exact categories: `__pycache__/`, `*.pyc`,
`work/logs/*.pid`, `work/logs/*.err`, temporary test output, and OS/editor
metadata. Do not ignore `outputs/tai_cai_tracker/data/paper_ledger.jsonl` or
source files.

- [ ] **Step 2: Initialize and inspect the repository**

Run:

~~~powershell
git init
git status --short
~~~

Expected: an initialized repository with no credentials, browser profiles, or
unrelated private files shown for addition.

- [ ] **Step 3: Commit the existing reproducible baseline**

Run:

~~~powershell
git add AGENTS.md PORTABLE_SETUP.md START_NEW_CHAT.md docs outputs work .gitignore
git status --short
git commit -m "chore: initialize auditable tracker repository"
~~~

Expected: one baseline commit. Stop before committing if the staged output
contains credentials, browser profiles, or unrelated private files.

### Task 2: Read-Only Dashboard Summary

**Files:**
- Create: `work/tai_cai_tracker_tests/test_dashboard.py`
- Create: `outputs/tai_cai_tracker/dashboard.py`

- [ ] **Step 1: Write the failing summary test**

Add a `DashboardSummaryTests` class with a temporary JSONL ledger generated from existing test helpers. Require a summary that carries report metrics, sorts strategy rows, returns at most ten recent records, and proves no ledger mutation:

~~~python
def test_build_summary_returns_report_metrics_and_recent_read_only_records(self):
    before = self.ledger_path.read_text(encoding="utf-8")

    summary = build_summary(
        ledger_path=self.ledger_path,
        scheduler_log_path=self.missing_scheduler_log,
        now=datetime(2099, 1, 2, tzinfo=timezone.utc),
    )

    self.assertEqual(summary["calibration"]["settled_records"], 1)
    self.assertEqual(summary["operations"]["status"], "unavailable")
    self.assertLessEqual(len(summary["recent_records"]), 10)
    self.assertEqual(before, self.ledger_path.read_text(encoding="utf-8"))
~~~

- [ ] **Step 2: Run the focused test and verify red**

Run:

~~~powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DashboardSummaryTests.test_build_summary_returns_report_metrics_and_recent_read_only_records
~~~

Expected: import failure for `outputs.tai_cai_tracker.dashboard` or missing `build_summary`.

- [ ] **Step 3: Implement the minimal read-only summary builder**

Create `dashboard.py` with these public functions:

~~~python
def build_summary(ledger_path, scheduler_log_path=None, now=None) -> dict:
    records = read_records(ledger_path)
    report = calibration_report(records)
    return {
        "generated_at": _timestamp(now),
        "calibration": _calibration_view(report),
        "strategies": _strategy_rows(report),
        "operations": _operations_view(scheduler_log_path, now),
        "evidence": _evidence_view(report),
        "recent_records": _recent_records(records),
        "warnings": _warnings(report, scheduler_log_path),
    }
~~~

Use only reads. Convert `Decimal` values to strings so `json.dumps()` succeeds. Normalize each recent record to `record_type`, `match_id`, `captured_at`, and content-hash prefix; never include source URLs or probability maps.

- [ ] **Step 4: Run the focused test and verify green**

Run the command from Step 2. Expected: `OK`.

- [ ] **Step 5: Add missing/malformed scheduler coverage**

Add two tests:

~~~python
def test_missing_scheduler_log_is_reported_as_unavailable(self):
    summary = build_summary(self.ledger_path, self.missing_scheduler_log)
    self.assertEqual(summary["operations"]["status"], "unavailable")
    self.assertTrue(summary["warnings"])

def test_malformed_final_scheduler_line_becomes_a_warning(self):
    self.scheduler_log.write_text("not-json\n", encoding="utf-8")
    summary = build_summary(self.ledger_path, self.scheduler_log)
    self.assertEqual(summary["operations"]["status"], "unavailable")
    self.assertIn("scheduler log", " ".join(summary["warnings"]).lower())
~~~

- [ ] **Step 6: Run the new tests red, then implement and verify green**

Run:

~~~powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DashboardSummaryTests
~~~

Expected before implementation: each new behavior fails. Implement `_operations_view()` to parse only the final non-empty JSONL line, calculate `age_hours`, and set `stale` when age exceeds 24 hours. Re-run the same command and require `OK`.

- [ ] **Step 7: Commit the focused unit**

~~~powershell
git add outputs/tai_cai_tracker/dashboard.py work/tai_cai_tracker_tests/test_dashboard.py
git commit -m "feat: add read-only dashboard summary"
~~~

### Task 3: Localhost HTTP Server

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_dashboard.py`
- Modify: `outputs/tai_cai_tracker/dashboard.py`

- [ ] **Step 1: Write failing HTTP contract tests**

Add a test fixture that starts the server on `127.0.0.1` with port `0`, then uses `urllib.request.urlopen` against the resolved port:

~~~python
def test_api_summary_returns_json(self):
    with urlopen(f"http://127.0.0.1:{self.port}/api/summary") as response:
        payload = json.load(response)
    self.assertEqual(response.status, 200)
    self.assertIn("calibration", payload)

def test_api_health_returns_ok(self):
    with urlopen(f"http://127.0.0.1:{self.port}/api/health") as response:
        payload = json.load(response)
    self.assertEqual(payload, {"status": "ok"})
~~~

- [ ] **Step 2: Run HTTP tests and verify red**

Run:

~~~powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DashboardHttpTests
~~~

Expected: missing `create_server` failure.

- [ ] **Step 3: Implement a local-only server**

Add `create_server(ledger_path, scheduler_log_path, host="127.0.0.1", port=8765)` and a request handler. Route `/api/summary` to `build_summary`, `/api/health` to a successful summary build and `{ "status": "ok" }`, and `/` to the static HTML. Return JSON errors without tracebacks and set `Content-Type` precisely.

Add `main(argv=None)` with `--ledger-path`, `--scheduler-log-path`, `--host`, and `--port`; reject hosts other than loopback addresses.

- [ ] **Step 4: Run HTTP tests and verify green**

Run the command from Step 2. Expected: `OK`.

- [ ] **Step 5: Add static-page test and verify red/green**

Test `GET /` returns `text/html` and includes exact title `TaiCaiTracker Dashboard`. Add `outputs/tai_cai_tracker/static/dashboard.html`, then re-run the focused test class and require `OK`.

- [ ] **Step 6: Commit the server unit**

~~~powershell
git add outputs/tai_cai_tracker/dashboard.py outputs/tai_cai_tracker/static/dashboard.html work/tai_cai_tracker_tests/test_dashboard.py
git commit -m "feat: serve local tracker dashboard"
~~~

### Task 4: Dashboard Interface

**Files:**
- Modify: `outputs/tai_cai_tracker/static/dashboard.html`
- Modify: `work/tai_cai_tracker_tests/test_dashboard.py`

- [ ] **Step 1: Write a failing static-safety test**

Add a test that reads the static page and verifies the intended API contract without browser tooling:

~~~python
def test_dashboard_page_uses_only_local_api_and_exposes_required_sections(self):
    page = DASHBOARD_PAGE.read_text(encoding="utf-8")
    self.assertIn("/api/summary", page)
    self.assertNotRegex(page, r"https?://")
    for label in ("Calibration", "Strategy", "Operations", "Evidence", "Agent workflow"):
        self.assertIn(label, page)
~~~

- [ ] **Step 2: Run the test and verify red**

Run:

~~~powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DashboardPageTests
~~~

Expected: missing section/API assertion failure.

- [ ] **Step 3: Implement the page**

Build a responsive, compact dashboard with semantic sections, a dark-text-on-light operational palette, 8px-or-less radii, and no external fonts, images, CSS, or scripts. `fetch("/api/summary")` populates metrics, strategy table, evidence panel, operations panel, warnings, recent records, and the four-stage workflow. Render unavailable states distinctly; show no calls-to-action related to purchasing, betting, or funding.

- [ ] **Step 4: Run page tests and verify green**

Run the command from Step 2. Expected: `OK`.

- [ ] **Step 5: Manually verify local rendering**

Run:

~~~powershell
python -m outputs.tai_cai_tracker.dashboard --ledger-path outputs/tai_cai_tracker/data/paper_ledger.jsonl --scheduler-log-path work/logs/scheduler.jsonl --port 8765
~~~

Open `http://127.0.0.1:8765`, confirm all panels render with data or an explicit unavailable state, and confirm browser DevTools shows no external requests. Stop the local process after verification.

- [ ] **Step 6: Commit the page unit**

~~~powershell
git add outputs/tai_cai_tracker/static/dashboard.html work/tai_cai_tracker_tests/test_dashboard.py
git commit -m "feat: render portfolio dashboard"
~~~

### Task 5: Portable Launchers

**Files:**
- Create: `work/tai_cai_tracker_tests/test_launcher_scripts.py`
- Modify: `work/run-tai-cai-task.ps1`
- Create: `work/start-dashboard.ps1`

- [ ] **Step 1: Write failing portability tests**

Use `Path.read_text()` and assert the scripts expose a parameter and fallback contract:

~~~python
def test_task_launcher_does_not_hard_code_a_python_drive_path(self):
    script = TASK_LAUNCHER.read_text(encoding="utf-8")
    self.assertNotIn("D:\\python\\python.exe", script)
    self.assertIn("TAICAI_PYTHON", script)
    self.assertIn("Get-Command python", script)

def test_dashboard_launcher_targets_the_read_only_local_server(self):
    script = DASHBOARD_LAUNCHER.read_text(encoding="utf-8")
    self.assertIn("outputs.tai_cai_tracker.dashboard", script)
    self.assertIn("127.0.0.1", script)
~~~

- [ ] **Step 2: Run launcher tests and verify red**

Run:

~~~powershell
python -m unittest work.tai_cai_tracker_tests.test_launcher_scripts
~~~

Expected: hard-coded Python-path assertion failure and missing dashboard launcher.

- [ ] **Step 3: Implement shared resolution behavior**

Add `-PythonPath` to both scripts. Resolve `$Python` in this exact order: supplied argument when it exists, `$env:TAICAI_PYTHON` when it exists, then `(Get-Command python -ErrorAction Stop).Source`. Throw a clear error naming these three supported remedies when resolution fails.

Keep `run-tai-cai-task.ps1` stage behavior unchanged. Make `start-dashboard.ps1` accept `-Port`, launch `python -m outputs.tai_cai_tracker.dashboard` with canonical ledger/log paths, and print `http://127.0.0.1:<port>` before blocking on the server process.

- [ ] **Step 4: Run launcher tests and verify green**

Run the command from Step 2. Expected: `OK`.

- [ ] **Step 5: Smoke test both launch paths**

Run:

~~~powershell
powershell -ExecutionPolicy Bypass -File work/start-dashboard.ps1 -Port 8766
~~~

Confirm the printed local URL works, then stop the server. Run `work/run-tai-cai-task.ps1 -Mode status` using the resolved Python and confirm it returns a JSON status entry.

- [ ] **Step 6: Commit launcher work**

~~~powershell
git add work/run-tai-cai-task.ps1 work/start-dashboard.ps1 work/tai_cai_tracker_tests/test_launcher_scripts.py
git commit -m "fix: make tracker launchers portable"
~~~

### Task 6: Portfolio Documentation

**Files:**
- Create: `README.md`
- Create: `docs/architecture.md`

- [ ] **Step 1: Write documentation acceptance tests**

Add `DocumentationTests` to `test_dashboard.py`:

~~~python
def test_readme_has_portfolio_quick_start_and_test_command(self):
    readme = ROOT / "README.md"
    content = readme.read_text(encoding="utf-8")
    self.assertIn("TaiCaiTracker", content)
    self.assertIn("start-dashboard.ps1", content)
    self.assertIn("python -m unittest discover", content)
    self.assertIn("paper-only", content)
~~~

- [ ] **Step 2: Run the test and verify red**

Run:

~~~powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DocumentationTests
~~~

Expected: missing root README failure.

- [ ] **Step 3: Write recruiter-facing documentation**

Create root README with project purpose, safety boundary, feature list, architecture link, quick start, test command, dashboard URL, data-integrity mechanisms, agent workflow, measured validation results, and known limitations. Describe collectors accurately as optional public-data ingestion components; describe ledger/reporting as local and paper-only.

Create `docs/architecture.md` with a Mermaid diagram covering external public sources, evidence validation, immutable JSONL ledger, CLI/report calculation, localhost dashboard, and test suite. Explain why only the CLI writes the ledger and why the dashboard is read-only.

- [ ] **Step 4: Run the documentation test and verify green**

Run the command from Step 2. Expected: `OK`.

- [ ] **Step 5: Commit documentation**

~~~powershell
git add README.md docs/architecture.md work/tai_cai_tracker_tests/test_dashboard.py
git commit -m "docs: add portfolio project guide"
~~~

### Task 7: Full Verification And Portfolio Review

**Files:**
- Modify only if verification reveals a defect in a preceding task.

- [ ] **Step 1: Run all tests**

Run:

~~~powershell
python -m unittest discover -s work/tai_cai_tracker_tests -p "test_*.py"
~~~

Expected: all pre-existing and new tests pass with exit code `0`.

- [ ] **Step 2: Validate immutable-ledger behavior**

Run before and after an API request:

~~~powershell
Get-FileHash outputs/tai_cai_tracker/data/paper_ledger.jsonl -Algorithm SHA256
Invoke-WebRequest http://127.0.0.1:8765/api/summary | Select-Object StatusCode
Get-FileHash outputs/tai_cai_tracker/data/paper_ledger.jsonl -Algorithm SHA256
~~~

Expected: both SHA-256 values are identical and the API returns HTTP 200.

- [ ] **Step 3: Validate static network boundary**

Run:

~~~powershell
rg -n "https?://" outputs/tai_cai_tracker/static/dashboard.html
~~~

Expected: no matches.

- [ ] **Step 4: Inspect Git state**

Run:

~~~powershell
git status --short
git log --oneline -5
~~~

Expected: clean worktree and readable incremental history.

- [ ] **Step 5: Record final evidence in README**

Update the README only with fresh measured test count/time and dashboard smoke-test evidence from this task. Re-run the full suite after the documentation edit, then commit:

~~~powershell
git add README.md
git commit -m "docs: record dashboard verification"
~~~

