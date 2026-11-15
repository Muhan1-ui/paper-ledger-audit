"""Unit tests for the read-only daily paper slip (``daily_slip.py``).

Every fixture here is synthetic. No test reads the real paper ledger, calls a
public source, writes a file, or places/funds a lottery bet.
"""

import unittest
from datetime import datetime, timedelta, timezone

from outputs.tai_cai_tracker.daily_slip import (
    build_daily_slip,
    compute_ev,
    crs_probabilities,
    format_daily_slip,
    hafu_probabilities,
    hhad_probabilities,
    normalize_scorelines,
    spf_probabilities,
    ttg_probabilities,
)

TZ = timezone(timedelta(hours=8))
DAY = "2026-11-16"


def _model(scorelines, blended, half_time_full_time=None):
    return {
        "scorelines": [
            {"home_goals": home, "away_goals": away, "probability": probability}
            for (home, away), probability in scorelines.items()
        ],
        "blended_outcomes": blended,
        "half_time_full_time": half_time_full_time
        or {
            "home_win:home_win": 0.4,
            "draw:draw": 0.3,
            "away_win:away_win": 0.3,
        },
    }


def _prediction(match_id, model, kickoff="2026-11-16T20:00:00+08:00", ordinal=1):
    return {
        "record_type": "prediction",
        "status": "frozen",
        "prediction_role": "primary",
        "match_id": match_id,
        "content_hash": f"pred-{match_id}-{ordinal}",
        "captured_at": f"2026-11-16T10:0{ordinal % 10}:00+08:00",
        "kickoff_at": kickoff,
        "match_context": {"home_team": "主队FC", "away_team": "客队FC"},
        "model": model,
    }


def _snapshot(prediction, handicap=-1, spf=None, hhad=None):
    return {
        "record_type": "all_market_snapshot",
        "content_hash": f"snap-{prediction['content_hash']}",
        "prediction_hash": prediction["content_hash"],
        "status": "frozen",
        "markets": {
            "spf": {
                "status": "frozen",
                "official_fixed_prizes": spf
                or {"home_win": "2.00", "draw": "3.50", "away_win": "4.00"},
            },
            "hhad": {
                "status": "frozen",
                "home_handicap": str(handicap),
                "official_fixed_prizes": hhad
                or {"home_win": "2.00", "draw": "3.50", "away_win": "4.00"},
            },
            "ttg": {
                "status": "frozen",
                "official_fixed_prizes": {
                    "0": "10.0", "1": "5.0", "2": "4.0", "3": "4.0",
                    "4": "5.0", "5": "8.0", "6": "15.0", "7+": "20.0",
                },
            },
            "crs": {
                "status": "frozen",
                "official_fixed_prizes": {
                    "0:0": "10.0", "1:0": "8.0", "2:1": "9.0",
                    "1:1": "7.0", "0:1": "9.0", "2:0": "10.0",
                },
            },
            "hafu": {
                "status": "frozen",
                "official_fixed_prizes": {
                    "HH": "3.0", "DD": "5.0", "AA": "6.0",
                    "HD": "12.0", "DH": "8.0", "AA": "6.0",
                },
            },
        },
    }


def _records(*pairs, handicap=-1, spf=None, hhad=None):
    records = []
    for prediction in pairs:
        records.append(prediction)
        records.append(_snapshot(prediction, handicap=handicap, spf=spf, hhad=hhad))
    return records


class ProbabilityNormalizationTests(unittest.TestCase):
    """Each market distribution is independently normalized to one."""

    def setUp(self):
        self.model = _model(
            {(2, 1): 0.4, (1, 1): 0.3, (0, 1): 0.3},
            {"home_win": 0.4, "draw": 0.3, "away_win": 0.3},
        )
        self.scores = normalize_scorelines(self.model)

    def test_spf_probabilities_sum_to_one(self):
        probabilities = spf_probabilities(self.model)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)

    def test_hhad_probabilities_sum_to_one(self):
        probabilities = hhad_probabilities(self.scores, -1)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)

    def test_ttg_probabilities_sum_to_one(self):
        probabilities = ttg_probabilities(self.scores)
        self.assertEqual(len(probabilities), 8)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)

    def test_crs_probabilities_sum_to_one_when_all_scores_are_offered(self):
        probabilities, residual = crs_probabilities(self.scores, ("2:1", "1:1", "0:1"))
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)
        self.assertAlmostEqual(residual, 0.0, places=12)

    def test_hafu_probabilities_sum_to_one(self):
        probabilities = hafu_probabilities(self.model)
        self.assertEqual(len(probabilities), 9)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)


class HandicapAndBucketTests(unittest.TestCase):
    def test_home_2_1_is_a_draw_after_minus_one_handicap(self):
        scores = normalize_scorelines(_model({(2, 1): 1.0}, {}))
        probabilities = hhad_probabilities(scores, -1)
        self.assertAlmostEqual(probabilities["draw"], 1.0, places=12)
        self.assertAlmostEqual(probabilities["home_win"], 0.0, places=12)
        self.assertAlmostEqual(probabilities["away_win"], 0.0, places=12)

    def test_home_3_1_still_wins_after_minus_one_handicap(self):
        scores = normalize_scorelines(_model({(3, 1): 1.0}, {}))
        probabilities = hhad_probabilities(scores, -1)
        self.assertAlmostEqual(probabilities["home_win"], 1.0, places=12)

    def test_seven_and_eight_goals_both_land_in_7plus(self):
        model = _model({(7, 0): 0.3, (8, 0): 0.7}, {})
        probabilities = ttg_probabilities(normalize_scorelines(model))
        self.assertAlmostEqual(probabilities["7+"], 1.0, places=12)
        for option in ("0", "1", "2", "3", "4", "5", "6"):
            self.assertAlmostEqual(probabilities[option], 0.0, places=12)


class SelectionTests(unittest.TestCase):
    def test_spf_uses_frozen_blended_outcomes_not_scoreline_aggregate(self):
        # All scorelines say home win, but the frozen SPF blend says 50%.
        model = _model({(2, 1): 1.0}, {"home_win": 0.5, "draw": 0.3, "away_win": 0.2})
        probabilities = spf_probabilities(model)
        self.assertAlmostEqual(probabilities["home_win"], 0.5, places=12)
        self.assertAlmostEqual(probabilities["away_win"], 0.2, places=12)

    def test_ev_over_thirty_percent_is_never_selected(self):
        # EV = 0.80 * 2.82 - 1 = +125.6%
        self.assertAlmostEqual(compute_ev(0.80, 2.82), 1.256, places=9)
        model = _model(
            {(2, 1): 0.4, (1, 1): 0.3, (0, 1): 0.3},
            {"home_win": 0.80, "draw": 0.10, "away_win": 0.10},
        )
        prediction = _prediction("周一001", model)
        records = _records(
            prediction,
            spf={"home_win": "2.82", "draw": "5.00", "away_win": "5.00"},
            hhad={"home_win": "1.01", "draw": "1.01", "away_win": "1.01"},
        )
        report = build_daily_slip(records, DAY, budget=10)
        self.assertEqual(len(report["matches"]), 1)
        self.assertEqual(report["selections"], [])
        self.assertEqual(report["total_stake"], 0)

    def test_budget_is_respected_with_two_yuan_stakes(self):
        records = []
        for index in range(8):
            model = _model(
                {(2, 1): 0.4, (1, 1): 0.3, (0, 1): 0.3},
                {"home_win": 0.52, "draw": 0.28, "away_win": 0.20},
            )
            prediction = _prediction(
                f"周一{index + 1:03d}",
                model,
                kickoff=f"2026-11-16T{18 + index // 4:02d}:{index % 4 * 15:02d}:00+08:00",
                ordinal=index + 1,
            )
            records.extend(
                _records(
                    prediction,
                    spf={"home_win": "2.10", "draw": "3.50", "away_win": "4.00"},
                    hhad={"home_win": "1.01", "draw": "1.01", "away_win": "1.01"},
                )
            )
        report = build_daily_slip(records, DAY, budget=10)
        self.assertEqual(len(report["selections"]), 5)
        self.assertEqual(report["total_stake"], 10)
        self.assertLessEqual(report["total_stake"], 10)
        self.assertEqual(report["remaining"], 0)
        for selection in report["selections"]:
            self.assertEqual(selection["stake"], 2)
        self.assertEqual(len({selection["match_id"] for selection in report["selections"]}), 5)

    def test_at_most_one_selection_per_match(self):
        # SPF home EV = 0.52*2.10-1 = +9.2%; HHAD home EV = 0.60*2.00-1 = +20%.
        # After the -1 line: 2:0 and 3:1 are home wins (0.6 total), 0:1 is away.
        model = _model(
            {(2, 0): 0.4, (3, 1): 0.2, (0, 1): 0.4},
            {"home_win": 0.52, "draw": 0.28, "away_win": 0.20},
        )
        prediction = _prediction("周一009", model)
        records = _records(
            prediction,
            handicap=-1,
            spf={"home_win": "2.10", "draw": "3.50", "away_win": "4.00"},
            hhad={"home_win": "2.00", "draw": "3.50", "away_win": "4.00"},
        )
        report = build_daily_slip(records, DAY, budget=10)
        self.assertEqual(len(report["selections"]), 1)
        self.assertEqual(report["selections"][0]["market"], "HHAD")


class EmptyDayTests(unittest.TestCase):
    def test_day_without_predictions_returns_empty_without_raising(self):
        report = build_daily_slip([], DAY, budget=10)
        self.assertEqual(report["matches"], [])
        self.assertEqual(report["selections"], [])
        self.assertEqual(report["in_window"], 0)
        text = format_daily_slip(report)
        self.assertIn(f"{DAY} 没有可用场次", text)

    def test_prediction_on_another_day_is_ignored(self):
        model = _model({(2, 1): 1.0}, {"home_win": 0.5, "draw": 0.3, "away_win": 0.2})
        prediction = _prediction("周一010", model, kickoff="2026-11-17T20:00:00+08:00")
        records = _records(prediction)
        report = build_daily_slip(records, DAY, budget=10)
        self.assertEqual(report["in_window"], 0)
        self.assertEqual(report["matches"], [])


class FormatTests(unittest.TestCase):
    def test_replay_marks_settled_fixture_and_prints_all_markets(self):
        model = _model(
            {(2, 1): 0.4, (1, 1): 0.3, (0, 1): 0.3},
            {"home_win": 0.4, "draw": 0.3, "away_win": 0.3},
        )
        prediction = _prediction("周一011", model)
        records = _records(prediction)
        snapshot = records[1]
        records.append(
            {
                "record_type": "all_market_settlement",
                "status": "settled",
                "snapshot_hash": snapshot["content_hash"],
                "final_score": {"home": 2, "away": 1},
                "markets": {},
            }
        )
        report = build_daily_slip(records, DAY, budget=10)
        text = format_daily_slip(report)
        self.assertIn("已结算 2:1", text)
        for section in ("SPF 胜平负", "HHAD 让球(-1)", "TTG 总进球", "CRS 比分", "HAFU 半全场"):
            self.assertIn(section, text)
        self.assertIn("自检", text)
        self.assertIn("只投 SPF 和 HHAD", text)


if __name__ == "__main__":
    unittest.main()
