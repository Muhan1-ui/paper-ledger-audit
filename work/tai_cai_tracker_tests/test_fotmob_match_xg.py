import unittest
from unittest.mock import patch

from outputs.tai_cai_tracker import fotmob_match_xg


MATCH_DETAILS_FIXTURE = {
    "general": {"coverageLevel": "xG"},
    "content": {
        "stats": {
            "Periods": {
                "All": {
                    "stats": [
                        {
                            "groupName": "Top stats",
                            "stats": [
                                {"title": "Expected goals (xG)", "stats": ["1.93", "0.24"]},
                                {"title": "xG on target (xGOT)", "stats": ["2.35", "0.09"]},
                                {"title": "Ball possession", "stats": ["55%", "45%"]},
                            ],
                        },
                        # Group header duplicate without a value array must be ignored.
                        {"title": "Expected goals (xG)"},
                        {
                            "groupName": "Expected goals",
                            "stats": [
                                {"title": "xG open play", "stats": ["1.42", "0.11"]},
                                {"title": "xG set play", "stats": ["0.51", "0.13"]},
                                {"title": "xG non-penalty", "stats": ["1.93", "0.24"]},
                            ],
                        },
                    ]
                }
            }
        }
    },
}

MATCHES_FIXTURE = {
    "leagues": [
        {
            "name": "Premier League",
            "ccode": "ENG",
            "matches": [
                {
                    "id": 5868011,
                    "home": {"name": "Arsenal"},
                    "away": {"name": "Chelsea"},
                    "status": {
                        "utcTime": "2026-08-15T17:30:00Z",
                        "finished": True,
                        "scoreStr": "3 - 0",
                    },
                },
                {
                    "id": 5868012,
                    "home": {"name": "Liverpool"},
                    "away": {"name": "Everton"},
                    "status": {
                        "utcTime": "2026-08-15T20:00:00Z",
                        "finished": False,
                    },
                },
            ],
        },
        {"name": "No matches league", "ccode": "XXX", "matches": []},
    ]
}


class FotMobMatchXgTests(unittest.TestCase):
    def test_match_xg_extracts_all_fields_as_floats(self):
        with patch.object(fotmob_match_xg, "_http_json", return_value=MATCH_DETAILS_FIXTURE):
            result = fotmob_match_xg.match_xg("5868011")

        self.assertEqual(result["coverage"], "xG")
        self.assertEqual(result["home_xg"], 1.93)
        self.assertEqual(result["away_xg"], 0.24)
        self.assertEqual(result["home_xgot"], 2.35)
        self.assertEqual(result["away_xgot"], 0.09)
        self.assertEqual(result["home_xg_open_play"], 1.42)
        self.assertEqual(result["away_xg_open_play"], 0.11)
        self.assertEqual(result["home_xg_set_play"], 0.51)
        self.assertEqual(result["away_xg_set_play"], 0.13)
        self.assertEqual(result["home_xg_non_penalty"], 1.93)
        self.assertEqual(result["away_xg_non_penalty"], 0.24)
        for key in fotmob_match_xg._XG_KEYS:
            self.assertIsInstance(result[key], float)

    def test_match_xg_returns_none_values_for_ratings_coverage(self):
        fixture = {
            "general": {"coverageLevel": "ratings"},
            "content": {
                "stats": {
                    "Periods": {
                        "All": {
                            "stats": [
                                {
                                    "groupName": "Top stats",
                                    "stats": [
                                        {"title": "Ball possession", "stats": ["50%", "50%"]},
                                    ],
                                }
                            ]
                        }
                    }
                }
            },
        }
        with patch.object(fotmob_match_xg, "_http_json", return_value=fixture):
            result = fotmob_match_xg.match_xg("1234567")

        self.assertIsInstance(result, dict)
        self.assertEqual(result["coverage"], "ratings")
        for key in fotmob_match_xg._XG_KEYS:
            self.assertIsNone(result[key])

    def test_match_xg_leaves_missing_titles_as_none(self):
        fixture = {
            "general": {"coverageLevel": "xG"},
            "content": {
                "stats": {
                    "Periods": {
                        "All": {
                            "stats": [
                                {
                                    "groupName": "Top stats",
                                    "stats": [
                                        {"title": "Expected goals (xG)", "stats": ["0.8", "1.1"]},
                                    ],
                                }
                            ]
                        }
                    }
                }
            },
        }
        with patch.object(fotmob_match_xg, "_http_json", return_value=fixture):
            result = fotmob_match_xg.match_xg("7654321")

        self.assertEqual(result["home_xg"], 0.8)
        self.assertEqual(result["away_xg"], 1.1)
        self.assertIsNone(result["home_xgot"])
        self.assertIsNone(result["away_xg_open_play"])

    def test_list_matches_by_date_parses_league_matches(self):
        with patch.object(fotmob_match_xg, "_http_json", return_value=MATCHES_FIXTURE) as http_json:
            matches = fotmob_match_xg.list_matches_by_date("20260815")

        http_json.assert_called_once_with(
            "https://www.fotmob.com/api/data/matches?date=20260815"
        )
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0], {
            "match_id": "5868011",
            "home": "Arsenal",
            "away": "Chelsea",
            "utc_time": "2026-08-15T17:30:00Z",
            "league": "Premier League",
            "ccode": "ENG",
            "finished": True,
            "score": "3 - 0",
        })
        self.assertEqual(matches[1]["match_id"], "5868012")
        self.assertEqual(matches[1]["finished"], False)
        self.assertIsNone(matches[1]["score"])


if __name__ == "__main__":
    unittest.main()
