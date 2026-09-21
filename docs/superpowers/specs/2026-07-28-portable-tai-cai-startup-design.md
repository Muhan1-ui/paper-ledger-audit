# Portable Tai Cai Startup Package

## Goal

Make the tracker usable from a fresh Codex task on another computer and a
different account without re-pasting the conversation's operating rules.

## Scope

The portable package consists of three project-root documents:

1. `AGENTS.md` is the machine-readable operating contract for Codex. It
   defines paper-only behavior, evidence thresholds, immutable ledger rules,
   and concise match-report fields.
2. `START_NEW_CHAT.md` is the user-facing entry point. It gives the smallest
   set of task prompts needed to start a new monitoring, settlement, or review
   session.
3. `PORTABLE_SETUP.md` explains how to transfer the whole project, install the
   required Python runtime, open the project in Codex, and preserve the ledger.

## Behavior

`AGENTS.md` must require the agent to:

- keep work paper-only: no purchase, funding, or betting instructions;
- use China Sports Lottery public pages for match identity, official fixed
  prizes, and settlement;
- never invent odds, lineups, injuries, xG, weather, or final results;
- freeze one primary prediction only when required evidence is available;
- settle only from a traceable official final-result page;
- preserve frozen JSONL records and append only through the tracker CLI;
- handle one match at a time unless the user explicitly asks for a batch;
- state missing evidence and calibration sample limits clearly.

The package must not require the receiving computer to have the locally
installed `tai-cai-audit` skill. When it is available, it may be used as an
additional workflow guard; the project files remain the source of truth.

## Transfer Boundary

The user copies the complete project directory, including `outputs`, `work`,
and `docs`. The immutable ledger remains at
`outputs/tai_cai_tracker/data/paper_ledger.jsonl`. The package contains no
account credentials, browser profile, or private API keys.

## Verification

Review the three resulting documents for consistent rules, valid local paths,
and an explicit no-betting boundary. No tracker code, frozen prediction, or
ledger record is changed by this work.
