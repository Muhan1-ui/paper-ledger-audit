import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from outputs.tai_cai_tracker.football433 import (
    _433_event_days,
    _collect_match_list_page,
    discover_433_detail_urls,
    discover_433_detail_url,
    find_match_detail_url,
    parse_match_detail,
)


DETAIL_HTML = """
<div class="homeTeamName"><p>纽卡斯尔联</p></div>
<p class="dataTime">2026-08-27 02:45</p>
<div class="guestTeamName"><p>西布朗</p></div>
<table id="match_1">
  <tr><td>公司</td><td></td><td>主胜</td><td>和局</td><td>客胜</td></tr>
  <tr><td rowspan="2"><span>Bet 365</span></td><td>初盘</td><td>0.83</td><td>1/1.5</td><td>1.03</td><td>1.38</td><td>4.33</td><td>7.00</td></tr>
  <tr><td>即时</td><td>0.98</td><td>1.5</td><td>0.88</td><td>1.33</td><td>5</td><td>8.5</td></tr>
  <tr><td rowspan="2"><span>皇冠</span></td><td>初盘</td><td>0.82</td><td>1/1.5</td><td>1.00</td><td>1.36</td><td>4.80</td><td>6.40</td></tr>
  <tr><td>即时</td><td>0.97</td><td>1.5</td><td>0.91</td><td>1.31</td><td>5</td><td>7.1</td></tr>
</table>
"""


class Football433Tests(unittest.TestCase):
    def test_event_days_fall_back_to_previous_list_day_for_after_midnight_fixture(self):
        self.assertEqual(
            _433_event_days("2026-09-08T00:30:00+08:00"),
            ["2026-09-08", "2026-09-07"],
        )

    def test_find_match_detail_url_accepts_verified_portuguese_name_variants(self):
        kickoff_at = "2026-09-08T03:15:00+08:00"
        timestamp_ms = int(datetime.fromisoformat(kickoff_at).astimezone(timezone.utc).timestamp() * 1000)
        row = [1334549, timestamp_ms, 0, 0, False, False, False, False, "", 33, 636, 0, 0,
               200, "伊斯托里尔", "伊斯托里尔", 0, 0, 0, 11,
               1091, "阿鲁卡", "阿鲁卡", 0, 0, 0, 2, False, False, False, False]

        result = find_match_detail_url(
            [row],
            "埃斯托里",
            "阿罗卡",
            kickoff_at,
        )

        self.assertEqual(result, "https://www.433.cool/zqzhishu/data/1334549.html")

    def test_batch_discovery_keeps_unavailable_status_for_single_list_day(self):
        fixture = {
            "match_id": 2041326,
            "home_team": "埃斯托里",
            "away_team": "阿罗卡",
            "kickoff_at": "2026-09-08T03:15:00+08:00",
        }
        with patch(
            "outputs.tai_cai_tracker.football433._collect_match_list_page",
            return_value=[],
        ):
            result = discover_433_detail_urls([fixture])

        self.assertEqual(result["2041326"]["status"], "unavailable")

    def test_list_page_submits_requested_page_index_in_post_payload(self):
        captured_requests = []

        class Response:
            def read(self):
                return b'[1, 0, 0, 0, 0, [0, 0, []]]'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def open(self, request, timeout):
                captured_requests.append((request, timeout))
                return Response()

        with patch(
            "outputs.tai_cai_tracker.football433.coordinated_public_request",
            side_effect=lambda _url, load: load(),
        ), patch(
            "outputs.tai_cai_tracker.football433.build_opener",
            return_value=Opener(),
        ):
            _collect_match_list_page(2, "2026-09-08")

        request, timeout = captured_requests[0]
        self.assertEqual(timeout, 20)
        self.assertIn(b"pageindex=2", request.data)
        self.assertIn(b"searchdate=2026-09-08", request.data)

    def test_batch_discovery_reads_list_pages_once_for_multiple_matches(self):
        first = [1331072, 1787769900000, 0, 0, False, False, False, False, "", 33, 636, 0, 0,
                 200, "纽卡斯尔联", "紐卡素聯", 0, 0, 0, 11,
                 1091, "西布朗", "西布朗", 0, 0, 0, 2, False, False, False, False]
        second = [1331073, 1787773500000, 0, 0, False, False, False, False, "", 33, 636, 0, 0,
                  201, "维拉", "維拉", 0, 0, 0, 11,
                  1092, "阿森纳", "阿仙奴", 0, 0, 0, 2, False, False, False, False]
        fixtures = [
            {
                "match_id": 2041183,
                "home_team": "纽卡斯尔",
                "away_team": "西布罗姆",
                "kickoff_at": "2026-08-27T02:45:00+08:00",
            },
            {
                "match_id": 2041184,
                "home_team": "维拉",
                "away_team": "阿森纳",
                "kickoff_at": "2026-08-27T03:45:00+08:00",
            },
        ]
        with patch(
            "outputs.tai_cai_tracker.football433._collect_match_list_page",
            side_effect=[[first], [second], []],
        ) as collect_page:
            result = discover_433_detail_urls(fixtures)

        self.assertEqual(
            [call.args for call in collect_page.call_args_list],
            [(1, "2026-08-27"), (2, "2026-08-27"), (3, "2026-08-27")],
        )
        self.assertEqual(result["2041183"]["status"], "resolved")
        self.assertEqual(
            result["2041183"]["source_url"],
            "https://www.433.cool/zqzhishu/data/1331072.html",
        )
        self.assertEqual(result["2041184"]["status"], "resolved")
        self.assertEqual(
            result["2041184"]["source_url"],
            "https://www.433.cool/zqzhishu/data/1331073.html",
        )

    def test_batch_discovery_deduplicates_repeated_433_rows_before_identity_check(self):
        row = [1331072, 1787769900000, 0, 0, False, False, False, False, "", 33, 636, 0, 0,
               200, "纽卡斯尔联", "紐卡素聯", 0, 0, 0, 11,
               1091, "西布朗", "西布朗", 0, 0, 0, 2, False, False, False, False]
        fixture = {
            "match_id": 2041183,
            "home_team": "纽卡斯尔",
            "away_team": "西布罗姆",
            "kickoff_at": "2026-08-27T02:45:00+08:00",
        }
        with patch(
            "outputs.tai_cai_tracker.football433._collect_match_list_page",
            side_effect=[[row], [row], [row]],
        ):
            result = discover_433_detail_urls([fixture])

        self.assertEqual(result["2041183"], {
            "status": "resolved",
            "source_url": "https://www.433.cool/zqzhishu/data/1331072.html",
        })

    def test_batch_discovery_prefers_official_day_for_afternoon_kickoffs(self):
        row = [1332797, 1788343200000, 0, 0, False, False, False, False, "", 33, 636, 0, 0,
               200, "大阪樱花", "大阪櫻花", 0, 0, 0, 11,
               1091, "柏太阳神", "柏太陽神", 0, 0, 0, 2, False, False, False, False]
        fixture = {
            "match_id": 2041224,
            "home_team": "大阪樱花",
            "away_team": "柏太阳神",
            "kickoff_at": "2026-09-02T18:00:00+08:00",
        }
        with patch(
            "outputs.tai_cai_tracker.football433._collect_match_list_page",
            side_effect=[[row], [], []],
        ) as collect_page:
            result = discover_433_detail_urls([fixture])

        self.assertEqual(result["2041224"], {
            "status": "resolved",
            "source_url": "https://www.433.cool/zqzhishu/data/1332797.html",
        })
        self.assertEqual(
            [call.args for call in collect_page.call_args_list],
            [(1, "2026-09-02"), (2, "2026-09-02"), (3, "2026-09-02")],
        )

    def test_batch_discovery_falls_back_to_next_day_for_afternoon_kickoffs(self):
        row = [1332797, 1788343200000, 0, 0, False, False, False, False, "", 33, 636, 0, 0,
               200, "大阪樱花", "大阪櫻花", 0, 0, 0, 11,
               1091, "柏太阳神", "柏太陽神", 0, 0, 0, 2, False, False, False, False]
        fixture = {
            "match_id": 2041224,
            "home_team": "大阪樱花",
            "away_team": "柏太阳神",
            "kickoff_at": "2026-09-02T18:00:00+08:00",
        }
        with patch(
            "outputs.tai_cai_tracker.football433._collect_match_list_page",
            side_effect=[[], [], [], [row], [], []],
        ) as collect_page:
            result = discover_433_detail_urls([fixture])

        self.assertEqual(result["2041224"]["status"], "resolved")
        self.assertEqual(
            [call.args for call in collect_page.call_args_list],
            [
                (1, "2026-09-02"), (2, "2026-09-02"), (3, "2026-09-02"),
                (1, "2026-09-03"), (2, "2026-09-03"), (3, "2026-09-03"),
            ],
        )

    def test_parse_match_detail_keeps_named_immediate_1x2_rows(self):
        parsed = parse_match_detail(DETAIL_HTML)

        self.assertEqual(parsed["event"], {
            "home_team": "纽卡斯尔联",
            "away_team": "西布朗",
            "kickoff_local": "2026-08-27 02:45",
        })
        rows = parsed["market"]["rows"]
        self.assertEqual(rows[0]["bookmaker"], "Bet 365")
        self.assertEqual(rows[0]["prices"], {
            "home_win": "1.33", "draw": "5", "away_win": "8.5"
        })
        self.assertEqual(rows[1]["bookmaker"], "皇冠")

    def test_parse_match_detail_rejects_incomplete_identity(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            parse_match_detail("<table id='match_1'></table>")

    def test_parse_match_detail_deduplicates_language_variant_bookmaker_name(self):
        doubled = DETAIL_HTML.replace(
            "<span>Bet 365</span>",
            "<span>Bet 365</span><span>Bet 365</span>",
        )

        parsed = parse_match_detail(doubled)

        self.assertEqual(parsed["market"]["rows"][0]["bookmaker"], "Bet 365")

    def test_find_match_detail_url_uses_kickoff_and_conservative_team_aliases(self):
        matches = [
            [1331072, 1787769900000, 0, 0, False, False, False, False, "", 33, 636, 0, 0,
             200, "纽卡斯尔联", "紐卡素聯", 0, 0, 0, 11,
             1091, "西布朗", "西布朗", 0, 0, 0, 2, False, False, False, False],
        ]

        url = find_match_detail_url(
            matches,
            home_team="纽卡斯尔",
            away_team="西布罗姆",
            kickoff_at="2026-08-27T02:45:00+08:00",
        )

        self.assertEqual(url, "https://www.433.cool/zqzhishu/data/1331072.html")

    def test_find_match_detail_url_accepts_verified_current_chinese_name_variants(self):
        variants = [
            ("奥萨苏纳", "赫塔费", "奥萨苏纳", "赫塔菲"),
            ("第戎", "圣埃蒂安", "迪卓恩", "圣伊天"),
            ("维拉", "阿森纳", "阿斯顿维拉", "阿森纳"),
            ("本菲卡", "埃斯托里", "本菲卡", "伊斯托里尔"),
            ("布拉加", "吉马良斯", "布拉加", "甘马雷斯"),
            ("巴萨", "巴列卡诺", "巴塞罗那", "巴列卡诺"),
            ("林肯城", "布莱克本", "林肯城", "布力般流浪"),
            ("普雷斯顿", "布城", "普雷斯顿", "布里斯托城"),
            ("谢菲联", "博尔顿", "谢菲尔德联", "保顿"),
            ("水户蜀葵", "鹿岛鹿角", "水户霍利克", "鹿岛鹿角"),
            ("萨索洛", "弗洛西诺", "森索罗", "弗洛辛诺内"),
            ("山形山神", "甲府风林", "山形蒙迪奥", "甲府风林"),
            ("济州SK", "蔚山现代", "济州联队", "蔚山现代"),
            ("雅典AEK", "LASK林茨", "AEK雅典", "LASK林茨"),
            ("奈梅亨", "SBV精英", "NEC尼美根", "SBV精英队"),
            ("皇马", "国际米兰", "皇家马德里", "国际米兰"),
            ("里尔", "贝蒂斯", "利尔", "皇家贝蒂斯"),
            ("波尔图", "曼城", "波尔图", "曼彻斯特城"),
            ("弗鲁米嫩", "普拉滕斯", "弗鲁米嫩塞", "普拉腾斯"),
            ("南安普敦", "斯旺西", "南安普敦", "史云斯"),
            ("巴萨", "费耶诺德", "巴塞罗那", "飞燕诺"),
            ("特温特", "特尔斯达", "川迪", "特士达"),
            ("里斯本", "加拉塔萨", "葡萄牙体育", "加拉塔沙雷"),
            ("利物浦", "马竞", "利物浦", "马德里竞技"),
            ("巴黎圣曼", "布拉迪斯", "巴黎圣日耳曼", "斯洛云"),
            ("帕梅拉斯", "基多体大", "帕尔梅拉斯", "奎托"),
        ]
        for index, (official_home, official_away, source_home, source_away) in enumerate(variants):
            with self.subTest(official_home=official_home, official_away=official_away):
                matches = [[
                    1333000 + index, 1787769900000, 0, 0, False, False, False, False,
                    "", 33, 636, 0, 0, 200, source_home, source_home, 0, 0, 0, 11,
                    1091, source_away, source_away, 0, 0, 0, 2, False, False, False, False,
                ]]

                url = find_match_detail_url(
                    matches,
                    official_home,
                    official_away,
                    "2026-08-27T02:45:00+08:00",
                )

                self.assertEqual(url, f"https://www.433.cool/zqzhishu/data/{1333000 + index}.html")

    def test_discover_433_detail_url_checks_bounded_later_pages(self):
        target = [1331072, 1787769900000, 0, 0, False, False, False, False, "", 33, 636, 0, 0,
                  200, "纽卡斯尔联", "紐卡素聯", 0, 0, 0, 11,
                  1091, "西布朗", "西布朗", 0, 0, 0, 2, False, False, False, False]
        with patch(
            "outputs.tai_cai_tracker.football433._collect_match_list_page",
            side_effect=[[], [target], []],
        ) as collect_page:
            url = discover_433_detail_url(
                "纽卡斯尔", "西布罗姆", "2026-08-27T02:45:00+08:00", max_pages=3
            )

        self.assertEqual(url, "https://www.433.cool/zqzhishu/data/1331072.html")
        self.assertEqual(
            [call.args for call in collect_page.call_args_list],
            [(1, "2026-08-27"), (2, "2026-08-27"), (3, "2026-08-27")],
        )

    def test_batch_discovery_supports_an_extended_bounded_page_limit(self):
        target = [1331072, 1787769900000, 0, 0, False, False, False, False, "", 33, 636, 0, 0,
                  200, "纽卡斯尔联", "紐卡素聯", 0, 0, 0, 11,
                  1091, "西布朗", "西布朗", 0, 0, 0, 2, False, False, False, False]
        fixture = {
            "match_id": 2041183,
            "home_team": "纽卡斯尔",
            "away_team": "西布罗姆",
            "kickoff_at": "2026-08-27T02:45:00+08:00",
        }
        with patch(
            "outputs.tai_cai_tracker.football433._collect_match_list_page",
            side_effect=[[], [], [], [target]],
        ) as collect_page:
            result = discover_433_detail_urls([fixture], max_pages=4)

        self.assertEqual(result["2041183"]["status"], "resolved")
        self.assertEqual(
            [call.args for call in collect_page.call_args_list],
            [(1, "2026-08-27"), (2, "2026-08-27"), (3, "2026-08-27"), (4, "2026-08-27")],
        )


if __name__ == "__main__":
    unittest.main()
