"""Offline unit tests for the standalone Dixon-Coles module."""

import math
import unittest
from datetime import date, timedelta

from outputs.tai_cai_tracker import dixon_coles as dc


def _fixture():
    """A small deterministic four-team league spread over two years."""

    teams = ("Alpha", "Bravo", "Charlie", "Delta")
    matches = []
    day = date(2022, 8, 1)
    for index in range(64):
        home = teams[index % 4]
        away = teams[(index + 1 + index // 4) % 4]
        if home == away:
            away = teams[(teams.index(away) + 1) % 4]
        home_goals = (index * 3 + index // 2) % 4
        away_goals = (index * 5 + 1) % 3
        matches.append(
            {
                "date": (day + timedelta(days=13 * index)).isoformat(),
                "season": "2022/23" if index < 32 else "2023/24",
                "division": "T0",
                "home_team": home,
                "away_team": away,
                "home_goals": home_goals,
                "away_goals": away_goals,
            }
        )
    return matches


class DixonColesProbabilityTest(unittest.TestCase):
    def test_outcome_probabilities_sum_to_one(self):
        probabilities = dc.outcome_probabilities(1.45, 1.05, -0.08)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)
        for value in probabilities.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_score_matrix_sums_to_one(self):
        matrix = dc.score_matrix(1.6, 0.9, -0.12)
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=12)

    def test_rho_only_changes_the_four_low_score_cells(self):
        independent = dc.score_matrix(1.4, 1.0, 0.0, normalize=False)
        adjusted = dc.score_matrix(1.4, 1.0, -0.15, normalize=False)
        low_cells = {(0, 0), (0, 1), (1, 0), (1, 1)}
        for home in range(4):
            for away in range(4):
                if (home, away) in low_cells:
                    self.assertNotAlmostEqual(
                        independent[home][away], adjusted[home][away], places=9
                    )
                else:
                    self.assertAlmostEqual(
                        independent[home][away], adjusted[home][away], places=12
                    )
        # The correction is exactly the tau factor.
        self.assertAlmostEqual(
            adjusted[0][0], independent[0][0] * (1.0 - 1.4 * 1.0 * -0.15), places=12
        )
        self.assertAlmostEqual(adjusted[1][1], independent[1][1] * (1.0 + 0.15), places=12)


class DixonColesWeightTest(unittest.TestCase):
    def test_time_weight_is_monotonic_and_half_life_anchored(self):
        reference = date(2024, 8, 1)
        ages = [0, 30, 100, 300, 600]
        weights = [dc.time_weight(reference - timedelta(days=age), reference, 300) for age in ages]
        for older, newer in zip(weights[1:], weights[:-1]):
            self.assertLess(older, newer)
        self.assertAlmostEqual(weights[0], 1.0, places=12)
        self.assertAlmostEqual(
            dc.time_weight(reference - timedelta(days=300), reference, 300), 0.5, places=12
        )

    def test_half_life_changes_the_weights(self):
        reference = date(2025, 1, 1)
        match_day = reference - timedelta(days=200)
        fast = dc.time_weight(match_day, reference, 60)
        slow = dc.time_weight(match_day, reference, 300)
        self.assertLess(fast, slow)
        self.assertAlmostEqual(fast, 0.5 ** (200 / 60), places=12)


class DixonColesTeamFallbackTest(unittest.TestCase):
    def test_unseen_team_falls_back_to_league_mean(self):
        params = {
            "home_adv": 0.2,
            "rho": -0.05,
            "teams": {
                "Alpha": {"attack": 0.4, "defence": 0.1},
                "Bravo": {"attack": 0.0, "defence": 0.3},
            },
        }
        attack, defence = dc.team_parameters(params, "Unknown")
        self.assertAlmostEqual(attack, 0.2, places=12)
        self.assertAlmostEqual(defence, 0.2, places=12)

        with_unknown = dc.predict_match(params, "Unknown", "Bravo")
        with_average = dc.predict_match(
            {
                "home_adv": 0.2,
                "rho": -0.05,
                "teams": {
                    "Average": {"attack": 0.2, "defence": 0.2},
                    "Bravo": {"attack": 0.0, "defence": 0.3},
                },
            },
            "Average",
            "Bravo",
        )
        for outcome in dc.OUTCOMES:
            self.assertAlmostEqual(with_unknown[outcome], with_average[outcome], places=12)
        self.assertAlmostEqual(sum(with_unknown.values()), 1.0, places=12)

    def test_empty_or_missing_parameters_do_not_crash(self):
        for params in (None, {}, {"teams": {}}, {"divisions": {}}, {"home_adv": 0.1}):
            probabilities = dc.predict_match(params, "Home", "Away")
            self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)
        self.assertEqual(dc.summarize([]), {"n": 0, "rps": None, "brier": None, "top1": None})


class DixonColesScoringTest(unittest.TestCase):
    def test_rps_and_brier_match_hand_calculation(self):
        probabilities = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}
        self.assertAlmostEqual(dc.rps(probabilities, "home_win"), 0.145, places=12)
        self.assertAlmostEqual(dc.rps(probabilities, "away_win"), 0.445, places=12)
        self.assertAlmostEqual(dc.brier(probabilities, "home_win"), 0.38, places=12)
        self.assertAlmostEqual(dc.brier(probabilities, "away_win"), 0.98, places=12)
        self.assertEqual(dc.top1(probabilities), "home_win")


class DixonColesLeakageTest(unittest.TestCase):
    def test_rolling_window_excludes_future_seasons(self):
        matches = [
            {"season": "2022/23", "date": "2023-05-01", "division": "T0"},
            {"season": "2023/24", "date": "2024-05-01", "division": "T0"},
            {"season": "2024/25", "date": "2025-05-01", "division": "T0"},
            {"season": "2025/26", "date": "2026-05-01", "division": "T0"},
        ]
        train = dc.training_matches(matches, "2024/25")
        self.assertEqual([match["season"] for match in train], ["2022/23", "2023/24"])
        self.assertTrue(all(match["date"] < "2024-07-01" for match in train))
        self.assertEqual(len(dc.training_matches(matches, "2026/27")), 4)


class DixonColesFitTest(unittest.TestCase):
    def test_fit_returns_finite_parameters_for_every_team(self):
        matches = _fixture()
        params = dc.fit_division(
            matches, half_life=300, reference_date=date(2024, 7, 1), iterations=150
        )
        self.assertEqual(
            set(params["teams"]), {"Alpha", "Bravo", "Charlie", "Delta"}
        )
        self.assertTrue(math.isfinite(params["home_adv"]))
        self.assertTrue(math.isfinite(params["rho"]))
        for entry in params["teams"].values():
            self.assertTrue(math.isfinite(entry["attack"]))
            self.assertTrue(math.isfinite(entry["defence"]))
        probabilities = dc.predict_match(params, "Alpha", "Bravo")
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)

    def test_half_life_changes_fitted_parameters(self):
        matches = _fixture()
        slow = dc.fit_division(matches, half_life=300, reference_date=date(2024, 7, 1), iterations=120)
        fast = dc.fit_division(matches, half_life=60, reference_date=date(2024, 7, 1), iterations=120)
        self.assertNotAlmostEqual(slow["rho"], fast["rho"], places=9)


class DixonColesMarketTest(unittest.TestCase):
    def _match(self, **odds):
        return {"odds": odds}

    def test_market_priority_and_fallback(self):
        match = self._match(
            avg={"fair": {"home": 0.5, "draw": 0.3, "away": 0.2}},
            b365={"fair": {"home": 0.6, "draw": 0.2, "away": 0.2}},
            pinnacle={"fair": {"home": 0.7, "draw": 0.2, "away": 0.1}},
        )
        self.assertAlmostEqual(dc.market_close_probabilities(match)["home_win"], 0.5, places=12)

        match["odds"].pop("avg")
        self.assertAlmostEqual(dc.market_close_probabilities(match)["home_win"], 0.6, places=12)

        match["odds"].pop("b365")
        self.assertAlmostEqual(dc.market_close_probabilities(match)["home_win"], 0.7, places=12)

        match["odds"].pop("pinnacle")
        self.assertIsNone(dc.market_close_probabilities(match))

    def test_nested_close_fair_is_accepted(self):
        match = self._match(
            avg={"close": {"fair": {"home": 0.4, "draw": 0.3, "away": 0.3}}}
        )
        probabilities = dc.market_close_probabilities(match)
        self.assertAlmostEqual(probabilities["home_win"], 0.4, places=12)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)


if __name__ == "__main__":
    unittest.main()
