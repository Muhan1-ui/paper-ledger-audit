# Automatic Context Link Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover and strictly verify public event URLs for pregame context without accepting ambiguous matches.

**Architecture:** Extend the existing resolver with a focused event-source adapter and a versioned alias registry. The resolver returns a context event only after a high score and a clear lead; `pregame-context` remains the independent final identity gate.

**Tech Stack:** Python standard library, existing resolver scoring, public event JSON, JSON registry, unittest.

---

### Task 1: Add strict context-event ranking

**Files:**
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Modify: `work/tai_cai_tracker_tests/test_resolver.py`

- [ ] Write failing tests for a `0.90` minimum score, `0.08` winner margin,
  kickoff normalization, and ambiguous-result skips.
- [ ] Run `python -m unittest work.tai_cai_tracker_tests.test_resolver -v` and
  verify the new tests fail.
- [ ] Implement `rank_context_event_candidates(sporttery, candidates)` that
  returns `status: adopted` only for an eligible unique candidate; otherwise
  returns `status: skipped` with score evidence and the top two candidates.
- [ ] Re-run the resolver tests and verify they pass.

### Task 2: Add a guarded alias registry

**Files:**
- Create: `outputs/tai_cai_tracker/data/team_aliases.json`
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Modify: `work/tai_cai_tracker_tests/test_resolver.py`

- [ ] Write failing fixture tests for loading aliases, persisting an adopted
  candidate with source team IDs, and rejecting a conflicting alias.
- [ ] Run focused tests and verify failure.
- [ ] Implement atomic JSON registry read/write. Only adopted candidates with
  both source IDs may add aliases; retain source URL, score, and first-seen
  timestamp. Never overwrite a contradictory mapping.
- [ ] Re-run focused tests and verify pass.

### Task 3: Collect public event candidates

**Files:**
- Create: `outputs/tai_cai_tracker/context_resolver.py`
- Modify: `work/tai_cai_tracker_tests/test_context_resolver.py`

- [ ] Write failing tests using a public-event fixture with multiple events;
  assert date filtering, candidate projection, source URL construction, and
  no-result handling.
- [ ] Run the focused tests and verify failure.
- [ ] Implement one event-source adapter that requests the target date,
  projects only ID, teams, tournament, kickoff, capture time, and direct URL,
  and passes candidates to the strict resolver.
- [ ] Re-run focused tests and verify pass.

### Task 4: Expose one automatic context workflow

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `outputs/tai_cai_tracker/README.md`
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] Write a failing CLI help/output test for `resolve-context-event`.
- [ ] Run it and verify failure.
- [ ] Implement a read-only command accepting an official identity JSON path;
  output adopted/skipped evidence, event URL, and a `pregame-context` command
  only for adopted events. It must not write predictions or edit aliases on
  skipped results.
- [ ] Re-run command tests and verify pass.

### Task 5: Verify integration and documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-07-27-automatic-context-link-resolution-design.md`

- [ ] Document public-source limitations, strict thresholds, alias evidence,
  and the no-guess rule.
- [ ] Run `python -m unittest discover -s work/tai_cai_tracker_tests -t . -v`.
- [ ] Run `python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl --json` and verify existing ledger totals remain readable.
