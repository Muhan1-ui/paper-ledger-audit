# Match Link Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a conservative resolver that returns auditable OddsPortal and FotMob link candidates for one Sporttery match.

**Architecture:** Add a focused `resolver.py` module with pure normalization/scoring helpers and a thin public-collection wrapper. Keep risky live lookup separate from deterministic tests so the system remains stable.

**Tech Stack:** Python standard library, existing `outputs.tai_cai_tracker` CLI style, `unittest`.

---

## File Structure

- Create `outputs/tai_cai_tracker/resolver.py`: normalization, similarity scoring, candidate ranking, manual alias support, and safe result formatting.
- Create `work/tai_cai_tracker_tests/test_resolver.py`: deterministic tests for normalization, scoring, tie handling, and CLI command exposure.
- Modify `outputs/tai_cai_tracker/cli.py`: add `resolve-links` command.
- Modify `outputs/tai_cai_tracker/README.md`: document command and confidence policy.

### Task 1: Pure Team Normalization And Similarity

**Files:**
- Create: `outputs/tai_cai_tracker/resolver.py`
- Test: `work/tai_cai_tracker_tests/test_resolver.py`

- [ ] **Step 1: Write the failing tests**

```python
import unittest

from outputs.tai_cai_tracker.resolver import normalize_team_name, team_similarity


class ResolverTests(unittest.TestCase):
    def test_normalize_team_name_removes_common_suffixes_and_aliases(self):
        self.assertEqual(normalize_team_name("Ulsan HD FC"), "ulsan hd")
        self.assertEqual(normalize_team_name("FC Seoul"), "seoul")
        self.assertEqual(normalize_team_name("首尔足球俱乐部"), "seoul")

    def test_team_similarity_scores_aliases_above_unrelated_names(self):
        self.assertGreaterEqual(team_similarity("首尔", "FC Seoul"), 0.80)
        self.assertLess(team_similarity("首尔", "Pohang Steelers"), 0.50)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: fail because `outputs.tai_cai_tracker.resolver` does not exist.

- [ ] **Step 3: Implement minimal normalization and similarity**

Create `outputs/tai_cai_tracker/resolver.py` with:

```python
import re
from difflib import SequenceMatcher


ALIASES = {
    "首尔": "seoul",
    "首尔足球俱乐部": "seoul",
    "蔚山": "ulsan hd",
    "蔚山hd": "ulsan hd",
}

STOP_WORDS = {"fc", "football", "club", "足球俱乐部"}


def normalize_team_name(value):
    if not isinstance(value, str):
        return ""
    compact = re.sub(r"\s+", " ", value.strip().lower())
    no_space = compact.replace(" ", "")
    if no_space in ALIASES:
        return ALIASES[no_space]
    tokens = [token for token in re.split(r"[^a-z0-9]+", compact) if token]
    tokens = [token for token in tokens if token not in STOP_WORDS]
    if tokens and tokens[0] == "fc":
        tokens = tokens[1:]
    return " ".join(tokens)


def team_similarity(left, right):
    left_norm = normalize_team_name(left)
    right_norm = normalize_team_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: OK.

### Task 2: Candidate Scoring And Confidence

**Files:**
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Modify: `work/tai_cai_tracker_tests/test_resolver.py`

- [ ] **Step 1: Add failing tests for candidate ranking**

Append tests:

```python
from outputs.tai_cai_tracker.resolver import rank_match_candidates


class CandidateRankingTests(unittest.TestCase):
    def test_rank_match_candidates_selects_high_confidence_pair(self):
        sporttery = {
            "home_team": "首尔",
            "away_team": "蔚山HD",
            "competition": "K League 1",
            "kickoff_at": "2026-07-27T18:30:00+08:00",
        }
        candidates = [
            {
                "home_team": "FC Seoul",
                "away_team": "Ulsan HD FC",
                "competition": "K League 1",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
                "source_url": "https://www.oddsportal.com/football/h2h/seoul/ulsan/#abc",
            }
        ]

        result = rank_match_candidates(sporttery, candidates)

        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["status"], "candidate")
        self.assertGreaterEqual(result["score"], 0.90)

    def test_rank_match_candidates_skips_close_tie(self):
        sporttery = {"home_team": "首尔", "away_team": "蔚山HD"}
        candidates = [
            {"home_team": "FC Seoul", "away_team": "Ulsan HD FC", "source_url": "https://a.test"},
            {"home_team": "Seoul E-Land", "away_team": "Ulsan HD FC", "source_url": "https://b.test"},
        ]

        result = rank_match_candidates(sporttery, candidates, tie_margin=0.20)

        self.assertEqual(result["status"], "skipped")
        self.assertIn("ambiguous", result["reason"])
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: fail because `rank_match_candidates` is missing.

- [ ] **Step 3: Implement ranking**

Add to `resolver.py`:

```python
from datetime import datetime


def _date_score(left, right):
    if not left or not right:
        return 0.0
    try:
        left_dt = datetime.fromisoformat(left)
        right_dt = datetime.fromisoformat(right)
    except ValueError:
        return 0.0
    delta_seconds = abs((left_dt - right_dt).total_seconds())
    if delta_seconds <= 3600:
        return 1.0
    if delta_seconds <= 86400:
        return 0.5
    return 0.0


def _confidence(score):
    if score >= 0.86:
        return "high"
    if score >= 0.70:
        return "medium"
    return "low"


def _candidate_score(sporttery, candidate):
    home = team_similarity(sporttery.get("home_team"), candidate.get("home_team"))
    away = team_similarity(sporttery.get("away_team"), candidate.get("away_team"))
    date = _date_score(sporttery.get("kickoff_at"), candidate.get("kickoff_at"))
    score = (home * 0.45) + (away * 0.45) + (date * 0.10)
    return score, [
        f"home_similarity={home:.2f}",
        f"away_similarity={away:.2f}",
        f"date_score={date:.2f}",
    ]


def rank_match_candidates(sporttery, candidates, tie_margin=0.04):
    scored = []
    for candidate in candidates:
        score, evidence = _candidate_score(sporttery, candidate)
        scored.append((score, evidence, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return {"status": "skipped", "reason": "no candidates"}
    if len(scored) > 1 and scored[0][0] - scored[1][0] < tie_margin:
        return {"status": "skipped", "reason": "ambiguous candidates", "candidates": [item[2] for item in scored[:2]]}
    score, evidence, candidate = scored[0]
    confidence = _confidence(score)
    if confidence == "low":
        return {"status": "skipped", "reason": "low confidence", "score": score, "candidate": candidate}
    return {"status": "candidate", "confidence": confidence, "score": score, "source_url": candidate.get("source_url"), "evidence": evidence, "candidate": candidate}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_resolver -v
```

Expected: OK.

### Task 3: CLI Exposure With Manual Candidate Files

**Files:**
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [ ] **Step 1: Add failing CLI help test**

Add to `test_cli.py`:

```python
    def test_resolve_links_command_is_exposed(self):
        result = self.run_cli("resolve-links", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("candidate", result.stdout)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_resolve_links_command_is_exposed -v
```

Expected: fail because command does not exist.

- [ ] **Step 3: Implement CLI command**

Add a parser command in `cli.py`:

```python
resolve_links_parser = commands.add_parser(
    "resolve-links",
    help="rank manually supplied OddsPortal/FotMob candidate links for one Sporttery match",
)
resolve_links_parser.add_argument("input_path")
```

Add branch in `main`:

```python
elif arguments.command == "resolve-links":
    print(json.dumps(resolve_links(arguments.input_path), ensure_ascii=False, sort_keys=True))
```

Add to `resolver.py`:

```python
import json
from pathlib import Path


def resolve_links(input_path):
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    sporttery = payload["sporttery"]
    return {
        "sporttery": sporttery,
        "oddsportal": rank_match_candidates(sporttery, payload.get("oddsportal_candidates", [])),
        "fotmob_home": rank_match_candidates(
            {"home_team": sporttery.get("home_team"), "away_team": sporttery.get("home_team"), "kickoff_at": sporttery.get("kickoff_at")},
            [{"home_team": candidate.get("team_name"), "away_team": candidate.get("team_name"), "source_url": candidate.get("source_url")} for candidate in payload.get("home_fotmob_candidates", [])],
        ),
        "fotmob_away": rank_match_candidates(
            {"home_team": sporttery.get("away_team"), "away_team": sporttery.get("away_team"), "kickoff_at": sporttery.get("kickoff_at")},
            [{"home_team": candidate.get("team_name"), "away_team": candidate.get("team_name"), "source_url": candidate.get("source_url")} for candidate in payload.get("away_fotmob_candidates", [])],
        ),
    }
```

- [ ] **Step 4: Run CLI help test**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_resolve_links_command_is_exposed -v
```

Expected: OK.

### Task 4: Documentation And Full Verification

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`

- [ ] **Step 1: Document the manual candidate resolver**

Add a README section:

```markdown
## Match Link Resolver

`resolve-links` ranks user-supplied OddsPortal and FotMob candidate links for
one Sporttery match. It is conservative: ambiguous or low-confidence candidates
are skipped and must be manually confirmed.

```powershell
python -m outputs.tai_cai_tracker.cli resolve-links candidates.json
```
```

- [ ] **Step 2: Run all tests**

Run:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

Expected: all tests pass.

- [ ] **Step 3: Run read-only ledger report**

Run:

```powershell
python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl --json
```

Expected: command exits 0 and does not append records.

