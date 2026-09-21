import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from outputs.tai_cai_tracker.pregame import (
    _context_evidence_event,
    _season_bridge_fallback,
    build_pregame_bundle,
    collect_auto_pregame_bundle,
    collect_pregame_bundle,
    force_oddsportal_1x2_url,
    match_identity_from_overrides,
    quality_gate,
    select_named_1x2_capture,
    sofascore_split_xg_evidence,
)
from outputs.tai_cai_tracker.resolver import (
    adopted_source_aliases,
    cached_source_links,
    record_source_links,
)


class PregameBundleTests(unittest.TestCase):
    def test_collect_bundle_uses_thesportsdb_context_after_sofascore_skips(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        official = {"official_source_url": match["official_source_url"], "official_markets": {"spf": {
            "status": "frozen", "published_at": "2099-01-01T10:00:00+08:00",
            "official_source_url": match["official_source_url"],
            "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
        }}}
        odds = {"provider": "433足球网", "source_url": "https://odds.example.test/1", "captured_at": "2099-01-01T10:01:00+08:00", "market": {"market_name": "1X2", "rows": [{"bookmaker": "ExampleBook", "prices": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"}}]}}
        xg = {"provider": "Understat", "xg_inputs": {"source_url": "https://stats.example.test/1", "home_xg": "1.1", "away_xg": "0.9", "model_version": "poisson-v1", "model_weight": "0.40", "confidence_ceiling": "medium", "evidence": {"source_url": "https://stats.example.test/1", "published_at": "2099-01-01T10:02:00+08:00", "competition": "Test League", "season": "2099", "sample_matches": 10, "scope": "league_total", "home_attack_xg": "11", "home_defense_xga": "9", "away_attack_xg": "10", "away_defense_xga": "8", "formula": "test"}}}
        fallback_context = {
            "event": {"status": "matched", "provider": "TheSportsDB", "source_url": "https://www.thesportsdb.com/api/v1/json/123/lookupevent.php?id=1", "captured_at": "2099-01-01T10:03:00+08:00", "raw_content_sha256": "a" * 64, "event_id": "1", "competition": "Test League", "season": "2099"},
            "split_xg": {"status": "unavailable", "reason": "the_sports_db_does_not_publish_home_away_xg"},
            "weather": {"status": "unavailable", "reason": "venue_coordinates_unavailable"},
            "lineup": {"status": "unavailable", "reason": "complete_lineup_unavailable"},
            "risk_summary": {"confidence_ceiling": "medium", "split_xg_status": "unavailable", "lineup_status": "unavailable"},
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_fixed_bonus", return_value=official), \
             patch("outputs.tai_cai_tracker.pregame.collect_433", return_value=odds), \
             patch("outputs.tai_cai_tracker.pregame.resolve_context_event", return_value={"status": "skipped", "reason": "public event source unavailable"}), \
             patch("outputs.tai_cai_tracker.pregame.resolve_thesportsdb_event", return_value={"status": "adopted", "provider": "TheSportsDB", "event_url": fallback_context["event"]["source_url"]}) as resolve_fallback, \
             patch("outputs.tai_cai_tracker.pregame.collect_thesportsdb_pregame_context", return_value=fallback_context) as collect_fallback:
            bundle = collect_pregame_bundle(
                match["match_id"], odds["source_url"], schedule_match=match, understat_xg=xg,
                context_home_team_names=["Home FC"], context_away_team_names=["Away FC"],
            )

        resolve_fallback.assert_called_once_with(match, ["Home FC"], ["Away FC"])
        collect_fallback.assert_called_once_with(match, fallback_context["event"]["source_url"], ["Home FC"], ["Away FC"])
        self.assertEqual(bundle["context_resolution"]["provider"], "TheSportsDB")
        self.assertEqual(bundle["pregame_context"]["weather"]["status"], "unavailable")

    def test_context_evidence_marks_thesportsdb_provider_and_collector(self):
        context = {
            "event": {"status": "matched", "provider": "TheSportsDB", "source_url": "https://www.thesportsdb.com/api/v1/json/123/lookupevent.php?id=1", "captured_at": "2099-01-01T10:03:00+08:00", "raw_content_sha256": "a" * 64, "event_id": "1", "competition": "Test League", "season": "2099"},
            "split_xg": {"status": "unavailable"},
            "lineup": {"status": "unavailable"},
            "risk_summary": {"confidence_ceiling": "medium"},
        }

        event = _context_evidence_event({"match_id": "20990101001"}, context)

        self.assertEqual(event["collector"], "thesportsdb-pregame-context")
        self.assertEqual(event["observed_facts"]["provider"], "TheSportsDB")
    def test_collect_bundle_prefers_supplied_understat_venue_split_without_lower_tier_requests(self):
        match = {
            "match_id": 20990101001,
            "competition": "Premier League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        official = {"official_source_url": match["official_source_url"], "official_markets": {"spf": {
            "status": "frozen", "published_at": "2099-01-01T10:00:00+08:00",
            "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
        }}}
        odds = {"provider": "433", "source_url": "https://www.433.cool/zqzhishu/data/1.html", "captured_at": "2099-01-01T10:01:00+08:00", "market": {"market_name": "1X2", "rows": [{"bookmaker": "ExampleBook", "prices": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"}}]}}
        understat = {
            "provider": "Understat",
            "captured_at": "2099-01-01T10:02:00+08:00",
            "source_url": "https://understat.com/getLeagueData/EPL/2098",
            "xg_inputs": {
                "source_url": "https://understat.com/getLeagueData/EPL/2098",
                "home_xg": "1.4", "away_xg": "1.1", "model_version": "poisson-v1",
                "model_weight": "0.40", "confidence_ceiling": "high",
                "evidence": {
                    "source_url": "https://understat.com/getLeagueData/EPL/2098",
                    "captured_at": "2099-01-01T10:02:00+08:00",
                    "published_at_unavailable_reason": "Understat does not expose publication time",
                    "competition": "Premier League", "season": "2098/2099",
                    "home_sample_matches": 12, "away_sample_matches": 11,
                    "scope": "home_away_split", "home_attack_xg": "16.8",
                    "home_defense_xga": "10.0", "away_attack_xg": "12.1",
                    "away_defense_xga": "13.2", "formula": "venue split test",
                },
            },
            "stat_observations": [],
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_fixed_bonus", return_value=official), \
             patch("outputs.tai_cai_tracker.pregame.collect_schedule", return_value={"matches": [match]}), \
             patch("outputs.tai_cai_tracker.pregame.collect_433", return_value=odds), \
             patch("outputs.tai_cai_tracker.pregame.resolve_context_event", return_value={"status": "skipped", "reason": "test"}), \
             patch("outputs.tai_cai_tracker.pregame.collect_xgscore", return_value=understat) as collect_xgscore, \
             patch("outputs.tai_cai_tracker.pregame.collect_match_xg_evidence") as collect_fotmob:
            bundle = collect_pregame_bundle(
                20990101001,
                odds["source_url"],
                understat_xg=understat,
                xgscore_url="https://xgscore.io/premier-league/home-fc-away-fc/xgscore",
            )

        self.assertEqual(bundle["evidence"]["xg"]["provider"], "Understat")
        collect_xgscore.assert_not_called()
        collect_fotmob.assert_not_called()
        selected = next(item for item in bundle["xg_source_cascade"] if item["status"] == "selected")
        self.assertEqual(selected["provider"], "Understat")
        self.assertEqual(selected["quality_tier"], 3)
        xgscore = next(item for item in bundle["xg_source_cascade"] if item["provider"] == "xGscore")
        self.assertEqual(xgscore["status"], "unavailable")
        self.assertEqual(xgscore["reason"], "not_collected_higher_tier_xg_available")

    def test_collect_bundle_uses_explicit_xgscore_url_before_other_xg_fallbacks(self):
        match = {
            "match_id": 20990101001,
            "competition": "Eliteserien",
            "home_team": "Molde",
            "away_team": "KFUM",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        official = {"official_source_url": match["official_source_url"], "official_markets": {"spf": {
            "status": "frozen", "published_at": "2099-01-01T10:00:00+08:00",
            "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
        }}}
        odds = {"provider": "433", "source_url": "https://www.433.cool/zqzhishu/data/1.html", "captured_at": "2099-01-01T10:01:00+08:00", "market": {"market_name": "1X2", "rows": [{"bookmaker": "ExampleBook", "prices": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"}}]}}
        xgscore = {"provider": "xGscore", "captured_at": "2099-01-01T10:02:00+08:00", "source_url": "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore", "xg_inputs": {"source_url": "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore", "home_xg": "1.5", "away_xg": "1.0", "model_version": "poisson-v1", "model_weight": "0.40", "confidence_ceiling": "medium", "evidence": {"source_url": "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore", "captured_at": "2099-01-01T10:02:00+08:00", "published_at_unavailable_reason": "source does not expose publication time", "competition": "Eliteserien", "season": "2099", "sample_matches": 18, "scope": "league_total", "home_attack_xg": "27", "home_defense_xga": "18", "away_attack_xg": "18", "away_defense_xga": "27", "formula": "test"}}, "stat_observations": []}

        with patch("outputs.tai_cai_tracker.pregame.collect_fixed_bonus", return_value=official), \
             patch("outputs.tai_cai_tracker.pregame.collect_schedule", return_value={"matches": [match]}), \
             patch("outputs.tai_cai_tracker.pregame.collect_433", return_value=odds), \
             patch("outputs.tai_cai_tracker.pregame.collect_xgscore", return_value=xgscore) as collect_xgscore, \
             patch("outputs.tai_cai_tracker.pregame.resolve_context_event", return_value={"status": "skipped", "reason": "test"}), \
             patch("outputs.tai_cai_tracker.pregame.collect_match_xg_evidence") as fotmob:
            bundle = collect_pregame_bundle(
                20990101001, odds["source_url"], xgscore_url=xgscore["source_url"],
            )

        self.assertEqual(bundle["evidence"]["xg"]["provider"], "xGscore")
        self.assertEqual(collect_xgscore.call_args.args[0], xgscore["source_url"])
        fotmob.assert_not_called()

    def test_collect_bundle_uses_xgscore_1xbet_when_433_url_is_unavailable(self):
        match = {
            "match_id": 20990101001,
            "competition": "Eliteserien",
            "home_team": "Molde",
            "away_team": "KFUM",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        official = {"official_source_url": match["official_source_url"], "official_markets": {"spf": {
            "status": "frozen", "published_at": "2099-01-01T10:00:00+08:00",
            "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
        }}}
        xgscore = {
            "provider": "xGscore",
            "captured_at": "2099-01-01T10:02:00+08:00",
            "source_url": "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore",
            "market": {"market_name": "1X2", "rows": [{
                "label": "1X2", "bookmaker": "1xbet",
                "prices": {"home_win": "1.90", "draw": "3.50", "away_win": "4.00"},
            }]},
            "xg_inputs": {
                "source_url": "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore",
                "home_xg": "1.5", "away_xg": "1.0", "model_version": "poisson-v1",
                "model_weight": "0.40", "confidence_ceiling": "medium",
                "evidence": {
                    "source_url": "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore",
                    "captured_at": "2099-01-01T10:02:00+08:00",
                    "published_at_unavailable_reason": "source does not expose publication time",
                    "competition": "Eliteserien", "season": "2099", "sample_matches": 18,
                    "scope": "league_total", "home_attack_xg": "27", "home_defense_xga": "18",
                    "away_attack_xg": "18", "away_defense_xga": "27", "formula": "test",
                },
            },
            "stat_observations": [],
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_fixed_bonus", return_value=official), \
             patch("outputs.tai_cai_tracker.pregame.collect_433", side_effect=AssertionError("433 must not be requested")) as collect_433, \
             patch("outputs.tai_cai_tracker.pregame.collect_xgscore", return_value=xgscore) as collect_xgscore, \
             patch("outputs.tai_cai_tracker.pregame.resolve_context_event", return_value={"status": "skipped", "reason": "test"}), \
             patch("outputs.tai_cai_tracker.pregame.collect_match_xg_evidence") as collect_fotmob:
            bundle = collect_pregame_bundle(
                match["match_id"],
                None,
                schedule_match=match,
                xgscore_url=xgscore["source_url"],
                xgscore_home_team="Molde",
                xgscore_away_team="KFUM",
            )

        collect_433.assert_not_called()
        collect_xgscore.assert_called_once()
        collect_fotmob.assert_not_called()
        self.assertEqual(bundle["odds_import_input"]["provider"], "xGscore")
        self.assertEqual(bundle["odds_import_input"]["captures"][0]["bookmaker"], "1xbet")
        self.assertEqual(bundle["evidence"]["xg"], xgscore)
        self.assertTrue(bundle["readiness"]["can_prepare_prediction_input"])

    def test_auto_bundle_records_strictly_adopted_source_team_aliases(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "主队",
            "away_team": "客队",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        adopted = {
            "status": "adopted",
            "score": 0.95,
            "source_url": "https://www.sofascore.com/api/v1/event/1",
            "candidate": {
                "home_team": "Home FC",
                "away_team": "Away FC",
                "home_team_id": 1,
                "away_team_id": 2,
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            alias_path = Path(temporary_directory) / "aliases.json"
            with patch(
                "outputs.tai_cai_tracker.pregame.collect_pregame_bundle",
                return_value={"context_resolution": adopted},
            ):
                collect_auto_pregame_bundle(
                    match["match_id"],
                    "https://www.433.cool/zqzhishu/data/1.html",
                    schedule_match=match,
                    source_alias_registry_path=alias_path,
                )
            aliases = adopted_source_aliases(alias_path)

        self.assertEqual(aliases["主队"], "Home FC")
        self.assertEqual(aliases["客队"], "Away FC")

    def test_auto_bundle_forwards_explicit_xgscore_identity_names(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        with patch("outputs.tai_cai_tracker.pregame.collect_pregame_bundle", return_value={}) as collect:
            collect_auto_pregame_bundle(
                match["match_id"],
                "https://www.433.cool/zqzhishu/data/1.html",
                schedule_match=match,
                xgscore_url="https://xgscore.io/test-league/inter-roma/xgscore",
                xgscore_home_team=["Inter Milan", "Inter"],
                xgscore_away_team=["Roma"],
            )

        self.assertEqual(collect.call_args.kwargs["xgscore_home_team"], ["Inter Milan", "Inter"])
        self.assertEqual(collect.call_args.kwargs["xgscore_away_team"], ["Roma"])

    def test_auto_bundle_allows_verified_xgscore_when_433_is_unresolved(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_pregame_bundle", return_value={}) as collect, \
             patch("outputs.tai_cai_tracker.pregame.discover_433_detail_url") as discover_433:
            collect_auto_pregame_bundle(
                match["match_id"],
                schedule_match=match,
                allow_433_discovery=False,
                xgscore_url="https://xgscore.io/test-league/inter-roma/xgscore",
                xgscore_home_team="Inter Milan",
                xgscore_away_team="Roma",
            )

        discover_433.assert_not_called()
        self.assertIsNone(collect.call_args.args[1])
        self.assertEqual(
            collect.call_args.kwargs["xgscore_url"],
            "https://xgscore.io/test-league/inter-roma/xgscore",
        )

    def test_auto_bundle_keeps_433_priority_when_both_sources_are_available(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        european_433_url = "https://www.433.cool/zqzhishu/data/1.html"

        with patch("outputs.tai_cai_tracker.pregame.collect_pregame_bundle", return_value={}) as collect, \
             patch("outputs.tai_cai_tracker.pregame.discover_433_detail_url", return_value=european_433_url) as discover_433:
            collect_auto_pregame_bundle(
                match["match_id"],
                schedule_match=match,
                xgscore_url="https://xgscore.io/test-league/inter-roma/xgscore",
                xgscore_home_team="Inter Milan",
                xgscore_away_team="Roma",
            )

        discover_433.assert_called_once_with("国际米兰", "罗马", match["kickoff_at"])
        self.assertEqual(collect.call_args.args[1], european_433_url)

    def test_auto_bundle_uses_cached_wikidata_names_for_xgscore_without_fotmob(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        anchors = {
            "国际米兰": {"english_name": "Inter Milan", "english_names": ["Inter Milan", "Inter"]},
            "罗马": {"english_name": "Roma", "english_names": ["Roma"]},
        }
        with patch("outputs.tai_cai_tracker.pregame.cached_fotmob_team_identity", return_value=None), \
             patch("outputs.tai_cai_tracker.pregame.cached_wikidata_team_identity", side_effect=lambda _path, team: anchors[team]), \
             patch("outputs.tai_cai_tracker.pregame.collect_pregame_bundle", return_value={}) as collect:
            collect_auto_pregame_bundle(
                match["match_id"],
                "https://www.433.cool/zqzhishu/data/1.html",
                schedule_match=match,
                xgscore_url="https://xgscore.io/test-league/inter-roma/xgscore",
            )

        self.assertEqual(collect.call_args.kwargs["xgscore_home_team"], ["Inter Milan", "Inter"])
        self.assertEqual(collect.call_args.kwargs["xgscore_away_team"], ["Roma"])

    def test_auto_bundle_records_only_resolved_source_links(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        links = {
            "european_433_url": "https://www.433.cool/zqzhishu/data/1.html",
            "home_fotmob_url": "https://www.fotmob.com/teams/1/overview/home-fc",
            "away_fotmob_url": "https://www.fotmob.com/teams/2/overview/away-fc",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            with patch("outputs.tai_cai_tracker.pregame.discover_433_detail_url", return_value=links["european_433_url"]), \
                 patch("outputs.tai_cai_tracker.pregame.collect_pregame_bundle", return_value={"resolved_source_links": links}):
                collect_auto_pregame_bundle(
                    match["match_id"],
                    schedule_match=match,
                    source_link_registry_path=registry_path,
                )
            recorded = cached_source_links(registry_path, match)

        self.assertEqual(recorded, links)

    def test_auto_bundle_reuses_registered_source_links_without_rediscovery(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        links = {
            "european_433_url": "https://www.433.cool/zqzhishu/data/1.html",
            "home_fotmob_url": "https://www.fotmob.com/teams/1/overview/home-fc",
            "away_fotmob_url": "https://www.fotmob.com/teams/2/overview/away-fc",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            record_source_links(registry_path, match, links)
            with (
                patch("outputs.tai_cai_tracker.pregame.discover_433_detail_url") as discover_433,
                patch("outputs.tai_cai_tracker.pregame.discover_match_team_stats_urls") as discover_fotmob,
                patch("outputs.tai_cai_tracker.pregame.collect_pregame_bundle", return_value={"ledger_action": "observation_only"}) as collect,
            ):
                result = collect_auto_pregame_bundle(
                    match["match_id"],
                    schedule_match=match,
                    source_link_registry_path=registry_path,
                )

        self.assertEqual(result, {"ledger_action": "observation_only"})
        discover_433.assert_not_called()
        discover_fotmob.assert_not_called()
        self.assertEqual(collect.call_args.args[:2], (match["match_id"], links["european_433_url"]))
        self.assertEqual(collect.call_args.kwargs["home_fotmob_url"], links["home_fotmob_url"])
        self.assertEqual(collect.call_args.kwargs["away_fotmob_url"], links["away_fotmob_url"])

    def test_season_bridge_fallback_uses_understat_provider_team_names(self):
        match = {
            "competition": "Premier League",
            "home_team": "Official Home",
            "away_team": "Official Away",
            "kickoff_at": "2099-08-01T20:00:00+08:00",
        }
        understat = {
            "home": {"team_name": "Understat Home"},
            "away": {"team_name": "Understat Away"},
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_season_bridge_xg", return_value={"provider": "Understat season bridge"}) as collect_bridge:
            result = _season_bridge_fallback(match, understat, "0.40")

        self.assertEqual(result["provider"], "Understat season bridge")
        fixture = collect_bridge.call_args.args[2]
        self.assertEqual(fixture["home_team"], "Understat Home")
        self.assertEqual(fixture["away_team"], "Understat Away")
    def test_sofascore_split_xg_becomes_primary_model_evidence(self):
        split_xg = {
            "status": "available",
            "source_url": "https://www.sofascore.com/api/v1/event/123",
            "captured_at": "2099-01-01T10:02:00+08:00",
            "competition": "Test League",
            "season": "2098/99",
            "home_sample_matches": 8,
            "away_sample_matches": 7,
            "home_attack_xg": "12.8",
            "home_defense_xga": "8.0",
            "away_attack_xg": "9.1",
            "away_defense_xga": "10.5",
            "home_xg": "1.55",
            "away_xg": "1.15",
            "formula": "home_away_split: test",
            "supporting_source_urls": {
                "home_home": "https://www.sofascore.com/api/v1/team/10/statistics/home",
                "away_away": "https://www.sofascore.com/api/v1/team/20/statistics/away",
            },
        }

        evidence = sofascore_split_xg_evidence(split_xg, model_weight="0.40")

        self.assertEqual(evidence["provider"], "SofaScore")
        self.assertEqual(evidence["xg_inputs"]["home_xg"], "1.55")
        self.assertEqual(evidence["xg_inputs"]["away_xg"], "1.15")
        self.assertEqual(evidence["xg_inputs"]["confidence_ceiling"], "high")
        self.assertEqual(evidence["xg_inputs"]["evidence"]["scope"], "home_away_split")
        self.assertEqual(
            evidence["xg_inputs"]["evidence"]["published_at_unavailable_reason"],
            "SofaScore statistics responses do not expose publication time",
        )

    def test_auto_bundle_defers_fotmob_discovery_to_the_fallback(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        discovered = {
            "home": {"source_url": "https://www.fotmob.com/teams/1/overview/home-fc"},
            "away": {"source_url": "https://www.fotmob.com/teams/2/overview/away-fc"},
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_schedule", return_value={"matches": [match]}), \
             patch("outputs.tai_cai_tracker.pregame.discover_match_team_stats_urls", return_value=discovered) as discover, \
             patch("outputs.tai_cai_tracker.pregame.collect_pregame_bundle", return_value={"ledger_action": "observation_only"}) as collect:
            result = collect_auto_pregame_bundle(
                20990101001,
                "https://www.oddsportal.com/football/h2h/home/away/#event",
            )

        self.assertEqual(result, {"ledger_action": "observation_only"})
        discover.assert_not_called()
        collect.assert_called_once_with(
            20990101001,
            "https://www.oddsportal.com/football/h2h/home/away/#event",
            method="concern",
            model_weight="0.40",
            kickoff_at="2099-01-01T20:00:00+08:00",
            home_team="Home FC",
            away_team="Away FC",
            competition="Test League",
            schedule_match=match,
            context_home_team_names=[],
            context_away_team_names=[],
        )

    def test_auto_bundle_passes_cached_identity_names_to_context_resolution(self):
        match = {
            "match_id": 20990101001,
            "competition": "Serie A",
            "home_team": "国际米兰",
            "away_team": "罗马",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_schedule", return_value={"matches": [match]}), \
             patch("outputs.tai_cai_tracker.pregame.cached_source_links", return_value={}), \
             patch("outputs.tai_cai_tracker.pregame.context_source_aliases", return_value={}), \
             patch("outputs.tai_cai_tracker.pregame.cached_fotmob_team_identity", side_effect=[{"source_name": "Inter"}, {"source_name": "Roma"}]), \
             patch("outputs.tai_cai_tracker.pregame.cached_wikidata_team_identity", return_value=None), \
             patch("outputs.tai_cai_tracker.pregame.collect_pregame_bundle", return_value={"ledger_action": "observation_only"}) as collect:
            collect_auto_pregame_bundle(
                20990101001,
                "https://www.433.cool/zqzhishu/data/1.html",
            )

        self.assertEqual(collect.call_args.kwargs["context_home_team_names"], ["Inter"])
        self.assertEqual(collect.call_args.kwargs["context_away_team_names"], ["Roma"])

    def test_auto_bundle_resolves_433_detail_when_url_is_not_supplied(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        discovered = {
            "home": {"source_url": "https://www.fotmob.com/teams/1/overview/home-fc"},
            "away": {"source_url": "https://www.fotmob.com/teams/2/overview/away-fc"},
        }
        with patch("outputs.tai_cai_tracker.pregame.collect_schedule", return_value={"matches": [match]}), \
             patch("outputs.tai_cai_tracker.pregame.discover_match_team_stats_urls", return_value=discovered), \
             patch("outputs.tai_cai_tracker.pregame.discover_433_detail_url", return_value="https://www.433.cool/zqzhishu/data/1.html"), \
             patch("outputs.tai_cai_tracker.pregame.collect_pregame_bundle", return_value={"ledger_action": "observation_only"}) as collect:
            collect_auto_pregame_bundle(20990101001)

        self.assertEqual(collect.call_args.args[1], "https://www.433.cool/zqzhishu/data/1.html")

    def test_collect_bundle_falls_back_to_understat_when_fotmob_has_no_xg(self):
        match = {
            "match_id": 20990101001,
            "competition": "Serie A",
            "home_team": "Bologna",
            "away_team": "Lazio",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        official = {
            "official_source_url": match["official_source_url"],
            "official_markets": {
                "spf": {
                    "status": "frozen",
                    "published_at": "2099-01-01T10:00:00+08:00",
                    "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
                }
            },
        }
        oddsportal = {
            "provider": "OddsPortal",
            "source_url": "https://odds.example.test/matches/20990101001",
            "captured_at": "2099-01-01T10:01:00+08:00",
            "market": {"market_name": "1X2", "rows": [{"label": "1X2", "bookmakers": [{"bookmaker": "ExampleBook", "prices": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"}}]}]},
        }
        understat = {
            "provider": "Understat",
            "captured_at": "2099-01-01T10:02:00+08:00",
            "xg_inputs": {
                "source_url": "https://understat.com/getLeagueData/Serie%20A/2098",
                "home_xg": "1.2",
                "away_xg": "0.8",
                "model_version": "poisson-v1",
                "model_weight": "0.40",
                "confidence_ceiling": "medium",
                "evidence": {
                    "source_url": "https://understat.com/getLeagueData/Serie%20A/2098",
                    "published_at_unavailable_reason": "source does not expose publication time",
                    "competition": "Serie A",
                    "season": "2098/2099",
                    "sample_matches": 10,
                    "scope": "league_total",
                    "home_attack_xg": "12",
                    "home_defense_xga": "8",
                    "away_attack_xg": "8",
                    "away_defense_xga": "12",
                    "formula": "test",
                },
            },
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_fixed_bonus", return_value=official), \
             patch("outputs.tai_cai_tracker.pregame.collect_schedule", return_value={"matches": [match]}), \
             patch("outputs.tai_cai_tracker.pregame.collect_433", return_value=oddsportal), \
             patch("outputs.tai_cai_tracker.pregame.collect_match_xg_evidence", side_effect=ValueError("FotMob has zero samples")), \
             patch("outputs.tai_cai_tracker.pregame.collect_understat_xg", return_value=understat), \
             patch("outputs.tai_cai_tracker.pregame.resolve_context_event", return_value={"status": "skipped", "reason": "test"}):
            bundle = collect_pregame_bundle(20990101001, "https://odds.example.test/matches/20990101001", "https://fotmob.example.test/home", "https://fotmob.example.test/away")

        self.assertEqual(bundle["evidence"]["xg"]["provider"], "Understat")
        self.assertIn("xg_publication_time_unavailable", bundle["quality_gate"]["reasons"])

    def test_collect_bundle_uses_season_bridge_when_current_xg_sample_is_low(self):
        match = {
            "match_id": 20990101001,
            "competition": "Premier League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        official = {
            "official_source_url": match["official_source_url"],
            "official_markets": {"spf": {
                "status": "frozen",
                "published_at": "2099-01-01T10:00:00+08:00",
                "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
            }},
        }
        odds = {
            "provider": "433",
            "source_url": "https://www.433.cool/zqzhishu/data/1.html",
            "captured_at": "2099-01-01T10:01:00+08:00",
            "market": {"market_name": "1X2", "rows": [{
                "bookmaker": "ExampleBook",
                "prices": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
            }]},
        }
        low_sample_fotmob = {
            "provider": "FotMob",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "captured_at": "2099-01-01T10:02:00+08:00",
            "xg_inputs": {
                "source_url": "https://www.fotmob.com/teams/1/stats/home-fc/teams",
                "home_xg": "1.00", "away_xg": "1.00",
                "model_version": "poisson-v1", "model_weight": "0.40",
                "confidence_ceiling": "medium",
                "evidence": {
                    "source_url": "https://www.fotmob.com/teams/1/stats/home-fc/teams",
                    "published_at": "2099-01-01T09:00:00+08:00",
                    "competition": "Premier League", "season": "2099/2100",
                    "sample_matches": 2, "scope": "league_total",
                    "home_attack_xg": "2", "home_defense_xga": "2",
                    "away_attack_xg": "2", "away_defense_xga": "2", "formula": "test",
                },
            },
        }
        bridge = {
            "provider": "Understat season bridge",
            "home_team": "Home FC", "away_team": "Away FC",
            "captured_at": "2099-01-01T10:03:00+08:00",
            "xg_inputs": {
                "source_url": "https://understat.com/getLeagueData/EPL/2099",
                "home_xg": "1.20", "away_xg": "0.90",
                "model_version": "poisson-v1", "model_weight": "0.40",
                "confidence_ceiling": "medium",
                "evidence": {
                    "source_url": "https://understat.com/getLeagueData/EPL/2099",
                    "captured_at": "2099-01-01T10:03:00+08:00",
                    "published_at_unavailable_reason": "Understat does not publish response times",
                    "competition": "Premier League", "season": "2099/2100",
                    "home_sample_matches": 2, "away_sample_matches": 2,
                    "scope": "home_away_split", "home_attack_xg": "1.20",
                    "home_defense_xga": "0.90", "away_attack_xg": "0.90",
                    "away_defense_xga": "1.20", "formula": "season_bridge test",
                    "current_season": {"home_sample_matches": 2, "away_sample_matches": 2},
                    "prior_season": {"home_sample_matches": 12, "away_sample_matches": 12},
                    "current_weight": "0.1428571428571428571428571429",
                    "prior_weight": "0.8571428571428571428571428571",
                },
            },
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_fixed_bonus", return_value=official), \
             patch("outputs.tai_cai_tracker.pregame.collect_schedule", return_value={"matches": [match]}), \
             patch("outputs.tai_cai_tracker.pregame.collect_433", return_value=odds), \
             patch("outputs.tai_cai_tracker.pregame.collect_match_xg_evidence", return_value=low_sample_fotmob), \
             patch("outputs.tai_cai_tracker.pregame.collect_season_bridge_xg", return_value=bridge) as collect_bridge, \
             patch("outputs.tai_cai_tracker.pregame.resolve_context_event", return_value={"status": "skipped", "reason": "test"}):
            bundle = collect_pregame_bundle(20990101001, odds["source_url"], "https://fotmob.example.test/home", "https://fotmob.example.test/away")

        collect_bridge.assert_called_once_with("EPL", "2098", match, model_weight="0.40")
        self.assertEqual(bundle["evidence"]["xg"]["provider"], "Understat season bridge")
        self.assertEqual(bundle["quality_gate"]["status"], "review_ready")
        self.assertEqual(bundle["quality_gate"]["confidence_ceiling"], "medium")

    def test_collect_bundle_keeps_official_and_odds_observation_when_free_xg_is_unavailable(self):
        match = {
            "match_id": 20990101001,
            "competition": "EFL Cup",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        official = {
            "official_source_url": match["official_source_url"],
            "official_markets": {
                "spf": {
                    "status": "frozen",
                    "published_at": "2099-01-01T10:00:00+08:00",
                    "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
                }
            },
        }
        odds = {
            "provider": "433足球网",
            "source_url": "https://www.433.cool/zqzhishu/data/1.html",
            "captured_at": "2099-01-01T10:01:00+08:00",
            "market": {"market_name": "1X2", "rows": [{"bookmaker": "ExampleBook", "prices": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"}}]},
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_fixed_bonus", return_value=official), \
             patch("outputs.tai_cai_tracker.pregame.collect_schedule", return_value={"matches": [match]}), \
             patch("outputs.tai_cai_tracker.pregame.collect_433", return_value=odds), \
             patch("outputs.tai_cai_tracker.pregame.resolve_context_event", return_value={"status": "skipped", "reason": "no match"}), \
             patch("outputs.tai_cai_tracker.pregame.collect_match_xg_evidence", side_effect=ValueError("no FotMob sample")), \
             patch("outputs.tai_cai_tracker.pregame.collect_understat_xg", side_effect=ValueError("Understat fallback does not cover competition: EFL Cup")):
            bundle = collect_pregame_bundle(20990101001, odds["source_url"], "https://fotmob.example.test/home", "https://fotmob.example.test/away")

        self.assertEqual(bundle["ledger_action"], "observation_only")
        self.assertEqual(bundle["quality_gate"], {"status": "blocked", "reasons": ["xg_evidence_unavailable"]})
        self.assertEqual(bundle["evidence"]["xg"], {
            "status": "unavailable",
            "reason": "all_free_xg_sources_unavailable",
        })
        self.assertEqual(bundle["odds_import_input"]["captures"][0]["bookmaker"], "ExampleBook")
        self.assertNotIn("xg_inputs", bundle["prediction_input_template"])
        self.assertEqual(
            bundle["prediction_input_template"]["fixed_prizes"],
            official["official_markets"]["spf"]["fixed_prizes"],
        )
        self.assertNotIn("model_preview", bundle)

    def test_quality_gate_requires_split_xg_and_confirmed_lineups_for_review(self):
        match = {"kickoff_at": "2026-07-27T18:30:00+08:00"}
        official = {"official_markets": {"spf": {"status": "frozen", "published_at": "2026-07-27T10:00:00+08:00"}}}
        capture = {"captured_at": "2026-07-27T10:10:00+08:00"}
        xg = {"xg_inputs": {"evidence": {"published_at": "2026-07-27T10:15:00+08:00", "scope": "home_away_split"}}}
        context = {
            "split_xg": {"status": "available"},
            "lineup": {"status": "confirmed"},
        }

        result = quality_gate(match, official, capture, xg, context, "2026-07-27T10:20:00+08:00")

        self.assertEqual(result, {"status": "review_ready", "reasons": []})

    def test_quality_gate_allows_adequately_sampled_league_total_xg_as_medium_confidence(self):
        match = {"kickoff_at": "2026-07-27T18:30:00+08:00"}
        official = {"official_markets": {"spf": {"status": "frozen", "published_at": "2026-07-27T10:00:00+08:00"}}}
        capture = {"captured_at": "2026-07-27T10:10:00+08:00"}
        xg = {"xg_inputs": {"evidence": {"published_at": "2026-07-27T10:15:00+08:00", "scope": "league_total", "sample_matches": 8}}}
        context = {
            "split_xg": {"status": "unavailable"},
            "lineup": {"status": "unavailable"},
        }

        result = quality_gate(match, official, capture, xg, context, "2026-07-27T10:20:00+08:00")

        self.assertEqual(result["status"], "review_ready")
        self.assertEqual(result["confidence_ceiling"], "medium")
        self.assertEqual(
            result["reasons"],
            ["league_total_xg", "starting_lineups_unconfirmed", "context_event_unresolved"],
        )

    def test_quality_gate_allows_adequately_sampled_source_match_model_as_medium_confidence(self):
        match = {"kickoff_at": "2026-07-27T18:30:00+08:00"}
        official = {"official_markets": {"spf": {"status": "frozen", "published_at": "2026-07-27T10:00:00+08:00"}}}
        capture = {"captured_at": "2026-07-27T10:10:00+08:00"}
        xg = {"xg_inputs": {"evidence": {
            "captured_at": "2026-07-27T10:15:00+08:00",
            "published_at_unavailable_reason": "source does not expose publication time",
            "scope": "source_match_model",
            "home_sample_matches": 13,
            "away_sample_matches": 13,
        }}, "captured_at": "2026-07-27T10:15:00+08:00"}
        context = {
            "split_xg": {"status": "unavailable"},
            "lineup": {"status": "unavailable"},
        }

        result = quality_gate(match, official, capture, xg, context, "2026-07-27T10:20:00+08:00")

        self.assertEqual(result["status"], "review_ready")
        self.assertEqual(result["confidence_ceiling"], "medium")
        self.assertEqual(
            result["reasons"],
            ["xg_publication_time_unavailable", "source_match_model_xg", "starting_lineups_unconfirmed", "context_event_unresolved"],
        )

    def test_quality_gate_keeps_low_sample_league_total_xg_observation_only(self):
        match = {"kickoff_at": "2026-07-27T18:30:00+08:00"}
        official = {"official_markets": {"spf": {"status": "frozen", "published_at": "2026-07-27T10:00:00+08:00"}}}
        capture = {"captured_at": "2026-07-27T10:10:00+08:00"}
        xg = {"xg_inputs": {"evidence": {"published_at": "2026-07-27T10:15:00+08:00", "scope": "league_total", "sample_matches": 7}}}
        context = {
            "split_xg": {"status": "unavailable"},
            "lineup": {"status": "reported"},
        }

        result = quality_gate(match, official, capture, xg, context, "2026-07-27T10:20:00+08:00")

        self.assertEqual(result["status"], "observation_only")
        self.assertEqual(
            result["reasons"],
            ["league_total_xg", "xg_sample_below_minimum", "starting_lineups_unconfirmed", "context_event_unresolved"],
        )

    def test_quality_gate_blocks_late_capture(self):
        result = quality_gate(
            {"kickoff_at": "2026-07-27T18:30:00+08:00"},
            {"official_markets": {"spf": {"status": "frozen", "published_at": "2026-07-27T10:00:00+08:00"}}},
            {"captured_at": "2026-07-27T10:10:00+08:00"},
            {"xg_inputs": {"evidence": {"published_at": "2026-07-27T10:15:00+08:00", "scope": "home_away_split"}}},
            {"split_xg": {"status": "available"}, "lineup": {"status": "confirmed"}},
            "2026-07-27T18:30:00+08:00",
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reasons"], ["pre_kickoff_capture_required"])

    def test_quality_gate_allows_pre_kickoff_xg_without_publication_time_as_observation(self):
        result = quality_gate(
            {"kickoff_at": "2026-07-27T18:30:00+08:00"},
            {"official_markets": {"spf": {"status": "frozen", "published_at": "2026-07-27T10:00:00+08:00"}}},
            {"captured_at": "2026-07-27T10:10:00+08:00"},
            {
                "captured_at": "2026-07-27T10:12:00+08:00",
                "xg_inputs": {
                    "evidence": {
                        "published_at_unavailable_reason": "source does not expose publication time",
                        "scope": "league_total",
                    }
                },
            },
            {"split_xg": {"status": "unavailable"}, "lineup": {"status": "reported"}},
            "2026-07-27T10:20:00+08:00",
        )

        self.assertEqual(result["status"], "observation_only")
        self.assertNotIn("pre_kickoff_capture_required", result["reasons"])
        self.assertIn("xg_publication_time_unavailable", result["reasons"])

    def test_collect_bundle_auto_adopts_context_and_preserves_it_for_review(self):
        match = {
            "match_id": 20990101001,
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff_at": "2099-01-01T20:00:00+08:00",
            "official_source_url": "https://official.example.test/matches/20990101001",
        }
        official = {
            "match_id": 20990101001,
            "official_source_url": match["official_source_url"],
            "official_markets": {
                "spf": {
                    "status": "frozen",
                    "published_at": "2099-01-01T10:00:00+08:00",
                    "official_source_url": match["official_source_url"],
                    "fixed_prizes": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"},
                }
            },
        }
        oddsportal = {
            "provider": "OddsPortal",
            "source_url": "https://odds.example.test/matches/20990101001",
            "captured_at": "2099-01-01T10:01:00+08:00",
            "market": {"market_name": "1X2", "rows": [{"label": "1X2", "bookmakers": [{"bookmaker": "ExampleBook", "prices": {"home_win": "2.00", "draw": "3.00", "away_win": "4.00"}}]}]},
        }
        xg = {"xg_inputs": {"source_url": "https://stats.example.test/home", "home_xg": "1.1", "away_xg": "0.9", "model_version": "poisson-v1", "model_weight": "0.40", "confidence_ceiling": "medium", "evidence": {"source_url": "https://stats.example.test/home", "published_at": "2099-01-01T10:02:00+08:00", "competition": "Test League", "season": "2099", "sample_matches": 10, "scope": "league_total", "home_attack_xg": "11", "home_defense_xga": "9", "away_attack_xg": "10", "away_defense_xga": "8", "formula": "test"}}}
        context = {
            "event": {"status": "matched"},
            "split_xg": {
                "status": "available",
                "source_url": "https://www.sofascore.com/api/v1/event/123",
                "captured_at": "2099-01-01T10:02:00+08:00",
                "competition": "Test League",
                "season": "2098/99",
                "home_sample_matches": 8,
                "away_sample_matches": 7,
                "home_attack_xg": "12.8",
                "home_defense_xga": "8.0",
                "away_attack_xg": "9.1",
                "away_defense_xga": "10.5",
                "home_xg": "1.55",
                "away_xg": "1.15",
                "formula": "home_away_split: test",
            },
            "weather": {"status": "unavailable", "reason": "venue_coordinates_unavailable"},
            "lineup": {"status": "confirmed"},
            "risk_summary": {"confidence_ceiling": "high", "split_xg_status": "available", "lineup_status": "confirmed"},
        }

        with patch("outputs.tai_cai_tracker.pregame.collect_fixed_bonus", return_value=official), \
             patch("outputs.tai_cai_tracker.pregame.collect_schedule", return_value={"matches": [match]}), \
             patch("outputs.tai_cai_tracker.pregame.collect_433", return_value=oddsportal), \
             patch("outputs.tai_cai_tracker.pregame.collect_match_xg_evidence", side_effect=AssertionError("FotMob should not run")), \
             patch("outputs.tai_cai_tracker.pregame.resolve_context_event", return_value={"status": "adopted", "event_url": "https://context.example.test/events/1"}), \
             patch("outputs.tai_cai_tracker.pregame.collect_pregame_context", return_value=context):
            bundle = collect_pregame_bundle(20990101001, "https://odds.example.test/matches/20990101001", "https://stats.example.test/home", "https://stats.example.test/away")

        self.assertEqual(bundle["context_resolution"]["status"], "adopted")
        self.assertEqual(bundle["pregame_context"]["lineup"]["status"], "confirmed")
        self.assertEqual(bundle["prediction_input_template"]["pregame_context"]["split_xg"]["status"], "available")
        self.assertEqual(bundle["evidence"]["xg"]["provider"], "SofaScore")
        self.assertEqual(bundle["quality_gate"], {
            "status": "observation_only",
            "reasons": ["xg_publication_time_unavailable"],
        })

    def test_force_oddsportal_1x2_url_reuses_event_fragment(self):
        source_url = (
            "https://www.oddsportal.com/football/h2h/seoul-8tzeYeHp/"
            "ulsan-hyundai-EVEwgyOM/#b59Hqhfo:ht-ft;2;31"
        )

        self.assertEqual(
            force_oddsportal_1x2_url(source_url),
            (
                "https://www.oddsportal.com/football/h2h/seoul-8tzeYeHp/"
                "ulsan-hyundai-EVEwgyOM/#b59Hqhfo:1X2;2"
            ),
        )

    def test_select_named_1x2_capture_uses_first_complete_named_row(self):
        oddsportal = {
            "provider": "OddsPortal",
            "source_url": "https://www.oddsportal.com/match#abc:1X2;2",
            "captured_at": "2026-07-27T10:00:00+08:00",
            "market": {
                "market_name": "1X2",
                "rows": [
                    {
                        "label": "1X2",
                        "bookmakers": [
                            {
                                "bookmaker": "ExampleBook",
                                "prices": {
                                    "home_win": "1.80",
                                    "draw": "3.70",
                                    "away_win": "4.20",
                                },
                            }
                        ],
                    }
                ],
            },
        }

        capture = select_named_1x2_capture(
            oddsportal,
            match_id=2040623,
            kickoff_at="2026-07-27T18:30:00+08:00",
        )

        self.assertEqual(capture["bookmaker"], "ExampleBook")
        self.assertEqual(capture["source_quality"], "named_bookmaker")
        self.assertEqual(capture["odds"]["home_win"], "1.80")

    def test_select_named_1x2_capture_accepts_433_immediate_row(self):
        evidence = {
            "provider": "433足球网",
            "source_url": "https://www.433.cool/zqzhishu/data/1331072.html",
            "captured_at": "2026-08-26T12:00:00+08:00",
            "market": {
                "market_name": "1X2",
                "rows": [{
                    "bookmaker": "Bet 365",
                    "prices": {"home_win": "1.33", "draw": "5.00", "away_win": "8.50"},
                }],
            },
        }

        capture = select_named_1x2_capture(
            evidence,
            match_id=2041061,
            kickoff_at="2026-08-27T02:45:00+08:00",
        )

        self.assertEqual(capture["bookmaker"], "Bet 365")
        self.assertEqual(capture["source_url"], "https://www.433.cool/zqzhishu/data/1331072.html")

    def test_match_identity_from_overrides_requires_kickoff_and_teams(self):
        identity = match_identity_from_overrides(
            match_id=2040623,
            kickoff_at="2026-07-27T18:30:00+08:00",
            home_team="FC Seoul",
            away_team="Ulsan HD FC",
            competition="K League 1",
            official_source_url="https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
        )

        self.assertEqual(identity["match_id"], 2040623)
        self.assertEqual(identity["home_team"], "FC Seoul")

        with self.assertRaisesRegex(ValueError, "kickoff"):
            match_identity_from_overrides(
                match_id=2040623,
                kickoff_at=None,
                home_team="FC Seoul",
                away_team="Ulsan HD FC",
                competition="K League 1",
                official_source_url="https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
            )

    def test_build_pregame_bundle_outputs_import_and_snapshot_templates(self):
        match = {
            "match_id": 2040623,
            "match_num": "周一001",
            "competition": "K League 1",
            "home_team": "FC Seoul",
            "away_team": "Ulsan HD FC",
            "kickoff_at": "2026-07-27T18:30:00+08:00",
            "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
        }
        official = {
            "match_id": 2040623,
            "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
            "captured_at": "2026-07-27T10:05:00+08:00",
            "raw_content_sha256": "b" * 64,
            "official_markets": {
                "spf": {
                    "status": "frozen",
                    "published_at": "2026-07-27T10:00:00+08:00",
                    "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
                    "fixed_prizes": {
                        "home_win": "1.80",
                        "draw": "3.40",
                        "away_win": "4.00",
                    },
                },
                "hhad": {"status": "skipped", "reason": "not in sample"},
                "crs": {"status": "skipped", "reason": "not in sample"},
                "ttg": {"status": "skipped", "reason": "not in sample"},
                "hafu": {"status": "skipped", "reason": "not in sample"},
            },
        }
        oddsportal = {
            "provider": "OddsPortal",
            "source_url": "https://www.oddsportal.com/match#abc:1X2;2",
            "captured_at": "2026-07-27T10:10:00+08:00",
            "market_raw_content_sha256": "a" * 64,
            "market": {
                "market_name": "1X2",
                "rows": [
                    {
                        "label": "1X2",
                        "bookmakers": [
                            {
                                "bookmaker": "ExampleBook",
                                "prices": {
                                    "home_win": "1.75",
                                    "draw": "3.60",
                                    "away_win": "4.50",
                                },
                            }
                        ],
                    }
                ],
            },
        }
        xg = {
            "provider": "FotMob",
            "xg_inputs": {
                "source_url": "https://www.fotmob.com/teams/92630/stats/fc-seoul/teams",
                "home_xg": "1.51",
                "away_xg": "1.52",
                "model_version": "poisson-v1",
                "model_weight": "0.40",
                "confidence_ceiling": "medium",
                "evidence": {
                    "source_url": "https://www.fotmob.com/teams/92630/stats/fc-seoul/teams",
                    "published_at": "2026-07-26T12:34:40+00:00",
                    "competition": "K League 1",
                    "season": "2026",
                    "sample_matches": 19,
                    "scope": "league_total",
                    "home_attack_xg": "31.4",
                    "home_defense_xga": "23.3",
                    "away_attack_xg": "34.3",
                    "away_defense_xga": "26.1",
                    "formula": "league_total",
                },
            },
            "stat_observations": [
                {
                    "source_url": "https://data.fotmob.com/stats/expected-goals-home.json",
                    "captured_at": "2026-07-27T10:12:00+08:00",
                    "published_at": "2026-07-26T12:34:40+00:00",
                    "raw_content_sha256": "c" * 64,
                    "team": "FC Seoul",
                    "side": "home",
                    "stat_name": "expected_goals_team",
                    "value": "31.4",
                    "sample_matches": 19,
                },
                {
                    "source_url": "https://data.fotmob.com/stats/expected-goals-away.json",
                    "captured_at": "2026-07-27T10:13:00+08:00",
                    "published_at": "2026-07-26T12:34:40+00:00",
                    "raw_content_sha256": "d" * 64,
                    "team": "Ulsan HD FC",
                    "side": "away",
                    "stat_name": "expected_goals_conceded_team",
                    "value": "26.1",
                    "sample_matches": 19,
                },
            ],
        }
        context = {
            "event": {
                "status": "matched",
                "source_url": "https://www.sofascore.com/api/v1/event/123",
                "captured_at": "2026-07-27T10:14:00+08:00",
                "raw_content_sha256": "e" * 64,
                "event_id": 123,
                "competition": "K League 1",
                "season": "2026",
            },
            "split_xg": {"status": "available"},
            "weather": {"status": "unavailable", "reason": "venue_coordinates_unavailable"},
            "lineup": {"status": "confirmed"},
            "risk_summary": {"confidence_ceiling": "high", "split_xg_status": "available", "lineup_status": "confirmed"},
        }

        bundle = build_pregame_bundle(match, official, oddsportal, xg, pregame_context=context)

        self.assertEqual(bundle["readiness"]["can_prepare_prediction_input"], True)
        self.assertEqual(bundle["odds_import_input"]["captures"][0]["bookmaker"], "ExampleBook")
        self.assertEqual(bundle["prediction_input_template"]["match_id"], 2040623)
        self.assertEqual(
            bundle["prediction_input_template"]["european_odds"]["capture_hash"],
            "<fill_after_odds-import>",
        )
        self.assertEqual(bundle["all_market_snapshot_input_template"]["prediction_hash"], "<fill_after_snapshot>")
        self.assertNotIn(
            "status",
            bundle["all_market_snapshot_input_template"]["official_markets"]["spf"],
        )
        self.assertEqual(
            bundle["all_market_snapshot_input_template"]["official_markets"]["hhad"],
            {"status": "skipped", "reason": "not in sample"},
        )
        self.assertEqual(bundle["model_preview"]["top_scores"][0]["home_goals"], 1)
        events = bundle["evidence_event_inputs"]
        self.assertEqual(
            [event["evidence_kind"] for event in events],
            ["official_market", "european_1x2", "xg", "xg", "pregame_context"],
        )
        self.assertEqual(events[0]["raw_content_sha256"], "b" * 64)
        self.assertEqual(events[0]["observed_facts"]["market"], "spf")
        self.assertEqual(events[1]["raw_content_sha256"], "a" * 64)
        self.assertEqual(events[1]["observed_facts"]["bookmaker"], "ExampleBook")
        self.assertEqual(events[2]["raw_content_sha256"], "c" * 64)
        self.assertEqual(events[2]["observed_facts"]["stat_name"], "expected_goals_team")
        self.assertEqual(events[4]["raw_content_sha256"], "e" * 64)
        self.assertEqual(events[4]["observed_facts"]["event_id"], 123)
        for event in events:
            self.assertEqual(event["record_type"], "source_observation")
            self.assertEqual(event["match_id"], "周一001")
            self.assertTrue(event["source_url"].startswith("https://"))
            self.assertTrue(event["captured_at"])
            self.assertTrue(event["observed_facts"])

    def test_build_pregame_bundle_omits_untraceable_evidence_templates(self):
        match = {
            "match_id": 2040623,
            "match_num": None,
            "competition": "K League 1",
            "home_team": "FC Seoul",
            "away_team": "Ulsan HD FC",
            "kickoff_at": "2026-07-27T18:30:00+08:00",
            "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
        }
        official = {
            "official_source_url": match["official_source_url"],
            "official_markets": {"spf": {"status": "frozen", "fixed_prizes": {"home_win": "1.80", "draw": "3.40", "away_win": "4.00"}}},
        }
        oddsportal = {
            "provider": "OddsPortal",
            "source_url": "https://www.oddsportal.com/match#abc:1X2;2",
            "captured_at": "2026-07-27T10:10:00+08:00",
            "market": {"market_name": "1X2", "rows": [{"label": "1X2", "bookmakers": [{"bookmaker": "ExampleBook", "prices": {"home_win": "1.75", "draw": "3.60", "away_win": "4.50"}}]}]},
        }
        xg = {
            "xg_inputs": {
                "source_url": "https://www.fotmob.com/teams/92630/stats/fc-seoul/teams",
                "home_xg": "1.51", "away_xg": "1.52", "model_version": "poisson-v1", "model_weight": "0.40",
                "evidence": {"source_url": "https://www.fotmob.com/teams/92630/stats/fc-seoul/teams", "published_at": "2026-07-26T12:34:40+00:00", "competition": "K League 1", "season": "2026", "sample_matches": 19, "scope": "league_total", "home_attack_xg": "31.4", "home_defense_xga": "23.3", "away_attack_xg": "34.3", "away_defense_xga": "26.1", "formula": "test"},
            },
            "stat_observations": [{"source_url": "https://data.fotmob.com/stats/xg.json", "captured_at": "2026-07-27T10:12:00+08:00", "raw_content_sha256": "f" * 64}],
        }

        bundle = build_pregame_bundle(match, official, oddsportal, xg)

        self.assertEqual(bundle["evidence_event_inputs"], [])


if __name__ == "__main__":
    unittest.main()
