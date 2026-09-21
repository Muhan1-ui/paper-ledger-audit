# Portfolio Dashboard Design

## Goal

Turn TaiCaiTracker into a credible, local-first portfolio project for backend,
test-development, full-stack, and agent-engineering interviews without changing
the immutable paper-ledger workflow or suggesting lottery purchases.

## Scope

The delivery adds a read-only local dashboard, a small JSON view-model layer,
an environment-aware PowerShell launcher, and recruiter-facing documentation.
It does not add accounts, payments, wagering actions, a database, automatic
model tuning, or new web scraping behavior.

## Architecture

The existing CLI and JSONL ledger remain the system of record. A new
`dashboard.py` module reads the ledger, report output, scheduler log, and
optional pre-match state through explicit paths and builds a JSON-safe view
model. It must never append, rewrite, settle, or freeze a ledger record.

A standard-library HTTP server exposes the view model at local-only endpoints
and serves a static single-page dashboard. The browser page renders only data
returned by those endpoints. The page is an inspection surface, not a command
surface: no action changes data and no network source is called from the
browser.

```
JSONL ledger + scheduler log + pre-match state
                  |
                  v
           dashboard.py (read-only)
                  |
                  v
       local server /api/summary + static UI
```

## Dashboard Content

The first screen uses compact operational panels rather than marketing cards:

1. **Calibration overview**: settled primary-record count, phase, Brier score,
   log loss, ECE, and the sample gates for observational calibration and
   enhanced-model weight evaluation.
2. **Strategy comparison**: rows from the report's strategy breakdown, with
   counts, Brier score, log loss, and top-pick accuracy. A visible note states
   that groups are observational and not causal comparisons.
3. **Operations health**: latest scheduler event timestamp/status, age in
   hours, whether a run is stale (more than 24 hours), and enabled/disabled
   status only when provided by the launcher. Missing logs display a concrete
   unavailable state.
4. **Evidence health**: source observation and collection-failure counts from
   the existing report.
5. **Recent ledger activity**: the newest records with type, match identity,
   timestamp, and immutable content hash prefix. Raw source URLs and full
   probability matrices stay out of the landing view.
6. **Agent workflow**: a static four-stage trace of source collection,
   evidence validation, prediction freeze, and official settlement. It links
   back to the project rules and makes all non-action behaviors explicit.

## API Contract

`GET /api/summary` returns a JSON object with:

- `generated_at`: ISO-8601 timestamp in local time.
- `calibration`: normalized fields from the existing report plus explicit
  sample gates.
- `strategies`: sorted strategy rows.
- `operations`: latest scheduler summary, age, and staleness state.
- `evidence`: evidence-event and evidence-source aggregates.
- `recent_records`: at most 10 normalized ledger records.
- `warnings`: concrete, human-readable data-quality warnings.

`GET /api/health` returns `{ "status": "ok" }` only after the dashboard can
load a ledger and build a summary. Invalid ledger data returns HTTP 500 with a
non-sensitive error payload. `GET /` serves the static dashboard.

The server binds to `127.0.0.1` by default, accepts a configurable port, and
opens no external network connection.

## Error Handling

An absent scheduler log, pre-match state, or optional evidence field does not
make the dashboard fail. The corresponding panel shows an unavailable state
and a warning. A missing or malformed ledger is a startup/API error because no
trustworthy report can be computed. The dashboard must expose the error in the
browser without a traceback.

## Launcher And Portability

`work/run-tai-cai-task.ps1` resolves Python in this order: an optional
`-PythonPath` argument, an existing `TAICAI_PYTHON` environment variable, then
the `python` command on `PATH`. It emits a concise actionable error when no
Python 3.10+ executable is available. It removes the machine-specific
`D:\python\python.exe` dependency.

A new `work/start-dashboard.ps1` launches the local dashboard with the same
Python-resolution helper and prints its local URL. It does not register,
enable, or modify Windows scheduled tasks.

## Testing

Tests use `unittest` and create temporary ledger/log fixtures. They cover:

- correct summary construction from a minimal valid ledger;
- missing scheduler log producing an unavailable operational state;
- malformed optional scheduler log producing a warning rather than failing the
  entire summary;
- dashboard API health and summary responses through a real local HTTP server;
- Python-resolution precedence in the PowerShell script through static and
  command-level checks where the host permits it.

The existing full suite must remain green. New production behavior is written
only after its focused test has failed for the expected missing feature.

## Documentation

The top-level README explains the problem, architecture, safety boundary,
quick start, dashboard URL, test command, and representative engineering
decisions. It must distinguish offline ledger writes from the optional public
data collectors, correcting the current ambiguous "does not scrape" wording.

An architecture diagram in `docs/` presents source collection, immutable
ledger, CLI/report pipeline, dashboard, and testing boundary. All claims use
measured project facts and avoid promising predictive performance.

## Acceptance Criteria

- A clean Windows machine with Python on `PATH` can start the dashboard without
  editing a hard-coded drive path.
- The dashboard is local-only, read-only, responsive, and makes no external
  browser requests.
- Its summary matches the CLI report for the same ledger.
- Missing operational logs degrade visibly instead of crashing.
- Focused new tests demonstrate red then green behavior; the whole suite passes.
- README gives a recruiter enough context to run, inspect, and discuss the
  project in under five minutes.
