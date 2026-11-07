import unittest
import json
from unittest.mock import MagicMock, patch


class XGScoreTests(unittest.TestCase):
    def test_event_request_uses_the_configured_public_source_proxy(self):
        from outputs.tai_cai_tracker.xgscore import _http_get

        response = MagicMock()
        response.read.return_value = b"<html>event</html>"
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        proxy = "http://127.0.0.1:7892"

        with patch.dict("os.environ", {"TAICAI_HTTP_PROXY": proxy}), \
             patch("outputs.tai_cai_tracker.xgscore.coordinated_public_request", side_effect=lambda _url, load: load()), \
             patch("outputs.tai_cai_tracker.xgscore.ProxyHandler") as proxy_handler, \
             patch("outputs.tai_cai_tracker.xgscore.build_opener", return_value=opener):
            self.assertEqual(
                _http_get("https://xgscore.io/league/event/xgscore"),
                ("<html>event</html>", "bd3e8f65b7c0b5625b946635091346297c1002a38cd6b310f2e4155bb1b9c9ea"),
            )

        proxy_handler.assert_called_once_with({"http": proxy, "https": proxy})

    def test_discover_event_urls_reads_homepage_once_for_multiple_verified_name_pairs(self):
        from unittest.mock import patch
        from outputs.tai_cai_tracker.xgscore import discover_xgscore_event_urls

        homepage = '''
        <a href="/champions-league/aek-lask/preview">AEK vs LASK</a>
        <a href="/champions-league/real-madrid-inter/preview">Real Madrid vs Inter</a>
        '''
        fixtures = [
            {"match_id": "one", "home_team": "AEK Athens", "away_team": "LASK"},
            {"match_id": "two", "home_team": "Real Madrid", "away_team": "Inter"},
            {"match_id": "three", "home_team": "Unknown", "away_team": "Team"},
        ]

        with patch("outputs.tai_cai_tracker.xgscore._http_get", return_value=(homepage, "a" * 64)) as fetch:
            result = discover_xgscore_event_urls(fixtures)

        fetch.assert_called_once_with("https://xgscore.io")
        self.assertEqual(result["one"], {
            "status": "candidate",
            "source_url": "https://xgscore.io/champions-league/aek-lask/xgscore",
        })
        self.assertEqual(result["two"], {
            "status": "candidate",
            "source_url": "https://xgscore.io/champions-league/real-madrid-inter/xgscore",
        })
        self.assertEqual(result["three"], {
            "status": "unavailable",
            "reason": "xgscore homepage has no uniquely matched public preview",
        })

    def test_discover_event_url_accepts_only_one_public_preview_slug_matching_both_provider_names(self):
        from unittest.mock import patch
        from outputs.tai_cai_tracker.xgscore import discover_xgscore_event_url

        homepage = '''
        <a href="/champions-league/aek-lask/preview">AEK vs LASK</a>
        <a href="/champions-league/real-madrid-inter/preview">Real Madrid vs Inter</a>
        '''
        with patch("outputs.tai_cai_tracker.xgscore._http_get", return_value=(homepage, "a" * 64)):
            result = discover_xgscore_event_url("AEK Athens", "LASK")

        self.assertEqual(result, "https://xgscore.io/champions-league/aek-lask/xgscore")

    def test_discover_event_url_rejects_ambiguous_or_unmatched_public_preview_slugs(self):
        from unittest.mock import patch
        from outputs.tai_cai_tracker.xgscore import discover_xgscore_event_url

        homepage = '''
        <a href="/league/aek-lask/preview">AEK vs LASK</a>
        <a href="/cup/aek-lask/preview">AEK vs LASK</a>
        '''
        with patch("outputs.tai_cai_tracker.xgscore._http_get", return_value=(homepage, "a" * 64)):
            with self.assertRaisesRegex(ValueError, "unique"):
                discover_xgscore_event_url("AEK Athens", "LASK")

    def test_discovery_uses_exact_wikidata_english_aliases_for_preview_slugs(self):
        from unittest.mock import patch
        from outputs.tai_cai_tracker.xgscore import discover_xgscore_event_urls

        homepage = '<a href="/champions-league/psg-roma/preview">PSG vs Roma</a>'
        fixtures = [{
            "match_id": "one",
            "home_team": "Paris Saint-Germain",
            "home_team_aliases": ["Paris Saint-Germain", "PSG"],
            "away_team": "Roma",
        }]

        with patch("outputs.tai_cai_tracker.xgscore._http_get", return_value=(homepage, "a" * 64)):
            result = discover_xgscore_event_urls(fixtures)

        self.assertEqual(result["one"], {
            "status": "candidate",
            "source_url": "https://xgscore.io/champions-league/psg-roma/xgscore",
        })

    def test_parse_event_page_accepts_current_source_match_model_state(self):
        from outputs.tai_cai_tracker.xgscore import parse_event_page

        rows = [
            {
                "game": {
                    "datetime": f"2026-08-{day:02d}T18:00:00.000Z",
                    "xG": {"s": 1.0 + day / 100, "c": 0.8 + day / 100},
                },
            }
            for day in range(1, 9)
        ]
        state = {
            "match": {
                "b": {
                    "forecastScore": {"h": 1.95, "a": 1.35},
                    "homeTeamMetadata": {
                        "forecastGoals": {"s": 2.05, "c": 0.99},
                        "impact": 0.74,
                        "gameTables": [{"type": "home", "rows": rows}],
                    },
                    "awayTeamMetadata": {
                        "forecastGoals": {"s": 1.93, "c": 1.58},
                        "impact": 0.26,
                        "gameTables": [{"type": "away", "rows": rows}],
                    },
                    "standings": [{"seasonId": 20262027}],
                },
            },
        }
        html = f'''<script type="application/ld+json">{{"@type":"SportsEvent","startDate":"2026-09-08T16:45:00.000Z","homeTeam":{{"name":"AEK Athens"}},"awayTeam":{{"name":"LASK"}}}}</script>
        <xgs-odds><div><img alt="1xbet"><small> Result </small><xgs-odds-cell><span>1</span><mark><span>1.80</span></mark></xgs-odds-cell><xgs-odds-cell><span>x</span><mark><span>4.07</span></mark></xgs-odds-cell><xgs-odds-cell><span>2</span><mark><span>4.47</span></mark></xgs-odds-cell></div></xgs-odds>
        <script id="serverApp-state" type="application/json">{json.dumps(state)}</script>'''

        result = parse_event_page(html)

        self.assertEqual(result["event"], {
            "home_team": "AEK Athens",
            "away_team": "LASK",
            "kickoff_at": "2026-09-08T16:45:00+00:00",
        })
        self.assertEqual(result["source_match_xg"], {
            "home_xg": "1.95",
            "away_xg": "1.35",
            "home_attack_xg": "2.05",
            "home_defense_xga": "0.99",
            "away_attack_xg": "1.93",
            "away_defense_xga": "1.58",
            "home_sample_matches": 8,
            "away_sample_matches": 8,
            "home_impact": "0.74",
            "away_impact": "0.26",
        })
        self.assertEqual(result["season"], "2026/2027")

    def test_parse_event_page_accepts_server_rendered_xgscore_structure(self):
        from outputs.tai_cai_tracker.xgscore import parse_event_page

        state = {
            "event": {
                "b": {
                    "homeTeamMetadata": {"seasonTable": {"rows": [{"stats": {
                        "games": 18, "xgScored": 33.65, "xgConceded": 24.69,
                        "team": {"name": "Molde"}, "season": {"start": 2026},
                    }}]}},
                    "awayTeamMetadata": {"seasonTable": {"rows": [{"stats": {
                        "games": 18, "xgScored": 21.88, "xgConceded": 33.07,
                        "team": {"name": "KFUM"}, "season": {"start": 2026},
                    }}]}},
                },
            },
        }
        html = f'''<script type="application/ld+json">{{"@type":"SportsEvent","startDate":"2026-09-06T15:00:00.000Z","homeTeam":{{"name":"Molde"}},"awayTeam":{{"name":"KFUM"}}}}</script>
        <xgs-odds><div><img alt="1xbet"><small> Result </small><xgs-odds-cell><span>1</span><mark><span>1.32</span></mark></xgs-odds-cell><xgs-odds-cell><span>x</span><mark><span>5.8</span></mark></xgs-odds-cell><xgs-odds-cell><span>2</span><mark><span>8.7</span></mark></xgs-odds-cell><small> Handicap </small><xgs-odds-cell><span>1</span><mark><span>1.83</span></mark></xgs-odds-cell><xgs-odds-cell><span>2</span><mark><span>2.00</span></mark></xgs-odds-cell></div></xgs-odds>
        <script id="serverApp-state" type="application/json">{json.dumps(state)}</script>'''

        result = parse_event_page(html)

        self.assertEqual(result["event"], {
            "home_team": "Molde", "away_team": "KFUM", "kickoff_at": "2026-09-06T15:00:00+00:00",
        })
        self.assertEqual(result["market"]["rows"][0]["prices"], {
            "home_win": "1.32", "draw": "5.8", "away_win": "8.7",
        })
        self.assertEqual(result["season_xg"]["home"], {
            "sample_matches": 18, "xg_per_match": "1.869444444444444444444444444", "xga_per_match": "1.371666666666666666666666667",
        })
    def test_parse_event_page_keeps_only_explicit_1xbet_and_season_xg_totals(self):
        from outputs.tai_cai_tracker.xgscore import parse_event_page

        html = """
        <xgs-game_team class="xgs-game_team -home"><a>Molde</a></xgs-game_team>
        <time>23:00 September 6, 2026</time>
        <xgs-game_team class="xgs-game_team -away"><a>KFUM</a></xgs-game_team>
        <section class="bookmaker"><img alt="1xbet"><small>Result</small>
          <span>1</span><strong>1.32</strong><span>x</span><strong>5.80</strong>
          <span>2</span><strong>8.10</strong></section>
        <section class="bookmaker"><img alt="Pinnacle888"><small>Result</small>
          <span>1</span><strong>1.30</strong><span>x</span><strong>5.70</strong>
          <span>2</span><strong>8.00</strong></section>
        <xgs-team-metadata class="xgs-game_team -home">
          <span>All Season's Matches</span><span>Season 2026</span><span>MP</span><b>18</b>
          <span>Average per game</span><span>Scored goals</span><b>2.0</b><span>xG</span><b>1.87</b>
          <span>Conceded goals</span><b>1.6</b><span>xGA</span><b>1.37</b>
        </xgs-team-metadata>
        <xgs-team-metadata class="xgs-game_team -away">
          <span>All Season's Matches</span><span>Season 2026</span><span>MP</span><b>18</b>
          <span>Average per game</span><span>Scored goals</span><b>1.1</b><span>xG</span><b>1.05</b>
          <span>Conceded goals</span><b>1.7</b><span>xGA</span><b>1.63</b>
        </xgs-team-metadata>
        """

        result = parse_event_page(html)

        self.assertEqual(result["event"], {
            "home_team": "Molde",
            "away_team": "KFUM",
            "kickoff_local": "23:00 September 6, 2026",
        })
        self.assertEqual(result["market"]["rows"], [{
            "label": "1X2",
            "bookmaker": "1xbet",
            "prices": {"home_win": "1.32", "draw": "5.80", "away_win": "8.10"},
        }])
        self.assertEqual(result["season_xg"]["home"], {
            "sample_matches": 18, "xg_per_match": "1.87", "xga_per_match": "1.37",
        })
        self.assertEqual(result["season_xg"]["away"], {
            "sample_matches": 18, "xg_per_match": "1.05", "xga_per_match": "1.63",
        })
        self.assertEqual(result["season"], "2026")

    def test_build_xg_inputs_keeps_unavailable_publication_time_and_total_scope(self):
        from outputs.tai_cai_tracker.xgscore import build_xg_inputs

        result = build_xg_inputs(
            {"home": {"sample_matches": 18, "xg_per_match": "1.87", "xga_per_match": "1.37"},
             "away": {"sample_matches": 18, "xg_per_match": "1.05", "xga_per_match": "1.63"}},
            "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore",
            "Eliteserien",
            "2026",
            "2026-09-06T12:00:00+08:00",
        )

        self.assertEqual(result["home_xg"], "1.75")
        self.assertEqual(result["away_xg"], "1.21")
        self.assertEqual(result["confidence_ceiling"], "medium")
        self.assertEqual(result["evidence"]["scope"], "league_total")
        self.assertEqual(result["evidence"]["sample_matches"], 18)
        self.assertEqual(
            result["evidence"]["published_at_unavailable_reason"],
            "source does not expose publication time",
        )

    def test_collect_rejects_identity_mismatch_before_returning_evidence(self):
        from unittest.mock import patch
        from outputs.tai_cai_tracker.xgscore import collect_xgscore

        parsed = {
            "event": {"home_team": "Molde", "away_team": "KFUM", "kickoff_local": "23:00 September 6, 2026"},
            "market": {"rows": [{"label": "1X2", "bookmaker": "1xbet", "prices": {"home_win": "1.32", "draw": "5.80", "away_win": "8.10"}}]},
            "season_xg": {"home": {"sample_matches": 18, "xg_per_match": "1.87", "xga_per_match": "1.37"}, "away": {"sample_matches": 18, "xg_per_match": "1.05", "xga_per_match": "1.63"}},
            "season": "2026",
        }
        with patch("outputs.tai_cai_tracker.xgscore._http_get", return_value=("<html>", "a" * 64)), \
             patch("outputs.tai_cai_tracker.xgscore.parse_event_page", return_value=parsed):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                collect_xgscore(
                    "https://xgscore.io/norway-eliteserien/molde-kfum/xgscore",
                    home_team="Wrong team",
                    away_team="KFUM",
                    kickoff_at="2026-09-06T23:00:00+08:00",
                    competition="Eliteserien",
                )

    def test_collect_accepts_an_exact_wikidata_english_alias_for_provider_identity(self):
        from unittest.mock import patch
        from outputs.tai_cai_tracker.xgscore import collect_xgscore

        parsed = {
            "event": {"home_team": "Inter", "away_team": "Roma", "kickoff_local": "20:00 August 20, 2099"},
            "market": {"rows": []},
            "season_xg": {
                "home": {"sample_matches": 8, "xg_per_match": "1.5", "xga_per_match": "1.0"},
                "away": {"sample_matches": 8, "xg_per_match": "1.2", "xga_per_match": "1.3"},
            },
            "season": "2099",
        }
        with patch("outputs.tai_cai_tracker.xgscore._http_get", return_value=("<html>", "a" * 64)), \
             patch("outputs.tai_cai_tracker.xgscore.parse_event_page", return_value=parsed):
            result = collect_xgscore(
                "https://xgscore.io/serie-a/inter-roma/xgscore",
                home_team=["Inter Milan", "Inter"],
                away_team=["Roma"],
                kickoff_at="2099-08-20T20:00:00+08:00",
                competition="Serie A",
            )

        self.assertEqual(result["event"]["home_team"], "Inter")


if __name__ == "__main__":
    unittest.main()
