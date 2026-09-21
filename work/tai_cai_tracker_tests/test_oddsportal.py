import json
import unittest

from outputs.tai_cai_tracker.oddsportal import (
    MARKET_SLUGS,
    build_match_event_url,
    decode_encrypted_response,
    extract_bookmakers,
    parse_event_header,
    summarize_market,
)


KNOWN_EMPTY_ODDS_RESPONSE = (
    "SXhPdlEzM0RJdytTRU0yTGZid0QrTTlwL01YZEV6T1ZPbmpQckdPM0YwenJYK1BZZG5KRTJqOVFL"
    "MU5WMm1JbFY0bkNHS2IydFpZQ3ZlSlM0RUdSOHcvcFZYZmE3NVMvQ1VSejZta2NRVitGRlVjNS"
    "95N0h3SktYUEV4cy9uT1JSU2tjZTcvYzBYcUtMOElFaDRGd3VhOUMwbEhFRm1Pd3BOQ1AyZW"
    "tsUHVNRE1VOUZLbzg1MHJUVjJxeEtlY2pBL3FkMFZBQlV0dklORFE1T1B0eTdXQm9LR21IQT"
    "Jhbk01OHdRdkszOXp3bExYZ2RhVE1QZkVMQUlvWUtONUd4b0xwQVRPbGhWZit5TnZ3TlFGUk"
    "dSRnVoam5TaXRiSXhhdjlLRWZwSEMyd0RJaHRvYi9Mc21rK2hTSWk0MmIxZVI6NDRhOTkxYz"
    "Y1ZWY5YWM4NTY2YzVhOGUwNGI1NWI3ZjQ="
)


class OddsPortalTests(unittest.TestCase):
    def test_decode_encrypted_response_returns_json_payload(self):
        decoded = decode_encrypted_response(KNOWN_EMPTY_ODDS_RESPONSE)

        self.assertEqual(decoded["s"], 1)
        self.assertEqual(decoded["d"]["encodeventId"], "8YXVev6T")
        self.assertEqual(decoded["d"]["oddsdata"], {"back": [], "lay": []})

    def test_parse_event_header_reads_embedded_json(self):
        html = """
        <div id="react-event-header" data='{
            "eventData": {
                "id": "abc123",
                "xhashf": "%79%6a%33%61%34",
                "sportId": 1,
                "versionId": 1,
                "home": "Seoul",
                "away": "Ulsan HD",
                "defaultBetId": 1,
                "defaultScopeId": 2
            },
            "eventBody": {
                "startDate": 1785121200,
                "additionalOddsInfo": {
                    "bettingTypes": {
                        "1": {"name": "1X2", "short-name": "1X2"},
                        "9": {"name": "Half Time / Full Time", "short-name": "HT/FT"}
                    }
                }
            },
            "requestPreMatch": {"url": "/match-event/1-1-abc123-1-2-yj3a4.dat?_="}
        }'></div>
        """

        event = parse_event_header(html)

        self.assertEqual(event["event_id"], "abc123")
        self.assertEqual(event["home_team"], "Seoul")
        self.assertEqual(event["away_team"], "Ulsan HD")
        self.assertEqual(event["xhashf"], "yj3a4")

    def test_parse_event_header_reports_current_layout_requires_browser_visible_capture(self):
        current_layout = '<html><script id="__NEXT_DATA__" type="application/json">{}</script></html>'

        with self.assertRaisesRegex(ValueError, "browser-visible named-bookmaker capture"):
            parse_event_header(current_layout)

    def test_build_match_event_url_uses_requested_market_slug(self):
        event = {
            "sport_id": 1,
            "version_id": 1,
            "event_id": "abc123",
            "default_bet_id": 1,
            "default_scope_id": 2,
            "xhashf": "yj3a4",
        }

        self.assertEqual(MARKET_SLUGS["ht-ft"], 9)
        url = build_match_event_url(
            event,
            "https://www.oddsportal.com/football/h2h/a/b/#abc123:ht-ft;2;31",
            timestamp_ms=1785123456000,
        )

        self.assertEqual(
            url,
            "https://www.oddsportal.com/match-event/1-1-abc123-9-2-yj3a4.dat?_=1785123456000",
        )

        overridden = build_match_event_url(
            event,
            "https://www.oddsportal.com/football/h2h/a/b/#abc123:ht-ft;2;31",
            timestamp_ms=1785123456000,
            betting_type_id=1,
            scope_id=2,
        )
        self.assertEqual(
            overridden,
            "https://www.oddsportal.com/match-event/1-1-abc123-1-2-yj3a4.dat?_=1785123456000",
        )

    def test_extract_bookmakers_maps_provider_ids_to_names(self):
        payload = {
            "s": 1,
            "d": {
                "bookmakers": {
                    "data": [
                        [
                            {"idProvider": 16, "WebName": "bet365"},
                            {"idProvider": 44, "WebName": "Betfair Exchange"},
                        ]
                    ]
                }
            },
        }

        self.assertEqual(
            extract_bookmakers(payload),
            {"16": "bet365", "44": "Betfair Exchange"},
        )

    def test_summarize_market_returns_rows_and_extremes(self):
        odds_payload = {
            "s": 1,
            "d": {
                "bt": 1,
                "sc": 2,
                "time-base": 1785121139,
                "oddsdata": {
                    "back": {
                        "E-1-2-0-0-0": {
                            "bettingTypeId": 1,
                            "scopeId": 2,
                            "handicapValue": 0,
                            "mixedParameterName": None,
                            "odds": {
                                "16": {"0": 1.75, "1": 3.60, "2": 4.00},
                                "44": {"0": 1.84, "1": 4.10, "2": 4.50},
                            },
                            "openingOdd": {
                                "16": {"0": 1.75, "1": 3.50, "2": 4.20}
                            },
                            "movement": {
                                "16": {"0": "up", "1": "down", "2": "down"}
                            },
                        }
                    }
                },
            },
        }

        summary = summarize_market(
            odds_payload,
            bookmakers={"16": "bet365", "44": "Betfair Exchange"},
        )

        self.assertEqual(summary["betting_type_id"], 1)
        self.assertEqual(summary["rows"][0]["label"], "1X2")
        self.assertEqual(summary["rows"][0]["bookmaker_count"], 2)
        self.assertEqual(summary["rows"][0]["bookmakers"][0]["bookmaker"], "bet365")
        self.assertEqual(summary["rows"][0]["best_prices"]["home_win"], "1.84")
        self.assertEqual(summary["rows"][0]["average_prices"]["away_win"], "4.25")

    def test_summarize_single_selection_market_rows(self):
        odds_payload = {
            "s": 1,
            "d": {
                "bt": 9,
                "sc": 2,
                "time-base": 1785121139,
                "oddsdata": {
                    "back": {
                        "E-9-2-0-0-27": {
                            "bettingTypeId": 9,
                            "scopeId": 2,
                            "mixedParameterName": "1/1",
                            "odds": {"16": [2.55], "44": [2.75]},
                            "openingOdd": {"16": [2.50]},
                            "movement": {"16": ["up"]},
                        }
                    }
                },
            },
        }

        summary = summarize_market(
            odds_payload,
            bookmakers={"16": "bet365", "44": "Betfair Exchange"},
        )

        self.assertEqual(summary["market_name"], "Half Time / Full Time")
        self.assertEqual(summary["rows"][0]["label"], "1/1")
        self.assertEqual(summary["rows"][0]["bookmakers"][0]["prices"], {"1/1": "2.55"})
        self.assertEqual(summary["rows"][0]["average_prices"], {"1/1": "2.65"})


if __name__ == "__main__":
    unittest.main()
