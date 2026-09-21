import unittest
import hashlib
from decimal import Decimal
from unittest.mock import patch

from outputs.tai_cai_tracker.context import (
    build_lineup,
    build_split_xg,
    build_weather,
    collect_weather_forecast,
    collect_pregame_context,
)


EVENT = {
    "source_url": "https://www.sofascore.com/event/123",
    "captured_at": "2026-07-27T18:00:00+08:00",
    "competition": "Test League",
    "season": "2026",
    "home_team_id": 10,
    "away_team_id": 20,
    "kickoff_at": "2026-07-28T01:00:00+08:00",
}

HOME_HOME = {
    "source_url": "https://www.sofascore.com/api/v1/team/10/stats/home",
    "captured_at": "2026-07-27T18:00:00+08:00",
    "matches": 10,
    "xg": "16.0",
    "xga": "11.0",
}

AWAY_AWAY = {
    "source_url": "https://www.sofascore.com/api/v1/team/20/stats/away",
    "captured_at": "2026-07-27T18:00:00+08:00",
    "matches": 8,
    "xg": "8.0",
    "xga": "12.0",
}


class SplitXgTests(unittest.TestCase):
    def test_build_split_xg_uses_verified_home_and_away_samples(self):
        evidence = build_split_xg(EVENT, HOME_HOME, AWAY_AWAY)

        self.assertEqual(evidence["status"], "available")
        self.assertEqual(evidence["scope"], "home_away_split")
        self.assertEqual(evidence["home_xg"], Decimal("1.55"))
        self.assertEqual(evidence["away_xg"], Decimal("1.05"))
        self.assertEqual(evidence["home_sample_matches"], 10)
        self.assertEqual(evidence["away_sample_matches"], 8)

    def test_build_split_xg_marks_incomplete_source_unavailable(self):
        unavailable = build_split_xg(EVENT, HOME_HOME, {"xg": None})

        self.assertEqual(unavailable, {
            "status": "unavailable",
            "reason": "home_away_xg_unavailable",
        })


class WeatherAndLineupTests(unittest.TestCase):
    def test_build_weather_selects_the_kickoff_hour_at_verified_venue(self):
        event = {
            **EVENT,
            "venue": {"latitude": "59.917", "longitude": "10.769"},
        }
        forecast = {
            "source_url": "https://api.open-meteo.com/v1/forecast?latitude=59.917",
            "captured_at": "2026-07-27T18:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "hourly": [
                {"at": "2026-07-28T00:00:00+08:00", "temperature_c": "14", "precipitation_mm": "0", "wind_speed_kmh": "6"},
                {"at": "2026-07-28T01:00:00+08:00", "temperature_c": "13", "precipitation_mm": "0.4", "wind_speed_kmh": "8"},
            ],
        }

        weather = build_weather(event, forecast)

        self.assertEqual(weather["status"], "available")
        self.assertEqual(weather["temperature_c"], Decimal("13"))
        self.assertEqual(weather["precipitation_mm"], Decimal("0.4"))
        self.assertEqual(weather["wind_speed_kmh"], Decimal("8"))

    def test_build_weather_marks_missing_coordinates_unavailable(self):
        self.assertEqual(
            build_weather(EVENT, {"hourly": []}),
            {"status": "unavailable", "reason": "venue_coordinates_unavailable"},
        )

    def test_collect_weather_forecast_maps_open_meteo_hourly_data(self):
        event = {**EVENT, "venue": {"latitude": "59.917", "longitude": "10.769"}}
        payload = {
            "timezone": "Europe/Oslo",
            "hourly": {
                "time": ["2026-07-27T18:00", "2026-07-27T19:00"],
                "temperature_2m": [17, 16],
                "precipitation": [0, 0.4],
                "wind_speed_10m": [5, 8],
            },
        }

        with patch("outputs.tai_cai_tracker.context._http_get_json", return_value=payload):
            weather = collect_weather_forecast(event)

        self.assertEqual(weather["status"], "available")
        self.assertEqual(weather["forecast_at"], "2026-07-27T19:00:00+02:00")
        self.assertEqual(weather["precipitation_mm"], Decimal("0.4"))

    def test_build_lineup_requires_confirmation_and_twenty_two_starters(self):
        confirmed = build_lineup({
            "source_url": "https://www.sofascore.com/event/123#lineups",
            "captured_at": "2026-07-28T00:05:00+08:00",
            "confirmed": True,
            "home_starters": list(range(1, 12)),
            "away_starters": list(range(12, 23)),
        })
        reported = build_lineup({
            "source_url": "https://www.sofascore.com/event/123#lineups",
            "captured_at": "2026-07-28T00:05:00+08:00",
            "confirmed": False,
            "home_starters": list(range(1, 12)),
            "away_starters": list(range(12, 23)),
        })
        unavailable = build_lineup({
            "source_url": "https://www.sofascore.com/event/123#lineups",
            "captured_at": "2026-07-28T00:05:00+08:00",
            "confirmed": True,
            "home_starters": list(range(1, 11)),
            "away_starters": list(range(12, 23)),
        })

        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(reported["status"], "reported")
        self.assertEqual(unavailable, {
            "status": "unavailable",
            "reason": "complete_lineup_unavailable",
        })


class PregameContextCollectionTests(unittest.TestCase):
    def test_collect_pregame_context_keeps_missing_optional_data_auditable(self):
        identity = {
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2026-07-28T01:00:00+08:00",
        }
        event_url = "https://www.sofascore.com/api/v1/event/123"
        event_payload = {
            "event": {
                "id": 123,
                "homeTeam": {"id": 10, "name": "Home FC"},
                "awayTeam": {"id": 20, "name": "Away FC"},
                "startTimestamp": 1785171600,
                "tournament": {"name": "Test League", "uniqueTournament": {"id": 7}},
                "season": {"name": "2026", "id": 11},
            }
        }

        with patch("outputs.tai_cai_tracker.context._http_get_json", return_value=event_payload):
            bundle = collect_pregame_context(identity, event_url)

        self.assertEqual(bundle["event"]["status"], "matched")
        self.assertEqual(bundle["split_xg"], {
            "status": "unavailable",
            "reason": "venue_or_team_statistics_unavailable",
        })
        self.assertEqual(bundle["weather"], {
            "status": "unavailable",
            "reason": "venue_coordinates_unavailable",
        })
        self.assertEqual(bundle["lineup"], {
            "status": "unavailable",
            "reason": "complete_lineup_unavailable",
        })
        self.assertEqual(bundle["risk_summary"]["confidence_ceiling"], "medium")

    def test_collect_pregame_context_keeps_matched_event_raw_response_hash(self):
        identity = {
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2026-07-28T01:00:00+08:00",
        }
        event_url = "https://www.sofascore.com/api/v1/event/123"
        raw = b'{"event":{"id":123,"homeTeam":{"id":10,"name":"Home FC"},"awayTeam":{"id":20,"name":"Away FC"},"startTimestamp":1785171600,"tournament":{"name":"Test League","uniqueTournament":{"id":7}},"season":{"name":"2026","id":11}}}'

        class RawPayload(dict):
            raw_content_sha256 = hashlib.sha256(raw).hexdigest()

        with patch("outputs.tai_cai_tracker.context._http_get_json", return_value=RawPayload(__import__("json").loads(raw))):
            bundle = collect_pregame_context(identity, event_url)

        self.assertEqual(bundle["event"].get("raw_content_sha256"), hashlib.sha256(raw).hexdigest())

    def test_collect_pregame_context_verifies_event_against_provider_name_anchors(self):
        identity = {
            "competition": "Serie A",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2026-09-12T20:45:00+08:00",
        }
        event_url = "https://www.sofascore.com/api/v1/event/2"
        event_payload = {
            "event": {
                "id": 2,
                "homeTeam": {"id": 11, "name": "Inter"},
                "awayTeam": {"id": 22, "name": "Roma"},
                "startTimestamp": 1789217100,
                "tournament": {"name": "Serie A", "uniqueTournament": {"id": 17}},
                "season": {"name": "2026/27", "id": 27},
            }
        }

        with patch("outputs.tai_cai_tracker.context._http_get_json", return_value=event_payload):
            bundle = collect_pregame_context(
                identity,
                event_url,
                home_team_names=["Inter Milan", "Inter"],
                away_team_names=["Roma"],
            )

        self.assertEqual(bundle["event"]["status"], "matched")

    def test_collect_pregame_context_rejects_unverified_provider_name_anchor(self):
        identity = {
            "competition": "Serie A",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2026-09-12T20:45:00+08:00",
        }
        event_url = "https://www.sofascore.com/api/v1/event/2"
        event_payload = {
            "event": {
                "id": 2,
                "homeTeam": {"id": 11, "name": "Inter"},
                "awayTeam": {"id": 22, "name": "Roma"},
                "startTimestamp": 1789217100,
                "tournament": {"name": "Serie A", "uniqueTournament": {"id": 17}},
                "season": {"name": "2026/27", "id": 27},
            }
        }

        with patch("outputs.tai_cai_tracker.context._http_get_json", return_value=event_payload):
            bundle = collect_pregame_context(
                identity,
                event_url,
                home_team_names=["AC Milan"],
                away_team_names=["Roma"],
            )

        self.assertEqual(bundle["event"], {"status": "unavailable", "reason": "event_identity_unverified"})


if __name__ == "__main__":
    unittest.main()
