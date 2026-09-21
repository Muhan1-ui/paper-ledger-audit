import json
import hashlib
import unittest
from unittest import mock

import outputs.tai_cai_tracker.sporttery as sporttery
from outputs.tai_cai_tracker.sporttery import (
    collect_results,
    collect_fixed_bonus,
    extract_latest_fixed_bonus,
    fetch_json,
    parse_result_matches,
    parse_schedule_matches,
)


class SportteryCollectorTests(unittest.TestCase):
    def test_parse_result_matches_keeps_official_half_and_full_time_scores(self):
        payload = {
            "errorCode": "0",
            "value": {
                "matchInfoList": [{
                    "subMatchList": [{
                        "matchId": 2041050,
                        "matchNumStr": "周二002",
                        "leagueAbbName": "韩职",
                        "homeTeamAbbName": "济州SK",
                        "awayTeamAbbName": "浦项制铁",
                        "matchDate": "2026-08-25",
                        "matchTime": "18:30",
                        "matchStatus": "11",
                        "matchStatusName": "已完成",
                        "sectionsNo1": "0:1",
                        "sectionsNo999": "1:2",
                    }]
                }]
            },
        }

        matches = parse_result_matches(payload, "https://official.example.test/result")

        self.assertEqual(matches[0]["half_time_score"], {"home": 0, "away": 1})
        self.assertEqual(matches[0]["final_score"], {"home": 1, "away": 2})
        self.assertEqual(matches[0]["official_result_url"], "https://official.example.test/result")

    def test_collect_results_uses_the_official_result_endpoint(self):
        payload = sporttery._JsonPayload(
            {"errorCode": "0", "value": {"matchInfoList": []}}, "c" * 64
        )

        with mock.patch.object(sporttery, "fetch_json", return_value=payload) as fetch:
            result = collect_results(match_date="2026-08-25")

        self.assertEqual(result["method"], "result")
        self.assertIn("method=result", result["official_result_url"])
        self.assertIn("matchDate=2026-08-25", result["official_result_url"])
        self.assertIn("pageSize=100", result["official_result_url"])
        fetch.assert_called_once()

    def test_collect_results_follows_official_next_page_and_preserves_each_page_url(self):
        def payload(match_id, match_num, next_page, page_no):
            return sporttery._JsonPayload(
                {
                    "errorCode": "0",
                    "value": {
                        "pageNo": page_no,
                        "nextPage": next_page,
                        "matchInfoList": [{"subMatchList": [{
                            "matchId": match_id,
                            "matchNumStr": match_num,
                            "matchStatus": "11",
                            "sectionsNo999": "1:0",
                        }]}],
                    },
                },
                "a" * 64,
            )

        with mock.patch.object(
            sporttery,
            "fetch_json",
            side_effect=[payload(1, "周一001", "2", 1), payload(2, "周一002", "0", 2)],
        ) as fetch:
            result = collect_results(match_date="2026-08-25")

        self.assertEqual([match["match_num"] for match in result["matches"]], ["周一001", "周一002"])
        self.assertEqual(result["page_count"], 2)
        self.assertIn("pageNo=1", result["matches"][0]["official_result_url"])
        self.assertIn("pageNo=2", result["matches"][1]["official_result_url"])
        self.assertEqual(len(fetch.call_args_list), 2)

    def test_collect_results_rejects_an_official_pagination_cycle(self):
        payload = sporttery._JsonPayload(
            {
                "errorCode": "0",
                "value": {"pageNo": 1, "nextPage": "1", "matchInfoList": []},
            },
            "a" * 64,
        )
        with mock.patch.object(sporttery, "fetch_json", return_value=payload):
            with self.assertRaisesRegex(ValueError, "repeated"):
                collect_results(match_date="2026-08-25")

    def test_parse_schedule_matches_keeps_identity_status_and_prizes(self):
        payload = {
            "errorCode": "0",
            "value": {
                "matchInfoList": [
                    {
                        "subMatchList": [
                            {
                                "matchId": 2040641,
                                "matchNumStr": "周一201",
                                "leagueAbbName": "瑞超",
                                "homeTeamAbbName": "赫根",
                                "awayTeamAbbName": "索尔纳",
                                "matchDate": "2026-07-28",
                                "matchTime": "01:00",
                                "matchStatus": "3",
                                "matchStatusName": "暂停销售",
                                "h": "1.75",
                                "d": "3.75",
                                "a": "3.43",
                            }
                        ]
                    }
                ]
            },
        }

        matches = parse_schedule_matches(payload)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["match_id"], 2040641)
        self.assertEqual(matches[0]["match_num"], "周一201")
        self.assertEqual(matches[0]["kickoff_at"], "2026-07-28T01:00:00+08:00")
        self.assertEqual(matches[0]["status_name"], "暂停销售")
        self.assertEqual(
            matches[0]["spf_fixed_prizes"],
            {"home_win": "1.75", "draw": "3.75", "away_win": "3.43"},
        )

    def test_extract_latest_fixed_bonus_maps_all_five_football_markets(self):
        payload = {
            "errorCode": "0",
            "value": {
                "oddsHistory": {
                    "hadList": [
                        {"h": "1.66", "d": "3.50", "a": "4.15", "updateDate": "2026-07-24", "updateTime": "09:22:39"},
                        {"h": "1.65", "d": "3.50", "a": "4.20", "updateDate": "2026-07-26", "updateTime": "18:23:03"},
                    ],
                    "hhadList": [
                        {"h": "3.15", "d": "3.30", "a": "1.97", "goalLine": "-1", "updateDate": "2026-07-26", "updateTime": "18:07:31"}
                    ],
                    "crsList": [
                        {
                            "s01s00": "8.00",
                            "s00s00": "17.00",
                            "s00s01": "15.50",
                            "s-1sh": "37.00",
                            "s-1sd": "300.0",
                            "s-1sa": "150.0",
                            "updateDate": "2026-07-26",
                            "updateTime": "17:02:46",
                        }
                    ],
                    "ttgList": [
                        {
                            "s0": "17.00",
                            "s1": "6.30",
                            "s2": "3.65",
                            "s3": "3.15",
                            "s4": "4.60",
                            "s5": "8.50",
                            "s6": "15.00",
                            "s7": "23.00",
                            "updateDate": "2026-07-26",
                            "updateTime": "17:51:06",
                        }
                    ],
                    "hafuList": [
                        {
                            "hh": "2.35",
                            "hd": "15.00",
                            "ha": "34.00",
                            "dh": "4.30",
                            "dd": "6.10",
                            "da": "11.00",
                            "ah": "21.00",
                            "ad": "15.00",
                            "aa": "7.75",
                            "updateDate": "2026-07-26",
                            "updateTime": "16:58:34",
                        }
                    ],
                }
            },
        }

        markets = extract_latest_fixed_bonus(
            payload,
            official_source_url="https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
        )

        self.assertEqual(markets["spf"]["published_at"], "2026-07-26T18:23:03+08:00")
        self.assertEqual(markets["spf"]["fixed_prizes"]["home_win"], "1.65")
        self.assertEqual(markets["hhad"]["home_handicap"], "-1")
        self.assertEqual(markets["hhad"]["fixed_prizes"]["away_win"], "1.97")
        self.assertEqual(markets["crs"]["fixed_prizes"]["1:0"], "8.00")
        self.assertEqual(markets["crs"]["fixed_prizes"]["H_OTHER"], "37.00")
        self.assertEqual(markets["ttg"]["fixed_prizes"]["7+"], "23.00")
        self.assertEqual(markets["hafu"]["fixed_prizes"]["AA"], "7.75")

    def test_fetch_json_does_not_use_process_wide_proxy_urlopen(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({"errorCode": "0"}).encode("utf-8")

        fake_opener = mock.Mock()
        fake_opener.open.return_value = FakeResponse()

        with mock.patch.object(sporttery, "urlopen", side_effect=AssertionError("proxy urlopen used")):
            with mock.patch.object(sporttery, "build_opener", return_value=fake_opener, create=True):
                result = fetch_json("https://webapi.sporttery.cn/test", "https://m.sporttery.cn/")

        self.assertEqual(result, {"errorCode": "0"})
        fake_opener.open.assert_called_once()

    def test_fetch_json_retains_exact_raw_response_hash(self):
        raw = b'{"errorCode":"0","value":{}}'

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return raw

        fake_opener = mock.Mock()
        fake_opener.open.return_value = FakeResponse()
        with mock.patch.object(sporttery, "build_opener", return_value=fake_opener):
            result = fetch_json("https://webapi.sporttery.cn/test", "https://m.sporttery.cn/")

        self.assertEqual(result["errorCode"], "0")
        self.assertEqual(getattr(result, "raw_content_sha256", None), hashlib.sha256(raw).hexdigest())

    def test_collect_fixed_bonus_surfaces_response_hash_and_capture_time(self):
        payload = sporttery._JsonPayload(
            {"errorCode": "0", "value": {"oddsHistory": {}}},
            "a" * 64,
        )

        with mock.patch.object(sporttery, "fetch_json", return_value=payload):
            result = collect_fixed_bonus(2040623)

        self.assertEqual(result["raw_content_sha256"], "a" * 64)
        self.assertTrue(result["captured_at"])


if __name__ == "__main__":
    unittest.main()
