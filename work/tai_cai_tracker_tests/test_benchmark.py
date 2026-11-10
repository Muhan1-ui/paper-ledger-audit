"""Offline tests for the post-match closing-line benchmark module."""

import json
import tempfile
import unittest
from pathlib import Path

from outputs.tai_cai_tracker import benchmark

DEFAULT_PROBS = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}


def prob_strings(values):
    return {key: str(value) for key, value in values.items()}


def make_prediction(
    content_hash,
    match_id,
    kickoff,
    competition,
    home,
    away,
    *,
    role="primary",
    blended=None,
    market=None,
    outcomes=None,
):
    final = outcomes if outcomes is not None else (blended or DEFAULT_PROBS)
    record = {
        "record_type": "prediction",
        "content_hash": content_hash,
        "match_id": match_id,
        "kickoff_at": kickoff,
        "match_context": {
            "competition": competition,
            "home_team": home,
            "away_team": away,
        },
        "outcomes": [
            {"label": label, "probability": str(value)}
            for label, value in final.items()
        ],
        "status": "frozen",
    }
    if role is not None:
        record["prediction_role"] = role
    if blended is not None:
        record["model"] = {"blended_outcomes": prob_strings(blended)}
        if market is not None:
            record["model"]["market_outcomes"] = prob_strings(market)
    return record


def make_settlement(prediction_hash, actual="home_win"):
    return {
        "record_type": "settlement",
        "content_hash": "settlement-" + prediction_hash,
        "prediction_hash": prediction_hash,
        "actual_outcome": actual,
        "status": "settled",
    }


def make_history(division, match_date, home, away, fair=DEFAULT_PROBS, implied=DEFAULT_PROBS):
    return {
        "division": division,
        "date": match_date,
        "home_team": home,
        "away_team": away,
        "odds": {
            "pinnacle": {
                "fair": None if fair is None else prob_strings(fair),
                "implied": None if implied is None else prob_strings(implied),
            }
        },
    }


def alias_document(*names):
    return benchmark.build_team_alias(list(names), {})


class NormalizationTests(unittest.TestCase):
    def test_strips_accents_case_and_punctuation(self):
        self.assertEqual(benchmark.normalize_name("Atlético Madrid"), "atleticomadrid")
        self.assertEqual(
            benchmark.normalize_name("Atletico  Madrid!"), benchmark.normalize_name("Atlético Madrid")
        )
        self.assertEqual(benchmark.normalize_name("Málaga CF"), "malaga")
        self.assertEqual(benchmark.normalize_name("São Paulo"), "saopaulo")

    def test_common_word_mapping_is_single_pass(self):
        self.assertEqual(benchmark.normalize_name("FC Barcelona"), "barcelona")
        self.assertEqual(
            benchmark.normalize_name("Newcastle United"),
            benchmark.normalize_name("Newcastle Utd"),
        )
        # No fuzzy matching: an abbreviation is not expanded by similarity.
        self.assertNotEqual(
            benchmark.normalize_name("Manchester United"),
            benchmark.normalize_name("Man Utd"),
        )


class ExactTeamMatchKeyTests(unittest.TestCase):
    def test_club_suffix_and_accent_are_removed(self):
        self.assertEqual(benchmark.team_match_key("FC Barcelona"), "barcelona")
        self.assertEqual(benchmark.team_match_key("AFC Bournemouth"), "bournemouth")
        self.assertEqual(benchmark.team_match_key("Málaga CF"), "malaga")
        self.assertEqual(benchmark.team_match_key("AC Milan"), "milan")

    def test_only_exact_equality_matches(self):
        self.assertEqual(
            benchmark.team_match_key("Tottenham Hotspur"), "tottenham hotspur"
        )
        self.assertNotEqual(
            benchmark.team_match_key("Tottenham Hotspur"),
            benchmark.team_match_key("Tottenham"),
        )
        self.assertNotEqual(
            benchmark.team_match_key("Manchester City"),
            benchmark.team_match_key("Man City"),
        )


def resolved_entry(chinese, english, aliases=(), qid="Q1"):
    return {
        "status": "resolved",
        "official_team": chinese,
        "qid": qid,
        "chinese_name": chinese,
        "english_name": english,
        "english_names": [english, *aliases],
        "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json",
        "source_url": f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={qid}&format=json",
        "captured_at": "2026-11-10T12:00:00+00:00",
    }


class WikidataAliasTests(unittest.TestCase):
    def test_alias_sources_use_exact_normalized_names_only(self):
        teams = {
            "巴萨": resolved_entry("巴萨", "FC Barcelona"),
            "热刺": resolved_entry("热刺", "Tottenham Hotspur FC", aliases=["Spurs"]),
            "曼城": resolved_entry("曼城", "Manchester City FC"),
            "未知": {"status": "unresolved", "reason": "wikidata_exact_name_not_found"},
            "故障队": {
                "status": "failed",
                "reason": "HTTPError",
                "http_status": 403,
            },
        }
        sources, detail = benchmark.build_wikidata_alias_sources(
            teams, ["Barcelona", "Tottenham", "Man City"]
        )
        self.assertEqual(sources["巴萨"]["source_name"], "Barcelona")
        # "Tottenham Hotspur" does not equal football-data's "Tottenham".
        self.assertNotIn("热刺", sources)
        # "Manchester City" does not equal football-data's "Man City".
        self.assertNotIn("曼城", sources)
        self.assertEqual(len(detail["resolved"]), 1)
        self.assertEqual(len(detail["unmatched"]), 2)
        self.assertEqual(
            [item["official_team"] for item in detail["failed"]], ["故障队", "未知"]
        )
        self.assertEqual(detail["failed"][0]["http_status"], 403)

    def test_alias_source_becomes_chinese_alias_for_football_data_name(self):
        sources, _ = benchmark.build_wikidata_alias_sources(
            {"巴萨": resolved_entry("巴萨", "FC Barcelona")}, ["Barcelona", "Roma"]
        )
        document = benchmark.build_team_alias(
            ["Barcelona", "Roma"], sources
        )
        self.assertIn("巴萨", document["Barcelona"])
        self.assertEqual(document["Roma"], ["Roma"])

    def test_verified_registry_identity_seeds_cache_without_request(self):
        entry = resolved_entry("巴萨", "FC Barcelona")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "source_link_registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "matches": {},
                        "fotmob_teams": {},
                        "wikidata_teams": {"巴萨": entry},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cache_path = root / "wikidata_team_cache.json"
            teams = benchmark.seed_wikidata_cache_from_registry(
                ["巴萨"], cache_path, registry_path
            )
            self.assertEqual(teams["巴萨"]["status"], "resolved")
            self.assertEqual(teams["巴萨"]["cache_source"], "source_link_registry")
            sources, _ = benchmark.build_wikidata_alias_sources(
                teams, ["Barcelona"]
            )
            self.assertEqual(sources["巴萨"]["source_name"], "Barcelona")
            self.assertTrue(cache_path.exists())

    def test_resolve_wikidata_teams_caches_success_and_failure(self):
        calls = []

        def fake_resolve(name):
            calls.append(name)
            if name == "故障队":
                raise PermissionError("blocked")
            return resolved_entry(name, "FC Barcelona")

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "wikidata_team_cache.json"
            teams, failures = benchmark.resolve_wikidata_teams(
                ["巴萨", "故障队", "巴萨"], cache_path, resolve_identity=fake_resolve
            )
            self.assertEqual(calls, ["巴萨", "故障队"])
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["official_team"], "故障队")
            self.assertEqual(teams["巴萨"]["status"], "resolved")
            self.assertEqual(teams["故障队"]["status"], "failed")
            cached = benchmark.load_wikidata_cache(cache_path)
            self.assertEqual(cached["巴萨"]["qid"], "Q1")
            # Resolved names are never requested again.
            _, failures = benchmark.resolve_wikidata_teams(
                ["巴萨"], cache_path, resolve_identity=fake_resolve
            )
            self.assertEqual(calls, ["巴萨", "故障队"])
            self.assertFalse(failures)


class TeamAliasTests(unittest.TestCase):
    def test_build_team_alias_uses_real_names_only(self):
        document = benchmark.build_team_alias(
            ["Arsenal", "Atalanta", "Roma"],
            {"阿森纳": {"source_name": "Arsenal"}},
        )
        self.assertEqual(document["Arsenal"][0], "Arsenal")
        self.assertIn("阿森纳", document["Arsenal"])
        self.assertEqual(document["Atalanta"], ["Atalanta"])
        self.assertEqual(document["Roma"], ["Roma"])

    def test_alias_lookup_resolves_chinese_name_and_identity(self):
        document = benchmark.build_team_alias(
            ["Arsenal", "Atalanta"],
            {"阿森纳": {"source_name": "Arsenal"}},
        )
        lookup = benchmark.build_alias_lookup(document)
        self.assertEqual(benchmark.resolve_team("阿森纳", lookup), "Arsenal")
        self.assertEqual(benchmark.resolve_team("atalanta", lookup), "Atalanta")
        self.assertIsNone(benchmark.resolve_team("Unknown FC", lookup))


class DateWindowTests(unittest.TestCase):
    def test_kickoff_is_converted_to_utc_date(self):
        self.assertEqual(
            benchmark.kickoff_utc_date("2026-09-02T02:45:00+08:00").isoformat(),
            "2026-09-01",
        )

    def test_window_is_plus_or_minus_one_day(self):
        cases = [
            ("2026-08-31", True),
            ("2026-09-01", True),
            ("2026-09-02", True),
            ("2026-08-30", False),
            ("2026-09-03", False),
        ]
        for match_date, expected in cases:
            with self.subTest(match_date=match_date):
                prediction = make_prediction(
                    "p1",
                    "周一001",
                    "2026-09-02T02:45:00+08:00",
                    "英超",
                    "Chelsea",
                    "Fulham",
                )
                eligible, skipped = benchmark.eligible_predictions(
                    [prediction, make_settlement("p1", "away_win")]
                )
                rows = benchmark.match_eligible(
                    eligible,
                    [make_history("E0", match_date, "Chelsea", "Fulham")],
                    alias_document("Chelsea", "Fulham"),
                )
                self.assertEqual(skipped, 0)
                self.assertEqual(rows[0]["matched"], expected)
                if not expected:
                    self.assertEqual(rows[0]["reason"], "no_date_candidate")


class AmbiguityTests(unittest.TestCase):
    def test_equal_date_distance_is_ambiguous(self):
        prediction = make_prediction(
            "p1", "周一001", "2026-09-02T02:45:00+08:00", "英超", "Chelsea", "Fulham"
        )
        eligible, _ = benchmark.eligible_predictions(
            [prediction, make_settlement("p1")]
        )
        rows = benchmark.match_eligible(
            eligible,
            [
                make_history("E0", "2026-08-31", "Chelsea", "Fulham"),
                make_history("E0", "2026-09-02", "Chelsea", "Fulham"),
            ],
            alias_document("Chelsea", "Fulham"),
        )
        self.assertFalse(rows[0]["matched"])
        self.assertEqual(rows[0]["reason"], "ambiguous")

    def test_closer_date_wins(self):
        prediction = make_prediction(
            "p1", "周一001", "2026-09-02T02:45:00+08:00", "英超", "Chelsea", "Fulham"
        )
        eligible, _ = benchmark.eligible_predictions(
            [prediction, make_settlement("p1")]
        )
        rows = benchmark.match_eligible(
            eligible,
            [
                make_history("E0", "2026-08-31", "Chelsea", "Fulham"),
                make_history("E0", "2026-09-01", "Chelsea", "Fulham"),
            ],
            alias_document("Chelsea", "Fulham"),
        )
        self.assertTrue(rows[0]["matched"])
        self.assertEqual(rows[0]["fd_date"], "2026-09-01")


class ScoringTests(unittest.TestCase):
    def test_rps_and_brier_hand_computed(self):
        probabilities = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}
        self.assertAlmostEqual(benchmark.rps_score(probabilities, "away_win"), 0.445)
        self.assertAlmostEqual(benchmark.brier_score(probabilities, "away_win"), 0.98)
        uniform = {"home_win": 1 / 3, "draw": 1 / 3, "away_win": 1 / 3}
        self.assertAlmostEqual(benchmark.rps_score(uniform, "home_win"), 5 / 18)

    def test_evaluate_rows_scores_three_series(self):
        rows = [
            {
                "matched": True,
                "actual": "away_win",
                "your_blended": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
                "prematch_market": {"home_win": 0.4, "draw": 0.3, "away_win": 0.3},
                "market_close_fair": {"home_win": 0.2, "draw": 0.3, "away_win": 0.5},
                "market_close_source": "pinnacle",
            }
        ]
        metrics = benchmark.evaluate_rows(rows)
        self.assertAlmostEqual(metrics["your_blended"]["rps"], 0.445)
        self.assertAlmostEqual(metrics["your_blended"]["brier"], 0.98)
        self.assertEqual(metrics["your_blended"]["top1"], 0.0)
        self.assertAlmostEqual(metrics["prematch_market"]["rps"], 0.325)
        self.assertAlmostEqual(metrics["prematch_market"]["brier"], 0.74)
        self.assertAlmostEqual(metrics["market_close_fair"]["rps"], 0.145)
        self.assertAlmostEqual(metrics["market_close_fair"]["brier"], 0.38)
        self.assertEqual(metrics["market_close_fair"]["top1"], 1.0)
        self.assertEqual(metrics["market_close_sources"], {"pinnacle": 1})

    def test_divergence_count_and_prediction_hit(self):
        rows = [
            {
                "matched": True,
                "actual": "away_win",
                "your_blended": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
                "market_close_fair": {"home_win": 0.2, "draw": 0.3, "away_win": 0.5},
                "market_close_source": "pinnacle",
            },
            {
                "matched": True,
                "actual": "home_win",
                "your_blended": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
                "market_close_fair": {"home_win": 0.4, "draw": 0.3, "away_win": 0.3},
                "market_close_source": "avg",
            },
        ]
        # 新语义：divergence 只在 market_close_fair 非空且其 top1 与
        # your_blended 的 top1 不同时计数；第二行两者同为 home_win，故不计入。
        divergence = benchmark.evaluate_rows(rows)["divergence"]
        self.assertEqual(divergence["n"], 1)
        self.assertEqual(divergence["paired"], 2)
        self.assertEqual(divergence["pred_top1_hit"], 0.0)


class MissingEvidenceTests(unittest.TestCase):
    def test_missing_pinnacle_still_matches_without_crashing(self):
        prediction = make_prediction(
            "p1", "周一001", "2026-09-02T02:45:00+08:00", "英超", "Chelsea", "Fulham"
        )
        eligible, _ = benchmark.eligible_predictions(
            [prediction, make_settlement("p1", "home_win")]
        )
        rows = benchmark.match_eligible(
            eligible,
            [make_history("E0", "2026-09-01", "Chelsea", "Fulham", fair=None, implied=None)],
            alias_document("Chelsea", "Fulham"),
        )
        self.assertTrue(rows[0]["matched"])
        self.assertIsNone(rows[0]["market_close_fair"])
        self.assertIsNone(rows[0]["market_close_source"])
        metrics = benchmark.evaluate_rows(rows)
        self.assertEqual(metrics["market_close_fair"]["n"], 0)
        self.assertIsNone(metrics["market_close_fair"]["rps"])
        self.assertEqual(metrics["your_blended"]["n"], 1)


class FilteringTests(unittest.TestCase):
    def test_competition_filter_and_primary_role(self):
        records = [
            make_prediction(
                "p1", "周一001", "2026-09-02T02:45:00+08:00", "英超", "Chelsea", "Fulham"
            ),
            make_prediction(
                "p2", "周二002", "2026-09-02T03:00:00+08:00", "欧冠", "Chelsea", "Fulham"
            ),
            make_prediction(
                "p3", "周三003", "2026-09-02T02:45:00+08:00", "英冠", "Chelsea", "Fulham"
            ),
            make_prediction(
                "p4",
                "周四004",
                "2026-09-02T02:45:00+08:00",
                "英超",
                "Chelsea",
                "Fulham",
                role="comparison",
            ),
            make_prediction(
                "p5",
                "周五005",
                "2026-09-02T02:45:00+08:00",
                "英超",
                "Chelsea",
                "Fulham",
                role=None,
            ),
            make_settlement("p1"),
            make_settlement("p2"),
            make_settlement("p3"),
            make_settlement("p4"),
            make_settlement("p5"),
        ]
        eligible, skipped = benchmark.eligible_predictions(records)
        self.assertEqual(skipped, 1)
        self.assertEqual([prediction["content_hash"] for prediction, _ in eligible], ["p1", "p3"])

        rows = benchmark.match_eligible(
            eligible,
            [],
            alias_document("Chelsea", "Fulham"),
        )
        # 英冠 now maps to the collected E1 division, so it is no longer
        # reported as competition_unmapped.
        self.assertEqual(rows[0]["reason"], "no_date_candidate")
        self.assertEqual(rows[1]["reason"], "no_date_candidate")

    def test_uncollected_competition_is_reported_as_unmapped(self):
        prediction = make_prediction(
            "p1", "周一001", "2026-09-02T02:45:00+08:00", "欧冠", "Chelsea", "Fulham"
        )
        eligible, skipped = benchmark.eligible_predictions(
            [prediction, make_settlement("p1")]
        )
        self.assertEqual(skipped, 1)
        self.assertEqual(eligible, [])

    def test_team_unmapped_reason_is_recorded(self):
        prediction = make_prediction(
            "p1", "周一001", "2026-09-02T02:45:00+08:00", "英超", "Atlantis", "Fulham"
        )
        eligible, _ = benchmark.eligible_predictions(
            [prediction, make_settlement("p1")]
        )
        rows = benchmark.match_eligible(
            eligible,
            [make_history("E0", "2026-09-01", "Chelsea", "Fulham")],
            alias_document("Chelsea", "Fulham"),
        )
        self.assertFalse(rows[0]["matched"])
        self.assertEqual(rows[0]["reason"], "team_unmapped")


class LinkTests(unittest.TestCase):
    def test_link_writes_benchmark_and_alias_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "paper_ledger.jsonl"
            history_path = root / "history_matches.jsonl"
            chinese_map_path = root / "chinese_team_map.json"
            team_alias_path = root / "team_alias_fd.json"
            output_path = root / "ledger_benchmark.jsonl"

            prediction = make_prediction(
                "p1",
                "周二009",
                "2026-09-02T02:45:00+08:00",
                "意甲",
                "亚特兰大",
                "罗马",
                blended={"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
                market={"home_win": 0.4, "draw": 0.3, "away_win": 0.3},
            )
            records = [prediction, make_settlement("p1", "away_win")]
            ledger_path.write_text(
                "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                encoding="utf-8",
            )
            history_path.write_text(
                json.dumps(
                    make_history(
                        "I1",
                        "2026-09-01",
                        "Atalanta",
                        "Roma",
                        fair={"home_win": 0.2, "draw": 0.3, "away_win": 0.5},
                        implied={"home_win": 0.2, "draw": 0.3, "away_win": 0.5},
                    ),
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            chinese_map_path.write_text(
                json.dumps(
                    {
                        "亚特兰大": "Atalanta",
                        "罗马": "Roma",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            rows, summary = benchmark.run_link(
                ledger_path=ledger_path,
                history_path=history_path,
                chinese_team_map_path=chinese_map_path,
                team_alias_path=team_alias_path,
                output_path=output_path,
                wikidata_cache_path=root / "wikidata_team_cache.json",
                source_link_registry_path=root / "source_link_registry.json",
            )

            self.assertEqual(summary["eligible"], 1)
            self.assertEqual(summary["matched"], 1)
            self.assertEqual(summary["match_rate"], 1.0)
            self.assertEqual(summary["reasons"], {})
            self.assertEqual(rows[0]["fd_home"], "Atalanta")
            self.assertEqual(rows[0]["fd_date"], "2026-09-01")

            written = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(written), 1)
            self.assertTrue(written[0]["matched"])
            self.assertEqual(written[0]["market_close_fair"]["away_win"], 0.5)
            self.assertEqual(written[0]["market_close_source"], "pinnacle")
            self.assertEqual(written[0]["your_blended"]["home_win"], 0.5)
            self.assertEqual(written[0]["prematch_market"]["home_win"], 0.4)

            document = json.loads(team_alias_path.read_text(encoding="utf-8"))
            self.assertIn("亚特兰大", document["Atalanta"])
            self.assertIn("罗马", document["Roma"])

            metrics = benchmark.evaluate_rows(written)
            self.assertEqual(metrics["your_blended"]["n"], 1)
            self.assertEqual(metrics["market_close_fair"]["n"], 1)
            self.assertEqual(metrics["market_close_fair"]["top1"], 1.0)
            self.assertEqual(metrics["market_close_sources"], {"pinnacle": 1})
            self.assertEqual(metrics["divergence"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
