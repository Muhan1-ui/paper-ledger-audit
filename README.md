# TaiCaiTracker

Paper-only football data audit and calibration tracker. It preserves immutable pre-match records, validates evidence timing, and reviews outcomes without purchases, funding, or betting actions.

## Quick start

```powershell
powershell -ExecutionPolicy Bypass -File work/start-dashboard.ps1
```

Open `http://127.0.0.1:8765`. The dashboard is read-only and local-only.

## Engineering highlights

- Immutable JSONL ledger with linked prediction and settlement hashes.
- Evidence quality gates for official data, named odds references, xG context, and cutoff timing.
- Calibration reporting with Brier score, log loss, ECE, source cohorts, and sample gates.
- Agent workflow: collect evidence, validate constraints, freeze a paper record, settle only from official results.
- Standard-library localhost API with no external browser requests.

## Verify

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -p "test_*.py"
```

See [architecture](docs/architecture.md) and [project rules](AGENTS.md).
