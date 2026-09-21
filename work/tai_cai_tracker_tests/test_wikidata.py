import unittest

from outputs.tai_cai_tracker.wikidata import resolve_team_identity


class WikidataIdentityTests(unittest.TestCase):
    def test_resolves_one_exact_chinese_team_to_a_qid_and_english_name(self):
        search_url = "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=%E5%9B%BD%E9%99%85%E7%B1%B3%E5%85%B0&language=zh&format=json&limit=10"
        entity_url = "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q631&props=labels%7Caliases&languages=zh%7Cen&format=json"
        payloads = {
            search_url: {"search": [{"id": "Q631", "label": "国际米兰"}]},
            entity_url: {
                "entities": {
                    "Q631": {
                        "labels": {
                            "zh": {"language": "zh", "value": "国际米兰"},
                            "en": {"language": "en", "value": "Inter Milan"},
                        },
                        "aliases": {
                            "zh": [{"language": "zh", "value": "国米"}],
                            "en": [
                                {"language": "en", "value": "Inter"},
                                {"language": "en", "value": "Inter Milan"},
                            ],
                        },
                    },
                },
            },
        }

        result = resolve_team_identity(
            "国际米兰",
            fetch_json=lambda url: payloads[url],
            captured_at="2099-01-01T12:00:00+08:00",
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["qid"], "Q631")
        self.assertEqual(result["english_name"], "Inter Milan")
        self.assertEqual(result["english_names"], ["Inter Milan", "Inter"])
        self.assertEqual(result["source_url"], entity_url)

    def test_rejects_multiple_exact_team_candidates_without_guessing(self):
        search_url = "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=%E8%81%94%E9%98%9F&language=zh&format=json&limit=10"

        result = resolve_team_identity(
            "联队",
            fetch_json=lambda url: {"search": [
                {"id": "Q1", "label": "联队"},
                {"id": "Q2", "label": "联队"},
            ]},
            captured_at="2099-01-01T12:00:00+08:00",
        )

        self.assertEqual(result, {
            "status": "unresolved",
            "reason": "wikidata_ambiguous_exact_name",
            "candidate_qids": ["Q1", "Q2"],
            "search_url": search_url,
        })

    def test_accepts_only_a_generic_football_club_suffix_difference(self):
        search_url = "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=%E6%B5%8B%E8%AF%95%E9%98%9F&language=zh&format=json&limit=10"
        entity_url = "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q10&props=labels%7Caliases&languages=zh%7Cen&format=json"
        payloads = {
            search_url: {"search": [{"id": "Q10", "label": "测试队足球俱乐部"}]},
            entity_url: {"entities": {"Q10": {"labels": {
                "zh": {"language": "zh", "value": "测试队足球俱乐部"},
                "en": {"language": "en", "value": "Test FC"},
            }, "aliases": {}}}},
        }

        result = resolve_team_identity(
            "测试队", fetch_json=lambda url: payloads[url],
            captured_at="2099-01-01T12:00:00+08:00",
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["english_name"], "Test FC")

    def test_selects_one_explicit_football_club_among_same_name_candidates(self):
        search_url = "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=%E6%B5%8B%E8%AF%95%E9%98%9F&language=zh&format=json&limit=10"
        entity_url = "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q10&props=labels%7Caliases&languages=zh%7Cen&format=json"
        payloads = {
            search_url: {"search": [
                {"id": "Q10", "label": "测试队", "description": "职业足球俱乐部"},
                {"id": "Q11", "label": "测试队", "description": "同名城市"},
            ]},
            entity_url: {"entities": {"Q10": {"labels": {
                "zh": {"language": "zh", "value": "测试队"},
                "en": {"language": "en", "value": "Test FC"},
            }, "aliases": {}}}},
        }

        result = resolve_team_identity(
            "测试队", fetch_json=lambda url: payloads[url],
            captured_at="2099-01-01T12:00:00+08:00",
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["qid"], "Q10")
