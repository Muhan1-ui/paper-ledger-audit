import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from outputs.tai_cai_tracker import cli as tracker_cli
from outputs.tai_cai_tracker.cli import (
    format_report,
    prediction_from_input,
    read_records,
    settlement_queue,
)
from outputs.tai_cai_tracker.ledger import (
    ALL_MARKET_OPTIONS,
    freeze_all_market_snapshot,
    invalidate_settlement,
    freeze_odds_capture,
    freeze_prediction,
    settle_prediction,
)
from outputs.tai_cai_tracker.sales_cutoff import default_policy
from outputs.tai_cai_tracker.resolver import cached_wikidata_team_identity


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_INPUT = WORKSPACE_ROOT / "outputs" / "tai_cai_tracker" / "example_input.json"
EXAMPLE_ODDS_IMPORT = WORKSPACE_ROOT / "outputs" / "tai_cai_tracker" / "example_odds_import.json"


def _review_ready_bundle(match):
    prizes = {
        code: {option: str(Decimal(index + 2)) for index, option in enumerate(options)}
        for code, options in ALL_MARKET_OPTIONS.items()
    }
    official_markets = {
        code: {
            "published_at": "2099-01-01T10:00:00+08:00",
            "official_source_url": match["official_source_url"],
            "fixed_prizes": prizes[code],
            **({"home_handicap": "-1"} if code == "hhad" else {}),
        }
        for code in ALL_MARKET_OPTIONS
    }
    xg_inputs = {
        "source_url": "https://stats.example.test/league",
        "home_xg": "1.20",
        "away_xg": "0.80",
        "model_version": "poisson-v1",
        "model_weight": "0.40",
        "confidence_ceiling": "high",
        "evidence": {
            "source_url": "https://stats.example.test/league",
            "published_at": "2099-01-01T11:00:00+08:00",
            "competition": match["competition"],
            "season": "2098/99",
            "home_sample_matches": 8,
            "away_sample_matches": 8,
            "scope": "home_away_split",
            "home_attack_xg": "12.0",
            "home_defense_xga": "8.0",
            "away_attack_xg": "8.0",
            "away_defense_xga": "12.0",
            "formula": "test home/away split",
        },
    }
    return {
        "generated_at": "2099-01-01T12:00:00+08:00",
        "quality_gate": {"status": "review_ready", "reasons": []},
        "odds_import_input": {
            "provider": "433",
            "captures": [{
                "captured_at": "2099-01-01T11:30:00+08:00",
                "source_url": "https://www.433.cool/zqzhishu/data/1.html",
                "bookmaker": "ExampleBook",
                "odds": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
            }],
        },
        "prediction_input_template": {
            "market": "SPF",
            "match_id": match["match_id"],
            "kickoff_at": match["kickoff_at"],
            "captured_at": "2099-01-01T12:00:00+08:00",
            "source_urls": {"S1": match["official_source_url"], "S2": "https://www.433.cool/zqzhishu/data/1.html"},
            "match_context": {
                "competition": match["competition"],
                "home_team": match["home_team"],
                "away_team": match["away_team"],
            },
            "fixed_prizes": prizes["spf"],
            "european_odds": {"capture_hash": "<fill_after_odds-import>"},
            "xg_inputs": xg_inputs,
        },
        "all_market_snapshot_input_template": {
            "prediction_hash": "<fill_after_snapshot>",
            "captured_at": "2099-01-01T12:00:00+08:00",
            "official_source_url": match["official_source_url"],
            "official_markets": official_markets,
        },
    }


class CliWorkflowTests(unittest.TestCase):
    def test_historical_fotmob_alias_candidates_prioritizes_current_schedule_without_writing_identity_cache(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            registry_path = temporary_path / "source-links.json"
            alias_path = temporary_path / "fotmob-search-aliases.json"
            queue_path = temporary_path / "candidate-queue.json"
            identity_path = temporary_path / "source-links.json"
            progress_path.write_text(json.dumps({
                "version": 1,
                "days_back": 3,
                "range_start": "2099-01-01",
                "range_end": "2099-01-03",
                "attempted_teams": {
                    "oldfc": {
                        "team_name": "Old FC", "search_name": "Old FC", "status": "unresolved",
                        "attempted_at": "2099-01-03T10:00:00+08:00", "reason": "no high-confidence result",
                    },
                    "currentfc": {
                        "team_name": "Current FC", "search_name": "Current FC", "status": "unresolved",
                        "attempted_at": "2099-01-01T10:00:00+08:00", "reason": "no high-confidence result",
                        "official_references": [{
                            "match_id": 2042001, "match_date": "2099-01-01",
                            "official_result_url": "https://official.example.test/result?date=2099-01-01",
                        }],
                    },
                },
            }), encoding="utf-8")
            alias_path.write_text(json.dumps({"version": 1, "aliases": {
                "currentfc": {
                    "official_team": "Current FC", "search_name": "Current English FC", "provider": "FotMob",
                },
            }}), encoding="utf-8")
            evidence = {
                "provider": "FotMob", "search_term": "Current English FC",
                "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Current%20English%20FC",
                "captured_at": now.isoformat(), "raw_content_sha256": "b" * 64,
                "candidates": [{"team_id": 2, "team_name": "Current English FC", "source_url": "https://www.fotmob.com/teams/2/overview/current-english-fc"}],
            }
            with patch("outputs.tai_cai_tracker.cli.collect_team_search_candidates", return_value=evidence) as search:
                summary = tracker_cli.historical_fotmob_alias_candidates(
                    max_teams=1,
                    now=now,
                    progress_path=progress_path,
                    reviewed_alias_registry_path=alias_path,
                    source_link_registry_path=identity_path,
                    current_matches=[{"home_team": "Current FC", "away_team": "Opponent"}],
                    output_path=queue_path,
                )

            queue = json.loads(queue_path.read_text(encoding="utf-8"))

        search.assert_called_once_with("Current English FC")
        self.assertFalse(identity_path.exists())
        self.assertEqual(summary["queued_team_count"], 1)
        self.assertEqual(queue["items"][0]["official_team"], "Current FC")
        self.assertEqual(queue["items"][0]["search_evidence"]["candidates"][0]["team_id"], 2)

    def test_historical_fotmob_alias_candidates_uses_verified_static_fotmob_search_term(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            queue_path = temporary_path / "candidate-queue.json"
            source_link_path = temporary_path / "source-links.json"
            progress_path.write_text(json.dumps({
                "version": 1,
                "attempted_teams": {
                    "普雷斯顿": {"team_name": "普雷斯顿", "status": "unresolved"},
                },
            }), encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.collect_team_search_candidates",
                return_value={"candidates": [], "raw_content_sha256": "c" * 64},
            ) as search:
                tracker_cli.historical_fotmob_alias_candidates(
                    max_teams=1,
                    now=now,
                    progress_path=progress_path,
                    source_link_registry_path=source_link_path,
                    current_matches=[],
                    output_path=queue_path,
                )

        search.assert_called_once_with("Preston North End")

    def test_sporttery_settle_batch_appends_linked_official_results_only(self):
        prediction = freeze_prediction({
            "market": "SPF",
            "match_id": "周一001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T10:00:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/1",
                "S2": "https://odds.example.test/matches/1",
            },
            "match_context": {
                "competition": "Test League",
                "home_team": "Home FC",
                "away_team": "Away FC",
            },
            "outcomes": [
                {"label": "home_win", "probability": Decimal("0.50")},
                {"label": "draw", "probability": Decimal("0.30")},
                {"label": "away_win", "probability": Decimal("0.20")},
            ],
        })
        markets = {}
        for code, options in ALL_MARKET_OPTIONS.items():
            probabilities = {option: Decimal(0) for option in options}
            probabilities[options[0]] = Decimal(1)
            markets[code] = {
                "status": "frozen",
                "probabilities": probabilities,
                "selected_option": options[0],
                "selected_probability": Decimal(1),
                "tied_options": [options[0]],
                "official_fixed_prizes": {
                    option: Decimal(index + 2) for index, option in enumerate(options)
                },
                "published_at": "2099-01-01T09:00:00+08:00",
                "official_source_url": "https://official.example.test/matches/1",
                **({"home_handicap": Decimal("-1")} if code == "hhad" else {}),
            }
        snapshot = freeze_all_market_snapshot({
            "schema_version": 2,
            "prediction_hash": prediction["content_hash"],
            "match_id": prediction["match_id"],
            "kickoff_at": prediction["kickoff_at"],
            "captured_at": prediction["captured_at"],
            "official_source_url": "https://official.example.test/matches/1",
            "markets": markets,
        })
        results = {
            "matches": [{
                "match_id": 1,
                "match_num": "周一001",
                "match_date": "2099-01-01",
                "home_team": "Home FC",
                "away_team": "Away FC",
                "status": "11",
                "final_score": {"home": 2, "away": 0},
                "half_time_score": {"home": 1, "away": 0},
                "official_result_url": "https://official.example.test/results/1",
            }]
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text(
                "\n".join(json.dumps(record, default=str) for record in (prediction, snapshot)) + "\n",
                encoding="utf-8",
            )
            with patch("outputs.tai_cai_tracker.cli.collect_results", return_value=results):
                summary = tracker_cli.sporttery_settle_batch(ledger_path)

            records = read_records(ledger_path)

        self.assertEqual(summary["settled_count"], 1)
        self.assertEqual(summary["all_market_settled_count"], 1)
        self.assertEqual(records[-2]["final_score"], {"home": 2, "away": 0})
        self.assertEqual(records[-1]["markets"]["hhad"]["actual_option"], "home_win")
        self.assertEqual(records[-1]["markets"]["hafu"]["actual_option"], "HH")

    def test_sporttery_settle_batch_skips_network_when_ledger_has_no_pending_predictions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            registry_path = Path(temporary_directory) / "source-links.json"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_results") as collect:
                summary = tracker_cli.sporttery_settle_batch(ledger_path)

        self.assertEqual(summary["pending_predictions"], 0)
        self.assertEqual(summary["settled_count"], 0)
        collect.assert_not_called()

    def test_sporttery_settle_batch_queries_each_pending_kickoff_date_after_current_results(self):
        prediction = freeze_prediction({
            "market": "SPF",
            "match_id": "周一099",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T10:00:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/99",
                "S2": "https://odds.example.test/matches/99",
            },
            "match_context": {
                "competition": "Test League",
                "home_team": "Home FC",
                "away_team": "Away FC",
            },
            "outcomes": [
                {"label": "home_win", "probability": Decimal("0.50")},
                {"label": "draw", "probability": Decimal("0.30")},
                {"label": "away_win", "probability": Decimal("0.20")},
            ],
        })
        dated_result = {
            "matches": [{
                "match_id": 99,
                "match_num": "周一099",
                "match_date": "2099-01-01",
                "home_team": "Home FC",
                "away_team": "Away FC",
                "status": "11",
                "final_score": {"home": 1, "away": 0},
                "half_time_score": {"home": 0, "away": 0},
                "official_result_url": "https://official.example.test/results/99",
            }],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text(json.dumps(prediction, default=str) + "\n", encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                side_effect=[{"matches": []}, dated_result],
            ) as collect:
                summary = tracker_cli.sporttery_settle_batch(ledger_path)

        self.assertEqual(summary["settled_count"], 1)
        self.assertEqual(
            [invocation.kwargs for invocation in collect.call_args_list],
            [{"match_date": None}, {"match_date": "2099-01-01"}],
        )

    def test_sporttery_settle_batch_rejects_a_cross_date_weekday_sequence_collision(self):
        prediction = freeze_prediction({
            "market": "SPF",
            "match_id": "周二009",
            "kickoff_at": "2099-01-08T20:00:00+08:00",
            "captured_at": "2099-01-08T10:00:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/9",
                "S2": "https://odds.example.test/matches/9",
            },
            "match_context": {
                "competition": "Test League",
                "home_team": "Home FC",
                "away_team": "Away FC",
            },
            "outcomes": [
                {"label": "home_win", "probability": Decimal("0.50")},
                {"label": "draw", "probability": Decimal("0.30")},
                {"label": "away_win", "probability": Decimal("0.20")},
            ],
        })
        result = {
            "matches": [{
                "match_id": 999,
                "match_num": "周二009",
                "match_date": "2099-01-01",
                "home_team": "Home FC",
                "away_team": "Away FC",
                "status": "11",
                "final_score": {"home": 1, "away": 0},
                "official_result_url": "https://official.example.test/results/9",
            }],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text(json.dumps(prediction, default=str) + "\n", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_results", side_effect=[result, result]):
                summary = tracker_cli.sporttery_settle_batch(ledger_path)

        self.assertEqual(summary["settled_count"], 0)
        self.assertEqual(summary["items"], [{
            "match_id": "周二009",
            "status": "pending",
            "reason": "official completed result is unavailable",
        }])

    def test_sporttery_settle_batch_reports_official_pages_and_deduplicates_identical_results(self):
        prediction = freeze_prediction({
            "market": "SPF", "match_id": "周一099",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T10:00:00+08:00",
            "source_urls": {"S1": "https://official.example.test/matches/99", "S2": "https://odds.example.test/matches/99"},
            "match_context": {"competition": "Test League", "home_team": "Home FC", "away_team": "Away FC"},
            "outcomes": [
                {"label": "home_win", "probability": Decimal("0.50")},
                {"label": "draw", "probability": Decimal("0.30")},
                {"label": "away_win", "probability": Decimal("0.20")},
            ],
        })
        result = {
            "match_id": 99, "match_num": "周一099", "match_date": "2099-01-01",
            "home_team": "Home FC", "away_team": "Away FC", "status": "11", "final_score": {"home": 1, "away": 0},
            "official_result_url": "https://official.example.test/results?pageNo=1",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text(json.dumps(prediction, default=str) + "\n", encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                side_effect=[
                    {"page_count": 2, "matches": [result]},
                    {"page_count": 1, "matches": [dict(result)]},
                ],
            ):
                summary = tracker_cli.sporttery_settle_batch(ledger_path)

        self.assertEqual(summary["official_result_page_count"], 3)
        self.assertEqual(summary["official_result_duplicate_count"], 1)
        self.assertEqual(summary["settled_count"], 1)

    def test_sporttery_settle_batch_deduplicates_same_official_result_from_different_queries(self):
        prediction = freeze_prediction({
            "market": "SPF", "match_id": "周一099",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T10:00:00+08:00",
            "source_urls": {"S1": "https://official.example.test/matches/99", "S2": "https://odds.example.test/matches/99"},
            "match_context": {"competition": "Test League", "home_team": "Home FC", "away_team": "Away FC"},
            "outcomes": [
                {"label": "home_win", "probability": Decimal("0.50")},
                {"label": "draw", "probability": Decimal("0.30")},
                {"label": "away_win", "probability": Decimal("0.20")},
            ],
        })
        result = {
            "match_id": 99, "match_num": "周一099", "match_date": "2099-01-01",
            "home_team": "Home FC", "away_team": "Away FC", "status": "11", "final_score": {"home": 1, "away": 0},
            "official_result_url": "https://official.example.test/results?scope=current",
        }
        dated_result = {**result, "official_result_url": "https://official.example.test/results?matchDate=2099-01-01"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text(json.dumps(prediction, default=str) + "\n", encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                side_effect=[{"matches": [result]}, {"matches": [dated_result]}],
            ):
                summary = tracker_cli.sporttery_settle_batch(ledger_path)

        self.assertEqual(summary["official_result_duplicate_count"], 1)
        self.assertEqual(summary["settled_count"], 1)

    def test_schedule_driven_freeze_collects_only_open_unfrozen_matches_in_the_lead_window(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        eligible = {
            "match_id": 20990101001,
            "match_num": "\u5468\u4e00 001",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "2",
        }
        schedule = {"matches": [
            eligible,
            {**eligible, "match_id": 20990101002, "match_num": "\u5468\u4e00 002", "kickoff_at": "2099-01-01T13:00:00+08:00"},
            {**eligible, "match_id": 20990101003, "match_num": "\u5468\u4e00 003", "kickoff_at": "2099-01-01T17:00:00+08:00"},
            {**eligible, "match_id": 20990101004, "match_num": "\u5468\u4e00 004", "status": "1"},
        ]}

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            registry_path = Path(temporary_directory) / "source-links.json"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value=schedule) as collect_schedule, \
                 patch(
                     "outputs.tai_cai_tracker.cli.discover_433_detail_urls",
                     return_value={
                         str(eligible["match_id"]): {
                             "status": "resolved",
                             "source_url": "https://www.433.cool/zqzhishu/data/1.html",
                         }
                     },
                 ) as discover_433, \
                 patch("outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle", side_effect=ValueError("evidence unavailable")) as collect_bundle:
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    source_link_registry_path=registry_path,
                    freeze_window_minutes=0,
                )

        collect_schedule.assert_called_once_with(method="concern")
        discover_433.assert_called_once_with([eligible], max_pages=8)
        collect_bundle.assert_called_once_with(
            eligible["match_id"],
            "https://www.433.cool/zqzhishu/data/1.html",
            schedule_match=eligible,
            allow_433_discovery=False,
            source_link_registry_path=registry_path,
        )
        self.assertEqual(summary["scanned_count"], 4)
        self.assertEqual(summary["eligible_count"], 1)
        self.assertEqual(summary["frozen_count"], 0)
        self.assertEqual(summary["blocked_count"], 1)

    def test_schedule_driven_freeze_applies_a_positive_close_window_only_near_kickoff(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        far_match = {
            "match_id": 20990101005,
            "match_num": "周一005",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T12:45:00+08:00",
            "status": "2",
        }
        near_match = {
            "match_id": 20990101006,
            "match_num": "周一006",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T12:15:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101006",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [far_match, near_match]}), \
                 patch(
                     "outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle",
                     return_value=_review_ready_bundle(near_match),
                 ) as collect_bundle:
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=0,
                    max_lead_minutes=240,
                    now=now,
                    freeze_window_minutes=30,
                )
            records = read_records(ledger_path)

        collect_bundle.assert_called_once()
        self.assertEqual(summary["skipped_by_reason"]["outside_close_freeze_window"], 1)
        self.assertEqual(summary["frozen_count"], 1)
        self.assertEqual(records[1]["record_type"], "prediction")
        self.assertEqual(records[1]["match_id"], "周一006")
        self.assertNotIn(20990101005, [record.get("match_id") for record in records])

    def test_schedule_driven_freeze_refuses_a_concurrent_ledger_update(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")

            with tracker_cli._ledger_operation_lock(ledger_path), \
                 patch("outputs.tai_cai_tracker.cli.collect_schedule") as collect_schedule:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "ledger is already being updated",
                ):
                    tracker_cli.schedule_driven_freeze(ledger_path)

        collect_schedule.assert_not_called()

    def test_schedule_driven_freeze_uses_an_exact_cached_433_link_before_discovery(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        match = {
            "match_id": 20990101021,
            "match_num": "周一021",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101021",
        }
        cached_url = "https://www.433.cool/zqzhishu/data/21.html"
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            registry_path = Path(temporary_directory) / "source-links.json"
            ledger_path.write_text("", encoding="utf-8")
            tracker_cli.record_source_links(
                registry_path,
                match,
                {"european_433_url": cached_url},
            )
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls") as discover_433, \
                 patch(
                     "outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle",
                     return_value={"quality_gate": {"status": "observation_only", "reasons": ["test"]}},
                 ) as collect_bundle:
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    source_link_registry_path=registry_path,
                )

        discover_433.assert_not_called()
        collect_bundle.assert_called_once_with(
            match["match_id"],
            cached_url,
            schedule_match=match,
            allow_433_discovery=False,
            source_link_registry_path=registry_path,
        )
        self.assertEqual(summary["source_link_cache_hit_count"], 1)
        self.assertEqual(summary["source_link_fresh_discovery_count"], 0)
        self.assertEqual(summary["source_link_unresolved_count"], 0)

    def test_schedule_eligibility_allows_a_reused_weekday_sequence_with_a_new_kickoff(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-08T12:00:00+08:00")
        match = {
            "match_id": 20990108001,
            "match_num": "周六001",
            "kickoff_at": "2099-01-08T14:00:00+08:00",
            "status": "2",
        }
        historical_primary = {
            "record_type": "prediction",
            "match_id": "周六001",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "prediction_role": "primary",
        }

        eligible, skipped, _skipped_items, _timings = tracker_cli._eligible_schedule_matches(
            {"matches": [match]},
            [historical_primary],
            now,
            90,
            240,
            sales_cutoff_policy=default_policy(),
        )

        self.assertEqual(eligible, [match])
        self.assertNotIn("primary_prediction_exists", skipped)

    def test_schedule_eligibility_blocks_the_same_weekday_sequence_and_kickoff(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-08T12:00:00+08:00")
        match = {
            "match_id": 20990108001,
            "match_num": "周六001",
            "kickoff_at": "2099-01-08T14:00:00+08:00",
            "status": "2",
        }
        existing_primary = {
            "record_type": "prediction",
            "match_id": "周六001",
            "kickoff_at": match["kickoff_at"],
            "prediction_role": "primary",
        }

        eligible, skipped, _skipped_items, _timings = tracker_cli._eligible_schedule_matches(
            {"matches": [match]},
            [existing_primary],
            now,
            90,
            240,
            sales_cutoff_policy=default_policy(),
        )

        self.assertEqual(eligible, [])
        self.assertEqual(skipped["primary_prediction_exists"], 1)

    def test_operator_open_match_selection_excludes_the_next_sales_day(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-08T21:58:00+08:00")
        overnight = {
            "match_id": 20990108001,
            "match_num": "周六001",
            "kickoff_at": "2099-01-09T07:00:00+08:00",
            "status": "2",
        }
        later = {
            "match_id": 20990108002,
            "match_num": "周日001",
            "kickoff_at": "2099-01-10T03:00:00+08:00",
            "status": "2",
        }

        eligible, skipped, _skipped_items, _timings = tracker_cli._eligible_schedule_matches(
            {"matches": [overnight, later]},
            [],
            now,
            90,
            240,
            sales_cutoff_policy=default_policy(),
            include_open_matches=True,
        )

        self.assertEqual(eligible, [overnight])
        self.assertEqual(skipped["outside_freeze_window"], 1)

    def test_schedule_driven_freeze_uses_sales_cutoff_window_for_late_weekday_fixture(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-05T21:20:00+08:00")
        match = {
            "match_id": 20990105001,
            "match_num": "周一001",
            "competition": "英超",
            "home_team": "主队",
            "away_team": "客队",
            "kickoff_at": "2099-01-05T23:55:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990105001",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            registry_path = Path(temporary_directory) / "source-links.json"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls", return_value={
                     str(match["match_id"]): {"status": "resolved", "source_url": "https://www.433.cool/zqzhishu/data/1.html"},
                 }), \
                 patch("outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle", return_value=_review_ready_bundle(match)):
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    now=now,
                    source_link_registry_path=registry_path,
                )

        self.assertEqual(summary["eligible_count"], 1)
        self.assertEqual(summary["items"][0]["timing_basis"], "sales_cutoff_window")
        self.assertEqual(summary["items"][0]["sales_cutoff_at"], "2099-01-05T22:00:00+08:00")

    def test_schedule_driven_freeze_reports_official_sales_suspension(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-05T22:20:00+08:00")
        match = {
            "match_id": 20990105002,
            "match_num": "周一002",
            "kickoff_at": "2099-01-05T23:55:00+08:00",
            "status": "3",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}):
                summary = tracker_cli.schedule_driven_freeze(ledger_path, now=now)

        self.assertEqual(summary["eligible_count"], 0)
        self.assertEqual(summary["skipped_by_reason"]["official_sales_suspended"], 1)
        self.assertEqual(summary["skipped_items"][0]["timing_basis"], "sales_cutoff_window")

    def test_schedule_driven_freeze_reports_local_recheck_when_no_match_is_officially_open(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        match = {
            "match_id": 20990101003,
            "match_num": "周四003",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "1",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}):
                summary = tracker_cli.schedule_driven_freeze(ledger_path, now=now)

        self.assertEqual(summary["eligible_count"], 0)
        self.assertEqual(summary["sale_availability"], {
            "official_open_sale_count": 0,
            "official_open_time": None,
            "official_open_time_status": "not_published_by_current_official_schedule",
            "official_sale_pending_count": 1,
            "official_sales_suspended_count": 0,
            "next_system_check_at": "2099-01-01T21:00:00+08:00",
            "next_system_check_basis": "local_sales_cutoff_window_start_not_official_open_time",
            "timezone": "Asia/Shanghai",
        })

    def test_schedule_driven_freeze_batches_understat_for_adopted_team_aliases(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        matches = [
            {
                "match_id": 20990101001, "match_num": "周一001",
                "competition": "Premier League", "home_team": "主队甲", "away_team": "客队甲",
                "kickoff_at": "2099-01-01T14:00:00+08:00", "status": "2",
            },
            {
                "match_id": 20990101002, "match_num": "周一002",
                "competition": "Premier League", "home_team": "主队乙", "away_team": "客队乙",
                "kickoff_at": "2099-01-01T14:30:00+08:00", "status": "2",
            },
        ]
        aliases = {
            "version": 1,
            "aliases": {
                "主队甲": {"source_name": "Home One", "source_team_id": 1, "source_provider": "Understat"},
                "客队甲": {"source_name": "Away One", "source_team_id": 2, "source_provider": "Understat"},
                "主队乙": {"source_name": "Home Two", "source_team_id": 3, "source_provider": "Understat"},
                "客队乙": {"source_name": "Away Two", "source_team_id": 4, "source_provider": "Understat"},
            },
        }
        xg_inputs = {
            "source_url": "https://understat.com/getLeagueData/EPL/2098",
            "home_xg": "1.20", "away_xg": "0.80",
            "model_version": "poisson-v1", "model_weight": "0.40",
            "evidence": {
                "source_url": "https://understat.com/getLeagueData/EPL/2098",
                "captured_at": "2099-01-01T12:01:00+08:00",
                "published_at_unavailable_reason": "source does not expose publication time",
                "competition": "EPL", "season": "2098/2099",
                "home_sample_matches": 3, "away_sample_matches": 3,
                "scope": "home_away_split",
                "home_attack_xg": "3.6", "home_defense_xga": "2.4",
                "away_attack_xg": "2.4", "away_defense_xga": "3.6",
                "formula": "test",
            },
        }
        batch = {
            "fixtures": [
                {
                    "match_id": match["match_id"], "status": "available",
                    "source_url": xg_inputs["source_url"],
                    "captured_at": "2099-01-01T12:01:00+08:00",
                    "raw_content_sha256": "a" * 64,
                    "home": {"team_name": aliases["aliases"][match["home_team"]]["source_name"]},
                    "away": {"team_name": aliases["aliases"][match["away_team"]]["source_name"]},
                    "xg_inputs": xg_inputs,
                }
                for match in matches
            ],
        }
        urls = {
            str(match["match_id"]): {
                "status": "resolved",
                "source_url": f"https://www.433.cool/zqzhishu/data/{match['match_id']}.html",
            }
            for match in matches
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            alias_path = Path(temporary_directory) / "aliases.json"
            registry_path = Path(temporary_directory) / "source-links.json"
            alias_path.write_text(json.dumps(aliases), encoding="utf-8")
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": matches}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls", return_value=urls), \
                 patch("outputs.tai_cai_tracker.cli.collect_league_xg_batch", return_value=batch) as collect_batch, \
                 patch(
                     "outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle",
                     return_value={"quality_gate": {"status": "observation_only", "reasons": ["test"]}},
                 ) as collect_bundle:
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    source_alias_registry_path=alias_path,
                    source_link_registry_path=registry_path,
                )

        collect_batch.assert_called_once_with(
            "EPL",
            "2098",
            [
                {
                    "match_id": match["match_id"],
                    "home_team": aliases["aliases"][match["home_team"]]["source_name"],
                    "away_team": aliases["aliases"][match["away_team"]]["source_name"],
                    "kickoff_at": match["kickoff_at"],
                }
                for match in matches
            ],
        )
        self.assertEqual(summary["understat_batch_groups"], 1)
        self.assertEqual(collect_bundle.call_count, 2)
        self.assertEqual(
            collect_bundle.call_args_list[0].kwargs["understat_xg"]["provider"],
            "Understat",
        )

    def test_schedule_driven_freeze_passes_batch_xgscore_candidate_and_registry(self):
        match = {
            "match_id": 20990101001,
            "match_num": "周一001",
            "competition": "Test League",
            "home_team": "主队甲",
            "away_team": "客队甲",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "2",
        }
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            registry_path = Path(temporary_directory) / "source-links.json"
            ledger_path.write_text("", encoding="utf-8")
            registry_path.write_text(json.dumps({
                "version": 1,
                "matches": {},
                "fotmob_teams": {
                    "主队甲": {"team_name": "主队甲", "source_name": "Home One", "source_team_id": "1", "source_url": "https://www.fotmob.com/teams/1/overview/home-one", "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Home%20One", "score": 1.0},
                    "客队甲": {"team_name": "客队甲", "source_name": "Away One", "source_team_id": "2", "source_url": "https://www.fotmob.com/teams/2/overview/away-one", "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Away%20One", "score": 1.0},
                },
            }), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls", return_value={str(match["match_id"]): {"status": "unavailable"}}), \
                 patch("outputs.tai_cai_tracker.cli.discover_xgscore_event_urls", return_value={str(match["match_id"]): {"status": "candidate", "source_url": "https://xgscore.io/test-league/home-one-away-one/xgscore"}}) as discover_xgscore, \
                 patch("outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle", return_value={"quality_gate": {"status": "observation_only", "reasons": ["test"]}}) as collect_bundle:
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    source_link_registry_path=registry_path,
                )

        self.assertEqual(summary["xgscore_candidate_count"], 1)
        discover_xgscore.assert_called_once_with([{
            "match_id": str(match["match_id"]), "home_team": "Home One", "away_team": "Away One",
        }])
        self.assertEqual(collect_bundle.call_args.kwargs["xgscore_url"], "https://xgscore.io/test-league/home-one-away-one/xgscore")
        self.assertEqual(collect_bundle.call_args.kwargs["source_link_registry_path"], registry_path)

    def test_schedule_driven_freeze_passes_wikidata_xgscore_names_to_detail_collection(self):
        match = {
            "match_id": 20990101001,
            "match_num": "周一001",
            "competition": "Test League",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "2",
        }
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        xgscore_candidate = {
            str(match["match_id"]): {
                "status": "candidate",
                "source_url": "https://xgscore.io/test-league/inter-roma/xgscore",
                "xgscore_home_team": ["Inter Milan", "Inter"],
                "xgscore_away_team": ["Roma"],
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli._resolve_433_source_links", return_value=({}, {})), \
                 patch("outputs.tai_cai_tracker.cli._resolve_xgscore_source_links", return_value=(xgscore_candidate, {})), \
                 patch("outputs.tai_cai_tracker.cli._prefetch_understat_batch_xg", return_value=({}, 0, {})), \
                 patch("outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle", return_value={
                     "quality_gate": {"status": "observation_only", "reasons": ["test"]},
                 }) as collect_bundle:
                tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    freeze_window_minutes=0,
                )

        self.assertEqual(collect_bundle.call_args.kwargs["xgscore_url"], xgscore_candidate[str(match["match_id"])]["source_url"])
        self.assertEqual(collect_bundle.call_args.kwargs["xgscore_home_team"], ["Inter Milan", "Inter"])
        self.assertEqual(collect_bundle.call_args.kwargs["xgscore_away_team"], ["Roma"])

    def test_xgscore_batch_uses_wikidata_anchors_when_fotmob_identities_are_missing(self):
        match = {
            "match_id": 20990101001,
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2099-08-20T20:00:00+08:00",
        }
        anchors = {
            "国际米兰": {"english_name": "Inter Milan", "english_names": ["Inter Milan", "Inter"]},
            "罗马": {"english_name": "Roma", "english_names": ["Roma"]},
        }
        with patch("outputs.tai_cai_tracker.cli.cached_fotmob_team_identity", return_value=None), \
             patch("outputs.tai_cai_tracker.cli.cached_wikidata_team_identity", side_effect=lambda _path, team: anchors[team]), \
             patch("outputs.tai_cai_tracker.cli.discover_xgscore_event_urls", return_value={
                 str(match["match_id"]): {"status": "candidate", "source_url": "https://xgscore.io/serie-a/inter-roma/xgscore"},
             }) as discover:
            resolved, summary = tracker_cli._resolve_xgscore_source_links(
                [match], "work/source-links.json"
            )

        discover.assert_called_once_with([{
            "match_id": str(match["match_id"]),
            "home_team": "Inter Milan",
            "home_team_aliases": ["Inter Milan", "Inter"],
            "away_team": "Roma",
            "away_team_aliases": ["Roma"],
        }])
        self.assertEqual(resolved[str(match["match_id"])]["source_link_origin"], "fresh_discovery")
        self.assertEqual(resolved[str(match["match_id"])]["xgscore_home_team"], ["Inter Milan", "Inter"])
        self.assertEqual(resolved[str(match["match_id"])]["xgscore_away_team"], ["Roma"])
        self.assertEqual(summary["xgscore_wikidata_anchor_fixture_count"], 1)

    def test_understat_prefetch_uses_cached_wikidata_anchors_when_manual_aliases_are_missing(self):
        matches = [{
            "match_id": 20990101001,
            "competition": "意甲",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2099-08-20T20:00:00+08:00",
        }]
        anchors = {
            "国际米兰": {"english_name": "Inter Milan", "english_names": ["Inter Milan", "Inter"]},
            "罗马": {"english_name": "Roma", "english_names": ["Roma"]},
        }
        batch_fixture = {
            "match_id": 20990101001,
            "status": "available",
            "home_team": "Inter",
            "away_team": "Roma",
            "source_url": "https://understat.com/getLeagueData/Serie%20A/2099",
            "captured_at": "2099-08-20T10:00:00+08:00",
            "raw_content_sha256": "a" * 64,
            "xg_inputs": {"home_xg": "1.2", "away_xg": "0.9", "evidence": {
                "home_sample_matches": 2, "away_sample_matches": 2,
            }},
        }
        with patch("outputs.tai_cai_tracker.cli.cached_wikidata_team_identity", side_effect=lambda _path, team: anchors[team]), \
             patch("outputs.tai_cai_tracker.cli.collect_league_xg_batch", return_value={"fixtures": [batch_fixture]}) as collect_batch:
            prefetched, groups, summary = tracker_cli._prefetch_understat_batch_xg(
                matches,
                "work/no-aliases.json",
                "work/source-links.json",
            )

        collect_batch.assert_called_once_with("Serie_A", "2099", [{
            "match_id": 20990101001,
            "kickoff_at": "2099-08-20T20:00:00+08:00",
            "home_wikidata": anchors["国际米兰"],
            "away_wikidata": anchors["罗马"],
        }])
        self.assertEqual(groups, 1)
        self.assertEqual(summary["understat_wikidata_anchor_fixture_count"], 1)
        self.assertEqual(prefetched[str(matches[0]["match_id"])]["provider"], "Understat")

    def test_schedule_driven_freeze_appends_three_records_only_for_a_review_ready_bundle(self):
        match = {
            "match_id": 20990101001,
            "match_num": "\u5468\u4e00001",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle", return_value=_review_ready_bundle(match)):
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    freeze_window_minutes=0,
                )
            records = read_records(ledger_path)

        self.assertEqual(summary["frozen_count"], 1)
        self.assertEqual([record["record_type"] for record in records], [
            "odds_capture", "prediction", "all_market_snapshot",
        ])
        self.assertEqual(records[1]["match_id"], "\u5468\u4e00001")
        self.assertEqual(records[2]["prediction_hash"], records[1]["content_hash"])
        self.assertEqual(summary["items"][0]["freeze_level"], "five_market")
        self.assertEqual(summary["items"][0]["frozen_markets"], [
            "spf", "hhad", "crs", "ttg", "hafu",
        ])

    def test_schedule_driven_freeze_labels_a_partial_official_market_snapshot(self):
        match = {
            "match_id": 20990101002,
            "match_num": "\u5468\u4e00002",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101002",
        }
        bundle = _review_ready_bundle(match)
        for code in ("crs", "ttg", "hafu"):
            bundle["all_market_snapshot_input_template"]["official_markets"][code] = {
                "status": "skipped",
                "reason": "official_market_unavailable",
            }
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle", return_value=bundle):
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    freeze_window_minutes=0,
                )
            records = read_records(ledger_path)

        item = summary["items"][0]
        self.assertEqual(item["freeze_level"], "partial_markets")
        self.assertEqual(item["frozen_markets"], ["spf", "hhad"])
        self.assertEqual(item["skipped_markets"], {
            "crs": "official_market_unavailable",
            "ttg": "official_market_unavailable",
            "hafu": "official_market_unavailable",
        })
        self.assertEqual(records[2]["markets"]["hhad"]["status"], "frozen")
        self.assertEqual(records[2]["markets"]["crs"], {
            "status": "skipped",
            "reason": "official_market_unavailable",
        })

    def test_schedule_driven_freeze_leaves_observation_only_bundle_out_of_the_ledger(self):
        match = {
            "match_id": 20990101001,
            "match_num": "\u5468\u4e00001",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "2",
        }
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle", return_value={
                     "quality_gate": {"status": "observation_only", "reasons": ["starting_lineups_unconfirmed"]},
                     "readiness": {"missing": ["official hhad market"]},
                     "xg_source_cascade": [{
                         "provider": "Understat", "status": "unavailable",
                         "reason": "understat_batch_request_unavailable",
                     }],
                     "resolved_source_links": {"european_433_url": "https://www.433.cool/zqzhishu/data/1.html"},
                 }):
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    freeze_window_minutes=0,
                )
            contents = ledger_path.read_text(encoding="utf-8")

        self.assertEqual(summary["observation_count"], 1)
        self.assertEqual(summary["frozen_count"], 0)
        self.assertEqual(contents, "")
        diagnostics = summary["items"][0]["collection_diagnostics"]
        self.assertEqual(diagnostics["missing_inputs"], ["official hhad market"])
        self.assertEqual(diagnostics["xg_source_cascade"][0]["provider"], "Understat")
        self.assertEqual(diagnostics["resolved_source_links"]["european_433_url"], "https://www.433.cool/zqzhishu/data/1.html")

    def test_schedule_driven_freeze_freezes_a_market_baseline_when_xg_is_unavailable(self):
        match = {
            "match_id": 20990101001,
            "match_num": "周一001",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        baseline_bundle = {
            "generated_at": "2099-01-01T12:00:00+08:00",
            "quality_gate": {"status": "blocked", "reasons": ["xg_evidence_unavailable"]},
            "odds_import_input": {
                "provider": "433足球网",
                "captures": [{
                    "source_url": "https://www.433.cool/zqzhishu/data/1.html",
                    "bookmaker": "ExampleBook",
                    "captured_at": "2099-01-01T11:00:00+08:00",
                    "odds": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
                }],
            },
            "prediction_input_template": {
                "market": "SPF",
                "match_id": match["match_id"],
                "kickoff_at": match["kickoff_at"],
                "captured_at": "2099-01-01T12:00:00+08:00",
                "source_urls": {
                    "S1": match["official_source_url"],
                    "S2": "https://www.433.cool/zqzhishu/data/1.html",
                },
                "match_context": {
                    "competition": match["competition"],
                    "home_team": match["home_team"],
                    "away_team": match["away_team"],
                },
                "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
                "european_odds": {"capture_hash": "<fill_after_odds-import>"},
            },
        }
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle", return_value=baseline_bundle):
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    freeze_window_minutes=0,
                )
            records = read_records(ledger_path)

        self.assertEqual(summary["frozen_count"], 1)
        self.assertEqual(summary["items"][0]["freeze_level"], "market_baseline")
        self.assertEqual(summary["items"][0]["all_market_snapshot_status"], "not_frozen")
        self.assertEqual([record["record_type"] for record in records], ["odds_capture", "prediction"])
        self.assertNotIn("xg_inputs", records[1])

    def test_schedule_driven_freeze_freezes_an_isolated_official_market_baseline_without_xg(self):
        match = {
            "match_id": 20990101003,
            "match_num": "周一003",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T14:00:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101003",
        }
        baseline_bundle = _review_ready_bundle(match)
        baseline_bundle["quality_gate"] = {
            "status": "blocked", "reasons": ["xg_evidence_unavailable"],
        }
        baseline_bundle["prediction_input_template"].pop("xg_inputs")
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle", return_value=baseline_bundle):
                summary = tracker_cli.schedule_driven_freeze(
                    ledger_path,
                    min_lead_minutes=90,
                    max_lead_minutes=240,
                    now=now,
                    freeze_window_minutes=0,
                )
            records = read_records(ledger_path)

        self.assertEqual(summary["frozen_count"], 1)
        self.assertEqual(summary["items"][0]["freeze_level"], "official_market_five_market")
        self.assertEqual(summary["items"][0]["all_market_snapshot_status"], "frozen")
        self.assertEqual([record["record_type"] for record in records], [
            "odds_capture", "prediction", "all_market_snapshot",
        ])
        self.assertNotIn("xg_inputs", records[1])
        self.assertEqual(records[2]["schema_version"], 3)
        self.assertEqual(records[2]["snapshot_strategy"], "official_market_baseline")
        self.assertEqual(
            set(records[2]["markets"]), set(ALL_MARKET_OPTIONS),
        )

    def test_schedule_driven_freeze_command_forwards_window_worker_and_sales_policy_options(self):
        with patch("outputs.tai_cai_tracker.cli.schedule_driven_freeze", return_value={"frozen_count": 0}) as freeze, \
             patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "schedule-driven-freeze",
                "ledger.jsonl",
                "--min-lead-minutes", "75",
                "--max-lead-minutes", "210",
                "--max-workers", "3",
                "--sales-cutoff-policy", "work/custom-sales-policy.json",
            ])

        self.assertEqual(result, 0)
        freeze.assert_called_once_with(
            "ledger.jsonl",
            min_lead_minutes=75,
            max_lead_minutes=210,
            max_workers=3,
            sales_cutoff_policy_path="work/custom-sales-policy.json",
            include_open_matches=False,
            freeze_window_minutes=0,
        )
        self.assertEqual(json.loads(output.getvalue()), {"frozen_count": 0})

    def test_schedule_driven_freeze_command_forwards_include_open_matches(self):
        with patch("outputs.tai_cai_tracker.cli.schedule_driven_freeze", return_value={"frozen_count": 0}) as freeze, \
             patch("sys.stdout", io.StringIO()):
            result = tracker_cli.main([
                "schedule-driven-freeze",
                "ledger.jsonl",
                "--include-open-matches",
            ])

        self.assertEqual(result, 0)
        self.assertTrue(freeze.call_args.kwargs["include_open_matches"])

    def test_identity_preflight_does_not_queue_optional_understat_alias_gaps(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        unresolved = {
            "match_id": 20990101011, "match_num": "周一011", "competition": "英超",
            "home_team": "主队甲", "away_team": "客队甲",
            "kickoff_at": "2099-01-01T14:30:00+08:00", "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101011",
        }
        resolved = {
            "match_id": 20990101012, "match_num": "周一012", "competition": "英超",
            "home_team": "主队乙", "away_team": "客队乙",
            "kickoff_at": "2099-01-01T14:45:00+08:00", "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101012",
        }
        aliases = {
            "version": 1,
            "aliases": {
                "主队甲": {"source_name": "Home One", "source_team_id": 1, "source_provider": "Understat"},
                "主队乙": {"source_name": "Home Two", "source_team_id": 2, "source_provider": "Understat"},
                "客队乙": {"source_name": "Away Two", "source_team_id": 3, "source_provider": "Understat"},
            },
        }
        team_ids = {"Home One": "11", "客队甲": "12", "Home Two": "13", "Away Two": "14"}
        def fotmob_candidate(team_name):
            team_id = team_ids[team_name]
            return {
                "team_id": team_id,
                "team_name": team_name,
                "source_url": f"https://www.fotmob.com/teams/{team_id}/overview/{team_id}",
                "search_url": f"https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term={team_name}",
                "score": 0.95,
            }
        with tempfile.TemporaryDirectory() as temporary_directory:
            queue_path = Path(temporary_directory) / "identity-review-queue.json"
            alias_path = Path(temporary_directory) / "aliases.json"
            registry_path = Path(temporary_directory) / "source-links.json"
            alias_path.write_text(json.dumps(aliases), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [unresolved, resolved]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls", return_value={
                     str(unresolved["match_id"]): {"status": "resolved", "source_url": "https://www.433.cool/zqzhishu/data/11.html"},
                     str(resolved["match_id"]): {"status": "resolved", "source_url": "https://www.433.cool/zqzhishu/data/12.html"},
                 }), \
                 patch("outputs.tai_cai_tracker.cli.discover_team_stats_url", side_effect=fotmob_candidate):
                summary = tracker_cli.identity_preflight(
                    queue_path,
                    now=now,
                    source_alias_registry_path=alias_path,
                    source_link_registry_path=registry_path,
                )
            queue = json.loads(queue_path.read_text(encoding="utf-8"))

        self.assertEqual(summary["review_count"], 0)
        self.assertEqual(summary["eligible_count"], 2)
        self.assertEqual(queue["items"], [])
        self.assertEqual(summary["fotmob_resolved_team_count"], 4)

    def test_identity_preflight_persists_each_strictly_resolved_433_link(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        match = {
            "match_id": 20990101022,
            "match_num": "周一022",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T14:30:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101022",
        }
        source_url = "https://www.433.cool/zqzhishu/data/22.html"
        with tempfile.TemporaryDirectory() as temporary_directory:
            queue_path = Path(temporary_directory) / "identity-review-queue.json"
            registry_path = Path(temporary_directory) / "source-links.json"
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls", return_value={
                     str(match["match_id"]): {"status": "resolved", "source_url": source_url},
                 }), \
                 patch("outputs.tai_cai_tracker.cli.discover_team_stats_url", side_effect=[
                     {"team_id": "22", "team_name": "Home FC", "source_url": "https://www.fotmob.com/teams/22/overview/home", "search_url": "https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term=Home", "score": 0.95},
                     {"team_id": "23", "team_name": "Away FC", "source_url": "https://www.fotmob.com/teams/23/overview/away", "search_url": "https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term=Away", "score": 0.95},
                 ]):
                summary = tracker_cli.identity_preflight(
                    queue_path,
                    now=now,
                    source_link_registry_path=registry_path,
                )
            cached = tracker_cli.cached_source_links(registry_path, match)

        self.assertEqual(summary["source_link_fresh_discovery_count"], 1)
        self.assertEqual(cached["european_433_url"], source_url)

    def test_identity_preflight_prewarm_resolves_missing_fotmob_team_links(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        match = {
            "match_id": 20990101023,
            "match_num": "周一023",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T14:30:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990101023",
        }
        def fotmob_candidate(team_name):
            team_id = "23" if team_name == "Home FC" else "24"
            return {
                "team_id": team_id,
                "team_name": team_name,
                "source_url": f"https://www.fotmob.com/teams/{team_id}/overview/{team_id}",
                "search_url": f"https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term={team_name}",
                "score": 0.95,
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            queue_path = Path(temporary_directory) / "identity-review-queue.json"
            registry_path = Path(temporary_directory) / "source-links.json"
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls", return_value={
                     str(match["match_id"]): {"status": "resolved", "source_url": "https://www.433.cool/zqzhishu/data/23.html"},
                 }), \
                 patch("outputs.tai_cai_tracker.cli.discover_team_stats_url", side_effect=fotmob_candidate):
                summary = tracker_cli.identity_preflight(
                    queue_path,
                    now=now,
                    source_link_registry_path=registry_path,
                )
            cached = tracker_cli.cached_source_links(registry_path, match)

        self.assertEqual(summary["review_count"], 0)
        self.assertEqual(summary["fotmob_resolved_team_count"], 2)
        self.assertEqual(cached["home_fotmob_url"], "https://www.fotmob.com/teams/23/overview/23")
        self.assertEqual(cached["away_fotmob_url"], "https://www.fotmob.com/teams/24/overview/24")

    def test_identity_preflight_advances_for_late_sales_cutoff_fixture(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-05T19:30:00+08:00")
        match = {
            "match_id": 20990105011,
            "match_num": "周一011",
            "competition": "英超",
            "home_team": "主队甲",
            "away_team": "客队甲",
            "kickoff_at": "2099-01-05T23:55:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990105011",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            queue_path = Path(temporary_directory) / "identity-review-queue.json"
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls", return_value={
                     str(match["match_id"]): {"status": "unavailable", "reason": "not found"},
                 }), \
                 patch("outputs.tai_cai_tracker.cli.discover_team_stats_url", side_effect=ValueError("not found")):
                summary = tracker_cli.identity_preflight(queue_path, now=now)
            queue = json.loads(queue_path.read_text(encoding="utf-8"))

        self.assertEqual(summary["eligible_count"], 1)
        self.assertEqual(summary["review_count"], 1)
        self.assertEqual(queue["items"][0]["timing_basis"], "sales_cutoff_window")
        self.assertEqual(queue["items"][0]["sales_cutoff_at"], "2099-01-05T22:00:00+08:00")

    def test_identity_preflight_uses_an_explicit_cutoff_lead_window(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-05T19:55:00+08:00")
        match = {
            "match_id": 20990105012,
            "match_num": "周一012",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-05T23:55:00+08:00",
            "status": "2",
            "official_source_url": "https://official.example.test/matches/20990105012",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            queue_path = Path(temporary_directory) / "identity-review-queue.json"
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls", return_value={
                     str(match["match_id"]): {"status": "unavailable", "reason": "not found"},
                 }), \
                 patch("outputs.tai_cai_tracker.cli.discover_team_stats_url", side_effect=ValueError("not found")):
                summary = tracker_cli.identity_preflight(queue_path, now=now)

        self.assertEqual(summary["eligible_count"], 1)
        self.assertEqual(summary["review_count"], 1)

    def test_identity_preflight_command_forwards_window_and_sales_policy_options(self):
        with patch("outputs.tai_cai_tracker.cli.identity_preflight", return_value={"review_count": 0}) as preflight, \
             patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "identity-preflight",
                "queue.json",
                "--min-lead-minutes", "120",
                "--max-lead-minutes", "180",
                "--sales-cutoff-policy", "work/custom-sales-policy.json",
            ])

        self.assertEqual(result, 0)
        preflight.assert_called_once_with(
            "queue.json",
            min_lead_minutes=120,
            max_lead_minutes=180,
            sales_cutoff_policy_path="work/custom-sales-policy.json",
        )
        self.assertEqual(json.loads(output.getvalue()), {"review_count": 0})

    def test_identity_preflight_reuses_a_verified_cached_433_link(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        match = {
            "match_id": 20990101013, "match_num": "周一013", "competition": "意大利杯",
            "home_team": "主队", "away_team": "客队",
            "kickoff_at": "2099-01-01T14:30:00+08:00", "status": "2",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            queue_path = Path(temporary_directory) / "identity-review-queue.json"
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls", return_value={
                     str(match["match_id"]): {"status": "unavailable", "reason": "not found"},
                 }), \
                 patch("outputs.tai_cai_tracker.cli.cached_source_links", return_value={
                    "european_433_url": "https://www.433.cool/zqzhishu/data/13.html",
                    "home_fotmob_url": "https://www.fotmob.com/teams/13/overview/home",
                    "away_fotmob_url": "https://www.fotmob.com/teams/14/overview/away",
                 }):
                summary = tracker_cli.identity_preflight(queue_path, now=now)

        self.assertEqual(summary["review_count"], 0)

    def test_identity_coverage_report_counts_cached_links_aliases_and_operational_blocks(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        ready = {
            "match_id": 20990101031, "match_num": "周一031", "competition": "英超",
            "home_team": "主队甲", "away_team": "客队甲",
            "kickoff_at": "2099-01-01T14:30:00+08:00", "status": "2",
        }
        sale_pending = {
            "match_id": 20990101032, "match_num": "周一032", "competition": "英超",
            "home_team": "主队乙", "away_team": "客队乙",
            "kickoff_at": "2099-01-01T14:45:00+08:00", "status": "1",
        }
        not_covered = {
            "match_id": 20990101033, "match_num": "周一033", "competition": "测试杯",
            "home_team": "主队丙", "away_team": "客队丙",
            "kickoff_at": "2099-01-01T14:45:00+08:00", "status": "2",
        }
        aliases = {
            "version": 1,
            "aliases": {
                "主队甲": {"source_name": "Home One", "source_team_id": 1, "source_provider": "Understat"},
                "客队甲": {"source_name": "Away One", "source_team_id": 2, "source_provider": "Understat"},
                "主队乙": {"source_name": "Home Two", "source_team_id": 3, "source_provider": "Understat"},
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            alias_path = Path(temporary_directory) / "aliases.json"
            registry_path = Path(temporary_directory) / "source-links.json"
            alias_path.write_text(json.dumps(aliases), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [ready, sale_pending, not_covered]}), \
                 patch("outputs.tai_cai_tracker.cli.cached_source_links", side_effect=[
                     {"european_433_url": "https://www.433.cool/zqzhishu/data/31.html", "home_fotmob_url": "https://www.fotmob.com/teams/31/overview/home", "away_fotmob_url": "https://www.fotmob.com/teams/32/overview/away"},
                     {"european_433_url": "https://www.433.cool/zqzhishu/data/32.html", "home_fotmob_url": "https://www.fotmob.com/teams/33/overview/home"},
                     {},
                 ]) as cached, \
                 patch("outputs.tai_cai_tracker.cli.discover_433_detail_urls") as discover:
                report = tracker_cli.identity_coverage_report(
                    now=now,
                    source_alias_registry_path=alias_path,
                    source_link_registry_path=registry_path,
                )

        premier_league = report["competitions"]["英超"]
        self.assertEqual(report["scanned_count"], 3)
        self.assertEqual(premier_league["scheduled_count"], 2)
        self.assertEqual(premier_league["european_433_cached_count"], 2)
        self.assertEqual(premier_league["understat_both_alias_count"], 1)
        self.assertEqual(premier_league["fotmob_both_link_count"], 1)
        self.assertEqual(premier_league["freeze_window_eligible_count"], 1)
        self.assertEqual(premier_league["block_reason_counts"]["official_sale_pending"], 1)
        self.assertEqual(report["competitions"]["测试杯"]["understat_status"], "not_covered")
        self.assertEqual(cached.call_count, 3)
        discover.assert_not_called()

    def test_identity_coverage_keeps_optional_understat_alias_gap_out_of_required_identity_blockers(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        match = {
            "match_id": 20990101034, "match_num": "周一034", "competition": "英超",
            "home_team": "主队丁", "away_team": "客队丁",
            "kickoff_at": "2099-01-01T14:30:00+08:00", "status": "2",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            alias_path = Path(temporary_directory) / "aliases.json"
            registry_path = Path(temporary_directory) / "source-links.json"
            alias_path.write_text(json.dumps({"version": 1, "aliases": {}}), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.cached_source_links", return_value={
                     "european_433_url": "https://www.433.cool/zqzhishu/data/34.html",
                     "home_fotmob_url": "https://www.fotmob.com/teams/34/overview/home",
                     "away_fotmob_url": "https://www.fotmob.com/teams/35/overview/away",
                 }):
                report = tracker_cli.identity_coverage_report(
                    now=now,
                    source_alias_registry_path=alias_path,
                    source_link_registry_path=registry_path,
                )

        item = report["items"][0]
        summary = report["competitions"]["英超"]
        self.assertEqual(item["required_identity_status"], "ready")
        self.assertEqual(item["required_identity_blockers"], [])
        self.assertEqual(item["optional_xg_enhancements"], [
            "understat_home_alias_unresolved",
            "understat_away_alias_unresolved",
        ])
        self.assertEqual(summary["identity_ready_count"], 1)
        self.assertNotIn("understat_home_alias_unresolved", summary["block_reason_counts"])
        self.assertEqual(summary["optional_xg_gap_counts"]["understat_home_alias_unresolved"], 1)

    def test_identity_coverage_report_command_forwards_window_and_sales_policy_options(self):
        with patch("outputs.tai_cai_tracker.cli.identity_coverage_report", return_value={"scanned_count": 0}) as coverage, \
             patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "identity-coverage-report",
                "--min-lead-minutes", "75",
                "--max-lead-minutes", "210",
                "--sales-cutoff-policy", "work/custom-sales-policy.json",
            ])

        self.assertEqual(result, 0)
        coverage.assert_called_once_with(
            min_lead_minutes=75,
            max_lead_minutes=210,
            sales_cutoff_policy_path="work/custom-sales-policy.json",
        )
        self.assertEqual(json.loads(output.getvalue()), {"scanned_count": 0})

    def test_cli_configures_utf8_stdout_without_breaking_stringio(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            tracker_cli._configure_utf8_stdout()
            print("英超")

        self.assertEqual(output.getvalue(), "英超\n")

    def test_cli_configures_utf8_stderr_for_chinese_error_messages(self):
        class ReconfigurableErrorStream:
            encoding = "gbk"

            def reconfigure(self, *, encoding):
                self.encoding = encoding

        error_stream = ReconfigurableErrorStream()
        with patch("sys.stderr", error_stream):
            tracker_cli._configure_utf8_stdout()

        self.assertEqual(error_stream.encoding, "utf-8")

    def test_daily_freeze_status_includes_same_day_blocked_scan_not_in_ledger(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            log_path = Path(temporary_directory) / "scheduler.jsonl"
            log_path.write_text(json.dumps({
                "timestamp": "2099-01-01T19:50:00+08:00",
                "mode": "prematch",
                "status": "completed",
                "output": [json.dumps({
                    "items": [{
                        "match_id": 20990101011,
                        "match_num": "周一011",
                        "status": "blocked",
                        "reasons": ["xg_evidence_unavailable"],
                    }],
                })],
            }) + "\n", encoding="utf-8")

            summary = tracker_cli.daily_freeze_status(
                ledger_path,
                log_path,
                date="2099-01-01",
            )

        self.assertEqual(summary["frozen_count"], 0)
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(summary["not_frozen_count"], 1)
        self.assertEqual(summary["not_frozen"][0]["match_id"], 20990101011)
        self.assertEqual(summary["not_frozen"][0]["reasons"], ["xg_evidence_unavailable"])
        self.assertEqual(summary["not_frozen"][0]["attempted_at"], "2099-01-01T19:50:00+08:00")

    def test_daily_freeze_status_groups_prior_day_baseline_by_kickoff_and_exposes_all_markets(self):
        prediction = freeze_prediction({
            "market": "SPF",
            "match_id": "周日001",
            "kickoff_at": "2099-01-02T17:00:00+08:00",
            "captured_at": "2099-01-01T22:20:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/1",
                "S2": "https://odds.example.test/matches/1",
            },
            "match_context": {
                "competition": "Test League",
                "home_team": "Home FC",
                "away_team": "Away FC",
            },
            "outcomes": [
                {"label": "home_win", "probability": Decimal("0.50")},
                {"label": "draw", "probability": Decimal("0.30")},
                {"label": "away_win", "probability": Decimal("0.20")},
            ],
        })
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            log_path = Path(temporary_directory) / "scheduler.jsonl"
            ledger_path.write_text(json.dumps(prediction, default=str) + "\n", encoding="utf-8")
            log_path.write_text("", encoding="utf-8")
            summary = tracker_cli.daily_freeze_status(
                ledger_path,
                log_path,
                date="2099-01-02",
            )

        self.assertEqual(summary["frozen_count"], 1)
        item = summary["frozen"][0]
        self.assertEqual(item["freeze_level"], "market_baseline")
        self.assertEqual(item["markets"]["spf"]["status"], "frozen")
        self.assertEqual(item["markets"]["spf"]["selected_option"], "home_win")
        self.assertEqual(item["markets"]["hhad"], {
            "status": "not_frozen",
            "reason": "full_market_snapshot_not_frozen",
        })
        self.assertEqual(item["markets"]["crs"]["status"], "not_frozen")
        self.assertEqual(item["markets"]["ttg"]["status"], "not_frozen")
        self.assertEqual(item["markets"]["hafu"]["status"], "not_frozen")

    def test_daily_freeze_status_reopens_a_prediction_after_settlement_invalidation(self):
        prediction = freeze_prediction({
            "market": "SPF",
            "match_id": "周二009",
            "kickoff_at": "2099-01-02T03:00:00+08:00",
            "captured_at": "2099-01-01T20:00:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/9",
                "S2": "https://odds.example.test/matches/9",
            },
            "match_context": {
                "competition": "Test League",
                "home_team": "Home FC",
                "away_team": "Away FC",
            },
            "outcomes": [
                {"label": "home_win", "probability": Decimal("0.50")},
                {"label": "draw", "probability": Decimal("0.30")},
                {"label": "away_win", "probability": Decimal("0.20")},
            ],
        })
        settlement = settle_prediction(
            prediction,
            "home_win",
            "https://official.example.test/results/old",
            final_score={"home": 1, "away": 0},
        )
        invalidation = invalidate_settlement(settlement, {
            "settlement_hash": settlement["content_hash"],
            "prediction_hash": prediction["content_hash"],
            "invalidated_at": "2099-01-02T10:00:00+08:00",
            "reason_code": "identity_mismatch",
            "reason": "official result belongs to a different fixture",
            "official_result_url": "https://official.example.test/results/old",
            "official_result_identity": {
                "match_id": "20990101009",
                "match_num": "周二009",
                "match_date": "2099-01-01",
                "home_team": "Other Home",
                "away_team": "Other Away",
            },
        })
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            log_path = Path(temporary_directory) / "scheduler.jsonl"
            ledger_path.write_text(
                "\n".join(
                    json.dumps(record, default=str)
                    for record in (prediction, settlement, invalidation)
                ) + "\n",
                encoding="utf-8",
            )
            log_path.write_text("", encoding="utf-8")

            summary = tracker_cli.daily_freeze_status(
                ledger_path, log_path, date="2099-01-02"
            )

        self.assertEqual(summary["frozen"][0]["status"], "pending")

    def test_daily_freeze_status_includes_sales_cutoff_skip(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            log_path = Path(temporary_directory) / "scheduler.jsonl"
            log_path.write_text(json.dumps({
                "timestamp": "2099-01-05T22:20:00+08:00",
                "mode": "prematch",
                "status": "completed",
                "output": [json.dumps({
                    "skipped_items": [{
                        "match_id": 20990105021,
                        "match_num": "周一021",
                        "status": "skipped",
                        "official_status": "3",
                        "reasons": ["official_sales_suspended"],
                        "timing_basis": "sales_cutoff_window",
                        "sales_cutoff_at": "2099-01-05T22:00:00+08:00",
                    }],
                })],
            }) + "\n", encoding="utf-8")

            summary = tracker_cli.daily_freeze_status(ledger_path, log_path, date="2099-01-05")

        self.assertEqual(summary["not_frozen_count"], 1)
        self.assertEqual(summary["not_frozen"][0]["reasons"], ["official_sales_suspended"])
        self.assertEqual(summary["not_frozen"][0]["timing_basis"], "sales_cutoff_window")
        self.assertEqual(summary["not_frozen"][0]["sales_cutoff_at"], "2099-01-05T22:00:00+08:00")
        self.assertEqual(summary["not_frozen"][0]["official_status"], "3")

    def test_daily_freeze_status_command_forwards_paths_and_date(self):
        with patch("outputs.tai_cai_tracker.cli.daily_freeze_status", return_value={"frozen_count": 0}) as status, \
             patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "daily-freeze-status",
                "ledger.jsonl",
                "scheduler.jsonl",
                "--date", "2099-01-01",
            ])

        self.assertEqual(result, 0)
        status.assert_called_once_with("ledger.jsonl", "scheduler.jsonl", date="2099-01-01")
        self.assertEqual(json.loads(output.getvalue()), {"frozen_count": 0})

    def test_source_access_check_command_is_read_only_and_prints_the_report(self):
        report = {
            "proxy": {"configured": False},
            "sources": {"sofascore": {"status": "network_error", "url": "https://www.sofascore.com/api"}},
        }
        with patch("outputs.tai_cai_tracker.cli.source_access_report", return_value=report) as check, \
             patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main(["source-access-check"])

        self.assertEqual(result, 0)
        check.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_understat_alias_import_command_forwards_input_and_registry_paths(self):
        with patch("outputs.tai_cai_tracker.cli.import_manual_understat_aliases", return_value={"aliases": {"莱切": {}}}) as import_aliases, \
             patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main(["understat-alias-import", "aliases.json", "registry.json"])

        self.assertEqual(result, 0)
        import_aliases.assert_called_once_with("registry.json", "aliases.json")
        self.assertEqual(json.loads(output.getvalue()), {"aliases": {"莱切": {}}})

    def test_rapid_freeze_collects_once_and_appends_three_linked_records(self):
        match = {
            "match_id": 20990101001,
            "match_num": "周一001",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        prizes = {
            code: {option: str(Decimal(index + 2)) for index, option in enumerate(options)}
            for code, options in ALL_MARKET_OPTIONS.items()
        }
        official = {
            "official_source_url": match["official_source_url"],
            "official_markets": {
                code: {
                    "status": "frozen",
                    "published_at": "2099-01-01T10:00:00+08:00",
                    "official_source_url": match["official_source_url"],
                    "fixed_prizes": prizes[code],
                    **({"home_handicap": "-1"} if code == "hhad" else {}),
                }
                for code in ALL_MARKET_OPTIONS
            },
        }
        xg = {
            "provider": "FotMob",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "xg_inputs": {
                "source_url": "https://www.fotmob.com/teams/1/overview/home-fc",
                "home_xg": "1.2",
                "away_xg": "0.8",
                "model_version": "poisson-v1",
                "model_weight": "0.40",
                "confidence_ceiling": "medium",
                "evidence": {
                    "source_url": "https://www.fotmob.com/teams/1/overview/home-fc",
                    "published_at": "2099-01-01T09:00:00+08:00",
                    "competition": "Test League",
                    "season": "2099",
                    "sample_matches": 10,
                    "scope": "league_total",
                    "home_attack_xg": "12",
                    "home_defense_xga": "8",
                    "away_attack_xg": "8",
                    "away_defense_xga": "12",
                    "formula": "test",
                },
            },
        }
        manifest = {
            "match_id": 20990101001,
            "captured_at": "2099-01-01T12:00:00+08:00",
            "home_fotmob_url": "https://www.fotmob.com/teams/1/overview/home-fc",
            "away_fotmob_url": "https://www.fotmob.com/teams/2/overview/away-fc",
            "odds_capture": {
                "provider": "OddsPortal",
                "source_url": "https://www.oddsportal.com/football/h2h/home/away/#event",
                "bookmaker": "ExampleBook",
                "captured_at": "2099-01-01T11:00:00+08:00",
                "odds": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            manifest_path = temporary_path / "rapid.json"
            ledger_path = temporary_path / "ledger.jsonl"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}) as schedule, \
                 patch("outputs.tai_cai_tracker.cli.collect_fixed_bonus", return_value=official), \
                 patch("outputs.tai_cai_tracker.cli.collect_match_xg_evidence", return_value=xg), \
                 patch("sys.stdout", io.StringIO()) as output:
                result = tracker_cli.main(["rapid-freeze", str(manifest_path), str(ledger_path)])

            self.assertEqual(result, 0)
            self.assertEqual(schedule.call_count, 1)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["status"], "frozen")
            self.assertEqual(summary["match_id"], "周一001")
            self.assertEqual(len(read_records(ledger_path)), 3)

    def test_rapid_freeze_batch_fetches_schedule_once_and_keeps_blocked_jobs_out_of_ledger(self):
        first = {
            "match_id": 20990101001,
            "match_num": "周一001",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        second = {**first, "match_id": 20990101002, "match_num": "周一002"}
        job = {
            "captured_at": "2099-01-01T12:00:00+08:00",
            "home_fotmob_url": "https://www.fotmob.com/teams/1/overview/home-fc",
            "away_fotmob_url": "https://www.fotmob.com/teams/2/overview/away-fc",
            "odds_capture": {
                "provider": "OddsPortal", "source_url": "https://www.oddsportal.com/football/h2h/home/away/#event",
                "bookmaker": "ExampleBook", "captured_at": "2099-01-01T11:00:00+08:00",
                "odds": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
            },
        }
        manifest = {"jobs": [{**job, "match_id": first["match_id"]}, {**job, "match_id": second["match_id"]}]}

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            manifest_path = temporary_path / "rapid-batch.json"
            ledger_path = temporary_path / "ledger.jsonl"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [first, second]}) as schedule, \
                 patch("outputs.tai_cai_tracker.cli._rapid_stage", side_effect=[
                     {"records": [{"record_type": "odds_capture"}, {"record_type": "prediction", "match_id": "周一001", "prediction_role": "primary"}, {"record_type": "all_market_snapshot"}], "summary": {"status": "frozen", "match_id": "周一001"}},
                     ValueError("traceable xG evidence is unavailable"),
                 ]), \
                 patch("outputs.tai_cai_tracker.cli._append_record") as append:
                result = tracker_cli.rapid_freeze_batch(manifest_path, ledger_path, max_workers=2)

        self.assertEqual(schedule.call_count, 1)
        self.assertEqual(result["frozen_count"], 1)
        self.assertEqual(result["blocked_count"], 1)
        self.assertEqual(append.call_count, 3)

    def test_rapid_freeze_keeps_low_sample_league_total_out_of_ledger(self):
        match = {
            "match_id": 20990101001,
            "match_num": "周一001",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        bundle = _review_ready_bundle(match)
        official = {
            "official_source_url": match["official_source_url"],
            "official_markets": {
                code: {"status": "frozen", **market}
                for code, market in bundle["all_market_snapshot_input_template"]["official_markets"].items()
            },
        }
        xg = {
            "provider": "FotMob",
            "home_team": match["home_team"],
            "away_team": match["away_team"],
            "captured_at": "2099-01-01T10:02:00+08:00",
            "xg_inputs": {
                "source_url": "https://www.fotmob.com/teams/1/stats/home-fc/teams",
                "home_xg": "1.0", "away_xg": "1.0",
                "model_version": "poisson-v1", "model_weight": "0.40",
                "confidence_ceiling": "medium",
                "evidence": {
                    "source_url": "https://www.fotmob.com/teams/1/stats/home-fc/teams",
                    "published_at": "2099-01-01T09:00:00+08:00",
                    "competition": "Test League", "season": "2099",
                    "sample_matches": 7, "scope": "league_total",
                    "home_attack_xg": "7", "home_defense_xga": "7",
                    "away_attack_xg": "7", "away_defense_xga": "7", "formula": "test",
                },
            },
        }
        job = {
            "match_id": match["match_id"],
            "captured_at": "2099-01-01T12:00:00+08:00",
            "home_fotmob_url": "https://www.fotmob.com/teams/1/stats/home-fc/teams",
            "away_fotmob_url": "https://www.fotmob.com/teams/2/stats/away-fc/teams",
            "odds_capture": {
                "provider": "433", "source_url": "https://www.433.cool/zqzhishu/data/1.html",
                "bookmaker": "ExampleBook", "captured_at": "2099-01-01T11:00:00+08:00",
                "odds": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            manifest_path = temporary_path / "rapid.json"
            ledger_path = temporary_path / "ledger.jsonl"
            manifest_path.write_text(json.dumps(job), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.collect_fixed_bonus", return_value=official), \
                 patch("outputs.tai_cai_tracker.cli.collect_match_xg_evidence", return_value=xg):
                summary = tracker_cli.rapid_freeze(manifest_path, ledger_path)

            records = read_records(ledger_path) if ledger_path.exists() else []

        self.assertEqual(summary["status"], "observation_only")
        self.assertIn("xg_sample_below_minimum", summary["reasons"])
        self.assertEqual(records, [])

    def test_xg_evidence_accepts_an_explicit_missing_publication_time_reason(self):
        prediction = prediction_from_input(json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8")))
        prediction["xg_inputs"] = {
            "source_url": "https://understat.com/getLeagueData/Serie%20A/2026",
            "home_xg": "1.10",
            "away_xg": "0.90",
            "model_version": "poisson-v1",
            "model_weight": "0.40",
            "confidence_ceiling": "medium",
            "evidence": {
                "source_url": "https://understat.com/getLeagueData/Serie%20A/2026",
                "captured_at": "2099-01-01T10:02:00+08:00",
                "published_at_unavailable_reason": "Understat response does not expose publication time",
                "competition": "Serie A",
                "season": "2026/2027",
                "sample_matches": 10,
                "scope": "league_total",
                "home_attack_xg": "11",
                "home_defense_xga": "9",
                "away_attack_xg": "10",
                "away_defense_xga": "8",
                "formula": "test",
            },
        }

        record = freeze_prediction(prediction)

        self.assertEqual(
            record["xg_inputs"]["evidence"]["published_at_unavailable_reason"],
            "Understat response does not expose publication time",
        )

    def test_xg_evidence_accepts_traceable_medium_confidence_season_bridge(self):
        prediction = json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8"))
        source = lambda season, raw_hash: {
            "source_url": f"https://understat.com/getLeagueData/EPL/{season}",
            "captured_at": "2099-01-01T10:02:00+08:00",
            "raw_content_sha256": raw_hash,
            "competition": "Premier League",
            "season": f"{season}/{int(season) + 1}",
            "home_team_id": "1", "home_team_name": "Home FC", "home_sample_matches": 2 if season == "2099" else 12,
            "home_attack_xg": "4" if season == "2099" else "24", "home_defense_xga": "2" if season == "2099" else "12",
            "away_team_id": "2", "away_team_name": "Away FC", "away_sample_matches": 2 if season == "2099" else 12,
            "away_attack_xg": "3" if season == "2099" else "18", "away_defense_xga": "2.5" if season == "2099" else "15",
        }
        prediction["xg_inputs"] = {
            "source_url": "https://understat.com/getLeagueData/EPL/2099",
            "home_xg": "1.5", "away_xg": "1.25", "model_version": "poisson-v1", "model_weight": "0.40",
            "confidence_ceiling": "medium",
            "evidence": {
                "source_url": "https://understat.com/getLeagueData/EPL/2099",
                "captured_at": "2099-01-01T10:02:00+08:00",
                "published_at_unavailable_reason": "Understat league responses do not expose publication times",
                "competition": "Premier League", "season": "2099/2100",
                "home_sample_matches": 2, "away_sample_matches": 2, "scope": "home_away_split",
                "home_attack_xg": "2", "home_defense_xga": "1", "away_attack_xg": "1.5", "away_defense_xga": "1.25",
                "formula": "season_bridge test", "current_season": source("2099", "c" * 64),
                "prior_season": source("2098", "d" * 64), "current_weight": "0.1428571428571428571428571429", "prior_weight": "0.8571428571428571428571428571",
            },
        }

        capture = freeze_odds_capture({
            "provider": "Example provider",
            "match_id": prediction["match_id"],
            "kickoff_at": prediction["kickoff_at"],
            "captured_at": "2099-01-01T17:00:00+08:00",
            "source_url": prediction["source_urls"]["S2"],
            "bookmaker": "ExampleBook",
            "source_quality": "named_bookmaker",
            "odds": {"home_win": Decimal("2.00"), "draw": Decimal("3.00"), "away_win": Decimal("4.00")},
        })
        prediction["european_odds"] = {"capture_hash": capture["content_hash"]}
        record = freeze_prediction(prediction_from_input(prediction, [capture]))

        self.assertEqual(record["xg_inputs"]["confidence_ceiling"], "medium")
        self.assertEqual(record["xg_inputs"]["evidence"]["prior_season"]["raw_content_sha256"], "d" * 64)

    def test_xg_evidence_accepts_fotmob_multi_response_season_bridge(self):
        prediction = json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8"))
        stat_keys = {
            "home_expected_goals_team", "home_expected_goals_conceded_team",
            "away_expected_goals_team", "away_expected_goals_conceded_team",
        }

        def source(season, sample_matches, hash_char):
            return {
                "competition": "Championship", "season": season,
                "captured_at": "2099-01-01T10:02:00+08:00",
                "published_at": "2099-01-01T08:00:00+08:00",
                "source_urls": {key: f"https://data.fotmob.com/stats/{season}/{key}" for key in stat_keys},
                "raw_content_sha256": {key: hash_char * 64 for key in stat_keys},
                "home_team_id": "1", "home_team_name": "Home FC", "home_sample_matches": sample_matches,
                "home_attack_xg": "4", "home_defense_xga": "2",
                "away_team_id": "2", "away_team_name": "Away FC", "away_sample_matches": sample_matches,
                "away_attack_xg": "3", "away_defense_xga": "2.5",
            }

        prediction["xg_inputs"] = {
            "source_url": "https://data.fotmob.com/stats/current/expected_goals_team.json",
            "home_xg": "1.5", "away_xg": "1.25", "model_version": "poisson-v1", "model_weight": "0.40",
            "confidence_ceiling": "medium",
            "evidence": {
                "source_url": "https://data.fotmob.com/stats/current/expected_goals_team.json",
                "published_at": "2099-01-01T08:00:00+08:00",
                "competition": "Championship", "season": "2099/2100",
                "home_sample_matches": 2, "away_sample_matches": 2, "scope": "league_total",
                "home_attack_xg": "2", "home_defense_xga": "1", "away_attack_xg": "1.5", "away_defense_xga": "1.25",
                "formula": "league_total_season_bridge test", "current_season": source("2099/2100", 2, "c"),
                "prior_season": source("2098/2099", 12, "d"), "current_weight": "0.1428571428571428571428571429", "prior_weight": "0.8571428571428571428571428571",
            },
        }
        capture = freeze_odds_capture({
            "provider": "Example provider", "match_id": prediction["match_id"],
            "kickoff_at": prediction["kickoff_at"], "captured_at": "2099-01-01T17:00:00+08:00",
            "source_url": prediction["source_urls"]["S2"], "bookmaker": "ExampleBook",
            "source_quality": "named_bookmaker", "odds": {"home_win": Decimal("2.00"), "draw": Decimal("3.00"), "away_win": Decimal("4.00")},
        })
        prediction["european_odds"] = {"capture_hash": capture["content_hash"]}

        record = freeze_prediction(prediction_from_input(prediction, [capture]))

        self.assertEqual(set(record["xg_inputs"]["evidence"]["current_season"]["source_urls"]), stat_keys)
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "outputs.tai_cai_tracker.cli", *arguments],
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_snapshot_appends_one_frozen_prediction_from_hypothetical_example(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"

            result = self.run_cli("snapshot", str(EXAMPLE_INPUT), str(ledger_path))

            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["record_type"], "prediction")
            self.assertEqual(records[0]["status"], "frozen")
            self.assertEqual(records[0]["outcomes"][0]["probability"], "0.5")

    def test_evidence_record_appends_verified_observation(self):
        payload = {
            "record_type": "source_observation",
            "match_id": "20990101001",
            "evidence_kind": "european_1x2",
            "source_url": "https://odds.example.test/matches/20990101001",
            "captured_at": "2099-01-01T18:00:00+08:00",
            "published_at": "2099-01-01T17:59:00+08:00",
            "collector": "test-collector",
            "parser_version": "1.0",
            "raw_content_sha256": "b" * 64,
            "observed_facts": {"bookmaker": "ExampleBook", "odds": ["2.00", "3.00", "4.00"]},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            input_path = temporary_path / "evidence.json"
            ledger_path = temporary_path / "ledger.jsonl"
            input_path.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_cli("evidence-record", str(input_path), str(ledger_path))

            self.assertEqual(result.returncode, 0, result.stderr)
            records = read_records(ledger_path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["record_type"], "source_observation")
            self.assertEqual(records[0]["status"], "observed")

            tampered = json.loads(ledger_path.read_text(encoding="utf-8"))
            tampered["observed_facts"]["bookmaker"] = "TamperedBook"
            ledger_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_records(ledger_path)

    def test_match_preflight_uses_resolved_links_and_official_identity(self):
        summary = {
            "ready": True,
            "sporttery": {
                "match_id": 20990101001,
                "kickoff_at": "2099-01-01T20:00:00+08:00",
                "home_team": "Home FC",
                "away_team": "Away FC",
                "competition": "Test League",
            },
            "oddsportal_url": "https://odds.example.test/match",
            "home_fotmob_url": "https://stats.example.test/home",
            "away_fotmob_url": "https://stats.example.test/away",
        }
        output = io.StringIO()
        with patch("outputs.tai_cai_tracker.cli.link_summary", return_value=summary), \
             patch("outputs.tai_cai_tracker.cli.collect_pregame_bundle", return_value={"ledger_action": "observation_only"}) as collect, \
             patch("sys.stdout", output):
            self.assertEqual(tracker_cli.main(["match-preflight", "resolved.json"]), 0)

        collect.assert_called_once_with(
            20990101001,
            "https://odds.example.test/match",
            "https://stats.example.test/home",
            "https://stats.example.test/away",
            method="concern",
            model_weight="0.40",
            kickoff_at="2099-01-01T20:00:00+08:00",
            home_team="Home FC",
            away_team="Away FC",
            competition="Test League",
        )
        self.assertEqual(json.loads(output.getvalue())["ledger_action"], "observation_only")

    def test_auto_pregame_bundle_uses_automatic_fotmob_discovery(self):
        output = io.StringIO()
        with patch(
            "outputs.tai_cai_tracker.cli.collect_auto_pregame_bundle",
            return_value={"ledger_action": "observation_only"},
        ) as collect, patch("sys.stdout", output):
            self.assertEqual(
                tracker_cli.main([
                    "auto-pregame-bundle",
                    "20990101001",
                    "https://www.oddsportal.com/football/h2h/home/away/#event",
                ]),
                0,
            )

        collect.assert_called_once_with(
            "20990101001",
            "https://www.oddsportal.com/football/h2h/home/away/#event",
            method="concern",
            model_weight="0.40",
        )
        self.assertEqual(json.loads(output.getvalue())["ledger_action"], "observation_only")

    def test_fotmob_cache_warm_resolves_only_missing_sale_team_links(self):
        match = {
            "match_id": 20990101001,
            "match_num": "周一001",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-02T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
            "status": "2",
        }
        home_url = "https://www.fotmob.com/teams/1/overview/home-fc"
        away_url = "https://www.fotmob.com/teams/2/overview/away-fc"
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")

        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            tracker_cli.record_source_links(
                registry_path,
                match,
                {"home_fotmob_url": home_url},
            )
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch(
                     "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                     return_value={
                         "team_id": 2,
                         "team_name": "Away FC",
                         "source_url": away_url,
                         "score": 1.0,
                         "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Away%20FC",
                     },
                 ) as discover:
                summary = tracker_cli.prewarm_fotmob_source_links(
                    hours_ahead=48,
                    now=now,
                    source_link_registry_path=registry_path,
                )

            cached = tracker_cli.cached_source_links(registry_path, match)

        discover.assert_called_once_with("Away FC")
        self.assertEqual(cached, {"home_fotmob_url": home_url, "away_fotmob_url": away_url})
        self.assertEqual(summary["candidate_match_count"], 1)
        self.assertEqual(summary["cache_hit_team_count"], 1)
        self.assertEqual(summary["resolved_team_count"], 1)
        self.assertEqual(summary["unresolved_team_count"], 0)

    def test_fotmob_source_prewarm_command_forwards_horizon_workers_and_registry(self):
        with patch(
            "outputs.tai_cai_tracker.cli.prewarm_fotmob_source_links",
            return_value={"resolved_team_count": 0},
        ) as prewarm, patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "fotmob-source-prewarm",
                "--hours-ahead", "36",
                "--max-workers", "3",
                "--source-link-registry", "work/source-links.json",
                "--source-alias-registry", "work/aliases.json",
            ])

        self.assertEqual(result, 0)
        prewarm.assert_called_once_with(
            hours_ahead=36,
            max_workers=3,
            source_link_registry_path="work/source-links.json",
            source_alias_registry_path="work/aliases.json",
            upgrade_legacy=False,
        )
        self.assertEqual(json.loads(output.getvalue()), {"resolved_team_count": 0})

    def test_wikidata_source_prewarm_caches_each_open_team_once(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        matches = [
            {
                "match_id": 20990101001, "home_team": "国际米兰", "away_team": "罗马",
                "kickoff_at": "2099-01-01T14:00:00+08:00", "status": "2",
            },
            {
                "match_id": 20990101002, "home_team": "国际米兰", "away_team": "那不勒斯",
                "kickoff_at": "2099-01-01T15:00:00+08:00", "status": "2",
            },
        ]

        def identity(team_name, captured_at=None):
            qids = {"国际米兰": "Q631", "罗马": "Q220", "那不勒斯": "Q274"}
            return {
                "status": "resolved", "official_team": team_name, "qid": qids[team_name],
                "chinese_name": team_name, "english_name": {"国际米兰": "Inter Milan", "罗马": "Roma", "那不勒斯": "Napoli"}[team_name],
                "search_url": f"https://www.wikidata.org/w/api.php?action=wbsearchentities&search={team_name}&language=zh&format=json&limit=10",
                "source_url": f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={qids[team_name]}&props=labels%7Caliases&languages=zh%7Cen&format=json",
                "captured_at": captured_at,
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": matches}), \
                 patch("outputs.tai_cai_tracker.cli.resolve_team_identity", side_effect=identity) as resolve:
                summary = tracker_cli.prewarm_wikidata_source_identities(
                    hours_ahead=4,
                    now=now,
                    source_link_registry_path=registry_path,
                )
            cached = cached_wikidata_team_identity(registry_path, "国际米兰")

        self.assertEqual(resolve.call_count, 3)
        self.assertEqual(summary["resolved_team_count"], 3)
        self.assertEqual(summary["cache_hit_team_count"], 0)
        self.assertEqual(cached["english_name"], "Inter Milan")

    def test_wikidata_source_prewarm_stops_after_a_network_probe_failure(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        matches = [{
            "match_id": 20990101001, "home_team": "主队甲", "away_team": "客队甲",
            "kickoff_at": "2099-01-01T14:00:00+08:00", "status": "2",
        }]
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": matches}), \
                 patch("outputs.tai_cai_tracker.cli.resolve_team_identity", side_effect=OSError("TLS timeout")) as resolve:
                summary = tracker_cli.prewarm_wikidata_source_identities(
                    hours_ahead=4, now=now, source_link_registry_path=registry_path,
                )

        resolve.assert_called_once_with("主队甲", captured_at=now.isoformat())
        self.assertEqual(summary["network_failure_count"], 1)
        self.assertEqual(summary["skipped_by_reason"]["wikidata_network_unavailable_this_run"], 1)
        self.assertEqual(summary["items"][0]["failure_category"], "network_error")

    def test_wikidata_source_prewarm_stops_after_a_rate_limit_response(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        matches = [{
            "match_id": 20990101001, "home_team": "主队甲", "away_team": "客队甲",
            "kickoff_at": "2099-01-01T14:00:00+08:00", "status": "2",
        }]
        rate_limit = HTTPError("https://www.wikidata.org/w/api.php", 429, "Too Many Requests", None, None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": matches}), \
                 patch("outputs.tai_cai_tracker.cli.resolve_team_identity", side_effect=rate_limit) as resolve:
                summary = tracker_cli.prewarm_wikidata_source_identities(
                    hours_ahead=4, now=now, source_link_registry_path=registry_path,
                )

        resolve.assert_called_once_with("主队甲", captured_at=now.isoformat())
        self.assertEqual(summary["rate_limit_failure_count"], 1)
        self.assertEqual(summary["skipped_by_reason"]["wikidata_access_restricted_this_run"], 1)
        self.assertEqual(summary["items"][0]["failure_category"], "access_restricted")

    def test_daily_fotmob_identity_map_reuses_unexpired_wikidata_network_cooldown(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            daily_progress_path = temporary_path / "daily.json"
            daily_progress_path.write_text(json.dumps({
                "version": 1, "days_back": 3, "range_start": "2098-12-31", "range_end": "2099-01-02",
                "last_completed_date": "2098-12-31", "next_resume_date": None, "attempted_teams": {},
                "wikidata_network_cooldowns": {"主队甲": "2099-01-03T18:00:00+08:00"},
            }), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.prewarm_wikidata_source_identities", return_value={"items": []}) as wikidata, \
                 patch("outputs.tai_cai_tracker.cli.prewarm_fotmob_source_links", return_value={}) , \
                 patch("outputs.tai_cai_tracker.cli.historical_fotmob_source_prewarm", return_value={}):
                tracker_cli.daily_fotmob_identity_map(
                    now=now, historical_progress_path=temporary_path / "historical.json",
                    daily_progress_path=daily_progress_path,
                )

        self.assertEqual(wikidata.call_args.kwargs["skip_team_names"], {"主队甲"})

    def test_daily_fotmob_identity_map_opens_a_source_circuit_during_network_cooldown(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            daily_progress_path = temporary_path / "daily.json"
            daily_progress_path.write_text(json.dumps({
                "version": 1, "days_back": 3, "range_start": "2098-12-31", "range_end": "2099-01-02",
                "last_completed_date": "2098-12-31", "next_resume_date": None, "attempted_teams": {},
                "wikidata_network_circuit_open_until": "2099-01-03T18:00:00+08:00",
            }), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.prewarm_wikidata_source_identities", return_value={}) as wikidata, \
                 patch("outputs.tai_cai_tracker.cli.prewarm_fotmob_source_links", return_value={}), \
                 patch("outputs.tai_cai_tracker.cli.historical_fotmob_source_prewarm", return_value={}):
                summary = tracker_cli.daily_fotmob_identity_map(
                    now=now, historical_progress_path=temporary_path / "historical.json",
                    daily_progress_path=daily_progress_path,
                )

        self.assertFalse(wikidata.call_args.kwargs["network_enabled"])
        self.assertTrue(summary["wikidata_network_circuit_open"])

    def test_daily_fotmob_identity_map_uses_a_short_separate_cooldown_for_wikidata_rate_limits(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            daily_progress_path = temporary_path / "daily.json"
            with patch(
                "outputs.tai_cai_tracker.cli.prewarm_wikidata_source_identities",
                return_value={"items": [{
                    "team_name": "主队甲",
                    "failure_category": "access_restricted",
                    "failure_code": "http_429",
                }]},
            ), patch("outputs.tai_cai_tracker.cli.prewarm_fotmob_source_links", return_value={}), \
                 patch("outputs.tai_cai_tracker.cli.historical_fotmob_source_prewarm", return_value={}):
                summary = tracker_cli.daily_fotmob_identity_map(
                    now=now,
                    historical_progress_path=temporary_path / "historical.json",
                    daily_progress_path=daily_progress_path,
                )
            progress = json.loads(daily_progress_path.read_text(encoding="utf-8"))

        self.assertEqual(summary["wikidata_network_cooldown_count"], 0)
        self.assertEqual(summary["wikidata_rate_limit_cooldown_count"], 1)
        self.assertTrue(summary["wikidata_rate_limit_circuit_open"])
        self.assertEqual(
            progress["wikidata_rate_limit_cooldowns"]["主队甲"],
            "2099-01-03T13:00:00+08:00",
        )
        self.assertEqual(
            progress["wikidata_rate_limit_circuit_open_until"],
            "2099-01-03T13:00:00+08:00",
        )

    def test_daily_fotmob_identity_map_can_retry_wikidata_after_a_transport_fix(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            daily_progress_path = temporary_path / "daily.json"
            daily_progress_path.write_text(json.dumps({
                "version": 1, "days_back": 3, "range_start": "2098-12-31", "range_end": "2099-01-02",
                "last_completed_date": "2098-12-31", "next_resume_date": None, "attempted_teams": {},
                "wikidata_network_cooldowns": {"主队甲": "2099-01-03T18:00:00+08:00"},
                "wikidata_network_circuit_open_until": "2099-01-03T18:00:00+08:00",
            }), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.prewarm_wikidata_source_identities", return_value={"items": []}) as wikidata, \
                 patch("outputs.tai_cai_tracker.cli.prewarm_fotmob_source_links", return_value={}), \
                 patch("outputs.tai_cai_tracker.cli.historical_fotmob_source_prewarm", return_value={}):
                summary = tracker_cli.daily_fotmob_identity_map(
                    now=now, retry_wikidata_now=True,
                    historical_progress_path=temporary_path / "historical.json",
                    daily_progress_path=daily_progress_path,
                )

        self.assertEqual(wikidata.call_args.kwargs["skip_team_names"], set())
        self.assertTrue(wikidata.call_args.kwargs["network_enabled"])
        self.assertFalse(summary["wikidata_network_circuit_open"])

    def test_wikidata_source_prewarm_command_forwards_horizon_workers_and_registry(self):
        with patch(
            "outputs.tai_cai_tracker.cli.prewarm_wikidata_source_identities",
            return_value={"resolved_team_count": 0},
        ) as prewarm, patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "wikidata-source-prewarm",
                "--hours-ahead", "36",
                "--max-workers", "3",
                "--source-link-registry", "work/source-links.json",
            ])

        self.assertEqual(result, 0)
        prewarm.assert_called_once_with(
            hours_ahead=36,
            max_workers=3,
            source_link_registry_path="work/source-links.json",
        )
        self.assertEqual(json.loads(output.getvalue()), {"resolved_team_count": 0})

    def test_fotmob_prewarm_uses_cached_wikidata_english_anchor_after_reviewed_aliases(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        match = {
            "match_id": 20990101001, "home_team": "国际米兰", "away_team": "罗马",
            "kickoff_at": "2099-01-01T14:00:00+08:00", "status": "2",
        }
        identity = {
            "status": "resolved", "official_team": "国际米兰", "qid": "Q631",
            "chinese_name": "国际米兰", "english_name": "Inter Milan",
            "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=%E5%9B%BD%E9%99%85%E7%B1%B3%E5%85%B0&language=zh&format=json&limit=10",
            "source_url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q631&props=labels%7Caliases&languages=zh%7Cen&format=json",
            "captured_at": "2099-01-01T11:00:00+08:00",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            tracker_cli.record_wikidata_team_identity(registry_path, identity)
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch("outputs.tai_cai_tracker.cli.discover_team_stats_url", side_effect=ValueError("stop")) as discover:
                summary = tracker_cli.prewarm_fotmob_source_links(
                    hours_ahead=4,
                    now=now,
                    source_link_registry_path=registry_path,
                )

        self.assertEqual(discover.call_args_list[0].args, ("Inter Milan",))
        self.assertEqual(summary["wikidata_anchor_team_count"], 1)

    def test_historical_fotmob_source_prewarm_command_forwards_bounds_registries_and_progress(self):
        with patch(
            "outputs.tai_cai_tracker.cli.historical_fotmob_source_prewarm",
            return_value={"status": "in_progress"},
        ) as prewarm, patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "historical-fotmob-source-prewarm",
                "--days-back", "365",
                "--max-dates-per-run", "5",
                "--max-workers", "3",
                "--source-link-registry", "work/source-links.json",
                "--source-alias-registry", "work/aliases.json",
                "--reviewed-alias-registry", "work/fotmob-search-aliases.json",
                "--progress-path", "work/history-progress.json",
            ])

        self.assertEqual(result, 0)
        prewarm.assert_called_once_with(
            days_back=365,
            max_dates_per_run=5,
            max_workers=3,
            source_link_registry_path="work/source-links.json",
            source_alias_registry_path="work/aliases.json",
            reviewed_alias_registry_path="work/fotmob-search-aliases.json",
            progress_path="work/history-progress.json",
        )
        self.assertEqual(json.loads(output.getvalue()), {"status": "in_progress"})

    def test_daily_fotmob_identity_map_seeds_a_rolling_checkpoint_and_runs_both_sources(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            historical_progress_path = temporary_path / "historical.json"
            daily_progress_path = temporary_path / "daily.json"
            historical_progress_path.write_text(json.dumps({
                "version": 1,
                "days_back": 548,
                "range_start": "2097-07-05",
                "range_end": "2099-01-03",
                "last_completed_date": "2097-07-05",
                "next_resume_date": None,
                "attempted_teams": {
                    "oldfc": {"team_name": "Old FC", "search_name": "Old FC", "status": "unresolved"},
                },
            }), encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.prewarm_wikidata_source_identities",
                return_value={"resolved_team_count": 3, "unresolved_team_count": 1},
            ) as wikidata, patch(
                "outputs.tai_cai_tracker.cli.prewarm_fotmob_source_links",
                return_value={"resolved_team_count": 2, "unresolved_team_count": 1},
            ) as upcoming, patch(
                "outputs.tai_cai_tracker.cli.historical_fotmob_source_prewarm",
                return_value={"status": "complete", "resolved_team_count": 1, "unresolved_team_count": 0},
            ) as history:
                summary = tracker_cli.daily_fotmob_identity_map(
                    now=now,
                    source_link_registry_path=temporary_path / "source-links.json",
                    historical_progress_path=historical_progress_path,
                    daily_progress_path=daily_progress_path,
                )
            checkpoint = json.loads(daily_progress_path.read_text(encoding="utf-8"))

        wikidata.assert_called_once_with(
            hours_ahead=72,
            max_workers=2,
            now=now,
            source_link_registry_path=temporary_path / "source-links.json",
            skip_team_names=set(),
            network_enabled=True,
        )
        upcoming.assert_called_once_with(
            hours_ahead=72,
            max_workers=2,
            now=now,
            source_link_registry_path=temporary_path / "source-links.json",
            source_alias_registry_path=tracker_cli.DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
            require_verified_search_name=True,
        )
        history.assert_called_once_with(
            days_back=3,
            max_dates_per_run=3,
            max_workers=2,
            now=now,
            source_link_registry_path=temporary_path / "source-links.json",
            source_alias_registry_path=tracker_cli.DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
            reviewed_alias_registry_path=tracker_cli.DEFAULT_FOTMOB_SEARCH_ALIAS_REGISTRY_PATH,
            progress_path=daily_progress_path,
        )
        self.assertEqual(checkpoint["range_start"], "2098-12-31")
        self.assertEqual(checkpoint["range_end"], "2099-01-02")
        self.assertEqual(checkpoint["attempted_teams"]["oldfc"]["team_name"], "Old FC")
        self.assertEqual(summary["recent_result_window"], {"start": "2098-12-31", "end": "2099-01-02"})
        self.assertEqual(summary["wikidata"]["resolved_team_count"], 3)
        self.assertEqual(summary["upcoming"]["resolved_team_count"], 2)

    def test_daily_fotmob_identity_map_command_forwards_options(self):
        with patch(
            "outputs.tai_cai_tracker.cli.daily_fotmob_identity_map",
            return_value={"upcoming": {}, "recent_results": {}},
        ) as daily, patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "daily-fotmob-identity-map",
                "--hours-ahead", "48",
                "--result-days", "2",
                "--max-workers", "3",
                "--source-link-registry", "work/source-links.json",
                "--source-alias-registry", "work/aliases.json",
                "--reviewed-alias-registry", "work/fotmob-search-aliases.json",
                "--historical-progress-path", "work/historical.json",
                "--daily-progress-path", "work/daily.json",
                "--retry-wikidata-now",
            ])

        self.assertEqual(result, 0)
        daily.assert_called_once_with(
            hours_ahead=48,
            result_days=2,
            max_workers=3,
            source_link_registry_path="work/source-links.json",
            source_alias_registry_path="work/aliases.json",
            reviewed_alias_registry_path="work/fotmob-search-aliases.json",
            historical_progress_path="work/historical.json",
            daily_progress_path="work/daily.json",
            retry_wikidata_now=True,
        )
        self.assertEqual(json.loads(output.getvalue()), {"upcoming": {}, "recent_results": {}})

    def test_fotmob_search_alias_import_command_forwards_reviewed_input(self):
        with patch(
            "outputs.tai_cai_tracker.cli.import_reviewed_fotmob_search_aliases",
            return_value={"version": 1, "aliases": {}},
        ) as importer, patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "fotmob-search-alias-import",
                "work/reviewed-fotmob-aliases.json",
                "--registry-path", "work/fotmob-search-aliases.json",
            ])

        self.assertEqual(result, 0)
        importer.assert_called_once_with(
            "work/fotmob-search-aliases.json", "work/reviewed-fotmob-aliases.json"
        )
        self.assertEqual(json.loads(output.getvalue()), {"version": 1, "aliases": {}})

    def test_historical_fotmob_failure_report_command_forwards_progress_path(self):
        with patch(
            "outputs.tai_cai_tracker.cli.historical_fotmob_failure_report",
            return_value={"unresolved_team_count": 0},
        ) as report, patch("sys.stdout", io.StringIO()) as output:
            result = tracker_cli.main([
                "historical-fotmob-failure-report",
                "--progress-path", "work/history-progress.json",
            ])

        self.assertEqual(result, 0)
        report.assert_called_once_with("work/history-progress.json")
        self.assertEqual(json.loads(output.getvalue()), {"unresolved_team_count": 0})

    def test_historical_fotmob_source_prewarm_scans_dates_deduplicates_teams_and_records_only_strict_matches(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        first_date_match = {
            "match_id": 2042001,
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-03T20:00:00+08:00",
            "official_result_url": "https://official.example.test/result?date=2099-01-03",
        }
        second_date_match = {
            "home_team": "Home FC",
            "away_team": "Third FC",
        }
        candidates = {
            "Home FC": {
                "team_id": 1,
                "team_name": "Home FC",
                "source_url": "https://www.fotmob.com/teams/1/overview/home-fc",
                "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Home%20FC",
                "score": 1.0,
            },
            "Third FC": {
                "team_id": 3,
                "team_name": "Third FC",
                "source_url": "https://www.fotmob.com/teams/3/overview/third-fc",
                "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Third%20FC",
                "score": 1.0,
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            progress_path = Path(temporary_directory) / "historical-progress.json"
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                side_effect=[
                    {"matches": [first_date_match]},
                    {"matches": [second_date_match]},
                ],
            ) as collect, patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                side_effect=lambda name: candidates[name] if name in candidates else (_ for _ in ()).throw(ValueError("ambiguous")),
            ) as discover:
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=3,
                    max_dates_per_run=2,
                    max_workers=1,
                    now=now,
                    source_link_registry_path=registry_path,
                    progress_path=progress_path,
                )

            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            home_identity = tracker_cli.cached_fotmob_team_identity(registry_path, "Home FC")
            third_identity = tracker_cli.cached_fotmob_team_identity(registry_path, "Third FC")

        self.assertEqual([call.kwargs["match_date"] for call in collect.call_args_list], [
            "2099-01-03", "2099-01-02",
        ])
        self.assertEqual([call.args[0] for call in discover.call_args_list], [
            "Home FC", "Away FC", "Third FC",
        ])
        self.assertEqual(summary["official_match_count"], 2)
        self.assertEqual(summary["unique_team_count"], 3)
        self.assertEqual(summary["resolved_team_count"], 2)
        self.assertEqual(summary["unresolved_team_count"], 1)
        self.assertEqual(progress["last_completed_date"], "2099-01-02")
        self.assertEqual(progress["next_resume_date"], "2099-01-01")
        self.assertEqual(
            progress["attempted_teams"]["homefc"]["official_references"][0]["official_result_url"],
            "https://official.example.test/result?date=2099-01-03",
        )
        self.assertEqual(home_identity["source_team_id"], "1")
        self.assertEqual(third_identity["source_team_id"], "3")

    def test_historical_fotmob_source_prewarm_resolves_wikidata_before_fotmob_search(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        identities = {
            "国际米兰": {
                "status": "resolved", "official_team": "国际米兰", "qid": "Q631",
                "chinese_name": "国际米兰", "english_name": "Inter Milan",
                "english_names": ["Inter Milan", "Inter"],
                "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=%E5%9B%BD%E9%99%85%E7%B1%B3%E5%85%B0&language=zh&format=json&limit=10",
                "source_url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q631&props=labels%7Caliases&languages=zh%7Cen&format=json",
                "captured_at": now.isoformat(),
            },
            "罗马": {
                "status": "resolved", "official_team": "罗马", "qid": "Q220",
                "chinese_name": "罗马", "english_name": "Roma",
                "english_names": ["Roma"],
                "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=%E7%BD%97%E9%A9%AC&language=zh&format=json&limit=10",
                "source_url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q220&props=labels%7Caliases&languages=zh%7Cen&format=json",
                "captured_at": now.isoformat(),
            },
        }
        candidates = {
            "Inter Milan": {
                "team_id": 1, "team_name": "Inter Milan",
                "source_url": "https://www.fotmob.com/teams/1/overview/inter-milan",
                "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Inter%20Milan",
                "score": 1.0,
            },
            "Roma": {
                "team_id": 2, "team_name": "Roma",
                "source_url": "https://www.fotmob.com/teams/2/overview/roma",
                "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Roma",
                "score": 1.0,
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            registry_path = temporary_path / "source-links.json"
            progress_path = temporary_path / "historical-progress.json"
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": [
                    {"home_team": "国际米兰", "away_team": "罗马"},
                    {"home_team": "国际米兰", "away_team": "罗马"},
                ]},
            ), patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
                side_effect=lambda team_name, captured_at: identities[team_name],
            ) as resolve, patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                side_effect=lambda team_name: candidates[team_name],
            ) as discover:
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1,
                    max_workers=1,
                    now=now,
                    source_link_registry_path=registry_path,
                    progress_path=progress_path,
                )

            inter = cached_wikidata_team_identity(registry_path, "国际米兰")

        self.assertEqual([call.args[0] for call in resolve.call_args_list], ["国际米兰", "罗马"])
        self.assertEqual([call.args[0] for call in discover.call_args_list], ["Inter Milan", "Roma"])
        self.assertEqual(summary["wikidata"]["resolved_team_count"], 2)
        self.assertEqual(inter["english_name"], "Inter Milan")

    def test_historical_fotmob_source_prewarm_only_cools_down_wikidata_access_restrictions(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        server_error = HTTPError(
            "https://www.wikidata.org/w/api.php", 500, "Server Error", None, None,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": [{"home_team": "中文队"}]},
            ), patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
                side_effect=server_error,
            ), patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                side_effect=ValueError("no public candidates"),
            ):
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1,
                    now=now,
                    progress_path=progress_path,
                )

            progress = json.loads(progress_path.read_text(encoding="utf-8"))

        self.assertIsNone(summary["wikidata"]["circuit_open_until"])
        self.assertNotIn("wikidata_circuit_open_until", progress)
        self.assertEqual(summary["wikidata"]["items"][0]["status"], "unresolved")

    def test_historical_fotmob_source_prewarm_stops_wikidata_after_rate_limit(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        rate_limit = HTTPError(
            "https://www.wikidata.org/w/api.php", 429, "Too Many Requests", None, None,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": [{"home_team": "主队甲", "away_team": "客队乙"}]},
            ), patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
                side_effect=rate_limit,
            ) as resolve, patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                side_effect=ValueError("no public candidates"),
            ):
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1,
                    now=now,
                    progress_path=progress_path,
                )

            progress = json.loads(progress_path.read_text(encoding="utf-8"))

        resolve.assert_called_once_with("主队甲", captured_at=now.isoformat())
        self.assertEqual(summary["wikidata"]["items"][0]["failure_code"], "http_429")
        self.assertEqual(summary["wikidata"]["circuit_open_until"], "2099-01-03T13:00:00+08:00")
        self.assertEqual(progress["wikidata_circuit_open_until"], "2099-01-03T13:00:00+08:00")

    def test_historical_fotmob_source_prewarm_retries_rate_limited_team_from_queue(self):
        first_now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        second_now = tracker_cli.datetime.fromisoformat("2099-01-03T13:00:01+08:00")
        rate_limit = HTTPError(
            "https://www.wikidata.org/w/api.php", 429, "Too Many Requests", None, None,
        )
        identity = {
            "status": "resolved", "official_team": "主队甲", "qid": "Q10",
            "chinese_name": "主队甲", "english_name": "Home FC",
            "english_names": ["Home FC"],
            "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=x&format=json",
            "source_url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q10&format=json",
            "captured_at": second_now.isoformat(),
        }
        candidate = {
            "team_id": 1, "team_name": "Home FC",
            "source_url": "https://www.fotmob.com/teams/1/overview/home-fc",
            "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Home%20FC",
            "score": 1.0,
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            registry_path = temporary_path / "source-links.json"
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": [{"home_team": "主队甲"}]},
            ), patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
                side_effect=rate_limit,
            ), patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                side_effect=ValueError("no public candidates"),
            ):
                tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1, now=first_now, progress_path=progress_path,
                )

            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": []},
            ), patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
                return_value=identity,
            ) as resolve, patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                return_value=candidate,
            ) as discover:
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1, now=second_now, progress_path=progress_path,
                    source_link_registry_path=registry_path,
                )
            progress = json.loads(progress_path.read_text(encoding="utf-8"))

        resolve.assert_called_once_with("主队甲", captured_at=second_now.isoformat())
        discover.assert_called_once_with("Home FC")
        self.assertEqual(summary["wikidata"]["retried_queue_team_count"], 1)
        self.assertEqual(summary["wikidata"]["resolved_team_count"], 1)
        self.assertEqual(progress["wikidata_retry_queue"], {})

    def test_historical_fotmob_source_prewarm_migrates_legacy_rate_limit_attempt(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T13:00:01+08:00")
        identity = {
            "status": "resolved", "official_team": "主队甲", "qid": "Q10",
            "chinese_name": "主队甲", "english_name": "Home FC",
            "english_names": ["Home FC"],
            "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=x&format=json",
            "source_url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q10&format=json",
            "captured_at": now.isoformat(),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            registry_path = temporary_path / "source-links.json"
            progress_path.write_text(json.dumps({
                "version": 1, "days_back": 1, "range_start": "2099-01-03",
                "range_end": "2099-01-03", "last_completed_date": "2099-01-03",
                "next_resume_date": None, "attempted_teams": {},
                "wikidata_attempted_teams": {
                    "主队甲": {
                        "team_name": "主队甲", "status": "unresolved",
                        "failure_category": "access_restricted", "failure_code": "http_429",
                        "attempted_at": "2099-01-03T12:00:00+08:00",
                    },
                },
                "wikidata_circuit_open_until": "2099-01-03T13:00:00+08:00",
            }), encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
                return_value=identity,
            ) as resolve, patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                side_effect=ValueError("no public candidates"),
            ):
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1, now=now, progress_path=progress_path,
                    source_link_registry_path=registry_path,
                )

        resolve.assert_called_once_with("主队甲", captured_at=now.isoformat())
        self.assertEqual(summary["wikidata"]["retried_queue_team_count"], 1)

    def test_historical_fotmob_source_prewarm_queues_new_names_during_circuit_cooldown(self):
        first_now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        second_now = tracker_cli.datetime.fromisoformat("2099-01-04T01:00:01+08:00")
        identity = {
            "status": "resolved", "official_team": "主队甲", "qid": "Q10",
            "chinese_name": "主队甲", "english_name": "Home FC",
            "english_names": ["Home FC"],
            "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=x&format=json",
            "source_url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q10&format=json",
            "captured_at": second_now.isoformat(),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            registry_path = temporary_path / "source-links.json"
            progress_path.write_text(json.dumps({
                "version": 1, "days_back": 1, "range_start": "2099-01-03",
                "range_end": "2099-01-03", "last_completed_date": None,
                "next_resume_date": "2099-01-03", "attempted_teams": {},
                "wikidata_attempted_teams": {}, "wikidata_retry_queue": {},
                "wikidata_circuit_open_until": "2099-01-04T00:00:00+08:00",
            }), encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": [{"home_team": "主队甲"}]},
            ), patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
            ) as resolve, patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                side_effect=ValueError("no public candidates"),
            ):
                first_summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1, now=first_now, progress_path=progress_path,
                    source_link_registry_path=registry_path,
                )
            progress = json.loads(progress_path.read_text(encoding="utf-8"))

            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": []},
            ), patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
                return_value=identity,
            ) as resolve:
                tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1, now=second_now, progress_path=progress_path,
                    source_link_registry_path=registry_path,
                )

        self.assertEqual(first_summary["wikidata"]["retry_queue_count"], 1)
        self.assertEqual(progress["wikidata_retry_queue"]["主队甲"]["retry_reason"], "circuit_cooldown")
        resolve.assert_called_once_with("主队甲", captured_at=second_now.isoformat())

    def test_historical_fotmob_source_prewarm_recovers_uncached_historical_attempts_after_cooldown(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-04T01:00:01+08:00")
        identity = {
            "status": "resolved", "official_team": "主队甲", "qid": "Q10",
            "chinese_name": "主队甲", "english_name": "Home FC",
            "english_names": ["Home FC"],
            "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=x&format=json",
            "source_url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q10&format=json",
            "captured_at": now.isoformat(),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            registry_path = temporary_path / "source-links.json"
            progress_path.write_text(json.dumps({
                "version": 1, "days_back": 1, "range_start": "2099-01-03",
                "range_end": "2099-01-03", "last_completed_date": "2099-01-03",
                "next_resume_date": None,
                "attempted_teams": {
                    "主队甲": {
                        "team_name": "主队甲", "search_name": "主队甲",
                        "status": "unresolved",
                    },
                },
                "wikidata_attempted_teams": {}, "wikidata_retry_queue": {},
                "wikidata_circuit_open_until": "2099-01-04T00:00:00+08:00",
            }), encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
                return_value=identity,
            ) as resolve, patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                side_effect=ValueError("no public candidates"),
            ):
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1, now=now, progress_path=progress_path,
                    source_link_registry_path=registry_path,
                )

        resolve.assert_called_once_with("主队甲", captured_at=now.isoformat())
        self.assertEqual(summary["wikidata"]["retried_queue_team_count"], 1)

    def test_historical_fotmob_source_prewarm_retries_wikidata_after_identity_rule_upgrade(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        identity = {
            "status": "resolved", "official_team": "测试队", "qid": "Q10",
            "chinese_name": "测试队", "english_name": "Test FC",
            "english_names": ["Test FC"],
            "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=%E6%B5%8B%E8%AF%95%E9%98%9F&language=zh&format=json&limit=10",
            "source_url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q10&props=labels%7Caliases&languages=zh%7Cen&format=json",
            "captured_at": now.isoformat(),
        }
        candidate = {
            "team_id": 1, "team_name": "Test FC",
            "source_url": "https://www.fotmob.com/teams/1/overview/test-fc",
            "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Test%20FC",
            "score": 1.0,
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            registry_path = temporary_path / "source-links.json"
            progress_path.write_text(json.dumps({
                "version": 1, "days_back": 1, "range_start": "2099-01-03",
                "range_end": "2099-01-03", "last_completed_date": None,
                "next_resume_date": "2099-01-03", "attempted_teams": {},
                "wikidata_attempted_teams": {
                    "测试队": {"team_name": "测试队", "status": "unresolved"},
                },
            }), encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": [{"home_team": "测试队"}]},
            ), patch(
                "outputs.tai_cai_tracker.cli.resolve_team_identity",
                return_value=identity,
            ) as resolve, patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                return_value=candidate,
            ):
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1,
                    now=now,
                    progress_path=progress_path,
                    source_link_registry_path=registry_path,
                )

        resolve.assert_called_once_with("测试队", captured_at=now.isoformat())
        self.assertEqual(summary["wikidata"]["resolved_team_count"], 1)

    def test_historical_fotmob_source_prewarm_resumes_from_progress_without_retrying_attempted_teams(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        candidate = {
            "team_id": 2,
            "team_name": "New FC",
            "source_url": "https://www.fotmob.com/teams/2/overview/new-fc",
            "search_url": "https://www.fotmob.com/api/data/search/suggest?term=New%20FC",
            "score": 1.0,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            progress_path = Path(temporary_directory) / "historical-progress.json"
            registry_path = Path(temporary_directory) / "source-links.json"
            progress_path.write_text(json.dumps({
                "version": 1,
                "days_back": 3,
                "range_start": "2099-01-01",
                "range_end": "2099-01-03",
                "last_completed_date": "2099-01-02",
                "next_resume_date": "2099-01-01",
                "attempted_teams": {
                    "oldfc": {"team_name": "Old FC", "status": "unresolved"},
                },
            }), encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": [{"home_team": "Old FC", "away_team": "New FC"}]},
            ) as collect, patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                return_value=candidate,
            ) as discover:
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=3,
                    max_dates_per_run=2,
                    max_workers=1,
                    now=now,
                    source_link_registry_path=registry_path,
                    progress_path=progress_path,
                )

        self.assertEqual([call.kwargs["match_date"] for call in collect.call_args_list], ["2099-01-01"])
        discover.assert_called_once_with("New FC")
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["previously_attempted_team_count"], 1)
        self.assertEqual(summary["next_resume_date"], None)

    def test_historical_fotmob_source_prewarm_retries_an_unresolved_team_when_its_verified_search_name_changes(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        candidate = {
            "team_id": 2,
            "team_name": "Old English FC",
            "source_url": "https://www.fotmob.com/teams/2/overview/old-english-fc",
            "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Old%20English%20FC",
            "score": 1.0,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            registry_path = temporary_path / "source-links.json"
            alias_path = temporary_path / "aliases.json"
            progress_path.write_text(json.dumps({
                "version": 1,
                "days_back": 3,
                "range_start": "2099-01-01",
                "range_end": "2099-01-03",
                "last_completed_date": "2099-01-02",
                "next_resume_date": "2099-01-01",
                "attempted_teams": {
                    "oldfc": {"team_name": "Old FC", "search_name": "Old FC", "status": "unresolved"},
                },
            }), encoding="utf-8")
            alias_path.write_text(json.dumps({
                "version": 1,
                "aliases": {
                    "oldfc": {
                        "source_name": "Old English FC",
                        "source_team_id": 2,
                        "source_provider": "Understat",
                    },
                },
            }), encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": [{"home_team": "Old FC"}]},
            ), patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                return_value=candidate,
            ) as discover:
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=3,
                    now=now,
                    source_link_registry_path=registry_path,
                    source_alias_registry_path=alias_path,
                    progress_path=progress_path,
                )

        discover.assert_called_once_with("Old English FC")
        self.assertEqual(summary["previously_attempted_team_count"], 0)
        self.assertEqual(summary["resolved_team_count"], 1)

    def test_historical_fotmob_source_prewarm_prefers_reviewed_fotmob_search_alias_before_adopted_alias(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        candidate = {
            "team_id": 2,
            "team_name": "Reviewed English FC",
            "source_url": "https://www.fotmob.com/teams/2/overview/reviewed-english-fc",
            "search_url": "https://www.fotmob.com/api/data/search/suggest?term=Reviewed%20English%20FC",
            "score": 1.0,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            source_alias_path = temporary_path / "source-aliases.json"
            reviewed_alias_path = temporary_path / "fotmob-search-aliases.json"
            source_link_path = temporary_path / "source-links.json"
            progress_path.write_text(json.dumps({
                "version": 1,
                "days_back": 3,
                "range_start": "2099-01-01",
                "range_end": "2099-01-03",
                "last_completed_date": "2099-01-02",
                "next_resume_date": "2099-01-01",
                "attempted_teams": {},
            }), encoding="utf-8")
            source_alias_path.write_text(json.dumps({"version": 1, "aliases": {
                "oldfc": {"source_name": "Adopted English FC", "source_team_id": 1},
            }}), encoding="utf-8")
            reviewed_alias_path.write_text(json.dumps({"version": 1, "aliases": {
                "oldfc": {"provider": "FotMob", "search_name": "Reviewed English FC"},
            }}), encoding="utf-8")
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"matches": [{"home_team": "Old FC"}]},
            ), patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                return_value=candidate,
            ) as discover:
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=3,
                    now=now,
                    source_link_registry_path=source_link_path,
                    source_alias_registry_path=source_alias_path,
                    reviewed_alias_registry_path=reviewed_alias_path,
                    progress_path=progress_path,
                )

        discover.assert_called_once_with("Reviewed English FC")
        self.assertEqual(summary["resolved_team_count"], 1)

    def test_historical_fotmob_source_prewarm_persists_structured_unresolved_failure(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            progress_path = temporary_path / "historical-progress.json"
            source_link_path = temporary_path / "source-links.json"
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                return_value={"official_result_url": "https://official.example.test/results", "matches": [{"home_team": "中文队"}]},
            ), patch(
                "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                side_effect=tracker_cli.FotMobSearchResolutionError(
                    "no_public_candidates", "FotMob public search returned no team candidates"
                ),
            ):
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=1,
                    now=now,
                    source_link_registry_path=source_link_path,
                    progress_path=progress_path,
                )

            progress = json.loads(progress_path.read_text(encoding="utf-8"))

        entry = progress["attempted_teams"]["中文队"]
        self.assertEqual(summary["items"][0]["failure_code"], "no_public_candidates")
        self.assertEqual(entry["failure_category"], "search_alias_gap_suspected")
        self.assertEqual(entry["remediation"], "review_search_alias")

    def test_historical_fotmob_failure_report_separates_legacy_alias_gaps_from_transport_errors(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            progress_path = Path(temporary_directory) / "historical-progress.json"
            progress_path.write_text(json.dumps({
                "version": 1,
                "attempted_teams": {
                    "中文队": {
                        "team_name": "中文队", "search_name": "中文队", "status": "unresolved",
                        "reason": "no high-confidence FotMob team search result",
                    },
                    "englishfc": {
                        "team_name": "English FC", "search_name": "English FC", "status": "unresolved",
                        "failure_code": "no_public_candidates",
                        "failure_category": "no_public_candidates_after_source_query",
                        "remediation": "check_source_coverage",
                    },
                    "networkfc": {
                        "team_name": "Network FC", "search_name": "Network FC", "status": "unresolved",
                        "reason": "<urlopen error [WinError 10054] connection reset>",
                    },
                    "unrecorded": {
                        "team_name": "旧中文队", "status": "unresolved",
                        "reason": "no high-confidence FotMob team search result",
                    },
                },
            }), encoding="utf-8")

            report = tracker_cli.historical_fotmob_failure_report(progress_path)

        self.assertEqual(report["unresolved_team_count"], 4)
        self.assertEqual(report["failure_category_counts"]["search_alias_gap_suspected"], 1)
        self.assertEqual(report["failure_category_counts"]["no_public_candidates_after_source_query"], 1)
        self.assertEqual(report["failure_category_counts"]["transport_error"], 1)
        self.assertEqual(report["failure_category_counts"]["legacy_search_term_unrecorded"], 1)
        chinese_item = next(item for item in report["items"] if item["team_name"] == "中文队")
        self.assertEqual(chinese_item["classification_basis"], "legacy_reason_text")

    def test_historical_fotmob_source_prewarm_does_not_create_progress_when_official_date_fails(self):
        now = tracker_cli.datetime.fromisoformat("2099-01-03T12:00:00+08:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            progress_path = Path(temporary_directory) / "historical-progress.json"
            with patch(
                "outputs.tai_cai_tracker.cli.collect_results",
                side_effect=OSError("official endpoint unavailable"),
            ):
                summary = tracker_cli.historical_fotmob_source_prewarm(
                    days_back=3,
                    max_dates_per_run=2,
                    now=now,
                    progress_path=progress_path,
                )

        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["failed_date"], "2099-01-03")
        self.assertFalse(progress_path.exists())

    def test_fotmob_cache_warm_reuses_strict_source_alias_as_search_name(self):
        match = {
            "match_id": 20990101002,
            "match_num": "周一002",
            "competition": "Test League",
            "home_team": "主队",
            "away_team": "客队",
            "kickoff_at": "2099-01-02T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101002",
            "status": "2",
        }
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        aliases = {
            "version": 1,
            "aliases": {
                "主队": {
                    "source_name": "Home United",
                    "source_team_id": 1,
                    "source_url": "https://www.sofascore.com/api/v1/event/1",
                },
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            registry_path = temporary_path / "source-links.json"
            alias_path = temporary_path / "aliases.json"
            alias_path.write_text(json.dumps(aliases), encoding="utf-8")
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch(
                     "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                     side_effect=[
                         {"source_url": "https://www.fotmob.com/teams/1/overview/home-united"},
                         {"source_url": "https://www.fotmob.com/teams/2/overview/away-fc"},
                     ],
                 ) as discover:
                tracker_cli.prewarm_fotmob_source_links(
                    hours_ahead=48,
                    now=now,
                    source_link_registry_path=registry_path,
                    source_alias_registry_path=alias_path,
                )

        self.assertEqual(discover.call_args_list[0].args, ("Home United",))
        self.assertEqual(discover.call_args_list[1].args, ("客队",))

    def test_fotmob_cache_warm_can_explicitly_upgrade_legacy_url_cache(self):
        match = {
            "match_id": 20990101003,
            "match_num": "周一003",
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-02T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101003",
            "status": "2",
        }
        home_url = "https://www.fotmob.com/teams/1/overview/home-fc"
        away_url = "https://www.fotmob.com/teams/2/overview/away-fc"
        now = tracker_cli.datetime.fromisoformat("2099-01-01T12:00:00+08:00")
        candidate = lambda team_id, team_name, source_url: {
            "team_id": team_id,
            "team_name": team_name,
            "source_url": source_url,
            "search_url": f"https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term={team_name}",
            "score": 1.0,
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            tracker_cli.record_source_links(registry_path, match, {"home_fotmob_url": home_url})
            with patch("outputs.tai_cai_tracker.cli.collect_schedule", return_value={"matches": [match]}), \
                 patch(
                     "outputs.tai_cai_tracker.cli.discover_team_stats_url",
                     side_effect=[candidate(1, "Home FC", home_url), candidate(2, "Away FC", away_url)],
                 ) as discover:
                summary = tracker_cli.prewarm_fotmob_source_links(
                    hours_ahead=48,
                    now=now,
                    source_link_registry_path=registry_path,
                    upgrade_legacy=True,
                )
            identity = tracker_cli.cached_fotmob_team_identity(registry_path, "Home FC")

        self.assertEqual(discover.call_count, 2)
        self.assertEqual(identity["source_team_id"], "1")
        self.assertEqual(summary["legacy_upgrade_requested_count"], 1)

    def test_settlement_queue_lists_unsettled_predictions_with_official_sources(self):
        first = freeze_prediction({
            "market": "SPF", "match_id": "20990101001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T18:00:00+08:00",
            "source_urls": {"S1": "https://official.example.test/matches/1", "S2": "https://odds.example.test/matches/1"},
            "match_context": {"competition": "Test League", "home_team": "Home FC", "away_team": "Away FC"},
            "outcomes": [{"label": "home_win", "probability": Decimal("0.50")}, {"label": "draw", "probability": Decimal("0.30")}, {"label": "away_win", "probability": Decimal("0.20")}],
        })
        second = freeze_prediction({
            "market": "SPF", "match_id": "20990101002",
            "kickoff_at": "2099-01-01T18:00:00+08:00",
            "captured_at": "2099-01-01T17:00:00+08:00",
            "source_urls": {"S1": "https://official.example.test/matches/2", "S2": "https://odds.example.test/matches/2"},
            "outcomes": [{"label": "home_win", "probability": Decimal("0.50")}, {"label": "draw", "probability": Decimal("0.30")}, {"label": "away_win", "probability": Decimal("0.20")}],
        })
        settled = tracker_cli.settle_prediction(
            first, "home_win", "https://official.example.test/results/1"
        )

        queue = settlement_queue([first, second, settled])

        self.assertEqual(queue["pending_predictions"], 1)
        self.assertEqual(queue["items"][0]["prediction_hash"], second["content_hash"])
        self.assertEqual(queue["items"][0]["official_match_url"], "https://official.example.test/matches/2")
        self.assertEqual(queue["items"][0]["all_market_snapshot_status"], "not_frozen")
        self.assertEqual(queue["items"][0]["settlement_action"], "official_result_required")

    def test_report_restores_enhanced_model_weight_from_jsonl(self):
        prediction = freeze_prediction(
            {
                "market": "SPF",
                "match_id": "20990101001",
                "kickoff_at": "2099-01-01T20:00:00+08:00",
                "captured_at": "2099-01-01T18:00:00+08:00",
                "source_urls": {
                    "S1": "https://official.example.test/matches/20990101001",
                    "S2": "https://market.example.test/matches/20990101001",
                },
                "prediction_role": "primary",
                "outcomes": [
                    {"label": "home_win", "probability": Decimal("0.50")},
                    {"label": "draw", "probability": Decimal("0.30")},
                    {"label": "away_win", "probability": Decimal("0.20")},
                ],
                "model": {
                    "version": "poisson-v1",
                    "weight": Decimal("0.40"),
                    "scorelines": [
                        {
                            "home_goals": 2,
                            "away_goals": 0,
                            "probability": Decimal("0.10"),
                        }
                    ],
                },
            }
        )
        settlement = tracker_cli.settle_prediction(
            prediction,
            "home_win",
            "https://official.example.test/results/enhanced",
            final_score={"home": 2, "away": 0},
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text(
                "\n".join(
                    json.dumps(record, default=str, ensure_ascii=True)
                    for record in (prediction, settlement)
                ) + "\n",
                encoding="utf-8",
            )

            report = tracker_cli.report(ledger_path)

        self.assertIn("poisson-v1@0.40@primary", report["strategy_breakdown"])
        self.assertEqual(
            report["strategy_breakdown"]["poisson-v1@0.40@primary"]
            ["scoreline_evaluated_records"],
            1,
        )

    def test_snapshot_preserves_valid_pregame_context_without_changing_outcomes(self):
        payload = json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8"))
        baseline = prediction_from_input(payload)
        payload["pregame_context"] = {
            "split_xg": {"status": "unavailable", "reason": "venue_or_team_statistics_unavailable"},
            "weather": {"status": "unavailable", "reason": "venue_coordinates_unavailable"},
            "lineup": {"status": "unavailable", "reason": "complete_lineup_unavailable"},
            "risk_summary": {
                "confidence_ceiling": "medium",
                "split_xg_status": "unavailable",
                "lineup_status": "unavailable",
            },
        }

        prediction = prediction_from_input(payload)

        self.assertEqual(prediction["pregame_context"], payload["pregame_context"])
        self.assertEqual(prediction["outcomes"], baseline["outcomes"])

    def test_snapshot_rejects_second_primary_but_allows_comparison_for_same_match(self):
        payload = json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8"))
        comparison_payload = deepcopy(payload)
        comparison_payload["captured_at"] = "2099-01-01T18:30:00+08:00"
        comparison_payload["prediction_role"] = "comparison"
        second_primary_payload = deepcopy(payload)
        second_primary_payload["captured_at"] = "2099-01-01T18:15:00+08:00"

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            ledger_path = temporary_path / "ledger.jsonl"
            first_input_path = temporary_path / "first.json"
            second_primary_input_path = temporary_path / "second-primary.json"
            comparison_input_path = temporary_path / "comparison.json"
            first_input_path.write_text(json.dumps(payload), encoding="utf-8")
            second_primary_input_path.write_text(
                json.dumps(second_primary_payload), encoding="utf-8"
            )
            comparison_input_path.write_text(
                json.dumps(comparison_payload), encoding="utf-8"
            )

            self.assertEqual(
                self.run_cli("snapshot", str(first_input_path), str(ledger_path)).returncode,
                0,
            )
            duplicate = self.run_cli(
                "snapshot", str(second_primary_input_path), str(ledger_path)
            )
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn("primary", duplicate.stderr)

            comparison = self.run_cli(
                "snapshot", str(comparison_input_path), str(ledger_path)
            )
            self.assertEqual(comparison.returncode, 0, comparison.stderr)
            records = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["prediction_role"], "primary")
            self.assertEqual(records[1]["prediction_role"], "comparison")

    def test_read_records_rejects_tampered_frozen_prediction(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            snapshot = self.run_cli("snapshot", str(EXAMPLE_INPUT), str(ledger_path))
            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            record = json.loads(ledger_path.read_text(encoding="utf-8"))
            record["source_urls"]["S1"] = "https://official.example.test/tampered"
            ledger_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "content_hash"):
                read_records(ledger_path)

    def test_read_records_rejects_tampered_settlement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            snapshot = self.run_cli("snapshot", str(EXAMPLE_INPUT), str(ledger_path))
            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            prediction = json.loads(ledger_path.read_text(encoding="utf-8"))
            settlement = self.run_cli(
                "settle",
                str(ledger_path),
                prediction["content_hash"],
                "home_win",
                "https://official.example.test/results/20990101001",
            )
            self.assertEqual(settlement.returncode, 0, settlement.stderr)
            records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
            records[1]["actual_outcome"] = "away_win"
            ledger_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "content_hash"):
                read_records(ledger_path)

    def test_settlement_invalidation_excludes_a_misidentified_result_and_reopens_queue(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            ledger_path = temporary_path / "ledger.jsonl"
            invalidation_path = temporary_path / "invalidation.json"
            snapshot = self.run_cli("snapshot", str(EXAMPLE_INPUT), str(ledger_path))
            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            prediction = json.loads(ledger_path.read_text(encoding="utf-8"))
            settlement = self.run_cli(
                "settle",
                str(ledger_path),
                prediction["content_hash"],
                "home_win",
                "https://official.example.test/results/20990101001",
                "--final-score", "1", "0",
            )
            self.assertEqual(settlement.returncode, 0, settlement.stderr)
            settled_record = json.loads(settlement.stdout)
            invalidation_path.write_text(json.dumps({
                "settlement_hash": settled_record["content_hash"],
                "prediction_hash": prediction["content_hash"],
                "invalidated_at": "2099-01-02T10:00:00+08:00",
                "reason_code": "identity_mismatch",
                "reason": "official result identity conflicts with the frozen fixture",
                "official_result_url": "https://official.example.test/results/20990101001",
                "official_result_identity": {
                    "match_id": "20990101001",
                    "match_num": "\u5468\u4e8c009",
                    "match_date": "2099-01-01",
                    "home_team": "Other Home",
                    "away_team": "Other Away",
                },
            }), encoding="utf-8")

            invalidation = self.run_cli(
                "settlement-invalidate", str(invalidation_path), str(ledger_path)
            )

            self.assertEqual(invalidation.returncode, 0, invalidation.stderr)
            records = read_records(ledger_path)
            self.assertEqual(records[-1]["record_type"], "settlement_invalidation")
            self.assertEqual(tracker_cli.report(ledger_path)["settled_records"], 0)
            self.assertEqual(settlement_queue(records)["pending_predictions"], 1)

    def test_hypothetical_example_odds_import_creates_a_capture_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"

            result = self.run_cli("odds-import", str(EXAMPLE_ODDS_IMPORT), str(ledger_path))

            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["record_type"], "odds_capture")
            self.assertEqual(records[0]["match_id"], "20990101001")

    def test_sporttery_collector_commands_are_exposed(self):
        scan = self.run_cli("sporttery-scan", "--help")
        fixed_bonus = self.run_cli("sporttery-fixed-bonus", "--help")

        self.assertEqual(scan.returncode, 0, scan.stderr)
        self.assertIn("--method", scan.stdout)
        self.assertEqual(fixed_bonus.returncode, 0, fixed_bonus.stderr)
        self.assertIn("match_id", fixed_bonus.stdout)

    def test_understat_batch_xg_is_exposed_as_a_read_only_cli_command(self):
        result = self.run_cli("understat-batch-xg", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("one Understat league payload", result.stdout)
        self.assertIn("--rolling-matches", result.stdout)

    def test_pregame_context_command_is_exposed(self):
        result = self.run_cli("pregame-context", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("official_identity_path", result.stdout)

    def test_settlement_queue_command_is_exposed(self):
        result = self.run_cli("settlement-queue", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ledger_path", result.stdout)

    def test_resolve_context_event_command_is_exposed(self):
        result = self.run_cli("resolve-context-event", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("official_identity_path", result.stdout)
        self.assertIn("--alias-registry", result.stdout)

    def test_resolve_context_event_writes_aliases_only_for_adopted_event(self):
        identity = {
            "home_team": "首尔",
            "away_team": "蔚山HD",
            "competition": "K League 1",
            "kickoff_at": "2026-07-27T18:30:00+08:00",
        }
        adopted = {
            "status": "adopted",
            "score": 0.95,
            "source_url": "https://www.sofascore.com/api/v1/event/1",
            "event_url": "https://www.sofascore.com/api/v1/event/1",
            "candidate": {
                "home_team": "FC Seoul", "away_team": "Ulsan HD FC",
                "home_team_id": 10, "away_team_id": 20,
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            identity_path = temporary_path / "identity.json"
            registry_path = temporary_path / "aliases.json"
            identity_path.write_text(json.dumps(identity), encoding="utf-8")
            output = io.StringIO()
            with patch("outputs.tai_cai_tracker.cli.resolve_context_event", return_value=adopted), \
                 patch("sys.stdout", output):
                self.assertEqual(
                    tracker_cli.main([
                        "resolve-context-event", str(identity_path),
                        "--alias-registry", str(registry_path),
                    ]),
                    0,
                )

            result = json.loads(output.getvalue())
            registry = json.loads(registry_path.read_text(encoding="utf-8"))

        self.assertEqual(result["alias_registry_count"], 2)
        self.assertEqual(registry["aliases"]["首尔"]["source_name"], "FC Seoul")

    def test_resolve_links_command_is_exposed(self):
        result = self.run_cli("resolve-links", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("candidate", result.stdout)
        self.assertIn("--alias-registry", result.stdout)

    def test_resolve_links_writes_output_file(self):
        payload = {
            "sporttery": {
                "match_id": 2040623,
                "home_team": "首尔",
                "away_team": "蔚山HD",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
            },
            "oddsportal_candidates": [
                {
                    "home_team": "FC Seoul",
                    "away_team": "Ulsan HD FC",
                    "kickoff_at": "2026-07-27T18:30:00+08:00",
                    "source_url": "https://www.oddsportal.com/example",
                }
            ],
            "home_fotmob_candidates": [
                {
                    "team_name": "FC Seoul",
                    "source_url": "https://www.fotmob.com/teams/seoul",
                }
            ],
            "away_fotmob_candidates": [
                {
                    "team_name": "Ulsan HD FC",
                    "source_url": "https://www.fotmob.com/teams/ulsan",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            input_path = temporary_path / "candidates.json"
            output_path = temporary_path / "resolved" / "2040623.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_cli("resolve-links", str(input_path), "--output", str(output_path))

            self.assertEqual(result.returncode, 0, result.stderr)
            written = json.loads(output_path.read_text(encoding="utf-8"))
            status = json.loads(result.stdout)
            self.assertEqual(written["oddsportal"]["status"], "candidate")
            self.assertEqual(written["fotmob_home"]["status"], "candidate")
            self.assertEqual(status["status"], "written")
            self.assertEqual(Path(status["output_path"]), output_path)

    def test_link_summary_command_is_exposed(self):
        result = self.run_cli("link-summary", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resolved", result.stdout)

    def test_workflow_next_command_is_exposed(self):
        result = self.run_cli("workflow-next", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("match_id", result.stdout)
        self.assertIn("--plain", result.stdout)

    def test_candidate_template_command_is_exposed(self):
        result = self.run_cli("candidate-template", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("match_id", result.stdout)

    def test_candidate_template_writes_output_file(self):
        schedule = {
            "matches": [
                {
                    "match_id": 2040623,
                    "match_num": "周一001",
                    "competition": "K League 1",
                    "home_team": "首尔",
                    "away_team": "蔚山HD",
                    "kickoff_at": "2026-07-27T18:30:00+08:00",
                    "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "candidates" / "2040623.json"
            stdout = io.StringIO()

            with patch("outputs.tai_cai_tracker.resolver.collect_schedule", return_value=schedule):
                with contextlib.redirect_stdout(stdout):
                    exit_code = tracker_cli.main(
                        ["candidate-template", "2040623", "--output", str(output_path)]
                    )

            self.assertEqual(exit_code, 0)
            written = json.loads(output_path.read_text(encoding="utf-8"))
            status = json.loads(stdout.getvalue())
            self.assertEqual(written["sporttery"]["match_id"], 2040623)
            self.assertEqual(written["oddsportal_candidates"], [])
            self.assertEqual(status["status"], "written")
            self.assertEqual(Path(status["output_path"]), output_path)

    def test_odds_import_appends_named_bookmaker_capture(self):
        payload = {
            "provider": "manual-export",
            "captures": [
                {
                    "match_id": "周五201",
                    "kickoff_at": "2026-07-25T00:00:00+08:00",
                    "captured_at": "2026-07-24T14:00:00+08:00",
                    "source_url": "https://odds.example.test/jaro-sjk",
                    "bookmaker": "ExampleBook",
                    "source_quality": "named_bookmaker",
                    "odds": {
                        "home_win": "2.88",
                        "draw": "3.50",
                        "away_win": "2.15",
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            import_path = temporary_path / "odds.json"
            ledger_path = temporary_path / "ledger.jsonl"
            import_path.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_cli("odds-import", str(import_path), str(ledger_path))

            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["record_type"], "odds_capture")
            self.assertEqual(records[0]["status"], "captured")
            self.assertEqual(records[0]["odds"]["away_win"], "2.15")

    def test_snapshot_uses_named_imported_european_odds_as_probability_baseline(self):
        odds_import = {
            "provider": "manual-export",
            "captures": [
                {
                    "match_id": "周五201",
                    "kickoff_at": "2026-07-25T00:00:00+08:00",
                    "captured_at": "2026-07-24T14:00:00+08:00",
                    "source_url": "https://odds.example.test/jaro-sjk",
                    "bookmaker": "ExampleBook",
                    "source_quality": "named_bookmaker",
                    "odds": {"home_win": "2.88", "draw": "3.50", "away_win": "2.15"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            import_path = temporary_path / "odds.json"
            snapshot_path = temporary_path / "snapshot.json"
            ledger_path = temporary_path / "ledger.jsonl"
            import_path.write_text(json.dumps(odds_import), encoding="utf-8")
            imported = self.run_cli("odds-import", str(import_path), str(ledger_path))
            self.assertEqual(imported.returncode, 0, imported.stderr)
            capture_hash = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])["content_hash"]
            snapshot_path.write_text(
                json.dumps(
                    {
                        "market": "SPF",
                        "match_id": "周五201",
                        "kickoff_at": "2026-07-25T00:00:00+08:00",
                        "captured_at": "2026-07-24T15:00:00+08:00",
                        "source_urls": {
                            "S1": "https://official.example.test/jaro-sjk",
                            "S2": "https://odds.example.test/jaro-sjk",
                        },
                        "fixed_prizes": {"home_win": "2", "draw": "4", "away_win": "8"},
                        "european_odds": {"capture_hash": capture_hash},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = self.run_cli("snapshot", str(snapshot_path), str(ledger_path))

            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            record = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[1])
            self.assertEqual(record["outcomes"][0]["probability"], "0.3162163297894692608311972097")
            self.assertEqual(record["european_odds"]["capture_hash"], capture_hash)
            self.assertEqual(
                record["fixed_prizes"],
                {"away_win": "8", "draw": "4", "home_win": "2"},
            )

    def test_snapshot_rejects_unattributed_european_odds_capture(self):
        odds_import = {
            "provider": "manual-export",
            "captures": [
                {
                    "match_id": "周五201",
                    "kickoff_at": "2026-07-25T00:00:00+08:00",
                    "captured_at": "2026-07-24T14:00:00+08:00",
                    "source_url": "https://odds.example.test/jaro-sjk",
                    "bookmaker": None,
                    "source_quality": "unattributed",
                    "odds": {"home_win": "2.88", "draw": "3.50", "away_win": "2.15"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            import_path = temporary_path / "odds.json"
            snapshot_path = temporary_path / "snapshot.json"
            ledger_path = temporary_path / "ledger.jsonl"
            import_path.write_text(json.dumps(odds_import), encoding="utf-8")
            self.assertEqual(
                self.run_cli("odds-import", str(import_path), str(ledger_path)).returncode,
                0,
            )
            capture_hash = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])["content_hash"]
            snapshot_path.write_text(
                json.dumps(
                    {
                        "market": "SPF",
                        "match_id": "周五201",
                        "kickoff_at": "2026-07-25T00:00:00+08:00",
                        "captured_at": "2026-07-24T15:00:00+08:00",
                        "source_urls": {
                            "S1": "https://official.example.test/jaro-sjk",
                            "S2": "https://odds.example.test/jaro-sjk",
                        },
                        "fixed_prizes": {"home_win": "2", "draw": "4", "away_win": "8"},
                        "european_odds": {"capture_hash": capture_hash},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = self.run_cli("snapshot", str(snapshot_path), str(ledger_path))

            self.assertNotEqual(snapshot.returncode, 0)
            self.assertIn("named_bookmaker", snapshot.stderr)

    def test_enhanced_xg_snapshot_freezes_market_model_and_blended_probabilities(self):
        capture = freeze_odds_capture(
            {
                "provider": "manual-export",
                "match_id": "20990101001",
                "kickoff_at": "2099-01-01T20:00:00+08:00",
                "captured_at": "2099-01-01T18:00:00+08:00",
                "source_url": "https://odds.example.test/matches/20990101001",
                "bookmaker": "ExampleBook",
                "source_quality": "named_bookmaker",
                "odds": {"home_win": Decimal("2.00"), "draw": Decimal("3.00"), "away_win": Decimal("6.00")},
            }
        )
        payload = {
            "market": "SPF",
            "match_id": "20990101001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T18:30:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/20990101001",
                "S2": "https://odds.example.test/matches/20990101001",
            },
            "match_context": {
                "competition": "Test League",
                "home_team": "Home FC",
                "away_team": "Away FC",
            },
            "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
            "european_odds": {"capture_hash": capture["content_hash"]},
            "xg_inputs": {
                "source_url": "https://stats.example.test/matches/20990101001",
                "home_xg": "1.2",
                "away_xg": "0.8",
                "model_version": "poisson-v1",
                "model_weight": "0.40",
                "home_handicap": "-1",
            },
        }

        record = prediction_from_input(payload, [capture])
        outcome_probabilities = {
            outcome["label"]: outcome["probability"] for outcome in record["outcomes"]
        }

        self.assertEqual(record["model"]["version"], "poisson-v1")
        self.assertEqual(record["model"]["weight"], Decimal("0.40"))
        self.assertEqual(record["match_context"]["competition"], "Test League")
        self.assertEqual(outcome_probabilities, record["model"]["blended_outcomes"])
        self.assertEqual(len(record["model"]["top_scores"]), 5)
        self.assertIn("handicap_one_x_two", record["model"])
        self.assertEqual(len(record["model"]["half_time_full_time"]), 9)
        self.assertEqual(len(record["model"]["half_time_full_time_proxy"]), 2)
        self.assertEqual(sum(outcome_probabilities.values(), Decimal(0)), Decimal(1))

    def test_enhanced_xg_snapshot_rejects_nonzero_weight_without_named_bookmaker(self):
        payload = {
            "market": "SPF",
            "match_id": "20990101001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T18:30:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/20990101001",
                "S2": "https://market.example.test/matches/20990101001",
            },
            "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
            "xg_inputs": {
                "source_url": "https://stats.example.test/matches/20990101001",
                "home_xg": "1.2",
                "away_xg": "0.8",
                "model_version": "poisson-v1",
                "model_weight": "0.40",
            },
        }

        with self.assertRaisesRegex(ValueError, "named-bookmaker"):
            prediction_from_input(payload)

    def test_enhanced_snapshot_preserves_xg_evidence(self):
        capture = freeze_odds_capture(
            {
                "provider": "manual-export",
                "match_id": "20990101001",
                "kickoff_at": "2099-01-01T20:00:00+08:00",
                "captured_at": "2099-01-01T18:00:00+08:00",
                "source_url": "https://odds.example.test/matches/20990101001",
                "bookmaker": "ExampleBook",
                "source_quality": "named_bookmaker",
                "odds": {"home_win": Decimal("2"), "draw": Decimal("3"), "away_win": Decimal("6")},
            }
        )
        payload = {
            "market": "SPF",
            "match_id": "20990101001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T18:30:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/20990101001",
                "S2": "https://odds.example.test/matches/20990101001",
            },
            "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
            "european_odds": {"capture_hash": capture["content_hash"]},
            "xg_inputs": {
                "source_url": "https://stats.example.test/matches/20990101001",
                "home_xg": "1.2",
                "away_xg": "0.8",
                "model_version": "poisson-v1",
                "model_weight": "0.40",
                "evidence": {
                    "source_url": "https://stats.example.test/teams/season",
                    "published_at": "2099-01-01T17:53:00+08:00",
                    "competition": "Test League",
                    "season": "2099",
                    "sample_matches": 19,
                    "scope": "home_away_split",
                    "home_attack_xg": "31.4",
                    "home_defense_xga": "23.3",
                    "away_attack_xg": "34.3",
                    "away_defense_xga": "26.1",
                    "formula": "mean(home_attack_xg/sample_matches, away_defense_xga/sample_matches)",
                },
            },
        }

        record = prediction_from_input(payload, [capture])

        evidence = record["xg_inputs"]["evidence"]
        self.assertEqual(evidence["source_url"], "https://stats.example.test/teams/season")
        self.assertEqual(evidence["sample_matches"], 19)
        self.assertEqual(evidence["home_attack_xg"], Decimal("31.4"))
        self.assertEqual(evidence["away_defense_xga"], Decimal("26.1"))
        self.assertEqual(evidence["scope"], "home_away_split")
        self.assertNotIn("confidence_ceiling", record["xg_inputs"])

        rolling_payload = deepcopy(payload)
        rolling_evidence = rolling_payload["xg_inputs"]["evidence"]
        rolling_evidence.pop("published_at")
        rolling_evidence.pop("sample_matches")
        rolling_evidence.update({
            "captured_at": "2099-01-01T17:53:00+08:00",
            "published_at_unavailable_reason": "Understat league response does not expose a publication time",
            "home_sample_matches": 8,
            "away_sample_matches": 8,
            "sample_window_matches": 8,
            "history_cutoff_date": "2099-01-01",
            "home_sample_first_date": "2098-11-01",
            "home_sample_last_date": "2098-12-28",
            "away_sample_first_date": "2098-11-03",
            "away_sample_last_date": "2098-12-29",
        })
        rolling = prediction_from_input(rolling_payload, [capture])
        self.assertEqual(rolling["xg_inputs"]["evidence"]["sample_window_matches"], 8)
        self.assertEqual(rolling["xg_inputs"]["evidence"]["history_cutoff_date"], "2099-01-01")

        league_total_payload = deepcopy(payload)
        league_total_payload["xg_inputs"]["evidence"]["scope"] = "league_total"
        league_total = prediction_from_input(league_total_payload, [capture])
        self.assertEqual(league_total["xg_inputs"]["confidence_ceiling"], "medium")

        invalid_scope = deepcopy(payload)
        invalid_scope["xg_inputs"]["evidence"]["scope"] = "unknown"
        with self.assertRaisesRegex(ValueError, "scope"):
            prediction_from_input(invalid_scope, [capture])

        naive_publication = deepcopy(payload)
        naive_publication["xg_inputs"]["evidence"]["published_at"] = "2099-01-01T17:53:00"
        with self.assertRaisesRegex(ValueError, "published_at"):
            prediction_from_input(naive_publication, [capture])

        late_publication = deepcopy(payload)
        late_publication["xg_inputs"]["evidence"]["published_at"] = "2099-01-01T18:31:00+08:00"
        with self.assertRaisesRegex(ValueError, "postdate"):
            prediction_from_input(late_publication, [capture])

    def test_read_records_preserves_derived_medium_confidence_for_league_total_xg(self):
        payload = {
            "market": "SPF",
            "match_id": "20990101001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T18:30:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/20990101001",
                "S2": "https://market.example.test/matches/20990101001",
            },
            "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
            "xg_inputs": {
                "source_url": "https://stats.example.test/matches/20990101001",
                "home_xg": "1.2",
                "away_xg": "0.8",
                "model_version": "poisson-v1",
                "model_weight": "0",
                "evidence": {
                    "source_url": "https://stats.example.test/teams/season",
                    "published_at": "2099-01-01T17:53:00+08:00",
                    "competition": "Test League",
                    "season": "2099",
                    "sample_matches": 19,
                    "scope": "league_total",
                    "home_attack_xg": "31.4",
                    "home_defense_xga": "23.3",
                    "away_attack_xg": "34.3",
                    "away_defense_xga": "26.1",
                    "formula": "mean(home_attack_xg/sample_matches, away_defense_xga/sample_matches)",
                },
            },
        }
        frozen = freeze_prediction(prediction_from_input(payload))

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text(json.dumps(frozen, default=str) + "\n", encoding="utf-8")

            records = read_records(ledger_path)

        self.assertEqual(records[0]["xg_inputs"]["confidence_ceiling"], "medium")

    def test_prediction_rejects_non_medium_confidence_for_league_total_xg(self):
        payload = {
            "market": "SPF",
            "match_id": "20990101001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T18:30:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/20990101001",
                "S2": "https://market.example.test/matches/20990101001",
            },
            "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
            "xg_inputs": {
                "source_url": "https://stats.example.test/matches/20990101001",
                "home_xg": "1.2",
                "away_xg": "0.8",
                "model_version": "poisson-v1",
                "model_weight": "0",
                "confidence_ceiling": "high",
                "evidence": {
                    "source_url": "https://stats.example.test/teams/season",
                    "published_at": "2099-01-01T17:53:00+08:00",
                    "competition": "Test League",
                    "season": "2099",
                    "sample_matches": 19,
                    "scope": "league_total",
                    "home_attack_xg": "31.4",
                    "home_defense_xga": "23.3",
                    "away_attack_xg": "34.3",
                    "away_defense_xga": "26.1",
                    "formula": "mean(home_attack_xg/sample_matches, away_defense_xga/sample_matches)",
                },
            },
        }

        with self.assertRaisesRegex(ValueError, "confidence_ceiling"):
            prediction_from_input(payload)

    def test_prediction_accepts_separate_home_and_away_xg_sample_sizes(self):
        payload = {
            "market": "SPF",
            "match_id": "20990101001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T18:30:00+08:00",
            "source_urls": {"S1": "https://official.example.test/1", "S2": "https://market.example.test/1"},
            "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
            "xg_inputs": {
                "source_url": "https://stats.example.test/teams/1",
                "home_xg": "1.2", "away_xg": "1.6", "model_version": "poisson-v1", "model_weight": "0",
                "evidence": {
                    "source_url": "https://stats.example.test/season", "published_at": "2099-01-01T17:53:00+08:00",
                    "competition": "Test League", "season": "2099", "home_sample_matches": 24, "away_sample_matches": 23,
                    "scope": "league_total", "home_attack_xg": "30.8", "home_defense_xga": "44.1",
                    "away_attack_xg": "34.3", "away_defense_xga": "26.1", "formula": "per-team denominators",
                },
            },
        }

        result = prediction_from_input(payload)

        self.assertEqual(result["xg_inputs"]["evidence"]["home_sample_matches"], 24)
        self.assertEqual(result["xg_inputs"]["evidence"]["away_sample_matches"], 23)

    def test_prediction_rejects_medium_confidence_for_home_away_split_xg(self):
        payload = {
            "market": "SPF",
            "match_id": "20990101001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T18:30:00+08:00",
            "source_urls": {
                "S1": "https://official.example.test/matches/20990101001",
                "S2": "https://market.example.test/matches/20990101001",
            },
            "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
            "xg_inputs": {
                "source_url": "https://stats.example.test/matches/20990101001",
                "home_xg": "1.2",
                "away_xg": "0.8",
                "model_version": "poisson-v1",
                "model_weight": "0",
                "confidence_ceiling": "medium",
                "evidence": {
                    "source_url": "https://stats.example.test/teams/season",
                    "published_at": "2099-01-01T17:53:00+08:00",
                    "competition": "Test League",
                    "season": "2099",
                    "sample_matches": 19,
                    "scope": "home_away_split",
                    "home_attack_xg": "31.4",
                    "home_defense_xga": "23.3",
                    "away_attack_xg": "34.3",
                    "away_defense_xga": "26.1",
                    "formula": "mean(home_attack_xg/sample_matches, away_defense_xga/sample_matches)",
                },
            },
        }

        with self.assertRaisesRegex(ValueError, "confidence_ceiling"):
            prediction_from_input(payload)

    def test_prediction_accepts_medium_confidence_source_match_model_xg(self):
        payload = {
            "market": "SPF",
            "match_id": "20990101001",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "captured_at": "2099-01-01T18:30:00+08:00",
            "source_urls": {"S1": "https://official.example.test/1", "S2": "https://market.example.test/1"},
            "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
            "xg_inputs": {
                "source_url": "https://xgscore.io/test/home-away/xgscore",
                "home_xg": "1.95", "away_xg": "1.35", "model_version": "poisson-v1", "model_weight": "0",
                "confidence_ceiling": "medium",
                "evidence": {
                    "source_url": "https://xgscore.io/test/home-away/xgscore",
                    "captured_at": "2099-01-01T18:00:00+08:00",
                    "published_at_unavailable_reason": "source does not expose publication time",
                    "competition": "Champions League", "season": "2099/2100",
                    "home_sample_matches": 13, "away_sample_matches": 13,
                    "scope": "source_match_model", "home_attack_xg": "2.05", "home_defense_xga": "0.99",
                    "away_attack_xg": "1.93", "away_defense_xga": "1.58", "formula": "source_match_model: source forecastScore",
                },
            },
        }

        result = prediction_from_input(payload)

        self.assertEqual(result["xg_inputs"]["confidence_ceiling"], "medium")
        self.assertEqual(result["xg_inputs"]["evidence"]["scope"], "source_match_model")

    def test_all_market_snapshot_accepts_complete_official_prize_tables(self):
        capture = {
            "provider": "manual-export",
            "captures": [
                {
                    "match_id": "20990101001",
                    "kickoff_at": "2099-01-01T20:00:00+08:00",
                    "captured_at": "2099-01-01T18:00:00+08:00",
                    "source_url": "https://odds.example.test/matches/20990101001",
                    "bookmaker": "ExampleBook",
                    "source_quality": "named_bookmaker",
                    "odds": {"home_win": "2", "draw": "3", "away_win": "6"},
                }
            ],
        }
        complete_prizes = {
            code: {
                option: str(Decimal(index + 2))
                for index, option in enumerate(options)
            }
            for code, options in ALL_MARKET_OPTIONS.items()
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            ledger_path = temporary_path / "ledger.jsonl"
            capture_path = temporary_path / "capture.json"
            prediction_path = temporary_path / "prediction.json"
            markets_path = temporary_path / "markets.json"
            capture_path.write_text(json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                self.run_cli("odds-import", str(capture_path), str(ledger_path)).returncode,
                0,
            )
            capture_hash = json.loads(
                ledger_path.read_text(encoding="utf-8").splitlines()[0]
            )["content_hash"]
            prediction_path.write_text(
                json.dumps(
                    {
                        "market": "SPF",
                        "match_id": "20990101001",
                        "kickoff_at": "2099-01-01T20:00:00+08:00",
                        "captured_at": "2099-01-01T18:30:00+08:00",
                        "source_urls": {
                            "S1": "https://official.example.test/matches/20990101001",
                            "S2": "https://odds.example.test/matches/20990101001",
                        },
                        "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
                        "european_odds": {"capture_hash": capture_hash},
                        "xg_inputs": {
                            "source_url": "https://stats.example.test/matches/20990101001",
                            "home_xg": "1.2",
                            "away_xg": "0.8",
                            "model_version": "poisson-v1",
                            "model_weight": "0.40",
                        },
                    }
                ),
                encoding="utf-8",
            )
            prediction_result = self.run_cli(
                "snapshot", str(prediction_path), str(ledger_path)
            )
            self.assertEqual(prediction_result.returncode, 0, prediction_result.stderr)
            prediction_hash = json.loads(prediction_result.stdout)["content_hash"]

            official_markets = {
                code: {
                    "published_at": "2099-01-01T17:59:00+08:00",
                    "official_source_url": "https://official.example.test/matches/20990101001",
                    "fixed_prizes": complete_prizes[code],
                    **({"home_handicap": "-1"} if code == "hhad" else {}),
                }
                for code in ALL_MARKET_OPTIONS
            }
            markets_path.write_text(
                json.dumps(
                    {
                        "prediction_hash": prediction_hash,
                        "captured_at": "2099-01-01T18:31:00+08:00",
                        "official_source_url": "https://official.example.test/matches/20990101001",
                        "official_markets": official_markets,
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                "all-market-snapshot", str(markets_path), str(ledger_path)
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            snapshot = json.loads(result.stdout)
            records = read_records(ledger_path)
            self.assertEqual(records[-1]["content_hash"], snapshot["content_hash"])
            self.assertEqual(snapshot["schema_version"], 2)
            self.assertEqual(set(snapshot["markets"]), set(ALL_MARKET_OPTIONS))
            for code, options in ALL_MARKET_OPTIONS.items():
                entry = snapshot["markets"][code]
                self.assertEqual(entry["status"], "frozen")
                self.assertEqual(set(entry["official_fixed_prizes"]), set(options))
                self.assertEqual(
                    entry["official_source_url"],
                    "https://official.example.test/matches/20990101001",
                )
            self.assertEqual(len(snapshot["markets"]["crs"]["official_fixed_prizes"]), 31)
            self.assertEqual(len(snapshot["markets"]["ttg"]["official_fixed_prizes"]), 8)
            self.assertEqual(len(snapshot["markets"]["hafu"]["official_fixed_prizes"]), 9)

    def test_all_market_snapshot_records_unavailable_market_as_skipped(self):
        prediction = freeze_prediction(
            prediction_from_input(
                {
                    "market": "SPF",
                    "match_id": "20990101001",
                    "kickoff_at": "2099-01-01T20:00:00+08:00",
                    "captured_at": "2099-01-01T18:30:00+08:00",
                    "source_urls": {
                        "S1": "https://official.example.test/matches/20990101001",
                        "S2": "https://market.example.test/matches/20990101001",
                    },
                    "fixed_prizes": {"home_win": "2", "draw": "3", "away_win": "6"},
                    "xg_inputs": {
                        "source_url": "https://stats.example.test/matches/20990101001",
                        "home_xg": "1.2",
                        "away_xg": "0.8",
                        "model_version": "poisson-v1",
                        "model_weight": "0",
                    },
                }
            )
        )
        full_prizes = {
            code: {
                option: str(Decimal(index + 2))
                for index, option in enumerate(options)
            }
            for code, options in ALL_MARKET_OPTIONS.items()
        }
        official_markets = {
            code: {
                "published_at": "2099-01-01T17:59:00+08:00",
                "official_source_url": "https://official.example.test/matches/20990101001",
                "fixed_prizes": full_prizes[code],
                **({"home_handicap": "-1"} if code == "hhad" else {}),
            }
            for code in ALL_MARKET_OPTIONS
        }
        official_markets["hafu"] = {
            "status": "skipped",
            "reason": "official half-time/full-time prizes unavailable",
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            ledger_path = temporary_path / "ledger.jsonl"
            input_path = temporary_path / "markets.json"
            ledger_path.write_text(
                json.dumps(prediction, default=str) + "\n", encoding="utf-8"
            )
            input_path.write_text(
                json.dumps(
                    {
                        "prediction_hash": prediction["content_hash"],
                        "captured_at": "2099-01-01T18:31:00+08:00",
                        "official_source_url": "https://official.example.test/matches/20990101001",
                        "official_markets": official_markets,
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                "all-market-snapshot", str(input_path), str(ledger_path)
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            snapshot = json.loads(result.stdout)
            self.assertEqual(snapshot["markets"]["hafu"], official_markets["hafu"])
            self.assertEqual(snapshot["markets"]["crs"]["status"], "frozen")

    def test_settle_appends_a_settlement_linked_to_frozen_prediction(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            snapshot = self.run_cli("snapshot", str(EXAMPLE_INPUT), str(ledger_path))
            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            prediction = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])

            settlement = self.run_cli(
                "settle",
                str(ledger_path),
                prediction["content_hash"],
                "home_win",
                "https://www.lottery.gov.cn/example-results/20990101001",
            )

            self.assertEqual(settlement.returncode, 0, settlement.stderr)
            records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1]["record_type"], "settlement")
            self.assertEqual(records[1]["prediction_hash"], prediction["content_hash"])

    def test_settle_optionally_records_final_score(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            snapshot = self.run_cli("snapshot", str(EXAMPLE_INPUT), str(ledger_path))
            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            prediction = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])

            settlement = self.run_cli(
                "settle",
                str(ledger_path),
                prediction["content_hash"],
                "home_win",
                "https://www.lottery.gov.cn/example-results/20990101001",
                "--final-score",
                "2",
                "0",
            )

            self.assertEqual(settlement.returncode, 0, settlement.stderr)
            records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[1]["final_score"], {"home": 2, "away": 0})

    def test_report_says_calibration_is_unavailable_below_fifty_settlements(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            self.assertEqual(
                self.run_cli("snapshot", str(EXAMPLE_INPUT), str(ledger_path)).returncode,
                0,
            )
            prediction = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(
                self.run_cli(
                    "settle",
                    str(ledger_path),
                    prediction["content_hash"],
                    "home_win",
                    "https://www.lottery.gov.cn/example-results/20990101001",
                ).returncode,
                0,
            )

            report = self.run_cli("report", str(ledger_path))

            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertIn("Calibration unavailable", report.stdout)
            self.assertIn("50", report.stdout)

    def test_format_report_includes_probability_metrics_and_strategy_breakdown(self):
        rendered = format_report(
            {
                "available": True,
                "settled_records": 50,
                "brier_average": Decimal("0.38"),
                "log_loss_average": Decimal("0.69"),
                "expected_calibration_error": Decimal("0.05"),
                "phase": "observational",
                "strategy_breakdown": {
                    "market_baseline": {
                        "settled_records": 50,
                        "brier_average": Decimal("0.38"),
                        "log_loss_average": Decimal("0.69"),
                        "top_pick_accuracy": Decimal("0.56"),
                    }
                },
                "competition_breakdown": {
                    "Test League": {
                        "settled_records": 50,
                        "brier_average": Decimal("0.38"),
                        "log_loss_average": Decimal("0.69"),
                        "top_pick_accuracy": Decimal("0.56"),
                    }
                },
                "temporal_weight_evaluation": {
                    "available": True,
                    "enhanced_settled_records": 200,
                    "training_records": 140,
                    "holdout_records": 60,
                    "selected_weight": Decimal("0.4"),
                    "training_log_loss": Decimal("0.61"),
                    "holdout_log_loss": Decimal("0.62"),
                    "market_holdout_log_loss": Decimal("0.70"),
                    "model_holdout_log_loss": Decimal("0.65"),
                },
                "walk_forward_weight_evaluation": {
                    "available": True,
                    "enhanced_settled_records": 200,
                    "fold_count": 60,
                    "walk_forward_log_loss": Decimal("0.60"),
                    "market_walk_forward_log_loss": Decimal("0.70"),
                    "model_walk_forward_log_loss": Decimal("0.65"),
                },
                "evidence_event_counts": {
                    "source_observation": 2,
                    "collection_failure": 1,
                    "by_evidence_kind": {"european_1x2": 2, "xg": 1},
                },
            }
        )

        self.assertIn("Log loss 0.69", rendered)
        self.assertIn("ECE 0.05", rendered)
        self.assertIn("market_baseline", rendered)
        self.assertIn("Test League", rendered)
        self.assertIn("selected weight 0.4", rendered)
        self.assertIn("Walk-forward weight evaluation: 60 folds", rendered)
        self.assertIn("Evidence events: 2 observations; 1 failures", rendered)

    def test_format_report_shows_temporal_sample_gates_before_calibration(self):
        rendered = format_report({
            "available": False,
            "settled_records": 9,
            "minimum_settled_records": 50,
            "all_market_breakdown": {},
            "strategy_breakdown": {},
            "evidence_event_counts": {"source_observation": 0, "collection_failure": 0},
            "temporal_weight_evaluation": {
                "available": False,
                "enhanced_settled_records": 2,
                "minimum_enhanced_settled_records": 200,
            },
            "walk_forward_weight_evaluation": {
                "available": False,
                "enhanced_settled_records": 2,
                "minimum_enhanced_settled_records": 200,
            },
        })

        self.assertIn("Temporal weight evaluation unavailable: 2 enhanced settlements", rendered)
        self.assertIn("Walk-forward weight evaluation unavailable: 2 enhanced settlements", rendered)


if __name__ == "__main__":
    unittest.main()
