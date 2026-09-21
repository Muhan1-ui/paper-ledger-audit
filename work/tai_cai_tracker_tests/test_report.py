import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from outputs.tai_cai_tracker.ledger import (
    ALL_MARKET_OPTIONS,
    freeze_all_market_snapshot,
    freeze_evidence_event,
    freeze_odds_capture,
    freeze_prediction,
    settle_prediction,
)
from outputs.tai_cai_tracker.report import (
    all_market_strategy_breakdown,
    brier_score,
    calibration_report,
    log_loss,
    scoreline_log_loss,
    walk_forward_weight_evaluation,
)


SYNTHETIC_PREDICTION = {
    "market": "SPF",
    "match_id": "20990101001",
    "kickoff_at": "2099-01-01T20:00:00+08:00",
    "captured_at": "2099-01-01T18:00:00+08:00",
    "source_urls": {
        "S1": "https://official.example.test/matches/20990101001",
        "S2": "https://market.example.test/matches/20990101001",
    },
    "outcomes": [
        {"label": "home_win", "probability": Decimal("0.50")},
        {"label": "draw", "probability": Decimal("0.30")},
        {"label": "away_win", "probability": Decimal("0.20")},
    ],
}


class SettlementAndReportTests(unittest.TestCase):
    def setUp(self):
        self.prediction = freeze_prediction(SYNTHETIC_PREDICTION)
        self.settlement = settle_prediction(
            self.prediction,
            actual_outcome="home_win",
            official_result_url="https://official.example.test/results/20990101001",
        )

    def test_settlement_links_frozen_record_and_has_three_class_brier_contribution(self):
        self.assertEqual(self.settlement["record_type"], "settlement")
        self.assertEqual(self.settlement["prediction_hash"], self.prediction["content_hash"])
        self.assertEqual(self.settlement["actual_outcome"], "home_win")
        self.assertEqual(brier_score(self.prediction, self.settlement), Decimal("0.38"))

    def test_settlement_optionally_freezes_an_official_final_score(self):
        settlement = settle_prediction(
            self.prediction,
            actual_outcome="home_win",
            official_result_url="https://official.example.test/results/20990101001",
            final_score={"home": 2, "away": 0},
        )

        self.assertEqual(settlement["final_score"], {"home": 2, "away": 0})

        with self.assertRaises(ValueError):
            settle_prediction(
                self.prediction,
                actual_outcome="home_win",
                official_result_url="https://official.example.test/results/20990101001",
                final_score={"home": -1, "away": 0},
            )

    def test_scoreline_log_loss_uses_frozen_model_probability_when_score_is_available(self):
        prediction_input = deepcopy(SYNTHETIC_PREDICTION)
        prediction_input["model"] = {
            "scorelines": [
                {"home_goals": 2, "away_goals": 0, "probability": Decimal("0.1")}
            ]
        }
        prediction = freeze_prediction(prediction_input)
        settlement = settle_prediction(
            prediction,
            actual_outcome="home_win",
            official_result_url="https://official.example.test/results/20990101001",
            final_score={"home": 2, "away": 0},
        )

        self.assertEqual(scoreline_log_loss(prediction, settlement), -Decimal("0.1").ln())

    def test_calibration_is_unavailable_below_fifty_settled_predictions(self):
        report = calibration_report([self.prediction, self.settlement])

        self.assertFalse(report["available"])
        self.assertEqual(report["settled_records"], 1)
        self.assertEqual(report["minimum_settled_records"], 50)
        self.assertNotIn("brier_average", report)
        self.assertFalse(report["walk_forward_weight_evaluation"]["available"])
        self.assertEqual(
            report["walk_forward_weight_evaluation"]["enhanced_settled_records"],
            0,
        )

    def test_all_market_report_keeps_official_market_baselines_separate(self):
        def snapshot(strategy=None):
            markets = {}
            for code, options in ALL_MARKET_OPTIONS.items():
                probabilities = {
                    option: Decimal(1) if index == 0 else Decimal(0)
                    for index, option in enumerate(options)
                }
                entry = {
                    "status": "frozen",
                    "probabilities": probabilities,
                    "selected_option": options[0],
                    "selected_probability": Decimal(1),
                    "tied_options": [options[0]],
                    "official_fixed_prizes": {option: Decimal(2) for option in options},
                    "published_at": "2099-01-01T10:00:00+08:00",
                    "official_source_url": "https://official.example.test/matches/20990101001",
                }
                if code == "hhad":
                    entry["home_handicap"] = Decimal("-1")
                markets[code] = entry
            payload = {
                "schema_version": 3 if strategy else 2,
                "prediction_hash": "a" * 64 if strategy else "b" * 64,
                "match_id": "20990101001" if strategy else "20990101002",
                "kickoff_at": "2099-01-01T14:00:00+08:00",
                "captured_at": "2099-01-01T12:00:00+08:00",
                "official_source_url": "https://official.example.test/matches/20990101001",
                "markets": markets,
            }
            if strategy:
                payload["snapshot_strategy"] = strategy
            return freeze_all_market_snapshot(payload)

        breakdown = all_market_strategy_breakdown([
            snapshot(), snapshot("official_market_baseline"),
        ])

        self.assertEqual(set(breakdown), {"model_derived", "official_market_baseline"})
        self.assertEqual(breakdown["model_derived"]["spf"]["frozen_records"], 1)
        self.assertEqual(
            breakdown["official_market_baseline"]["spf"]["frozen_records"], 1,
        )

    def test_calibration_ignores_valid_odds_capture_records(self):
        odds_capture = freeze_odds_capture(
            {
                "provider": "test-export",
                "match_id": "周五201",
                "kickoff_at": "2026-07-25T00:00:00+08:00",
                "captured_at": "2026-07-24T14:00:00+08:00",
                "source_url": "https://odds.example.test/jaro-sjk",
                "bookmaker": "ExampleBook",
                "source_quality": "named_bookmaker",
                "odds": {
                    "home_win": Decimal("2.88"),
                    "draw": Decimal("3.50"),
                    "away_win": Decimal("2.15"),
                },
            }
        )

        report = calibration_report([odds_capture, self.prediction, self.settlement])

        self.assertFalse(report["available"])
        self.assertEqual(report["settled_records"], 1)

    def test_calibration_excludes_comparison_predictions_from_aggregate(self):
        primary = freeze_prediction(SYNTHETIC_PREDICTION)
        comparison_input = deepcopy(SYNTHETIC_PREDICTION)
        comparison_input["prediction_role"] = "comparison"
        comparison = freeze_prediction(comparison_input)
        records = [
            primary,
            settle_prediction(primary, "home_win", "https://official.example.test/results/primary"),
            comparison,
            settle_prediction(comparison, "away_win", "https://official.example.test/results/comparison"),
        ]

        report = calibration_report(records)

        self.assertEqual(report["settled_records"], 1)

    def test_calibration_reports_average_brier_at_fifty_settled_predictions(self):
        records = []
        for number in range(1, 51):
            synthetic_prediction = deepcopy(SYNTHETIC_PREDICTION)
            synthetic_prediction["match_id"] = f"20990101{number:03d}"
            prediction = freeze_prediction(synthetic_prediction)
            settlement = settle_prediction(
                prediction,
                actual_outcome="home_win",
                official_result_url=(
                    f"https://official.example.test/results/{synthetic_prediction['match_id']}"
                ),
            )
            records.extend((prediction, settlement))

        report = calibration_report(records)

        self.assertTrue(report["available"])
        self.assertEqual(report["settled_records"], 50)
        self.assertEqual(report["brier_average"], Decimal("0.38"))

    def test_calibration_reports_log_loss_ece_and_observational_phase_at_fifty(self):
        records = []
        for number in range(1, 51):
            synthetic_prediction = deepcopy(SYNTHETIC_PREDICTION)
            synthetic_prediction["match_id"] = f"20990101{number:03d}"
            prediction = freeze_prediction(synthetic_prediction)
            settlement = settle_prediction(
                prediction,
                actual_outcome="home_win",
                official_result_url=(
                    f"https://official.example.test/results/{synthetic_prediction['match_id']}"
                ),
            )
            records.extend((prediction, settlement))

        report = calibration_report(records)

        self.assertEqual(log_loss(self.prediction, self.settlement), -Decimal("0.50").ln())
        self.assertEqual(report["log_loss_average"], -Decimal("0.50").ln())
        self.assertEqual(report["expected_calibration_error"], Decimal("0.50"))
        self.assertEqual(report["phase"], "observational")
        self.assertIn("confidence_buckets", report)

    def test_calibration_groups_market_and_poisson_records_descriptively(self):
        records = []
        for number in range(1, 51):
            prediction_input = deepcopy(SYNTHETIC_PREDICTION)
            prediction_input["match_id"] = f"20990101{number:03d}"
            prediction_input["match_context"] = {
                "competition": "Test League",
                "home_team": "Home FC",
                "away_team": "Away FC",
            }
            prediction = freeze_prediction(prediction_input)
            records.extend(
                (
                    prediction,
                    settle_prediction(
                        prediction,
                        actual_outcome="home_win",
                        official_result_url=(
                            f"https://official.example.test/results/{prediction_input['match_id']}"
                        ),
                    ),
                )
            )

        enhanced_input = deepcopy(SYNTHETIC_PREDICTION)
        enhanced_input["match_id"] = "20990101051"
        enhanced_input["model"] = {
            "version": "poisson-v1",
            "weight": Decimal("0.40"),
            "scorelines": [],
        }
        enhanced_prediction = freeze_prediction(enhanced_input)
        records.extend(
            (
                enhanced_prediction,
                settle_prediction(
                    enhanced_prediction,
                    actual_outcome="home_win",
                    official_result_url="https://official.example.test/results/20990101051",
                ),
            )
        )

        report = calibration_report(records)

        baseline = report["strategy_breakdown"]["market_baseline"]
        poisson = report["strategy_breakdown"]["poisson-v1@0.40"]
        self.assertEqual(baseline["settled_records"], 50)
        self.assertEqual(poisson["settled_records"], 1)
        self.assertEqual(poisson["top_pick_accuracy"], Decimal(1))
        self.assertEqual(
            report["competition_breakdown"]["Test League"]["settled_records"], 50
        )

    def test_temporal_weight_evaluation_requires_two_hundred_enhanced_settlements(self):
        records = []
        for number in range(1, 201):
            prediction_input = deepcopy(SYNTHETIC_PREDICTION)
            prediction_input["match_id"] = f"20990102{number:03d}"
            prediction_input["model"] = {
                "version": "poisson-v1",
                "weight": Decimal("0.40"),
                "market_outcomes": {
                    "home_win": Decimal("0.10"),
                    "draw": Decimal("0.20"),
                    "away_win": Decimal("0.70"),
                },
                "model_outcomes": {
                    "home_win": Decimal("0.80"),
                    "draw": Decimal("0.10"),
                    "away_win": Decimal("0.10"),
                },
                "scorelines": [],
            }
            prediction = freeze_prediction(prediction_input)
            records.extend(
                (
                    prediction,
                    settle_prediction(
                        prediction,
                        actual_outcome="home_win",
                        official_result_url=(
                            f"https://official.example.test/results/{prediction_input['match_id']}"
                        ),
                    ),
                )
            )

        report = calibration_report(records)

        evaluation = report["temporal_weight_evaluation"]
        self.assertTrue(evaluation["available"])
        self.assertEqual(evaluation["enhanced_settled_records"], 200)
        self.assertEqual(evaluation["selected_weight"], Decimal("1.0"))

    def test_temporal_weight_evaluation_excludes_comparison_predictions_from_gate(self):
        records = []
        primary_input = deepcopy(SYNTHETIC_PREDICTION)
        primary_input["model"] = {
            "version": "poisson-v1",
            "weight": Decimal("0.40"),
            "market_outcomes": {
                "home_win": Decimal("0.10"), "draw": Decimal("0.20"), "away_win": Decimal("0.70"),
            },
            "model_outcomes": {
                "home_win": Decimal("0.80"), "draw": Decimal("0.10"), "away_win": Decimal("0.10"),
            },
            "scorelines": [],
        }
        primary = freeze_prediction(primary_input)
        records.extend((
            primary,
            settle_prediction(primary, "home_win", "https://official.example.test/results/primary"),
        ))
        for number in range(1, 201):
            prediction_input = deepcopy(SYNTHETIC_PREDICTION)
            prediction_input["match_id"] = f"209903{number:05d}"
            prediction_input["prediction_role"] = "comparison"
            prediction_input["model"] = {
                "version": "poisson-v1",
                "weight": Decimal("0.40"),
                "market_outcomes": {
                    "home_win": Decimal("0.10"),
                    "draw": Decimal("0.20"),
                    "away_win": Decimal("0.70"),
                },
                "model_outcomes": {
                    "home_win": Decimal("0.80"),
                    "draw": Decimal("0.10"),
                    "away_win": Decimal("0.10"),
                },
                "scorelines": [],
            }
            prediction = freeze_prediction(prediction_input)
            records.extend((
                prediction,
                settle_prediction(
                    prediction,
                    actual_outcome="home_win",
                    official_result_url=f"https://official.example.test/results/{number}",
                ),
            ))

        report = calibration_report(records)

        evaluation = report["temporal_weight_evaluation"]
        self.assertFalse(evaluation["available"])
        self.assertEqual(evaluation["enhanced_settled_records"], 1)

    def test_walk_forward_uses_only_prior_frozen_predictions_for_each_fold(self):
        records = []
        base_capture = datetime(2099, 1, 1, 8, tzinfo=timezone.utc)
        for number in range(1, 201):
            prediction_input = deepcopy(SYNTHETIC_PREDICTION)
            prediction_input["match_id"] = f"209901{number:05d}"
            captured_at = base_capture + timedelta(hours=number)
            prediction_input["captured_at"] = captured_at.isoformat()
            prediction_input["kickoff_at"] = (captured_at + timedelta(hours=2)).isoformat()
            prediction_input["model"] = {
                "version": "poisson-v1",
                "weight": Decimal("0.40"),
                "market_outcomes": {
                    "home_win": Decimal("0.10"),
                    "draw": Decimal("0.20"),
                    "away_win": Decimal("0.70"),
                },
                "model_outcomes": {
                    "home_win": Decimal("0.80"),
                    "draw": Decimal("0.10"),
                    "away_win": Decimal("0.10"),
                },
                "scorelines": [],
            }
            prediction = freeze_prediction(prediction_input)
            records.extend((
                prediction,
                settle_prediction(
                    prediction,
                    actual_outcome="home_win",
                    official_result_url=(
                        f"https://official.example.test/results/{prediction_input['match_id']}"
                    ),
                ),
            ))

        pairs = [(records[index], records[index + 1]) for index in range(0, len(records), 2)]
        evaluation = walk_forward_weight_evaluation(pairs)

        self.assertTrue(evaluation["available"])
        self.assertEqual(evaluation["fold_count"], 60)
        first_fold = evaluation["folds"][0]
        self.assertEqual(len(first_fold["training_prediction_hashes"]), 140)
        self.assertNotIn(first_fold["test_prediction_hash"], first_fold["training_prediction_hashes"])
        self.assertEqual(first_fold["selected_weight"], Decimal("1.0"))

    def test_calibration_counts_evidence_events_without_counting_them_as_settlements(self):
        observation = freeze_evidence_event({
            "record_type": "source_observation",
            "match_id": "20990101001",
            "evidence_kind": "european_1x2",
            "source_url": "https://odds.example.test/matches/20990101001",
            "captured_at": "2099-01-01T18:00:00+08:00",
            "published_at": "2099-01-01T17:59:00+08:00",
            "collector": "test-collector",
            "parser_version": "1.0",
            "raw_content_sha256": "c" * 64,
            "observed_facts": {"bookmaker": "ExampleBook"},
        })

        report = calibration_report([observation, self.prediction, self.settlement])

        self.assertEqual(report["settled_records"], 1)
        self.assertEqual(report["evidence_event_counts"]["source_observation"], 1)
        self.assertEqual(report["evidence_event_counts"]["collection_failure"], 0)
        self.assertEqual(report["evidence_event_counts"]["by_evidence_kind"]["european_1x2"], 1)

    def test_source_performance_groups_frozen_provider_domain_and_context(self):
        prediction_input = deepcopy(SYNTHETIC_PREDICTION)
        prediction_input["european_odds"] = {"provider": "OddsPortal"}
        prediction_input["xg_inputs"] = {
            "source_url": "https://www.fotmob.com/teams/1/stats/home",
            "evidence": {"source_url": "https://www.fotmob.com/teams/1/stats/home"},
        }
        prediction_input["pregame_context"] = {
            "split_xg": {"status": "unavailable", "reason": "source_unavailable"},
            "weather": {"status": "unavailable", "reason": "venue_unavailable"},
            "lineup": {"status": "unavailable", "reason": "lineup_unavailable"},
            "risk_summary": {
                "confidence_ceiling": "high",
                "split_xg_status": "unavailable",
                "lineup_status": "unavailable",
            },
        }
        prediction = freeze_prediction(prediction_input)
        settlement = settle_prediction(
            prediction,
            "home_win",
            "https://official.example.test/results/source-performance",
        )
        observation = freeze_evidence_event({
            "record_type": "source_observation",
            "match_id": "20990101001",
            "evidence_kind": "european_1x2",
            "source_url": "https://www.oddsportal.com/match",
            "captured_at": "2099-01-01T18:00:00+08:00",
            "published_at_unavailable_reason": "not shown",
            "collector": "oddsportal-extract",
            "parser_version": "1.0",
            "raw_content_sha256": "d" * 64,
            "observed_facts": {"provider": "OddsPortal"},
        })
        failure = freeze_evidence_event({
            "record_type": "collection_failure",
            "match_id": "20990101001",
            "evidence_kind": "xg",
            "attempted_at": "2099-01-01T18:01:00+08:00",
            "collector": "fotmob-xg-evidence",
            "parser_version": "1.0",
            "reason_code": "network_error",
            "reason": "public response timed out",
        })

        report = calibration_report([observation, failure, prediction, settlement])
        performance = report["source_performance"]

        self.assertEqual(performance["european_odds"]["OddsPortal"]["settled_records"], 1)
        self.assertEqual(performance["xg_source"]["www.fotmob.com"]["settled_records"], 1)
        self.assertEqual(performance["context_ceiling"]["high"]["settled_records"], 1)
        self.assertFalse(performance["european_odds"]["OddsPortal"]["calibration_available"])
        self.assertEqual(performance["evidence_sources"]["OddsPortal"]["observations"], 1)
        self.assertEqual(performance["evidence_sources"]["fotmob-xg-evidence"]["failures"], 1)

    def test_source_performance_coalesces_source_families_without_merging_xg_scopes(self):
        records = []
        fixtures = [
            ("20990101002", "OddsPortal", "https://www.fotmob.com/teams/1/stats/home", "league_total"),
            ("20990101003", "OddsPortal user-supplied screenshot", "https://data.fotmob.com/teams/2/stats/away", "home_away_split"),
        ]
        for match_id, provider, source_url, scope in fixtures:
            prediction_input = deepcopy(SYNTHETIC_PREDICTION)
            prediction_input["match_id"] = match_id
            prediction_input["european_odds"] = {"provider": provider}
            prediction_input["xg_inputs"] = {
                "source_url": source_url,
                "evidence": {"source_url": source_url, "scope": scope},
            }
            prediction = freeze_prediction(prediction_input)
            records.extend((
                prediction,
                settle_prediction(
                    prediction,
                    "home_win",
                    f"https://official.example.test/results/{match_id}",
                ),
            ))

        performance = calibration_report(records)["source_performance"]

        self.assertEqual(performance["source_families"]["european_odds"]["OddsPortal"]["settled_records"], 2)
        self.assertEqual(performance["source_families"]["xg_source"]["fotmob"]["settled_records"], 2)
        self.assertEqual(performance["source_families"]["xg_scope"]["fotmob|league_total"]["settled_records"], 1)
        self.assertEqual(performance["source_families"]["xg_scope"]["fotmob|home_away_split"]["settled_records"], 1)
        cohorts = performance["comparable_cohorts"]
        self.assertEqual(cohorts["minimum_settled_records"], 20)
        self.assertEqual(len(cohorts["items"]), 2)
        self.assertTrue(all(item["settled_records"] == 1 for item in cohorts["items"].values()))
        self.assertTrue(all(not item["comparison_ready"] for item in cohorts["items"].values()))


if __name__ == "__main__":
    unittest.main()
