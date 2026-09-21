import json
import unittest
from unittest.mock import patch

from outputs.tai_cai_tracker.understat import (
    build_season_bridge_xg,
    build_xg_inputs,
    collect_season_bridge_xg,
    collect_league_xg_batch,
    collect_league_xg,
    league_name_from_slug,
    parse_league_xg,
    resolve_anchored_team_name,
)


class UnderstatTests(unittest.TestCase):
    def test_resolves_one_understat_roster_title_from_exact_wikidata_english_names(self):
        payload = {
            "teams": {
                "1": {"id": "1", "title": "Inter", "history": []},
                "2": {"id": "2", "title": "Roma", "history": []},
            }
        }

        result = resolve_anchored_team_name(payload, {
            "english_name": "Inter Milan",
            "english_names": ["Inter Milan", "Inter"],
        })

        self.assertEqual(result, {
            "status": "resolved",
            "source_name": "Inter",
            "source_team_id": "1",
        })

    def test_rejects_ambiguous_understat_roster_matches_from_an_anchor(self):
        payload = {
            "teams": {
                "1": {"id": "1", "title": "Inter", "history": []},
                "2": {"id": "2", "title": "Inter", "history": []},
            }
        }

        result = resolve_anchored_team_name(payload, {
            "english_name": "Inter Milan",
            "english_names": ["Inter Milan", "Inter"],
        })

        self.assertEqual(result, {"status": "unavailable", "reason": "understat_anchor_ambiguous"})

    def test_batch_uses_wikidata_anchors_when_explicit_understat_names_are_unavailable(self):
        payload = {
            "teams": {
                "1": {"id": "1", "title": "Inter", "history": [
                    {"h_a": "h", "xG": "1.5", "xGA": "0.8"},
                ]},
                "2": {"id": "2", "title": "Roma", "history": [
                    {"h_a": "a", "xG": "1.2", "xGA": "1.4"},
                ]},
            }
        }
        fixtures = [{
            "match_id": "2041001",
            "home_wikidata": {"english_name": "Inter Milan", "english_names": ["Inter Milan", "Inter"]},
            "away_wikidata": {"english_name": "Roma", "english_names": ["Roma"]},
        }]

        with patch(
            "outputs.tai_cai_tracker.understat._http_get",
            return_value=(json.dumps(payload), {}, "a" * 64),
        ) as fetch:
            result = collect_league_xg_batch("Serie_A", "2026", fixtures)

        fetch.assert_called_once()
        fixture = result["fixtures"][0]
        self.assertEqual(fixture["status"], "available")
        self.assertEqual(fixture["home_team"], "Inter")
        self.assertEqual(fixture["away_team"], "Roma")
        self.assertEqual(fixture["home"]["team_id"], "1")

    def _bridge_source(self, season, home_samples, away_samples, competition="Premier League"):
        return {
            "source_url": f"https://understat.com/getLeagueData/EPL/{season}",
            "captured_at": "2099-08-20T10:00:00+08:00",
            "raw_content_sha256": str(season)[-1] * 64,
            "competition": competition,
            "season": f"{season}/{int(season) + 1}",
            "totals": {
                "home": {
                    "team_id": "1", "team_name": "Home FC",
                    "sample_matches": home_samples, "xg": str(home_samples * 2), "xga": str(home_samples),
                },
                "away": {
                    "team_id": "2", "team_name": "Away FC",
                    "sample_matches": away_samples, "xg": str(away_samples * 1.5), "xga": str(away_samples * 1.25),
                },
            },
        }

    def test_build_season_bridge_xg_requires_two_current_and_twelve_prior_venue_samples(self):
        result = build_season_bridge_xg(
            self._bridge_source("2099", 2, 2),
            self._bridge_source("2098", 12, 12),
            {"home_team": "Home FC", "away_team": "Away FC"},
        )

        evidence = result["xg_inputs"]["evidence"]
        self.assertEqual(result["provider"], "Understat season bridge")
        self.assertEqual(result["xg_inputs"]["confidence_ceiling"], "medium")
        self.assertEqual(evidence["scope"], "home_away_split")
        self.assertEqual(evidence["current_season"]["home_sample_matches"], 2)
        self.assertEqual(evidence["prior_season"]["away_sample_matches"], 12)
        self.assertIn("season_bridge", evidence["formula"])
        self.assertEqual(evidence["current_season"]["raw_content_sha256"], "9" * 64)
        self.assertEqual(evidence["prior_season"]["raw_content_sha256"], "8" * 64)

    def test_build_season_bridge_xg_rejects_unproven_continuity_or_samples(self):
        current = self._bridge_source("2099", 2, 2)
        prior = self._bridge_source("2098", 12, 12)
        fixture = {"home_team": "Home FC", "away_team": "Away FC"}

        with self.assertRaisesRegex(ValueError, "same competition"):
            build_season_bridge_xg(current, {**prior, "competition": "Championship"}, fixture)
        with self.assertRaisesRegex(ValueError, "current sample is below two"):
            build_season_bridge_xg(self._bridge_source("2099", 1, 2), prior, fixture)
        with self.assertRaisesRegex(ValueError, "prior sample is below twelve"):
            build_season_bridge_xg(current, self._bridge_source("2098", 11, 12), fixture)
        different_team = self._bridge_source("2098", 12, 12)
        different_team["totals"]["home"]["team_name"] = "Renamed FC"
        with self.assertRaisesRegex(ValueError, "team identity changed"):
            build_season_bridge_xg(current, different_team, fixture)

    def test_collect_season_bridge_xg_reads_current_and_prior_same_league_payloads(self):
        def payload(home_rows, away_rows):
            return {
                "teams": {
                    "1": {"id": "1", "title": "Home FC", "history": home_rows},
                    "2": {"id": "2", "title": "Away FC", "history": away_rows},
                }
            }

        current = payload(
            [{"date": "2099-08-01 18:00:00", "h_a": "h", "xG": "2", "xGA": "1"}] * 2,
            [{"date": "2099-08-02 18:00:00", "h_a": "a", "xG": "1.5", "xGA": "1.25"}] * 2,
        )
        prior = payload(
            [{"date": "2098-08-01 18:00:00", "h_a": "h", "xG": "2", "xGA": "1"}] * 12,
            [{"date": "2098-08-02 18:00:00", "h_a": "a", "xG": "1.5", "xGA": "1.25"}] * 12,
        )
        fixture = {
            "competition": "Premier League", "home_team": "Home FC", "away_team": "Away FC",
            "kickoff_at": "2099-08-30T20:00:00+08:00",
        }

        with patch("outputs.tai_cai_tracker.understat._http_get", side_effect=[
            (json.dumps(current), {}, "c" * 64), (json.dumps(prior), {}, "p" * 64),
        ]) as fetch:
            result = collect_season_bridge_xg("EPL", "2099", fixture)

        self.assertEqual(fetch.call_count, 2)
        self.assertIn("/2099", fetch.call_args_list[0].args[1])
        self.assertIn("/2098", fetch.call_args_list[1].args[1])
        self.assertEqual(result["xg_inputs"]["evidence"]["prior_season"]["season"], "2098/2099")
    def test_collect_league_xg_batch_uses_recent_venue_rows_strictly_before_kickoff_date(self):
        payload = {
            "teams": {
                "1": {
                    "id": "1", "title": "Bologna",
                    "history": [
                        {"date": "2026-08-01 18:00:00", "h_a": "h", "xG": "1", "xGA": "0.5"},
                        {"date": "2026-08-10 18:00:00", "h_a": "h", "xG": "2", "xGA": "1.5"},
                        {"date": "2026-08-19 18:00:00", "h_a": "h", "xG": "3", "xGA": "2.5"},
                        {"date": "2026-08-21 18:00:00", "h_a": "h", "xG": "99", "xGA": "99"},
                    ],
                },
                "2": {
                    "id": "2", "title": "Lazio",
                    "history": [
                        {"date": "2026-08-02 18:00:00", "h_a": "a", "xG": "1", "xGA": "1"},
                        {"date": "2026-08-18 18:00:00", "h_a": "a", "xG": "2", "xGA": "3"},
                        {"date": "2026-08-20 10:00:00", "h_a": "a", "xG": "99", "xGA": "99"},
                    ],
                },
            }
        }
        fixtures = [{
            "match_id": "2041003",
            "home_team": "Bologna",
            "away_team": "Lazio",
            "kickoff_at": "2026-08-20T20:00:00+08:00",
        }]

        with patch(
            "outputs.tai_cai_tracker.understat._http_get",
            return_value=(json.dumps(payload), {}, "b" * 64),
        ):
            result = collect_league_xg_batch("Serie_A", "2026", fixtures, rolling_matches=2)

        fixture = result["fixtures"][0]
        self.assertEqual(fixture["status"], "available")
        self.assertEqual(fixture["history_cutoff_date"], "2026-08-20")
        self.assertEqual(fixture["xg_inputs"]["home_xg"], "2.25")
        self.assertEqual(fixture["xg_inputs"]["away_xg"], "1.75")
        self.assertEqual(fixture["xg_inputs"]["evidence"]["sample_window_matches"], 2)
        self.assertEqual(fixture["xg_inputs"]["evidence"]["home_sample_last_date"], "2026-08-19")
        self.assertEqual(fixture["xg_inputs"]["evidence"]["away_sample_last_date"], "2026-08-18")

    def test_collect_league_xg_batch_reuses_one_payload_and_builds_venue_splits(self):
        payload = {
            "teams": {
                "1": {
                    "id": "1", "title": "Bologna",
                    "history": [
                        {"h_a": "h", "xG": "1.20", "xGA": "0.80"},
                        {"h_a": "h", "xG": "1.80", "xGA": "1.20"},
                        {"h_a": "a", "xG": "0.90", "xGA": "1.30"},
                    ],
                },
                "2": {
                    "id": "2", "title": "Lazio",
                    "history": [
                        {"h_a": "a", "xG": "1.10", "xGA": "1.40"},
                        {"h_a": "a", "xG": "0.90", "xGA": "1.60"},
                        {"h_a": "h", "xG": "1.70", "xGA": "0.70"},
                    ],
                },
                "3": {
                    "id": "3", "title": "Roma",
                    "history": [
                        {"h_a": "h", "xG": "2.00", "xGA": "0.50"},
                    ],
                },
            }
        }
        fixtures = [
            {"match_id": "2041001", "home_team": "Bologna", "away_team": "Lazio"},
            {"match_id": "2041002", "home_team": "Bologna", "away_team": "Roma"},
        ]

        with patch(
            "outputs.tai_cai_tracker.understat._http_get",
            return_value=(json.dumps(payload), {}, "a" * 64),
        ) as fetch:
            result = collect_league_xg_batch("Serie_A", "2026", fixtures)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["source_url"], "https://understat.com/getLeagueData/Serie%20A/2026")
        first, second = result["fixtures"]
        self.assertEqual(first["status"], "available")
        self.assertEqual(first["xg_inputs"]["home_xg"], "1.50")
        self.assertEqual(first["xg_inputs"]["away_xg"], "1.00")
        self.assertEqual(first["xg_inputs"]["evidence"]["scope"], "home_away_split")
        self.assertEqual(first["xg_inputs"]["evidence"]["home_sample_matches"], 2)
        self.assertEqual(first["xg_inputs"]["evidence"]["away_sample_matches"], 2)
        self.assertEqual(second["status"], "unavailable")
        self.assertEqual(second["reason"], "missing_understat_away_sample")

    def test_league_name_from_slug_uses_spaces_for_the_data_endpoint(self):
        self.assertEqual(league_name_from_slug("Serie_A"), "Serie A")

    def test_collect_league_xg_marks_the_data_request_as_ajax(self):
        payload = {"teams": {}}
        with patch(
            "outputs.tai_cai_tracker.understat._http_get",
            side_effect=[("", {}, "a" * 64), ("{}", {}, "b" * 64)],
        ) as fetch:
            with self.assertRaisesRegex(ValueError, "teams were not found"):
                collect_league_xg("Serie_A", "2026", "Bologna", "Lazio")

        self.assertEqual(fetch.call_args_list[1].kwargs["ajax"], True)

    def test_parse_league_xg_returns_league_totals_for_the_named_teams(self):
        payload = {
            "teams": {
                "1": {
                    "id": "1",
                    "title": "Bologna",
                    "history": [
                        {"xG": "1.20", "xGA": "0.80"},
                        {"xG": "0.70", "xGA": "1.10"},
                    ],
                },
                "2": {
                    "id": "2",
                    "title": "Lazio",
                    "history": [{"xG": "1.50", "xGA": "0.60"}],
                },
            }
        }

        result = parse_league_xg(payload, "Bologna", "Lazio")

        self.assertEqual(result["home"]["sample_matches"], 2)
        self.assertEqual(result["home"]["xg"], "1.90")
        self.assertEqual(result["home"]["xga"], "1.90")
        self.assertEqual(result["away"]["sample_matches"], 1)
        self.assertEqual(result["away"]["xg"], "1.50")
        self.assertEqual(result["away"]["xga"], "0.60")

    def test_parse_league_xg_rejects_missing_team(self):
        with self.assertRaisesRegex(ValueError, "team was not found"):
            parse_league_xg({"teams": {}}, "Bologna", "Lazio")

    def test_build_xg_inputs_keeps_missing_publication_time_explicit(self):
        totals = {
            "home": {"sample_matches": 10, "xg": "15", "xga": "9"},
            "away": {"sample_matches": 10, "xg": "11", "xga": "13"},
        }

        result = build_xg_inputs(
            totals,
            source_url="https://understat.com/getLeagueData/Serie%20A/2026",
            competition="Serie A",
            season="2026/2027",
            captured_at="2026-08-24T10:00:00+08:00",
        )

        self.assertEqual(result["evidence"]["published_at_unavailable_reason"], "source does not expose publication time")
        self.assertEqual(result["evidence"]["captured_at"], "2026-08-24T10:00:00+08:00")
        self.assertEqual(result["confidence_ceiling"], "medium")
        self.assertEqual(result["home_xg"], "1.4")
        self.assertEqual(result["away_xg"], "1.0")
        self.assertNotIn("captured_at", result)
