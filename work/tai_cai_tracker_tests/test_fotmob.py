import inspect
import json
import hashlib
import unittest
from unittest.mock import patch

from outputs.tai_cai_tracker.fotmob import (
    FOTMOB_SEARCH_ALIASES,
    FotMobSearchResolutionError,
    _registry_team_entry,
    build_fotmob_season_bridge_xg,
    build_match_xg_evidence,
    collect_team_search_candidates,
    discover_match_team_stats_urls,
    discover_team_stats_url,
    parse_stat_payload,
    parse_team_search_results,
    parse_team_page,
)


def _team_page(team_id=92630, name="FC Seoul", stat_value="31.4", conceded_value="23.3"):
    payload = {
        "props": {
            "pageProps": {
                "fallback": {
                    f"team-{team_id}": {
                        "details": {
                            "id": team_id,
                            "name": name,
                            "latestSeason": "2026",
                            "country": "KOR",
                        },
                        "stats": {
                            "teamId": str(team_id),
                            "primaryLeagueId": 9080,
                            "primarySeasonId": "30957",
                            "tournamentId": "30957",
                            "tournamentSeasons": [
                                {
                                    "name": "K League 1 2026",
                                    "season": "2026",
                                    "leagueName": "K League 1",
                                    "tournamentId": "30957",
                                    "parentLeagueId": "9080",
                                }
                            ],
                            "teams": [
                                {
                                    "header": "Expected goals",
                                    "participant": {
                                        "teamId": team_id,
                                        "value": float(stat_value),
                                        "stat": {"name": "expected_goals_team"},
                                    },
                                    "fetchAllUrl": "https://data.fotmob.com/stats/9080/season/30957/expected_goals_team.json",
                                },
                                {
                                    "header": "xG conceded",
                                    "participant": {
                                        "teamId": team_id,
                                        "value": float(conceded_value),
                                        "stat": {"name": "expected_goals_conceded_team"},
                                    },
                                    "fetchAllUrl": "https://data.fotmob.com/stats/9080/season/30957/expected_goals_conceded_team.json",
                                },
                            ],
                        },
                        "table": [
                            {
                                "data": {
                                    "leagueName": "K League 1",
                                    "table": {
                                        "all": [
                                            {
                                                "id": team_id,
                                                "name": name,
                                                "played": 19,
                                            }
                                        ]
                                    },
                                }
                            }
                        ],
                    }
                }
            }
        }
    }
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></html>"
    )


class FotMobTests(unittest.TestCase):
    def test_collect_team_search_candidates_keeps_raw_search_evidence_without_identity_adoption(self):
        payload = {
            "suggestions": [{
                "type": "team",
                "entity": {"id": 8564, "name": "AC Milan", "leagueName": "Serie A"},
            }]
        }
        with patch(
            "outputs.tai_cai_tracker.fotmob._http_get",
            return_value=(json.dumps(payload), {"X-Tai-Cai-Raw-Content-SHA256": "a" * 64}),
        ) as fetch:
            result = collect_team_search_candidates("AC Milan")

        self.assertEqual(result["search_term"], "AC Milan")
        self.assertEqual(result["raw_content_sha256"], "a" * 64)
        self.assertEqual(result["candidates"][0]["team_id"], 8564)
        self.assertEqual(result["candidates"][0]["team_name"], "AC Milan")
        self.assertIn("AC%20Milan", fetch.call_args.args[0])
        self.assertIn("captured_at", result)

    def test_curated_aliases_cover_verified_current_saudi_and_ecuadorian_clubs(self):
        self.assertEqual(FOTMOB_SEARCH_ALIASES["胡巴卡德"], "Al Kholood")
        self.assertEqual(FOTMOB_SEARCH_ALIASES["吉达国民"], "Al Ahli")
        self.assertEqual(FOTMOB_SEARCH_ALIASES["基多体大"], "LDU de Quito")

    def test_discover_team_stats_url_uses_provider_search_when_registry_has_no_entry(self):
        payload = {
            "suggestions": [{
                "type": "team",
                "entity": {"id": 12345, "name": "Salford City FC", "leagueName": "League Two"},
            }]
        }

        self.assertIsNone(_registry_team_entry("Salford City FC"))

        with patch("outputs.tai_cai_tracker.fotmob._http_get", return_value=(json.dumps(payload), {})) as fetch:
            result = discover_team_stats_url("Salford City FC")

        self.assertEqual(result["team_name"], "Salford City FC")
        self.assertIn("Salford%20City%20FC", fetch.call_args.args[0])

    def test_discover_team_stats_url_resolves_registered_name_without_network(self):
        entry = _registry_team_entry("普雷斯顿")
        self.assertIsNotNone(entry)

        with patch("outputs.tai_cai_tracker.fotmob._http_get") as fetch:
            result = discover_team_stats_url("普雷斯顿")

        fetch.assert_not_called()
        self.assertEqual(result["team_name"], entry["source_name"])
        self.assertEqual(result["source_url"], entry["source_url"])

    def test_discover_team_stats_url_resolves_registered_ac_milan_without_network(self):
        entry = _registry_team_entry("AC米兰")
        self.assertIsNotNone(entry)

        with patch("outputs.tai_cai_tracker.fotmob._http_get") as fetch:
            result = discover_team_stats_url("AC米兰")

        fetch.assert_not_called()
        self.assertEqual(result["team_name"], entry["source_name"])
        self.assertEqual(result["source_url"], entry["source_url"])

    def test_discover_team_stats_url_labels_empty_public_search_as_no_public_candidates(self):
        payload = {"suggestions": []}

        with patch("outputs.tai_cai_tracker.fotmob._http_get", return_value=(json.dumps(payload), {})):
            with self.assertRaises(FotMobSearchResolutionError) as raised:
                discover_team_stats_url("Unknown FC")

        self.assertEqual(raised.exception.failure_code, "no_public_candidates")

    def test_discover_team_stats_url_labels_tied_strict_matches_as_ambiguous_identity(self):
        payload = {
            "suggestions": [
                {"type": "team", "entity": {"id": 1, "name": "Seoul FC"}},
                {"type": "team", "entity": {"id": 2, "name": "Seoul Fc"}},
            ]
        }

        with patch("outputs.tai_cai_tracker.fotmob._http_get", return_value=(json.dumps(payload), {})):
            with self.assertRaises(FotMobSearchResolutionError) as raised:
                discover_team_stats_url("Seoul FC")

        self.assertEqual(raised.exception.failure_code, "ambiguous_identity")

    def test_fotmob_season_bridge_preserves_league_total_scope(self):
        def team(team_id, name, attack, defense, samples, season, source_suffix):
            return {
                "team_id": team_id,
                "team_name": name,
                "competition": "Championship",
                "season": season,
                "captured_at": "2026-09-01T12:00:00+00:00",
                "stats": {
                    "expected_goals_team": {
                        "source_url": f"https://data.fotmob.com/{source_suffix}/xg",
                        "raw_content_sha256": "a" * 64,
                        "published_at": "2026-09-01T10:00:00+00:00",
                        "value": str(attack),
                        "sample_matches": samples,
                    },
                    "expected_goals_conceded_team": {
                        "source_url": f"https://data.fotmob.com/{source_suffix}/xga",
                        "raw_content_sha256": "b" * 64,
                        "published_at": "2026-09-01T10:00:00+00:00",
                        "value": str(defense),
                        "sample_matches": samples,
                    },
                },
            }

        result = build_fotmob_season_bridge_xg(
            team(1, "Home FC", "3", "2", 3, "2026/2027", "current-home"),
            team(2, "Away FC", "4", "5", 3, "2026/2027", "current-away"),
            team(1, "Home FC", "50", "40", 20, "2025/2026", "prior-home"),
            team(2, "Away FC", "45", "55", 20, "2025/2026", "prior-away"),
        )

        evidence = result["xg_inputs"]["evidence"]
        self.assertEqual(result["provider"], "FotMob season bridge")
        self.assertEqual(evidence["scope"], "league_total")
        self.assertEqual(evidence["current_season"]["home_sample_matches"], 3)
        self.assertEqual(evidence["prior_season"]["away_sample_matches"], 20)

    def test_parse_team_search_results_keeps_only_team_entries_with_ids(self):
        payload = {
            "suggestions": [
                {
                    "type": "team",
                    "entity": {
                        "id": 133901,
                        "name": "Gimcheon Sangmu",
                        "leagueName": "K League 1",
                    },
                },
                {"type": "player", "entity": {"id": 77, "name": "Gimcheon Player"}},
                {"type": "team", "entity": {"name": "Missing identifier"}},
            ]
        }

        self.assertEqual(
            parse_team_search_results(payload),
            [{
                "team_id": 133901,
                "team_name": "Gimcheon Sangmu",
                "competition": "K League 1",
                "source_url": "https://www.fotmob.com/teams/133901/overview/gimcheon-sangmu",
            }],
        )

    def test_parse_team_search_results_reads_current_grouped_response(self):
        payload = [
            {
                "title": {"key": "all", "value": "All"},
                "suggestions": [
                    {
                        "type": "team",
                        "id": "133898",
                        "name": "Jeju SK",
                        "leagueName": "K-League 1",
                    },
                    {"type": "match", "id": "5140007", "homeTeamName": "Jeju SK"},
                ],
            },
            {
                "title": {"key": "teams", "value": "Teams"},
                "suggestions": [
                    {
                        "type": "team",
                        "id": "133898",
                        "name": "Jeju SK",
                        "leagueName": "K-League 1",
                    }
                ],
            },
        ]

        self.assertEqual(
            parse_team_search_results(payload),
            [{
                "team_id": 133898,
                "team_name": "Jeju SK",
                "competition": "K-League 1",
                "source_url": "https://www.fotmob.com/teams/133898/overview/jeju-sk",
            }],
        )

    def test_discover_team_stats_url_rejects_ambiguous_high_score_results(self):
        payload = {
            "suggestions": [
                {"type": "team", "entity": {"id": 1, "name": "Seoul FC"}},
                {"type": "team", "entity": {"id": 2, "name": "Seoul FC"}},
            ]
        }

        with patch("outputs.tai_cai_tracker.fotmob._http_get", return_value=(json.dumps(payload), {})):
            with self.assertRaisesRegex(ValueError, "ambiguous FotMob team search"):
                discover_team_stats_url("Seoul FC")

    def test_discover_team_stats_url_uses_provider_search_priority_to_break_a_name_tie(self):
        payload = [{
            "suggestions": [
                {"type": "team", "id": "8650", "name": "Liverpool", "score": 300996},
                {"type": "team", "id": "2219", "name": "Liverpool FC", "score": 300002},
            ]
        }]

        with patch("outputs.tai_cai_tracker.fotmob._http_get", return_value=(json.dumps(payload), {})):
            result = discover_team_stats_url("Liverpool")

        self.assertEqual(result["team_id"], 8650)
        self.assertEqual(result["source_search_score"], 300996)

    def test_discover_team_stats_url_uses_provider_priority_within_the_similarity_tie_margin(self):
        payload = [{
            "suggestions": [
                {"type": "team", "id": "9906", "name": "Atl\u00e9tico Madrid", "score": 300823},
                {"type": "team", "id": "189673", "name": "Atletico Madrid C", "score": 300000},
            ]
        }]

        with patch("outputs.tai_cai_tracker.fotmob._http_get", return_value=(json.dumps(payload), {})):
            result = discover_team_stats_url("Atletico Madrid")

        self.assertEqual(result["team_id"], 9906)

    def test_discover_team_stats_url_keeps_provider_search_for_unregistered_name(self):
        payload = {
            "suggestions": [
                {"type": "team", "entity": {"id": 12346, "name": "Havant and Waterlooville"}},
            ]
        }

        self.assertIsNone(_registry_team_entry("Havant and Waterlooville"))

        with patch("outputs.tai_cai_tracker.fotmob._http_get", return_value=(json.dumps(payload), {})) as get:
            result = discover_team_stats_url("Havant and Waterlooville")

        self.assertEqual(result["team_id"], 12346)
        self.assertIn("Havant%20and%20Waterlooville", get.call_args.args[0])

    def test_discover_match_team_stats_urls_resolves_both_teams(self):
        home = {"team_id": 1, "team_name": "Home FC", "source_url": "https://www.fotmob.com/teams/1/overview/home-fc"}
        away = {"team_id": 2, "team_name": "Away FC", "source_url": "https://www.fotmob.com/teams/2/overview/away-fc"}

        with patch(
            "outputs.tai_cai_tracker.fotmob.discover_team_stats_url",
            side_effect=[home, away],
        ):
            result = discover_match_team_stats_urls("Home FC", "Away FC")

        self.assertEqual(result, {"home": home, "away": away})

    def test_parse_team_page_extracts_team_identity_and_stat_urls(self):
        result = parse_team_page(
            _team_page(),
            "https://www.fotmob.com/teams/92630/stats/fc-seoul/teams",
        )

        self.assertEqual(result["team_id"], 92630)
        self.assertEqual(result["team_name"], "FC Seoul")
        self.assertEqual(result["competition"], "K League 1")
        self.assertEqual(result["season"], "2026")
        self.assertEqual(result["sample_matches"], 19)
        self.assertEqual(
            result["stat_urls"]["expected_goals_team"],
            "https://data.fotmob.com/stats/9080/season/30957/expected_goals_team.json",
        )

    def test_parse_stat_payload_extracts_team_value_and_publication_header(self):
        payload = {
            "LeagueName": "K League 1",
            "TopLists": [
                {
                    "StatName": "expected_goals_team",
                    "StatList": [
                        {
                            "TeamId": 92630,
                            "ParticipantName": "FC Seoul",
                            "StatValue": 31.4,
                            "MatchesPlayed": 19,
                            "StatValueCount": 19,
                        }
                    ],
                }
            ],
        }

        result = parse_stat_payload(
            payload,
            team_id=92630,
            stat_name="expected_goals_team",
            source_url="https://data.fotmob.com/stats/9080/season/30957/expected_goals_team.json",
            last_modified="Sun, 26 Jul 2026 12:34:40 GMT",
        )

        self.assertEqual(result["value"], "31.4")
        self.assertEqual(result["sample_matches"], 19)
        self.assertEqual(result["published_at"], "2026-07-26T12:34:40+00:00")

    def test_parse_stat_payload_preserves_exact_raw_response_hash(self):
        raw = b'{"LeagueName":"K League 1","TopLists":[{"StatName":"expected_goals_team","StatList":[{"TeamId":92630,"ParticipantName":"FC Seoul","StatValue":31.4,"MatchesPlayed":19}]}]}'
        payload = json.loads(raw)

        self.assertIn("raw_content_sha256", inspect.signature(parse_stat_payload).parameters)

    def test_build_match_xg_evidence_uses_league_total_formula(self):
        home = {
            "source_url": "https://www.fotmob.com/teams/92630/stats/fc-seoul/teams",
            "team_id": 92630,
            "team_name": "FC Seoul",
            "competition": "K League 1",
            "season": "2026",
            "sample_matches": 19,
            "stats": {
                "expected_goals_team": {
                    "value": "31.4",
                    "sample_matches": 19,
                    "source_url": "https://data.fotmob.com/stats/9080/season/30957/expected_goals_team.json",
                    "published_at": "2026-07-26T12:34:40+00:00",
                },
                "expected_goals_conceded_team": {
                    "value": "23.3",
                    "sample_matches": 19,
                    "source_url": "https://data.fotmob.com/stats/9080/season/30957/expected_goals_conceded_team.json",
                    "published_at": "2026-07-26T12:34:40+00:00",
                },
            },
        }
        away = {
            "source_url": "https://www.fotmob.com/teams/133896/stats/ulsan-hd-fc/teams",
            "team_id": 133896,
            "team_name": "Ulsan HD FC",
            "competition": "K League 1",
            "season": "2026",
            "sample_matches": 19,
            "stats": {
                "expected_goals_team": {
                    "value": "34.3",
                    "sample_matches": 19,
                    "source_url": "https://data.fotmob.com/stats/9080/season/30957/expected_goals_team.json",
                    "published_at": "2026-07-26T12:34:40+00:00",
                },
                "expected_goals_conceded_team": {
                    "value": "26.1",
                    "sample_matches": 19,
                    "source_url": "https://data.fotmob.com/stats/9080/season/30957/expected_goals_conceded_team.json",
                    "published_at": "2026-07-26T12:34:40+00:00",
                },
            },
        }

        result = build_match_xg_evidence(home, away, model_weight="0.40")

        self.assertEqual(result["xg_inputs"]["home_xg"], "1.513157894736842105263157895")
        self.assertEqual(result["xg_inputs"]["away_xg"], "1.515789473684210526315789474")
        self.assertEqual(result["xg_inputs"]["confidence_ceiling"], "medium")
        self.assertEqual(result["xg_inputs"]["evidence"]["scope"], "league_total")
        self.assertEqual(result["xg_inputs"]["evidence"]["sample_matches"], 19)
        self.assertIn("league_total", result["xg_inputs"]["evidence"]["formula"])

    def test_build_match_xg_evidence_uses_each_teams_verified_sample_when_counts_differ(self):
        home = {
            "source_url": "https://www.fotmob.com/teams/1/overview/home",
            "team_name": "Home FC",
            "competition": "K League 1",
            "season": "2026",
            "stats": {
                "expected_goals_team": {"value": "30.8", "sample_matches": 24, "source_url": "https://data.fotmob.com/xg", "published_at": "2026-08-25T12:21:22+00:00"},
                "expected_goals_conceded_team": {"value": "44.1", "sample_matches": 24, "source_url": "https://data.fotmob.com/xga", "published_at": "2026-08-25T12:21:22+00:00"},
            },
        }
        away = {
            "source_url": "https://www.fotmob.com/teams/2/overview/away",
            "team_name": "Away FC",
            "competition": "K League 1",
            "season": "2026",
            "stats": {
                "expected_goals_team": {"value": "34.3", "sample_matches": 23, "source_url": "https://data.fotmob.com/xg", "published_at": "2026-08-25T12:21:22+00:00"},
                "expected_goals_conceded_team": {"value": "26.1", "sample_matches": 23, "source_url": "https://data.fotmob.com/xga", "published_at": "2026-08-25T12:21:22+00:00"},
            },
        }

        result = build_match_xg_evidence(home, away)

        evidence = result["xg_inputs"]["evidence"]
        self.assertEqual(evidence["home_sample_matches"], 24)
        self.assertEqual(evidence["away_sample_matches"], 23)
        self.assertEqual(result["xg_inputs"]["home_xg"], "1.209057971014492753623188406")
        self.assertEqual(result["xg_inputs"]["away_xg"], "1.664402173913043478260869565")


if __name__ == "__main__":
    unittest.main()
