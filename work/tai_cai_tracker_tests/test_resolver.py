import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from outputs.tai_cai_tracker.resolver import (
    cached_fotmob_team_identity,
    cached_source_links,
    cached_wikidata_team_identity,
    candidate_template_from_match,
    context_source_aliases,
    format_workflow_next,
    import_manual_understat_aliases,
    import_reviewed_fotmob_search_aliases,
    link_summary_from_resolved,
    normalize_team_name,
    record_adopted_aliases,
    record_fotmob_team_identity,
    record_source_links,
    record_wikidata_team_identity,
    rank_match_candidates,
    rank_context_event_candidates,
    resolve_links,
    team_similarity,
    reviewed_fotmob_search_aliases,
    understat_source_aliases,
    workflow_next,
)


class ResolverTests(unittest.TestCase):
    def test_reviewed_fotmob_search_alias_only_records_audited_query_keyword(self):
        entry = {
            "official_team": "AC米兰",
            "search_name": "AC Milan",
            "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2042001",
            "fotmob_search_url": "https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term=AC%20Milan",
            "captured_at": "2099-01-01T12:00:00+08:00",
            "raw_content_sha256": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            input_path = temporary_path / "reviewed-aliases.json"
            registry_path = temporary_path / "fotmob-search-aliases.json"
            input_path.write_text(json.dumps({"aliases": [entry]}), encoding="utf-8")

            registry = import_reviewed_fotmob_search_aliases(registry_path, input_path)

            aliases = reviewed_fotmob_search_aliases(registry_path)

        stored = registry["aliases"]["ac米兰"]
        self.assertEqual(aliases, {"ac米兰": "AC Milan"})
        self.assertEqual(stored["search_name"], "AC Milan")
        self.assertNotIn("source_team_id", stored)
        self.assertNotIn("source_name", stored)

    def test_normalize_team_name_removes_common_suffixes_and_aliases(self):
        self.assertEqual(normalize_team_name("Ulsan HD FC"), "ulsan hd")
        self.assertEqual(normalize_team_name("FC Seoul"), "seoul")
        self.assertEqual(normalize_team_name("首尔足球俱乐部"), "seoul")

    def test_team_similarity_scores_aliases_above_unrelated_names(self):
        self.assertGreaterEqual(team_similarity("首尔", "FC Seoul"), 0.80)
        self.assertLess(team_similarity("首尔", "Pohang Steelers"), 0.50)


class CandidateRankingTests(unittest.TestCase):
    def test_manual_understat_alias_import_writes_traceable_aliases_for_batch_xg(self):
        payload = {
            "provider": "Understat",
            "confirmation_method": "manual_bilingual_review",
            "entries": [{
                "official_team": "莱切",
                "official_source_url": "https://m.sporttery.cn/zqlszl/bssj/?mid=2041177",
                "source_team": "Lecce",
                "source_team_id": "79",
                "source_url": "https://understat.com/getLeagueData/Serie%20A/2026",
                "captured_at": "2026-08-31T10:00:00+08:00",
                "raw_content_sha256": "a" * 64,
            }],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "aliases.json"
            registry = import_manual_understat_aliases(registry_path, payload)
            aliases = json.loads(registry_path.read_text(encoding="utf-8"))

        self.assertEqual(registry["aliases"]["莱切"]["source_name"], "Lecce")
        self.assertEqual(registry["aliases"]["莱切"]["confirmation_method"], "manual_bilingual_review")
        self.assertEqual(aliases["aliases"]["莱切"]["source_provider"], "Understat")

    def test_manual_understat_alias_import_rejects_untraceable_source(self):
        payload = {
            "provider": "Understat",
            "confirmation_method": "manual_bilingual_review",
            "entries": [{
                "official_team": "莱切",
                "official_source_url": "https://m.sporttery.cn/zqlszl/bssj/?mid=2041177",
                "source_team": "Lecce",
                "source_team_id": "79",
                "source_url": "https://example.test/teams/lecce",
                "captured_at": "2026-08-31T10:00:00+08:00",
                "raw_content_sha256": "a" * 64,
            }],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "aliases.json"
            with self.assertRaisesRegex(ValueError, "Understat source_url"):
                import_manual_understat_aliases(registry_path, payload)

    def test_understat_source_aliases_excludes_other_provider_names(self):
        registry = {
            "version": 1,
            "aliases": {
                "莱切": {"source_name": "Lecce", "source_team_id": "243", "source_provider": "Understat"},
                "罗马": {"source_name": "AS Roma", "source_team_id": "95", "source_provider": "SofaScore"},
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "aliases.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            aliases = understat_source_aliases(registry_path)

        self.assertEqual(aliases, {"莱切": "Lecce"})

    def test_context_source_aliases_excludes_understat_aliases(self):
        registry = {
            "version": 1,
            "aliases": {
                "莱切": {"source_name": "Lecce", "source_team_id": "243", "source_provider": "Understat"},
                "首尔": {"source_name": "FC Seoul", "source_team_id": "10"},
                "蔚山hd": {"source_name": "Ulsan HD", "source_team_id": "20", "source_provider": "SofaScore"},
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "aliases.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            aliases = context_source_aliases(registry_path)

        self.assertEqual(aliases, {"首尔": "FC Seoul", "蔚山hd": "Ulsan HD"})

    def test_source_link_registry_reuses_only_exact_match_identity_and_team_pages(self):
        match = {
            "match_id": 2040623, "home_team": "首尔", "away_team": "蔚山HD",
            "kickoff_at": "2026-07-27T18:30:00+08:00",
        }
        links = {
            "european_433_url": "https://www.433.cool/zqzhishu/data/1331072.html",
            "xgscore_url": "https://xgscore.io/k-league-1/seoul-ulsan/xgscore",
            "home_fotmob_url": "https://www.fotmob.com/teams/10/overview/fc-seoul",
            "away_fotmob_url": "https://www.fotmob.com/teams/20/overview/ulsan-hd",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            record_source_links(registry_path, match, links)
            exact = cached_source_links(registry_path, match)
            changed_kickoff = cached_source_links(
                registry_path, {**match, "kickoff_at": "2026-07-27T18:45:00+08:00"}
            )

        self.assertEqual(exact, links)
        self.assertEqual(changed_kickoff, {
            "home_fotmob_url": links["home_fotmob_url"],
            "away_fotmob_url": links["away_fotmob_url"],
        })

    def test_source_link_registry_keeps_both_concurrent_match_updates(self):
        matches = [
            {"match_id": 2041218, "home_team": "朴次茅斯", "away_team": "德比郡", "kickoff_at": "2026-09-02T02:45:00+08:00"},
            {"match_id": 2041219, "home_team": "普雷斯顿", "away_team": "布城", "kickoff_at": "2026-09-02T02:45:00+08:00"},
        ]
        links = [
            {"european_433_url": "https://www.433.cool/zqzhishu/data/1332725.html"},
            {"european_433_url": "https://www.433.cool/zqzhishu/data/1332673.html"},
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda pair: record_source_links(registry_path, *pair), zip(matches, links)))
            first = cached_source_links(registry_path, matches[0])
            second = cached_source_links(registry_path, matches[1])

        self.assertEqual(first["european_433_url"], links[0]["european_433_url"])
        self.assertEqual(second["european_433_url"], links[1]["european_433_url"])

    def test_fotmob_team_identity_records_verified_provider_id_metadata(self):
        candidate = {
            "team_id": 92630,
            "team_name": "FC Seoul",
            "source_url": "https://www.fotmob.com/teams/92630/overview/fc-seoul",
            "search_url": "https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term=FC%20Seoul",
            "score": 1.0,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            record_fotmob_team_identity(registry_path, "首尔FC", candidate)
            identity = cached_fotmob_team_identity(registry_path, "首尔FC")

        self.assertEqual(identity["source_team_id"], "92630")
        self.assertEqual(identity["source_name"], "FC Seoul")
        self.assertEqual(identity["source_url"], candidate["source_url"])

    def test_fotmob_team_identity_rejects_url_with_different_provider_team_id(self):
        candidate = {
            "team_id": 92630,
            "team_name": "FC Seoul",
            "source_url": "https://www.fotmob.com/teams/10/overview/fc-seoul",
            "search_url": "https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term=FC%20Seoul",
            "score": 1.0,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            with self.assertRaisesRegex(ValueError, "team ID"):
                record_fotmob_team_identity(registry_path, "首尔FC", candidate)

    def test_wikidata_identity_registry_retains_a_traceable_english_anchor(self):
        identity = {
            "status": "resolved",
            "official_team": "国际米兰",
            "qid": "Q631",
            "chinese_name": "国际米兰",
            "english_name": "Inter Milan",
            "english_names": ["Inter Milan", "Inter"],
            "search_url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=%E5%9B%BD%E9%99%85%E7%B1%B3%E5%85%B0&language=zh&format=json&limit=10",
            "source_url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q631&props=labels%7Caliases&languages=zh%7Cen&format=json",
            "captured_at": "2099-01-01T12:00:00+08:00",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "source-links.json"
            record_wikidata_team_identity(registry_path, identity)
            cached = cached_wikidata_team_identity(registry_path, "国际米兰")

        self.assertEqual(cached["qid"], "Q631")
        self.assertEqual(cached["english_name"], "Inter Milan")
        self.assertEqual(cached["english_names"], ["Inter Milan", "Inter"])
        self.assertEqual(cached["source_url"], identity["source_url"])
    def test_record_adopted_aliases_writes_only_source_identified_teams(self):
        adopted = {
            "status": "adopted",
            "score": 0.95,
            "source_url": "https://www.sofascore.com/api/v1/event/1",
            "candidate": {
                "home_team": "FC Seoul", "away_team": "Ulsan HD FC",
                "home_team_id": 10, "away_team_id": 20,
            },
        }
        sporttery = {"home_team": "\u9996\u5c14", "away_team": "\u851a\u5c71HD"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = Path(temporary_directory) / "aliases.json"
            registry = record_adopted_aliases(registry_path, sporttery, adopted)

        self.assertEqual(registry["aliases"]["首尔"]["source_name"], "FC Seoul")
        self.assertEqual(registry["aliases"]["蔚山hd"]["source_team_id"], 20)
    def test_context_event_requires_strict_score_and_margin_before_adoption(self):
        sporttery = {
            "home_team": "\u9996\u5c14",
            "away_team": "\u851a\u5c71HD",
            "competition": "K League 1",
            "kickoff_at": "2026-07-27T18:30:00+08:00",
        }
        candidates = [
            {
                "event_id": 1,
                "home_team": "FC Seoul",
                "away_team": "Ulsan HD FC",
                "competition": "K League 1",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
                "source_url": "https://www.sofascore.com/api/v1/event/1",
            },
            {
                "event_id": 2,
                "home_team": "Seoul E-Land",
                "away_team": "Ulsan HD FC",
                "competition": "K League 1",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
                "source_url": "https://www.sofascore.com/api/v1/event/2",
            },
        ]

        result = rank_context_event_candidates(sporttery, candidates)

        self.assertEqual(result["status"], "adopted")
        self.assertEqual(result["candidate"]["event_id"], 1)
        self.assertGreaterEqual(result["score"], 0.90)
        self.assertGreaterEqual(result["margin"], 0.08)

    def test_context_event_skips_when_runner_up_is_too_close(self):
        sporttery = {"home_team": "\u9996\u5c14", "away_team": "\u851a\u5c71HD", "kickoff_at": "2026-07-27T18:30:00+08:00"}
        candidates = [
            {"home_team": "FC Seoul", "away_team": "Ulsan HD FC", "kickoff_at": "2026-07-27T18:30:00+08:00", "source_url": "https://a.test"},
            {"home_team": "FC Seoul", "away_team": "Ulsan HD FC", "kickoff_at": "2026-07-27T18:45:00+08:00", "source_url": "https://b.test"},
        ]

        result = rank_context_event_candidates(sporttery, candidates)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "ambiguous candidates")
    def test_format_workflow_next_renders_plain_next_command(self):
        result = {
            "ready": False,
            "step": "create_candidate_template",
            "candidate_path": "work/candidates/2040623.json",
            "resolved_path": "work/resolved/2040623.json",
            "next_command": 'python -m outputs.tai_cai_tracker.cli candidate-template 2040623 --output "work/candidates/2040623.json"',
        }

        rendered = format_workflow_next(result)

        self.assertIn("create_candidate_template", rendered)
        self.assertIn("下一步命令", rendered)
        self.assertIn("candidate-template 2040623", rendered)

    def test_workflow_next_returns_candidate_template_command_when_candidate_file_missing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = workflow_next("2040623", base_dir=temporary_directory)

        self.assertEqual(result["step"], "create_candidate_template")
        self.assertFalse(result["ready"])
        self.assertIn("candidate-template 2040623", result["next_command"])

    def test_workflow_next_returns_resolve_command_when_candidate_exists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            candidates = base / "candidates"
            candidates.mkdir()
            (candidates / "2040623.json").write_text("{}", encoding="utf-8")

            result = workflow_next("2040623", base_dir=base)

        self.assertEqual(result["step"], "resolve_links")
        self.assertFalse(result["ready"])
        self.assertIn("resolve-links", result["next_command"])

    def test_workflow_next_reports_missing_links_when_resolved_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            (base / "candidates").mkdir()
            (base / "resolved").mkdir()
            (base / "candidates" / "2040623.json").write_text("{}", encoding="utf-8")
            (base / "resolved" / "2040623.json").write_text(
                json.dumps(
                    {
                        "sporttery": {"match_id": 2040623},
                        "oddsportal": {"status": "candidate", "source_url": "https://www.oddsportal.com/example"},
                        "fotmob_home": {"status": "skipped", "reason": "ambiguous candidates"},
                        "fotmob_away": {"status": "candidate", "source_url": "https://www.fotmob.com/teams/ulsan"},
                    }
                ),
                encoding="utf-8",
            )

            result = workflow_next("2040623", base_dir=base)

        self.assertEqual(result["step"], "fix_candidate_links")
        self.assertFalse(result["ready"])
        self.assertEqual(result["missing"], ["home_fotmob_url"])

    def test_workflow_next_returns_pregame_command_when_resolved_is_ready(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            (base / "candidates").mkdir()
            (base / "resolved").mkdir()
            (base / "candidates" / "2040623.json").write_text("{}", encoding="utf-8")
            (base / "resolved" / "2040623.json").write_text(
                json.dumps(
                    {
                        "sporttery": {"match_id": 2040623},
                        "oddsportal": {"status": "candidate", "source_url": "https://www.oddsportal.com/example"},
                        "fotmob_home": {"status": "candidate", "source_url": "https://www.fotmob.com/teams/seoul"},
                        "fotmob_away": {"status": "candidate", "source_url": "https://www.fotmob.com/teams/ulsan"},
                    }
                ),
                encoding="utf-8",
            )

            result = workflow_next("2040623", base_dir=base)

        self.assertEqual(result["step"], "run_pregame_bundle")
        self.assertTrue(result["ready"])
        self.assertIn("pregame-bundle 2040623", result["next_command"])

    def test_link_summary_from_resolved_returns_pregame_ready_urls(self):
        resolved = {
            "sporttery": {
                "match_id": 2040623,
                "home_team": "首尔",
                "away_team": "蔚山HD",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
            },
            "oddsportal": {
                "status": "candidate",
                "source_url": "https://www.oddsportal.com/example",
            },
            "fotmob_home": {
                "status": "candidate",
                "source_url": "https://www.fotmob.com/teams/seoul",
            },
            "fotmob_away": {
                "status": "candidate",
                "source_url": "https://www.fotmob.com/teams/ulsan",
            },
        }

        result = link_summary_from_resolved(resolved)

        self.assertTrue(result["ready"])
        self.assertEqual(result["oddsportal_url"], "https://www.oddsportal.com/example")
        self.assertEqual(result["home_fotmob_url"], "https://www.fotmob.com/teams/seoul")
        self.assertEqual(result["away_fotmob_url"], "https://www.fotmob.com/teams/ulsan")
        self.assertEqual(
            result["pregame_bundle_args"],
            [
                "2040623",
                "https://www.oddsportal.com/example",
                "https://www.fotmob.com/teams/seoul",
                "https://www.fotmob.com/teams/ulsan",
            ],
        )
        self.assertEqual(
            result["pregame_bundle_command"],
            'python -m outputs.tai_cai_tracker.cli pregame-bundle 2040623 "https://www.oddsportal.com/example" "https://www.fotmob.com/teams/seoul" "https://www.fotmob.com/teams/ulsan"',
        )

    def test_link_summary_from_resolved_reports_missing_skipped_candidates(self):
        resolved = {
            "sporttery": {"match_id": 2040623},
            "oddsportal": {"status": "candidate", "source_url": "https://www.oddsportal.com/example"},
            "fotmob_home": {"status": "skipped", "reason": "ambiguous candidates"},
            "fotmob_away": {"status": "candidate", "source_url": "https://www.fotmob.com/teams/ulsan"},
        }

        result = link_summary_from_resolved(resolved)

        self.assertFalse(result["ready"])
        self.assertIn("home_fotmob_url", result["missing"])
        self.assertNotIn("pregame_bundle_args", result)

    def test_candidate_template_from_match_copies_official_identity_only(self):
        match = {
            "match_id": 2040623,
            "match_num": "周一001",
            "competition": "K League 1",
            "home_team": "首尔",
            "away_team": "蔚山HD",
            "kickoff_at": "2026-07-27T18:30:00+08:00",
            "status": "2",
            "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
            "spf_fixed_prizes": {"home_win": "2.80", "draw": "3.10", "away_win": "2.20"},
        }

        result = candidate_template_from_match(match)

        self.assertEqual(
            result["sporttery"],
            {
                "match_id": 2040623,
                "match_num": "周一001",
                "competition": "K League 1",
                "home_team": "首尔",
                "away_team": "蔚山HD",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
                "official_source_url": "https://m.sporttery.cn/mjc/zqgdjjv1/?mid=2040623",
            },
        )
        self.assertEqual(result["oddsportal_candidates"], [])
        self.assertEqual(result["home_fotmob_candidates"], [])
        self.assertEqual(result["away_fotmob_candidates"], [])

    def test_rank_match_candidates_selects_high_confidence_pair(self):
        sporttery = {
            "home_team": "首尔",
            "away_team": "蔚山HD",
            "competition": "K League 1",
            "kickoff_at": "2026-07-27T18:30:00+08:00",
        }
        candidates = [
            {
                "home_team": "FC Seoul",
                "away_team": "Ulsan HD FC",
                "competition": "K League 1",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
                "source_url": "https://www.oddsportal.com/football/h2h/seoul/ulsan/#abc",
            }
        ]

        result = rank_match_candidates(sporttery, candidates)

        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["status"], "candidate")
        self.assertGreaterEqual(result["score"], 0.90)

    def test_rank_match_candidates_skips_close_tie(self):
        sporttery = {"home_team": "首尔", "away_team": "蔚山HD"}
        candidates = [
            {
                "home_team": "FC Seoul",
                "away_team": "Ulsan HD FC",
                "source_url": "https://a.test",
            },
            {
                "home_team": "Seoul E-Land",
                "away_team": "Ulsan HD FC",
                "source_url": "https://b.test",
            },
        ]

        result = rank_match_candidates(sporttery, candidates, tie_margin=0.20)

        self.assertEqual(result["status"], "skipped")
        self.assertIn("ambiguous", result["reason"])

    def test_resolve_links_reads_manual_candidate_file(self):
        payload = {
            "sporttery": {
                "home_team": "首尔",
                "away_team": "蔚山HD",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
            },
            "oddsportal_candidates": [
                {
                    "home_team": "FC Seoul",
                    "away_team": "Ulsan HD FC",
                    "kickoff_at": "2026-07-27T18:30:00+08:00",
                    "source_url": "https://www.oddsportal.com/example",
                }
            ],
            "home_fotmob_candidates": [
                {"team_name": "FC Seoul", "source_url": "https://www.fotmob.com/teams/seoul"}
            ],
            "away_fotmob_candidates": [
                {"team_name": "Ulsan HD FC", "source_url": "https://www.fotmob.com/teams/ulsan"}
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory) / "candidates.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")

            result = resolve_links(input_path)

        self.assertEqual(result["oddsportal"]["status"], "candidate")
        self.assertEqual(result["fotmob_home"]["status"], "candidate")
        self.assertEqual(result["fotmob_away"]["status"], "candidate")

    def test_resolve_links_rejects_candidate_from_unexpected_source_domain(self):
        payload = {
            "sporttery": {
                "home_team": "首尔",
                "away_team": "蔚山HD",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
            },
            "oddsportal_candidates": [
                {
                    "home_team": "FC Seoul",
                    "away_team": "Ulsan HD FC",
                    "kickoff_at": "2026-07-27T18:30:00+08:00",
                    "source_url": "https://untrusted.example.test/seoul-ulsan",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory) / "candidates.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")

            result = resolve_links(input_path)

        self.assertEqual(result["oddsportal"]["status"], "skipped")
        self.assertEqual(result["oddsportal"]["reason"], "source_url_domain_mismatch")
        self.assertEqual(
            result["oddsportal"]["rejections"][0]["candidate"]["source_url"],
            "https://untrusted.example.test/seoul-ulsan",
        )

    def test_resolve_links_uses_previously_adopted_source_aliases(self):
        payload = {
            "sporttery": {
                "home_team": "水原FC",
                "away_team": "首尔",
                "kickoff_at": "2026-07-27T18:30:00+08:00",
            },
            "oddsportal_candidates": [
                {
                    "home_team": "Suwon FC",
                    "away_team": "FC Seoul",
                    "kickoff_at": "2026-07-27T18:30:00+08:00",
                    "source_url": "https://www.oddsportal.com/football/south-korea/k-league-1/suwon-seoul/",
                }
            ],
        }
        registry = {
            "version": 1,
            "aliases": {
                "水原fc": {
                    "source_name": "Suwon FC",
                    "source_team_id": 1,
                    "source_url": "https://www.sofascore.com/api/v1/event/1",
                    "score": 1.0,
                    "first_seen_at": "2026-07-26T10:00:00+08:00",
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            input_path = base / "candidates.json"
            registry_path = base / "aliases.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = resolve_links(input_path, alias_registry_path=registry_path)

        self.assertEqual(result["oddsportal"]["status"], "candidate")
        self.assertGreaterEqual(result["oddsportal"]["score"], 0.90)


class NonLatinTeamSimilarityTests(unittest.TestCase):
    def test_team_similarity_matches_identical_non_latin_names_without_alias(self):
        self.assertEqual(team_similarity("赫根", "赫根"), 1.0)
        self.assertEqual(team_similarity("索尔纳", "索尔纳"), 1.0)


if __name__ == "__main__":
    unittest.main()
