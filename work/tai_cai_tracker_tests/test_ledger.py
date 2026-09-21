import hashlib
import unittest
from copy import deepcopy
from decimal import Decimal

from outputs.tai_cai_tracker.ledger import (
    ALL_MARKET_OPTIONS,
    freeze_all_market_snapshot,
    freeze_evidence_event,
    freeze_odds_capture,
    freeze_prediction,
    settle_all_market_snapshot,
    verify_all_market_settlement,
    verify_evidence_event,
    verify_odds_capture,
)


VALID_PREDICTION = {
    "market": "SPF",
    "match_id": "20260724001",
    "kickoff_at": "2026-07-24T20:00:00+08:00",
    "captured_at": "2026-07-24T18:00:00+08:00",
    "source_urls": {
        "S1": "https://www.lottery.gov.cn/matches/20260724001",
        "S2": "https://odds.example.test/matches/20260724001",
    },
    "outcomes": [
        {"label": "home_win", "probability": Decimal("0.50")},
        {"label": "draw", "probability": Decimal("0.30")},
        {"label": "away_win", "probability": Decimal("0.20")},
    ],
}

VALID_ODDS_CAPTURE = {
    "provider": "manual-export",
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

VALID_ALL_MARKET_SNAPSHOT = {
    "prediction_hash": "a" * 64,
    "match_id": "20260724001",
    "kickoff_at": "2026-07-24T20:00:00+08:00",
    "captured_at": "2026-07-24T18:00:00+08:00",
    "official_source_url": "https://www.lottery.gov.cn/matches/20260724001",
    "markets": {
        "spf": {"status": "frozen", "probabilities": {"home_win": Decimal("0.5"), "draw": Decimal("0.3"), "away_win": Decimal("0.2")}, "selected_option": "home_win", "selected_probability": Decimal("0.5"), "tied_options": ["home_win"], "official_fixed_prizes": {"home_win": Decimal("2"), "draw": Decimal("3"), "away_win": Decimal("4")}},
        "hhad": {"status": "frozen", "home_handicap": Decimal("-1"), "probabilities": {"home_win": Decimal("0.2"), "draw": Decimal("0.3"), "away_win": Decimal("0.5")}, "selected_option": "away_win", "selected_probability": Decimal("0.5"), "tied_options": ["away_win"], "official_fixed_prizes": {"home_win": Decimal("4"), "draw": Decimal("3"), "away_win": Decimal("2")}},
        "crs": {"status": "skipped", "reason": "fixture keeps exact-score input short"},
        "ttg": {"status": "skipped", "reason": "fixture keeps total-goal input short"},
        "hafu": {"status": "skipped", "reason": "fixture keeps half-full input short"},
    },
}

VALID_SOURCE_OBSERVATION = {
    "record_type": "source_observation",
    "match_id": "20260724001",
    "evidence_kind": "european_1x2",
    "source_url": "https://odds.example.test/matches/20260724001",
    "captured_at": "2026-07-24T18:00:00+08:00",
    "published_at_unavailable_reason": "provider page has no visible publication time",
    "collector": "oddsportal-extract",
    "parser_version": "2026.08.23",
    "raw_content_sha256": "a" * 64,
    "observed_facts": {
        "provider": "OddsPortal",
        "bookmaker": "ExampleBook",
        "odds": {"home_win": "2.10", "draw": "3.20", "away_win": "3.40"},
    },
}

VALID_COLLECTION_FAILURE = {
    "record_type": "collection_failure",
    "match_id": "20260724001",
    "evidence_kind": "xg",
    "attempted_at": "2026-07-24T18:01:00+08:00",
    "attempted_source_url": "https://stats.example.test/teams/home",
    "collector": "fotmob-xg-evidence",
    "parser_version": "2026.08.23",
    "reason_code": "incomplete_data",
    "reason": "public page does not expose a verified home/away split",
}


class FrozenPredictionTests(unittest.TestCase):
    def test_valid_spf_prediction_returns_frozen_hashed_record_without_result(self):
        record = freeze_prediction(VALID_PREDICTION)

        self.assertEqual(record["status"], "frozen")
        self.assertEqual(record["record_type"], "prediction")
        self.assertEqual(len(record["content_hash"]), hashlib.sha256().digest_size * 2)
        self.assertNotIn("actual_outcome", record)

    def test_accepts_standard_chinese_sports_lottery_weekday_sequence_match_id(self):
        prediction = deepcopy(VALID_PREDICTION)
        prediction["match_id"] = "周五201"

        record = freeze_prediction(prediction)

        self.assertEqual(record["match_id"], "周五201")

    def test_rejects_predictions_without_required_auditable_spf_fields(self):
        invalid_predictions = []

        wrong_match_id = deepcopy(VALID_PREDICTION)
        wrong_match_id["match_id"] = "friendly-1"
        invalid_predictions.append(wrong_match_id)

        late_capture = deepcopy(VALID_PREDICTION)
        late_capture["captured_at"] = late_capture["kickoff_at"]
        invalid_predictions.append(late_capture)

        missing_s2 = deepcopy(VALID_PREDICTION)
        del missing_s2["source_urls"]["S2"]
        invalid_predictions.append(missing_s2)

        wrong_labels = deepcopy(VALID_PREDICTION)
        wrong_labels["outcomes"][2]["label"] = "postponed"
        invalid_predictions.append(wrong_labels)

        wrong_total = deepcopy(VALID_PREDICTION)
        wrong_total["outcomes"][2]["probability"] = Decimal("0.19")
        invalid_predictions.append(wrong_total)

        for prediction in invalid_predictions:
            with self.subTest(prediction=prediction):
                with self.assertRaises(ValueError):
                    freeze_prediction(prediction)

    def test_validates_optional_match_context_before_freezing(self):
        prediction = deepcopy(VALID_PREDICTION)
        prediction["match_context"] = {
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
        }

        record = freeze_prediction(prediction)

        self.assertEqual(record["match_context"]["competition"], "Test League")

        prediction["match_context"] = {
            "competition": "Test League",
            "home_team": "Home FC",
        }
        with self.assertRaises(ValueError):
            freeze_prediction(prediction)

        prediction["match_context"] = {
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "   ",
        }
        with self.assertRaises(ValueError):
            freeze_prediction(prediction)


class OddsCaptureTests(unittest.TestCase):
    def test_freezes_named_bookmaker_odds_capture(self):
        record = freeze_odds_capture(VALID_ODDS_CAPTURE)

        self.assertEqual(record["record_type"], "odds_capture")
        self.assertEqual(record["status"], "captured")
        self.assertEqual(record["bookmaker"], "ExampleBook")
        self.assertEqual(len(record["content_hash"]), hashlib.sha256().digest_size * 2)

    def test_allows_unattributed_capture_as_observation(self):
        capture = deepcopy(VALID_ODDS_CAPTURE)
        capture["source_quality"] = "unattributed"
        capture["bookmaker"] = None

        record = freeze_odds_capture(capture)

        self.assertEqual(record["source_quality"], "unattributed")
        self.assertIsNone(record["bookmaker"])

    def test_rejects_invalid_odds_capture_fields(self):
        invalid_captures = []

        invalid_quality = deepcopy(VALID_ODDS_CAPTURE)
        invalid_quality["source_quality"] = "unknown"
        invalid_captures.append(invalid_quality)

        unnamed_bookmaker = deepcopy(VALID_ODDS_CAPTURE)
        unnamed_bookmaker["bookmaker"] = ""
        invalid_captures.append(unnamed_bookmaker)

        non_positive_odd = deepcopy(VALID_ODDS_CAPTURE)
        non_positive_odd["odds"]["draw"] = Decimal("0")
        invalid_captures.append(non_positive_odd)

        late_capture = deepcopy(VALID_ODDS_CAPTURE)
        late_capture["captured_at"] = late_capture["kickoff_at"]
        invalid_captures.append(late_capture)

        for capture in invalid_captures:
            with self.subTest(capture=capture):
                with self.assertRaises(ValueError):
                    freeze_odds_capture(capture)

    def test_rejects_tampered_odds_capture_record(self):
        record = freeze_odds_capture(VALID_ODDS_CAPTURE)
        record["odds"]["home_win"] = Decimal("9.99")

        with self.assertRaises(ValueError):
            verify_odds_capture(record)


class EvidenceEventTests(unittest.TestCase):
    def test_freezes_and_verifies_source_observation(self):
        record = freeze_evidence_event(VALID_SOURCE_OBSERVATION)

        self.assertEqual(record["record_type"], "source_observation")
        self.assertEqual(record["status"], "observed")
        self.assertEqual(len(record["content_hash"]), hashlib.sha256().digest_size * 2)
        verify_evidence_event(record)

        record["observed_facts"]["bookmaker"] = "TamperedBook"
        with self.assertRaises(ValueError):
            verify_evidence_event(record)

    def test_rejects_unknown_collection_failure_reason_code(self):
        failure = deepcopy(VALID_COLLECTION_FAILURE)
        failure["reason_code"] = "unknown"

        with self.assertRaises(ValueError):
            freeze_evidence_event(failure)


class AllMarketLedgerTests(unittest.TestCase):
    def test_version_two_snapshot_requires_complete_prizes_and_market_evidence(self):
        snapshot_input = deepcopy(VALID_ALL_MARKET_SNAPSHOT)
        snapshot_input["schema_version"] = 2
        snapshot_input["markets"] = {}
        for code, options in ALL_MARKET_OPTIONS.items():
            probabilities = {option: Decimal(0) for option in options}
            probabilities[options[0]] = Decimal(1)
            entry = {
                "status": "frozen",
                "probabilities": probabilities,
                "selected_option": options[0],
                "selected_probability": Decimal(1),
                "tied_options": [options[0]],
                "official_fixed_prizes": {
                    option: Decimal(index + 2)
                    for index, option in enumerate(options)
                },
                "published_at": "2026-07-24T18:00:00+08:00",
                "official_source_url": "https://www.lottery.gov.cn/matches/20260724001",
            }
            if code == "hhad":
                entry["home_handicap"] = Decimal("-1")
            snapshot_input["markets"][code] = entry

        frozen = freeze_all_market_snapshot(snapshot_input)
        self.assertEqual(frozen["schema_version"], 2)
        self.assertEqual(
            set(frozen["markets"]["crs"]["official_fixed_prizes"]),
            set(ALL_MARKET_OPTIONS["crs"]),
        )

        missing_prize = deepcopy(snapshot_input)
        del missing_prize["markets"]["crs"]["official_fixed_prizes"]["1:0"]
        with self.assertRaises(ValueError):
            freeze_all_market_snapshot(missing_prize)

        late_publication = deepcopy(snapshot_input)
        late_publication["markets"]["ttg"]["published_at"] = "2026-07-24T20:00:00+08:00"
        with self.assertRaises(ValueError):
            freeze_all_market_snapshot(late_publication)

    def test_freezes_and_settles_linked_all_market_snapshot(self):
        snapshot = freeze_all_market_snapshot(VALID_ALL_MARKET_SNAPSHOT)
        settlement = settle_all_market_snapshot(
            snapshot,
            {
                "official_result_url": "https://www.lottery.gov.cn/results/20260724001",
                "final_score": {"home": 2, "away": 0},
                "markets": {"spf": "home_win", "hhad": "away_win"},
            },
        )

        self.assertEqual(snapshot["record_type"], "all_market_snapshot")
        self.assertTrue(settlement["markets"]["spf"]["hit"])
        self.assertTrue(settlement["markets"]["hhad"]["hit"])
        settlement["markets"]["spf"]["actual_option"] = "draw"
        with self.assertRaises(ValueError):
            verify_all_market_settlement(settlement)

    def test_rejects_late_or_fractional_handicap_snapshot(self):
        late = deepcopy(VALID_ALL_MARKET_SNAPSHOT)
        late["captured_at"] = late["kickoff_at"]
        with self.assertRaises(ValueError):
            freeze_all_market_snapshot(late)

        fractional = deepcopy(VALID_ALL_MARKET_SNAPSHOT)
        fractional["markets"]["hhad"]["home_handicap"] = Decimal("-0.5")
        with self.assertRaises(ValueError):
            freeze_all_market_snapshot(fractional)


if __name__ == "__main__":
    unittest.main()
