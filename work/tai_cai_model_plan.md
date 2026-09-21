# China Sports Lottery Football Calibration Tracker Implementation Plan

**Goal:** Build a local, auditable paper-tracking tool that freezes China Sports Lottery football predictions before kick-off, settles them with official results, and reports calibration only after sufficient observations.

**Architecture:** A small Python package writes append-only JSONL records. `model.py` derives three-way probabilities from normalized fixed-prize odds and optionally blends an xG-based Poisson estimate; `ledger.py` validates and freezes a prediction record, then appends an official settlement; `report.py` calculates Brier score and rejects calibration reports below 50 settled matches.

**Tech Stack:** Python standard library, `unittest`, JSONL.

### Task 1: Market probability calculation

Files: `outputs/tai_cai_tracker/model.py`, `work/tai_cai_tracker_tests/test_model.py`

1. Write a failing test that expects decimal prizes `[2, 3, 6]` to normalize to probabilities `[0.5, 1/3, 1/6]`.
2. Run `python -m unittest work/tai_cai_tracker_tests/test_model.py -v`; expected failure: module `outputs.tai_cai_tracker.model` does not exist.
3. Implement `normalize_fixed_prizes(prizes)` with positive-three-prize validation and normalized reciprocal probabilities.
4. Re-run the command; expected pass.

### Task 2: Frozen prediction validation

Files: `outputs/tai_cai_tracker/ledger.py`, `work/tai_cai_tracker_tests/test_ledger.py`

1. Write a failing test that creates a valid `SPF` prediction and asserts the returned record is `frozen`, includes a SHA-256 content hash, and does not include an actual result.
2. Run `python -m unittest work/tai_cai_tracker_tests/test_ledger.py -v`; expected failure: module `ledger` does not exist.
3. Implement validation requiring China Sports Lottery match id, kick-off, capture time before kick-off, S1/S2 source URL, three outcomes, and probabilities summing to one.
4. Re-run the command; expected pass.

### Task 3: Official settlement and calibration gate

Files: `outputs/tai_cai_tracker/ledger.py`, `outputs/tai_cai_tracker/report.py`, `work/tai_cai_tracker_tests/test_report.py`

1. Write a failing test that settles a frozen record with `home_win` and checks its Brier contribution; write another that expects calibration to be unavailable under 50 records.
2. Run `python -m unittest work/tai_cai_tracker_tests/test_report.py -v`; expected failure because settlement/report functions do not exist.
3. Implement immutable settlement records, three-class Brier score, and a 50-settled-match gate.
4. Re-run the command; expected pass.

### Task 4: Command-line snapshot and settlement workflow

Files: `outputs/tai_cai_tracker/cli.py`, `outputs/tai_cai_tracker/README.md`, `outputs/tai_cai_tracker/example_input.json`, `work/tai_cai_tracker_tests/test_cli.py`

1. Write a failing test that runs `snapshot` against the example input and asserts exactly one JSONL prediction is appended; write a test that `settle` appends a linked settlement.
2. Run `python -m unittest discover -s work/tai_cai_tracker_tests -v`; expected failure because CLI does not exist.
3. Implement `snapshot`, `settle`, and `report` commands. Include only paper-tracking language and require a source URL, capture time, official match id, fixed prizes, and optional xG inputs.
4. Re-run the full suite; expected all pass.

### Task 5: Verify the user-facing workflow

Files: `outputs/tai_cai_tracker/README.md`, `outputs/tai_cai_tracker/data/`

1. Run a snapshot against the included clearly marked example data.
2. Run an official-result settlement only after a real result is supplied; do not invent a real outcome.
3. Run `report`; expected message: calibration unavailable until 50 settled predictions.
4. Document the exact source hierarchy: official China Sports Lottery for match identity and settlement, public odds source for European market prior, and cited statistics source for xG.
