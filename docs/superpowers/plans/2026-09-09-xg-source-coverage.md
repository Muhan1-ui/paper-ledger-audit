# High-Quality xG Source Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand traceable pre-kickoff xG coverage by resolving Understat and xGscore identities from strictly verified Wikidata anchors, without weakening evidence or altering the immutable paper ledger.

**Architecture:** Wikidata remains a read-only identity bridge. A cached official-team identity retains its English label and exact English aliases. For an Understat-covered competition, one freshly captured league response supplies the provider roster; an official team can use the anchor only when exactly one roster title exactly equals one unique Wikidata English name. xGscore discovery uses the same verified English names when FotMob identities are absent, and its detail collector still verifies both source teams and kickoff before evidence is accepted.

**Tech Stack:** Python standard library, existing public Understat and xGscore collectors, immutable JSONL ledger, mutable source-link registry, `unittest`.

---

### Task 1: Retain exact English Wikidata aliases

**Files:**
- Modify: `outputs/tai_cai_tracker/wikidata.py`
- Modify: `outputs/tai_cai_tracker/resolver.py`
- Modify: `work/tai_cai_tracker_tests/test_wikidata.py`
- Modify: `work/tai_cai_tracker_tests/test_resolver.py`

- [x] **Step 1: Write failing tests**

```python
def test_resolves_exact_english_aliases_from_the_verified_entity(self):
    # Entity has the label "Inter Milan" and alias "Inter".
    result = resolve_team_identity("国际米兰", fetch_json=fixture_fetch)
    self.assertEqual(result["english_names"], ["Inter Milan", "Inter"])

def test_registry_rejects_invalid_or_duplicated_english_aliases(self):
    identity = valid_identity | {"english_names": ["Inter Milan", "Inter Milan"]}
    with self.assertRaisesRegex(ValueError, "English names"):
        record_wikidata_team_identity(registry_path, identity)
```

- [x] **Step 2: Verify RED**

Run: `python -m unittest -v work.tai_cai_tracker_tests.test_wikidata work.tai_cai_tracker_tests.test_resolver.CandidateRankingTests`

Expected: the new assertions fail because resolved identities expose only `english_name` and the registry does not validate `english_names`.

- [x] **Step 3: Implement minimal identity extension**

```python
def _english_names(entity):
    label = entity["labels"]["en"]["value"].strip()
    aliases = [entry["value"].strip() for entry in entity.get("aliases", {}).get("en", [])]
    return [name for index, name in enumerate([label, *aliases]) if name and name.casefold() not in {prior.casefold() for prior in [label, *aliases][:index]}]
```

Return `english_names` in resolved Wikidata identities. Validate it only when present so prior mutable-registry entries remain readable. Require a non-empty list of distinct strings whose first item equals `english_name`.

- [x] **Step 4: Verify GREEN**

Run: `python -m unittest -v work.tai_cai_tracker_tests.test_wikidata work.tai_cai_tracker_tests.test_resolver.CandidateRankingTests`

Expected: all selected tests pass.

### Task 2: Resolve Understat roster identities from Wikidata anchors

**Files:**
- Modify: `outputs/tai_cai_tracker/understat.py`
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/tai_cai_tracker_tests/test_understat.py`
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [x] **Step 1: Write failing tests**

```python
def test_resolves_one_understat_roster_title_from_one_wikidata_english_anchor(self):
    payload = {"teams": {"1": {"title": "Inter", "history": []}}}
    result = resolve_anchored_team_name(payload, {"english_names": ["Inter Milan", "Inter"]})
    self.assertEqual(result, {"status": "resolved", "source_name": "Inter", "source_team_id": "1"})

def test_rejects_ambiguous_understat_roster_matches(self):
    payload = {"teams": {"1": {"title": "Inter"}, "2": {"title": "Inter"}}}
    self.assertEqual(resolve_anchored_team_name(payload, anchor)["reason"], "understat_anchor_ambiguous")
```

- [x] **Step 2: Verify RED**

Run: `python -m unittest -v work.tai_cai_tracker_tests.test_understat.UnderstatTests work.tai_cai_tracker_tests.test_cli.CliWorkflowTests`

Expected: import or attribute failure for the missing anchored resolver.

- [x] **Step 3: Implement one-response anchored batch planning**

```python
def resolve_anchored_team_name(payload, identity):
    # Compare compacted exact names only. One provider roster title and one anchor
    # name are required; otherwise return an explicit unavailable reason.
```

Add an Understat function that fetches one `league + season` response, resolves both teams from manual aliases first and Wikidata anchors second, then derives venue-split xG from that same response. It must retain the existing response URL, capture time, raw SHA-256, provider team IDs, and exact provider names. It must reject missing anchors, ambiguous roster matches, unsupported leagues, and zero venue samples. It must not write an alias registry or the paper ledger.

Update `_prefetch_understat_batch_xg` to pass `source_link_registry_path`, prefer explicit Understat aliases, then use the anchored batch function only for unresolved eligible teams. Report group count plus `understat_wikidata_anchor_fixture_count` and unavailable reason counts.

- [x] **Step 4: Verify GREEN**

Run: `python -m unittest -v work.tai_cai_tracker_tests.test_understat work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_schedule_driven_freeze_batches_understat_for_adopted_team_aliases`

Expected: selected tests pass, including the existing manual-alias batch path.

### Task 3: Remove FotMob as xGscore discovery prerequisite

**Files:**
- Modify: `outputs/tai_cai_tracker/cli.py`
- Modify: `work/tai_cai_tracker_tests/test_cli.py`

- [x] **Step 1: Write failing tests**

```python
def test_xgscore_batch_uses_verified_wikidata_names_when_fotmob_is_missing(self):
    # No FotMob identity exists. Both cached Wikidata anchors have exact English names.
    result, summary = _resolve_xgscore_source_links([match], registry_path)
    discover.assert_called_once_with([{"match_id": str(match["match_id"]), "home_team": "Inter", "away_team": "Roma"}])
    self.assertEqual(summary["xgscore_wikidata_anchor_fixture_count"], 1)
```

- [x] **Step 2: Verify RED**

Run: `python -m unittest -v work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_xgscore_batch_uses_verified_wikidata_names_when_fotmob_is_missing`

Expected: the result is unavailable because the current implementation requires cached FotMob identities.

- [x] **Step 3: Implement strict source-name precedence**

```python
def _xgscore_source_names(match, registry_path):
    # Existing complete FotMob identities first; otherwise cached Wikidata English
    # labels. Missing or duplicate names remain unavailable.
```

Change `_resolve_xgscore_source_links` to choose pair names in this order: complete cached FotMob identities, then a pair of cached Wikidata English labels. Preserve one homepage read per batch. Keep `collect_xgscore` as the authoritative final check of exact source teams and kickoff; discovery remains a candidate locator only. Add `xgscore_fotmob_anchor_fixture_count` and `xgscore_wikidata_anchor_fixture_count` to its summary.

- [x] **Step 4: Verify GREEN**

Run: `python -m unittest -v work.tai_cai_tracker_tests.test_xgscore work.tai_cai_tracker_tests.test_cli.CliWorkflowTests.test_xgscore_batch_uses_verified_wikidata_names_when_fotmob_is_missing`

Expected: selected tests pass and no FotMob lookup is needed for the Wikidata case.

### Task 4: Document the cascade and run complete verification

**Files:**
- Modify: `outputs/tai_cai_tracker/README.md`

- [x] **Step 1: Document source roles**

State that Understat roster resolution may use a unique exact Wikidata English anchor only after a freshly captured provider roster agrees; it is not a stored provider identity. State xGscore locator discovery may use the same anchor only when FotMob is absent, and detail-page identity plus kickoff remain mandatory. Preserve the quality tiers: Understat venue split and xGscore source match model have their existing frozen evidence scopes and ceilings.

- [x] **Step 2: Run the full suite**

Run: `python -m unittest discover -s work/tai_cai_tracker_tests -t . -v`

Expected: exit code `0` with all tracker tests passing. If it runs beyond the command window, run the CLI module separately and preserve its exit code and log.

- [x] **Step 3: Run a read-only ledger report**

Run: `python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl --json`

Expected: a JSON report; no JSONL lines are modified.
