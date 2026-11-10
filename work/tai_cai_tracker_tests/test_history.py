"""Offline tests for the isolated football-data history module."""

import unittest

from outputs.tai_cai_tracker import history

FULL_HEADER = (
    "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,"
    "PSH,PSD,PSA,PSCH,PSCD,PSCA,"
    "B365H,B365D,B365A,B365CH,B365CD,B365CA,"
    "AvgCH,AvgCD,AvgCA,MaxCH,MaxCD,MaxCA"
)

FULL_ROWS = (
    "E0,18/08/2012,Arsenal,Sunderland,0,0,D,"
    "1.50,4.00,6.00,1.60,4.20,5.50,"
    "1.55,3.90,5.75,1.58,4.10,5.60,"
    "1.57,4.05,5.65,1.65,4.35,6.10\n"
    "E0,25/08/12,Chelsea,Newcastle,2,1,H,"
    "1.40,4.50,7.00,1.45,4.60,6.50,"
    "1.44,4.40,6.25,1.46,4.50,6.40,"
    "1.45,4.45,6.35,1.52,4.75,6.90\n"
    "E0,01/09/2012,Everton,West Brom,0,1,A,"
    "1.70,3.60,4.80,1.75,3.70,4.60,"
    "1.72,3.55,4.70,1.74,3.65,4.55,"
    "1.73,3.60,4.65,1.82,3.85,4.95\n"
)


class RpsTests(unittest.TestCase):
    def test_rps_perfect_forecast_is_zero(self):
        self.assertAlmostEqual(
            history.rps({"home": 1.0, "draw": 0.0, "away": 0.0}, "home_win"), 0.0
        )
        self.assertAlmostEqual(
            history.rps({"home_win": 0.0, "draw": 1.0, "away_win": 0.0}, "draw"),
            0.0,
        )
        self.assertAlmostEqual(
            history.rps({"home": 0.0, "draw": 0.0, "away": 1.0}, "away_win"), 0.0
        )

    def test_rps_uniform_home_win_is_five_eighteenths(self):
        uniform = {"home": 1.0 / 3.0, "draw": 1.0 / 3.0, "away": 1.0 / 3.0}
        self.assertAlmostEqual(history.rps(uniform, "home_win"), 5.0 / 18.0)
        self.assertAlmostEqual(history.rps(uniform, "draw"), 2.0 / 18.0)
        self.assertAlmostEqual(history.rps(uniform, "away_win"), 5.0 / 18.0)

    def test_rps_hand_computed_away_win(self):
        probabilities = {"home": 0.5, "draw": 0.3, "away": 0.2}
        self.assertAlmostEqual(history.rps(probabilities, "away_win"), 0.445)

    def test_rps_rejects_unknown_outcome(self):
        with self.assertRaises(ValueError):
            history.rps({"home": 0.5, "draw": 0.3, "away": 0.2}, "home")


class BrierTests(unittest.TestCase):
    def test_brier_perfect_and_uniform(self):
        self.assertAlmostEqual(
            history.brier_multiclass(
                {"home": 1.0, "draw": 0.0, "away": 0.0}, "home_win"
            ),
            0.0,
        )
        uniform = {"home": 1.0 / 3.0, "draw": 1.0 / 3.0, "away": 1.0 / 3.0}
        self.assertAlmostEqual(history.brier_multiclass(uniform, "home_win"), 2.0 / 3.0)

    def test_brier_rejects_unknown_outcome(self):
        with self.assertRaises(ValueError):
            history.brier_multiclass({"home": 0.5, "draw": 0.3, "away": 0.2}, "x")


class ProbabilityTests(unittest.TestCase):
    def test_implied_raw_and_fair_normalized(self):
        implied, fair = history.implied_and_fair(
            {"home": 1.8, "draw": 3.5, "away": 4.5}
        )
        self.assertAlmostEqual(implied["home"], 1.0 / 1.8)
        self.assertGreater(sum(implied.values()), 1.0)
        self.assertAlmostEqual(sum(fair.values()), 1.0, places=12)
        self.assertAlmostEqual(
            fair["away"], (1.0 / 4.5) / sum(implied.values()), places=12
        )

    def test_normalize_returns_none_for_missing_or_zero(self):
        self.assertIsNone(history.normalize_probabilities(None))
        self.assertIsNone(
            history.normalize_probabilities({"home": 0.0, "draw": 0.0, "away": 0.0})
        )
        self.assertIsNone(history.implied_probabilities({"home": 0.0, "draw": 2.0, "away": 3.0}))


class ParsingTests(unittest.TestCase):
    def test_season_code_and_label(self):
        self.assertEqual(history.season_code(2012), "1213")
        self.assertEqual(history.season_code(2025), "2526")
        self.assertEqual(history.season_code(2026), "2627")
        self.assertEqual(history.season_label(2025), "2025/26")
        self.assertEqual(history.season_label(2026), "2026/27")

    def test_default_season_range_includes_2627_and_e1(self):
        self.assertIn("E1", history.DIVISIONS)
        seasons = range(history.DEFAULT_FIRST_SEASON, history.DEFAULT_LAST_SEASON)
        self.assertEqual(history.season_code(seasons[-1]), "2627")

    def test_redirect_opener_follows_redirects(self):
        import urllib.request

        self.assertTrue(
            any(
                isinstance(handler, urllib.request.HTTPRedirectHandler)
                for handler in history._REDIRECT_OPENER.handlers
            )
        )

    def test_e1_championship_csv_parses(self):
        csv_text = (
            "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,PSCH,PSCD,PSCA\n"
            "E1,08/08/2026,Leicester,Sheffield Wed,1,2,A,2.10,3.40,3.60\n"
        )
        matches, skipped = history.parse_csv_text(csv_text, "E1", 2026)
        self.assertEqual(len(matches), 1)
        self.assertEqual(sum(skipped.values()), 0)
        self.assertEqual(matches[0]["division"], "E1")
        self.assertEqual(matches[0]["season"], "2026/27")
        self.assertEqual(matches[0]["match_id"], "e1|2026-08-08|leicester|sheffield wed")
        self.assertEqual(matches[0]["result"], "away_win")
        self.assertAlmostEqual(
            matches[0]["odds"]["pinnacle"]["fair"]["away"],
            (1 / 3.60) / (1 / 2.10 + 1 / 3.40 + 1 / 3.60),
            places=12,
        )

    def test_inline_csv_parses_both_date_formats(self):
        matches, skipped = history.parse_csv_text(FULL_HEADER + "\n" + FULL_ROWS, "E0", 2012)
        self.assertEqual(len(matches), 3)
        self.assertEqual(sum(skipped.values()), 0)
        self.assertEqual(matches[0]["date"], "2012-08-18")
        self.assertEqual(matches[1]["date"], "2012-08-25")

    def test_inline_csv_fields_and_match_id(self):
        matches, _ = history.parse_csv_text(FULL_HEADER + "\n" + FULL_ROWS, "E0", 2012)
        first = matches[0]
        self.assertEqual(first["match_id"], "e0|2012-08-18|arsenal|sunderland")
        self.assertEqual(first["division"], "E0")
        self.assertEqual(first["season"], "2012/13")
        self.assertEqual(first["home_goals"], 0)
        self.assertEqual(first["away_goals"], 0)
        self.assertEqual(first["result"], "draw")
        self.assertEqual(first["odds"]["pinnacle"]["open"]["home"], 1.50)
        self.assertEqual(first["odds"]["pinnacle"]["close"]["away"], 5.50)
        self.assertAlmostEqual(first["odds"]["pinnacle"]["implied"]["home"], 0.625)

    def test_ftr_mapping_home_draw_away(self):
        matches, _ = history.parse_csv_text(FULL_HEADER + "\n" + FULL_ROWS, "E0", 2012)
        self.assertEqual([m["result"] for m in matches], ["draw", "home_win", "away_win"])

    def test_invalid_and_empty_ftr_are_skipped_and_counted(self):
        csv_text = FULL_HEADER + "\n" + FULL_ROWS + (
            "E0,15/09/2012,Fulham,Stoke,1,0,X,"
            "2.00,3.40,3.90,2.05,3.50,3.80,"
            "2.02,3.35,3.85,2.04,3.45,3.75,"
            "2.03,3.40,3.80,2.15,3.60,4.05\n"
            "E0,22/09/2012,Norwich,QPR,1,1,,"
            "2.20,3.30,3.40,2.25,3.35,3.30,"
            "2.22,3.28,3.38,2.24,3.32,3.28,"
            "2.23,3.30,3.35,2.35,3.50,3.55\n"
        )
        matches, skipped = history.parse_csv_text(csv_text, "E0", 2012)
        self.assertEqual(len(matches), 3)
        self.assertEqual(skipped["bad_result"], 2)

    def test_missing_odds_columns_become_null_without_exception(self):
        csv_text = (
            "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
            "18/08/2012,Arsenal,Sunderland,0,0,D\n"
        )
        matches, skipped = history.parse_csv_text(csv_text, "E0", 2012)
        self.assertEqual(len(matches), 1)
        self.assertEqual(sum(skipped.values()), 0)
        record = matches[0]
        for source in ("pinnacle", "b365", "avg", "max"):
            self.assertEqual(
                record["odds"][source],
                {"open": None, "close": None, "implied": None, "fair": None},
            )

    def test_non_numeric_odds_become_null(self):
        csv_text = (
            "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,PSCH,PSCD,PSCA\n"
            "18/08/2012,Arsenal,Sunderland,0,0,D,1.60,abc,\n"
        )
        matches, skipped = history.parse_csv_text(csv_text, "E0", 2012)
        self.assertEqual(sum(skipped.values()), 0)
        self.assertIsNone(matches[0]["odds"]["pinnacle"]["close"])
        self.assertIsNone(matches[0]["odds"]["pinnacle"]["fair"])

    def test_mirror_all_seasons_file_filters_by_season(self):
        csv_text = (
            "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
            "18/08/2011,Arsenal,Sunderland,0,0,D\n"
            "18/08/2012,Arsenal,Sunderland,0,0,D\n"
        )
        matches, skipped = history.parse_csv_text(
            csv_text, "E0", 2012, filter_season=True
        )
        self.assertEqual([m["date"] for m in matches], ["2012-08-18"])
        self.assertEqual(skipped["out_of_season"], 1)


if __name__ == "__main__":
    unittest.main()
