import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException

from outputs.tai_cai_tracker.ledger import (
    freeze_evidence_event,
    freeze_prediction,
    settle_prediction,
)
from outputs.tai_cai_tracker.service_api import create_app
from outputs.tai_cai_tracker.service_config import database_url_from_environment, save_database_configuration
from outputs.tai_cai_tracker.service_store import create_store


def _prediction(match_id="20990101001"):
    return freeze_prediction({
        "market": "SPF",
        "match_id": match_id,
        "kickoff_at": "2099-01-01T20:00:00+08:00",
        "captured_at": "2099-01-01T18:00:00+08:00",
        "source_urls": {
            "S1": f"https://official.example.test/matches/{match_id}",
            "S2": f"https://odds.example.test/matches/{match_id}",
        },
        "match_context": {
            "competition": "测试联赛",
            "home_team": "主队",
            "away_team": "客队",
        },
        "outcomes": [
            {"label": "home_win", "probability": Decimal("0.50")},
            {"label": "draw", "probability": Decimal("0.30")},
            {"label": "away_win", "probability": Decimal("0.20")},
        ],
    })


def _route(app, path):
    return next(route.endpoint for route in app.routes if route.path == path)


class ServiceStoreTests(unittest.TestCase):
    def test_sync_projects_frozen_prediction_without_mutating_the_ledger_record(self):
        prediction = _prediction()
        store = create_store("sqlite://")

        result = store.sync_records([prediction])
        total, matches = store.list_matches()

        self.assertEqual(result, {
            "projected_predictions": 1,
            "projected_settlements": 0,
            "projected_invalidations": 0,
            "projected_evidence_events": 0,
        })
        self.assertEqual(total, 1)
        self.assertEqual(matches[0]["prediction_hash"], prediction["content_hash"])
        self.assertEqual(matches[0]["official_match_url"], prediction["source_urls"]["S1"])
        self.assertEqual(prediction["record_type"], "prediction")
        self.assertNotIn("database_id", prediction)

    def test_sync_projects_evidence_events_and_exposes_them_in_match_detail(self):
        prediction = _prediction("20990101006")
        observation = freeze_evidence_event({
            "record_type": "source_observation",
            "match_id": prediction["match_id"],
            "evidence_kind": "xg",
            "source_url": "https://stats.example.test/20990101006",
            "captured_at": "2099-01-01T17:00:00+08:00",
            "published_at_unavailable_reason": "页面未显示发布时间",
            "collector": "test-collector",
            "parser_version": "1",
            "raw_content_sha256": "a" * 64,
            "observed_facts": {"scope": "league_total"},
        })
        failure = freeze_evidence_event({
            "record_type": "collection_failure",
            "match_id": prediction["match_id"],
            "evidence_kind": "european_1x2",
            "attempted_at": "2099-01-01T17:05:00+08:00",
            "collector": "test-collector",
            "parser_version": "1",
            "reason_code": "incomplete_data",
            "reason": "公开页面缺少具名机构信息",
        })
        store = create_store("sqlite://")

        result = store.sync_records([prediction, observation, failure])
        detail = store.match_detail(prediction["match_id"])

        self.assertEqual(result["projected_evidence_events"], 2)
        self.assertEqual(len(detail["evidence_events"]), 2)
        self.assertEqual(detail["evidence_events"][0]["record_hash"], observation["content_hash"])
        self.assertEqual(detail["evidence_events"][0]["observed_facts"], {"scope": "league_total"})
        self.assertEqual(detail["evidence_events"][1]["reason_code"], "incomplete_data")

    def test_local_configuration_builds_a_mysql_url_without_exposing_password(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / ".env"

            save_database_configuration(
                password="local password@1",
                host="127.0.0.1",
                port=3306,
                username="taicai_app",
                database="taicai_tracker",
                config_path=config_path,
            )
            database_url = database_url_from_environment({}, config_path=config_path)

            self.assertEqual(
                database_url,
                "mysql+pymysql://taicai_app:local%20password%401@127.0.0.1:3306/taicai_tracker?charset=utf8mb4",
            )
            self.assertNotIn("local password@1", database_url)
            self.assertIn("MYSQL_PASSWORD=local password@1", config_path.read_text(encoding="utf-8"))


class ServiceApiTests(unittest.TestCase):
    def test_health_and_match_list_routes_expose_a_read_only_projection(self):
        store = create_store("sqlite://")
        store.sync_records([_prediction("20990101001"), _prediction("20990101002")])
        app = create_app(store=store)

        health = _route(app, "/api/health")()
        matches = _route(app, "/api/matches")(limit=1)

        self.assertEqual(health, {"status": "ok", "database": "connected"})
        self.assertEqual(matches["total"], 2)
        self.assertEqual(len(matches["items"]), 1)
        self.assertEqual(matches["items"][0]["home_team"], "主队")
        self.assertEqual(matches["items"][0]["prediction_role"], "primary")
        self.assertEqual({route.path for route in app.routes} & {"/api/health", "/api/matches"}, {"/api/health", "/api/matches"})

    def test_match_detail_joins_frozen_prediction_with_official_settlement(self):
        prediction = _prediction("20990101003")
        settlement = settle_prediction(
            prediction,
            "home_win",
            "https://official.example.test/results/20990101003",
            final_score={"home": 2, "away": 0},
        )
        store = create_store("sqlite://")
        store.sync_records([prediction, settlement])
        app = create_app(store=store)

        detail = _route(app, "/api/matches/{match_id}")("20990101003")

        self.assertEqual(detail["match_id"], "20990101003")
        self.assertEqual(detail["total_predictions"], 1)
        self.assertEqual(detail["predictions"][0]["settlement"]["actual_outcome"], "home_win")
        self.assertEqual(detail["predictions"][0]["settlement"]["final_score"], {"home": 2, "away": 0})

    def test_review_routes_expose_derived_read_only_agent_views(self):
        prediction = _prediction("20990101004")
        store = create_store("sqlite://")
        store.sync_records([prediction])
        app = create_app(store=store, record_loader=lambda: [prediction])

        global_review = _route(app, "/api/reviews")()
        match_review = _route(app, "/api/matches/{match_id}/review")("20990101004")

        self.assertEqual(global_review["agent"]["mode"], "read_only")
        self.assertEqual(global_review["scope"]["prediction_count"], 1)
        self.assertEqual(match_review["scope"]["match_id"], "20990101004")
        self.assertEqual(match_review["model_output"][0]["prediction_hash"], prediction["content_hash"])
        self.assertTrue(any(
            gap["code"] == "official_settlement_missing"
            for gap in match_review["data_gaps"]
        ))

    def test_match_review_route_returns_not_found_for_an_unknown_match(self):
        store = create_store("sqlite://")
        app = create_app(store=store, record_loader=lambda: [])

        with self.assertRaises(HTTPException) as error:
            _route(app, "/api/matches/{match_id}/review")("20990101005")

        self.assertEqual(error.exception.status_code, 404)

    def test_dashboard_summary_route_exposes_the_injected_read_only_summary(self):
        summary = {
            "generated_at": "2099-01-01T20:00:00+08:00",
            "calibration": {"available": False, "settled_records": 0},
            "strategies": [],
            "operations": {"status": "unavailable"},
            "evidence": {"event_counts": {}},
            "recent_records": [],
            "warnings": [],
        }
        store = create_store("sqlite://")
        app = create_app(
            store=store,
            record_loader=lambda: [],
            summary_loader=lambda: summary,
        )

        result = _route(app, "/api/summary")()

        self.assertEqual(result, summary)
        self.assertIn("/", {route.path for route in app.routes})

    def test_dashboard_page_requests_summary_and_read_only_review_apis(self):
        dashboard = (
            Path(__file__).resolve().parents[2]
            / "outputs" / "tai_cai_tracker" / "static" / "dashboard.html"
        ).read_text(encoding="utf-8")

        self.assertIn('fetch("/api/summary"', dashboard)
        self.assertIn('fetch("/api/reviews"', dashboard)


if __name__ == "__main__":
    unittest.main()
