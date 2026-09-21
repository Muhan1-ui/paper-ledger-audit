import unittest
from unittest.mock import patch

from outputs.tai_cai_tracker.thesportsdb import (
    collect_thesportsdb_pregame_context,
    resolve_thesportsdb_event,
)


IDENTITY = {
    "competition": "K League 1",
    "home_team": "江原FC",
    "away_team": "全北现代",
    "kickoff_at": "2026-09-09T18:30:00+08:00",
}


class TheSportsDbResolverTests(unittest.TestCase):
    def test_adopts_exact_timestamp_with_verified_english_name_anchors(self):
        collected = {
            "status": "collected",
            "source_url": ["https://www.thesportsdb.com/api/v1/json/123/searchevents.php?e=Gangwon%20FC_vs_Jeonbuk%20Hyundai%20Motors&d=2026-09-09"],
            "captured_at": "2026-09-09T12:00:00+08:00",
            "candidates": [{
                "event_id": "2416546",
                "home_team": "Gangwon FC",
                "away_team": "Jeonbuk Hyundai Motors",
                "home_team_id": "135624",
                "away_team_id": "135625",
                "competition": "K League 1",
                "kickoff_at": "2026-09-09T18:30:00+08:00",
                "source_url": "https://www.thesportsdb.com/api/v1/json/123/lookupevent.php?id=2416546",
            }],
        }

        with patch(
            "outputs.tai_cai_tracker.thesportsdb.collect_thesportsdb_event_candidates",
            return_value=collected,
        ):
            result = resolve_thesportsdb_event(
                IDENTITY,
                home_team_names=["Gangwon FC"],
                away_team_names=["Jeonbuk Hyundai Motors"],
            )

        self.assertEqual(result["status"], "adopted")
        self.assertEqual(result["provider"], "TheSportsDB")
        self.assertEqual(result["event_url"], collected["candidates"][0]["source_url"])

    def test_rejects_candidate_with_one_hour_kickoff_difference(self):
        collected = {
            "status": "collected",
            "source_url": ["https://www.thesportsdb.com/api/v1/json/123/searchevents.php?e=Gangwon%20FC_vs_Jeonbuk%20Hyundai%20Motors&d=2026-09-09"],
            "captured_at": "2026-09-09T12:00:00+08:00",
            "candidates": [{
                "event_id": "2416546",
                "home_team": "Gangwon FC",
                "away_team": "Jeonbuk Hyundai Motors",
                "home_team_id": "135624",
                "away_team_id": "135625",
                "competition": "K League 1",
                "kickoff_at": "2026-09-09T19:30:00+08:00",
                "source_url": "https://www.thesportsdb.com/api/v1/json/123/lookupevent.php?id=2416546",
            }],
        }

        with patch(
            "outputs.tai_cai_tracker.thesportsdb.collect_thesportsdb_event_candidates",
            return_value=collected,
        ):
            result = resolve_thesportsdb_event(
                IDENTITY,
                home_team_names=["Gangwon FC"],
                away_team_names=["Jeonbuk Hyundai Motors"],
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no context-event candidates")


class TheSportsDbContextTests(unittest.TestCase):
    def test_context_uses_verified_venue_and_keeps_null_lineup_unavailable(self):
        event_url = "https://www.thesportsdb.com/api/v1/json/123/lookupevent.php?id=2416546"
        venue_url = "https://www.thesportsdb.com/api/v1/json/123/lookupvenue.php?id=17215"
        lineup_url = "https://www.thesportsdb.com/api/v1/json/123/lookuplineup.php?id=2416546"
        event = {
            "idEvent": "2416546",
            "idHomeTeam": "135624",
            "idAwayTeam": "135625",
            "strHomeTeam": "Gangwon FC",
            "strAwayTeam": "Jeonbuk Hyundai Motors",
            "strLeague": "K League 1",
            "strSeason": "2026",
            "strTimestamp": "2026-09-09T10:30:00+00:00",
            "idVenue": "17215",
        }

        with patch(
            "outputs.tai_cai_tracker.thesportsdb._http_get_json",
            side_effect=[
                {"events": [event]},
                {"venues": [{"strMap": "37.773660°N 128.897503°E"}]},
                {"lineup": None},
            ],
        ), patch(
            "outputs.tai_cai_tracker.thesportsdb.collect_weather_forecast",
            return_value={"status": "available", "source_url": "https://api.open-meteo.com/example"},
        ):
            context = collect_thesportsdb_pregame_context(
                IDENTITY,
                event_url,
                home_team_names=["Gangwon FC"],
                away_team_names=["Jeonbuk Hyundai Motors"],
            )

        self.assertEqual(context["event"]["status"], "matched")
        self.assertEqual(context["event"]["provider"], "TheSportsDB")
        self.assertEqual(context["event"]["venue"], {
            "latitude": "37.773660",
            "longitude": "128.897503",
        })
        self.assertEqual(context["weather"]["status"], "available")
        self.assertEqual(context["lineup"], {
            "status": "unavailable",
            "reason": "complete_lineup_unavailable",
        })
        self.assertEqual(context["split_xg"], {
            "status": "unavailable",
            "reason": "the_sports_db_does_not_publish_home_away_xg",
        })


if __name__ == "__main__":
    unittest.main()
