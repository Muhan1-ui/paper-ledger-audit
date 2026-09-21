import unittest
from decimal import Decimal

from outputs.tai_cai_tracker.model import (
    blend_probabilities,
    normalize_fixed_prizes,
    poisson_score_matrix,
)
from outputs.tai_cai_tracker.markets import (
    HALF_FULL_OPTIONS,
    TOTAL_GOAL_OPTIONS,
    derive_official_markets,
    select_top_option,
)


class NormalizeFixedPrizesTests(unittest.TestCase):
    def test_normalizes_reciprocal_probabilities_from_decimal_prizes(self):
        probabilities = normalize_fixed_prizes(
            [Decimal("2"), Decimal("3"), Decimal("6")]
        )

        self.assertEqual(
            probabilities,
            [Decimal("0.5"), Decimal(1) / Decimal(3), Decimal(1) / Decimal(6)],
        )

    def test_normalizes_tiny_finite_decimal_prizes_without_overflow(self):
        probabilities = normalize_fixed_prizes(
            [Decimal("1e-1000000"), Decimal("1"), Decimal("1")]
        )

        self.assertEqual(
            probabilities,
            [Decimal("1"), Decimal("1e-1000000"), Decimal("1e-1000000")],
        )

    def test_normalizes_extreme_decimal_prizes_to_finite_unit_sum(self):
        probabilities = normalize_fixed_prizes(
            [Decimal("1e-1000000"), Decimal("2"), Decimal("3")]
        )

        self.assertTrue(all(probability.is_finite() for probability in probabilities))
        self.assertEqual(sum(probabilities, Decimal(0)), Decimal(1))

    def test_normalizes_ordinary_decimal_prizes_to_an_exact_unit_sum(self):
        probabilities = normalize_fixed_prizes(
            [Decimal("3.00"), Decimal("3.10"), Decimal("2.30")]
        )

        self.assertEqual(sum(probabilities, Decimal(0)), Decimal(1))

    def test_rejects_invalid_prize_inputs(self):
        invalid_prizes = (
            [Decimal("2"), Decimal("3")],
            [Decimal("2"), Decimal("3"), Decimal("6"), Decimal("8")],
            [Decimal("2"), 3, Decimal("6")],
            [Decimal("2"), Decimal("0"), Decimal("6")],
            [Decimal("2"), Decimal("-3"), Decimal("6")],
            [Decimal("2"), Decimal("Infinity"), Decimal("6")],
            [Decimal("2"), Decimal("NaN"), Decimal("6")],
        )

        for prizes in invalid_prizes:
            with self.subTest(prizes=prizes):
                with self.assertRaises(ValueError):
                    normalize_fixed_prizes(prizes)


class PoissonScoreMatrixTests(unittest.TestCase):
    def test_derives_normalised_football_markets_from_expected_goals(self):
        result = poisson_score_matrix(
            Decimal("1.2"), Decimal("0.8"), home_handicap=Decimal("-1")
        )

        self.assertEqual(
            set(result["one_x_two"]), {"home_win", "draw", "away_win"}
        )
        self.assertEqual(sum(result["one_x_two"].values(), Decimal(0)), Decimal(1))
        self.assertGreater(
            result["one_x_two"]["home_win"], result["one_x_two"]["away_win"]
        )
        self.assertEqual(sum(result["total_goals"].values(), Decimal(0)), Decimal(1))
        self.assertEqual(len(result["top_scores"]), 5)
        self.assertEqual(result["top_scores"][0]["home_goals"], 1)
        self.assertEqual(result["top_scores"][0]["away_goals"], 0)
        self.assertEqual(
            sum(result["handicap_one_x_two"].values(), Decimal(0)), Decimal(1)
        )
        self.assertEqual(len(result["half_time_full_time_proxy"]), 2)

    def test_rejects_invalid_expected_goals_or_goal_limit(self):
        invalid_inputs = (
            (Decimal("-0.1"), Decimal("1"), 10),
            (Decimal("1"), Decimal("Infinity"), 10),
            (Decimal("1"), Decimal("1"), 0),
            (Decimal("1"), Decimal("1"), True),
        )

        for home_xg, away_xg, max_goals in invalid_inputs:
            with self.subTest(home_xg=home_xg, away_xg=away_xg, max_goals=max_goals):
                with self.assertRaises(ValueError):
                    poisson_score_matrix(home_xg, away_xg, max_goals=max_goals)

    def test_derives_all_official_market_distributions(self):
        matrix = poisson_score_matrix(
            Decimal("1.2"), Decimal("0.8"), home_handicap=Decimal("-1")
        )
        markets = derive_official_markets(matrix, Decimal("-1"))

        self.assertEqual(set(markets), {"hhad", "crs", "ttg", "hafu"})
        self.assertEqual(sum(markets["hhad"].values(), Decimal(0)), Decimal(1))
        self.assertEqual(sum(markets["crs"].values(), Decimal(0)), Decimal(1))
        self.assertEqual(sum(markets["ttg"].values(), Decimal(0)), Decimal(1))
        self.assertEqual(sum(markets["hafu"].values(), Decimal(0)), Decimal(1))
        self.assertEqual(tuple(markets["ttg"]), TOTAL_GOAL_OPTIONS)
        self.assertEqual(tuple(markets["hafu"]), HALF_FULL_OPTIONS)
        self.assertEqual(len(matrix["half_time_full_time"]), 9)

    def test_preserves_an_exact_1x2_sum_after_default_precision_rounding(self):
        matrix = poisson_score_matrix(
            Decimal("1.170588235294117647058823529"),
            Decimal("1.902777777777777777777777778"),
        )

        self.assertEqual(sum(matrix["one_x_two"].values(), Decimal(0)), Decimal(1))

    def test_select_top_option_uses_declared_order_for_ties(self):
        self.assertEqual(
            select_top_option(
                {"first": Decimal("0.5"), "second": Decimal("0.5")},
                ("first", "second"),
            ),
            ("first", Decimal("0.5"), ("first", "second")),
        )


class ProbabilityBlendTests(unittest.TestCase):
    def test_blend_endpoints_return_the_named_input_distribution(self):
        market = {
            "home_win": Decimal("0.2"),
            "draw": Decimal("0.3"),
            "away_win": Decimal("0.5"),
        }
        model = {
            "home_win": Decimal("0.5"),
            "draw": Decimal("0.3"),
            "away_win": Decimal("0.2"),
        }

        self.assertEqual(blend_probabilities(market, model, Decimal(0)), market)
        self.assertEqual(blend_probabilities(market, model, Decimal(1)), model)

    def test_rejects_invalid_distribution_or_weight(self):
        valid = {
            "home_win": Decimal("0.2"),
            "draw": Decimal("0.3"),
            "away_win": Decimal("0.5"),
        }

        with self.assertRaises(ValueError):
            blend_probabilities(valid, valid, Decimal("1.1"))
        with self.assertRaises(ValueError):
            blend_probabilities(valid, {"home_win": Decimal(1)}, Decimal("0.5"))

    def test_blend_rounds_to_an_exact_unit_distribution_at_default_precision(self):
        market = {
            "home_win": Decimal("0.5911746076430431416707720459"),
            "draw": Decimal("0.232036033499894433105778028"),
            "away_win": Decimal("0.1767893588570624252234499261"),
        }
        model = {
            "home_win": Decimal("0.4633705331800249211582020665"),
            "draw": Decimal("0.2340057369991400387922105831"),
            "away_win": Decimal("0.3026237298208350400495873504"),
        }

        probabilities = blend_probabilities(market, model, Decimal("0.40"))

        self.assertEqual(sum(probabilities.values(), Decimal(0)), Decimal(1))


if __name__ == "__main__":
    unittest.main()
