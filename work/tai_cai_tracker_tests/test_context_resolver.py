import unittest
from unittest.mock import patch

from outputs.tai_cai_tracker.context_resolver import collect_context_event_candidates, resolve_context_event


class ContextEventResolverTests(unittest.TestCase):
    def test_resolve_context_event_adopts_only_the_strictly_verified_candidate(self):
        identity = {"competition": "K League 1", "home_team": "\u9996\u5c14", "away_team": "\u851a\u5c71HD", "kickoff_at": "2026-07-27T18:30:00+08:00"}
        collected = {"status": "collected", "candidates": [{"event_id": 1, "home_team": "FC Seoul", "away_team": "Ulsan HD FC", "home_team_id": 10, "away_team_id": 20, "competition": "K League 1", "kickoff_at": "2026-07-27T18:30:00+08:00", "source_url": "https://www.sofascore.com/api/v1/event/1"}]}

        with patch("outputs.tai_cai_tracker.context_resolver.collect_context_event_candidates", return_value=collected):
            result = resolve_context_event(identity)

        self.assertEqual(result["status"], "adopted")
        self.assertEqual(result["event_url"], "https://www.sofascore.com/api/v1/event/1")

    def test_resolve_context_event_uses_verified_provider_name_anchors(self):
        identity = {
            "competition": "Serie A",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2026-09-12T20:45:00+08:00",
        }
        collected = {
            "status": "collected",
            "source_url": ["https://www.sofascore.com/api/v1/sport/football/scheduled-events/2026-09-12"],
            "captured_at": "2026-09-12T10:00:00+08:00",
            "candidates": [{
                "event_id": 2,
                "home_team": "Inter",
                "away_team": "Roma",
                "home_team_id": 11,
                "away_team_id": 22,
                "competition": "Serie A",
                "kickoff_at": "2026-09-12T20:45:00+08:00",
                "source_url": "https://www.sofascore.com/api/v1/event/2",
            }],
        }

        with patch("outputs.tai_cai_tracker.context_resolver.collect_context_event_candidates", return_value=collected):
            result = resolve_context_event(
                identity,
                home_team_names=["Inter Milan", "Inter"],
                away_team_names=["Roma"],
            )

        self.assertEqual(result["status"], "adopted")
        self.assertEqual(result["evidence"]["home_identity_anchor"], "Inter")
        self.assertEqual(result["evidence"]["away_identity_anchor"], "Roma")

    def test_resolve_context_event_rejects_anchor_candidate_with_wrong_kickoff(self):
        identity = {
            "competition": "Serie A",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2026-09-12T20:45:00+08:00",
        }
        collected = {
            "status": "collected",
            "source_url": ["https://www.sofascore.com/api/v1/sport/football/scheduled-events/2026-09-12"],
            "captured_at": "2026-09-12T10:00:00+08:00",
            "candidates": [{
                "event_id": 2,
                "home_team": "Inter",
                "away_team": "Roma",
                "home_team_id": 11,
                "away_team_id": 22,
                "competition": "Serie A",
                "kickoff_at": "2026-09-13T20:45:00+08:00",
                "source_url": "https://www.sofascore.com/api/v1/event/2",
            }],
        }

        with patch("outputs.tai_cai_tracker.context_resolver.collect_context_event_candidates", return_value=collected):
            result = resolve_context_event(
                identity,
                home_team_names=["Inter"],
                away_team_names=["Roma"],
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "low confidence")

    def test_resolve_context_event_checks_the_adopted_candidates_exact_kickoff(self):
        identity = {
            "competition": "Serie A",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2026-09-12T20:45:00+08:00",
        }
        collected = {
            "status": "collected",
            "source_url": ["https://www.sofascore.com/api/v1/sport/football/scheduled-events/2026-09-12"],
            "captured_at": "2026-09-12T10:00:00+08:00",
            "candidates": [
                {
                    "event_id": 2,
                    "home_team": "Inter",
                    "away_team": "Roma",
                    "home_team_id": 11,
                    "away_team_id": 22,
                    "competition": "Serie A",
                    "kickoff_at": "2026-09-12T20:45:00+08:00",
                    "source_url": "https://www.sofascore.com/api/v1/event/2",
                },
                {
                    "event_id": 3,
                    "home_team": "Inter B",
                    "away_team": "Roma B",
                    "home_team_id": 33,
                    "away_team_id": 44,
                    "competition": "Serie A",
                    "kickoff_at": "2026-09-13T20:45:00+08:00",
                    "source_url": "https://www.sofascore.com/api/v1/event/3",
                },
            ],
        }

        with patch("outputs.tai_cai_tracker.context_resolver.collect_context_event_candidates", return_value=collected):
            result = resolve_context_event(
                identity,
                home_team_names=["Inter"],
                away_team_names=["Roma"],
            )

        self.assertEqual(result["status"], "adopted")
        self.assertEqual(result["event_url"], "https://www.sofascore.com/api/v1/event/2")
    def test_collects_only_candidates_for_the_official_kickoff_date(self):
        identity = {
            "competition": "K League 1",
            "home_team": "\u9996\u5c14",
            "away_team": "\u851a\u5c71HD",
            "kickoff_at": "2026-07-27T18:30:00+08:00",
        }
        payload = {
            "events": [{
                "id": 123,
                "homeTeam": {"id": 10, "name": "FC Seoul"},
                "awayTeam": {"id": 20, "name": "Ulsan HD FC"},
                "startTimestamp": 1785148200,
                "tournament": {"name": "K League 1"},
            }]
        }

        with patch("outputs.tai_cai_tracker.context_resolver._http_get_json", return_value=payload):
            result = collect_context_event_candidates(identity)

        self.assertEqual(result["status"], "collected")
        self.assertEqual(result["candidates"][0]["event_id"], 123)
        self.assertEqual(result["candidates"][0]["source_url"], "https://www.sofascore.com/api/v1/event/123")

    def test_returns_auditable_skip_when_the_source_has_no_events(self):
        with patch("outputs.tai_cai_tracker.context_resolver._http_get_json", return_value={"events": []}):
            result = collect_context_event_candidates({"kickoff_at": "2026-07-27T18:30:00+08:00"})

        self.assertEqual(result, {"status": "skipped", "reason": "no public event candidates"})

    def test_checks_utc_date_when_kickoff_crosses_the_local_date_boundary(self):
        identity = {
            "competition": "Serie A",
            "home_team": "Bologna",
            "away_team": "Lazio",
            "kickoff_at": "2026-08-25T00:30:00+08:00",
        }
        payload = {
            "events": [{
                "id": 2041028,
                "homeTeam": {"id": 2685, "name": "Bologna"},
                "awayTeam": {"id": 2699, "name": "Lazio"},
                "startTimestamp": 1787589000,
                "tournament": {"name": "Serie A"},
            }]
        }

        with patch(
            "outputs.tai_cai_tracker.context_resolver._http_get_json",
            side_effect=[{"events": []}, payload],
        ) as fetch:
            result = collect_context_event_candidates(identity)

        self.assertEqual(result["status"], "collected")
        self.assertEqual(result["candidates"][0]["event_id"], 2041028)
        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            [
                "https://www.sofascore.com/api/v1/sport/football/scheduled-events/2026-08-25",
                "https://www.sofascore.com/api/v1/sport/football/scheduled-events/2026-08-24",
            ],
        )


if __name__ == "__main__":
    unittest.main()
