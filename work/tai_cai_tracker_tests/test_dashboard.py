import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from outputs.tai_cai_tracker.dashboard import _calibration_summary, build_summary, create_server
from outputs.tai_cai_tracker.ledger import freeze_evidence_event, freeze_prediction, settle_prediction


PREDICTION = {
    "market": "SPF",
    "match_id": "20990101001",
    "kickoff_at": "2099-01-01T20:00:00+00:00",
    "captured_at": "2099-01-01T18:00:00+00:00",
    "source_urls": {
        "S1": "https://official.example.test/matches/20990101001",
        "S2": "https://market.example.test/matches/20990101001",
    },
    "outcomes": [
        {"label": "home_win", "probability": Decimal("0.50")},
        {"label": "draw", "probability": Decimal("0.30")},
        {"label": "away_win", "probability": Decimal("0.20")},
    ],
    "match_context": {
        "competition": "Test League",
        "home_team": "Home FC",
        "away_team": "Away FC",
    },
}


class DashboardSummaryTests(unittest.TestCase):
    def test_available_calibration_uses_the_standard_sample_gate_when_report_omits_it(self):
        summary = _calibration_summary({
            "available": True,
            "settled_records": 98,
            "brier_average": Decimal("0.62"),
            "log_loss_average": Decimal("1.04"),
            "expected_calibration_error": Decimal("0.03"),
        })

        self.assertEqual(summary["minimum_settled_records"], 50)
        self.assertTrue(summary["sample_gate"]["met"])

    def _write_records(self, ledger_path, records):
        ledger_path.write_text(
            "".join(json.dumps(record, default=str) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_build_summary_is_json_safe_read_only_and_projects_dashboard_data(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            ledger_path = temporary_path / "ledger.jsonl"
            scheduler_path = temporary_path / "scheduler.jsonl"
            baseline = freeze_prediction(PREDICTION)
            enhanced_input = deepcopy(PREDICTION)
            enhanced_input["match_id"] = "20990101002"
            enhanced_input["captured_at"] = "2099-01-01T18:30:00+00:00"
            enhanced_input["model"] = {
                "version": "poisson-v1",
                "weight": Decimal("0.40"),
                "scorelines": [],
            }
            enhanced = freeze_prediction(enhanced_input)
            observation = freeze_evidence_event({
                "record_type": "source_observation",
                "match_id": "20990101002",
                "evidence_kind": "european_1x2",
                "source_url": "https://odds.example.test/matches/20990101002",
                "captured_at": "2099-01-01T18:45:00+00:00",
                "published_at": "2099-01-01T18:44:00+00:00",
                "collector": "fixture",
                "parser_version": "1.0",
                "raw_content_sha256": "a" * 64,
                "observed_facts": {"bookmaker": "FixtureBook"},
            })
            records = [
                baseline,
                settle_prediction(
                    baseline,
                    "home_win",
                    "https://official.example.test/results/20990101001",
                ),
                enhanced,
                settle_prediction(
                    enhanced,
                    "home_win",
                    "https://official.example.test/results/20990101002",
                ),
                observation,
            ]
            self._write_records(ledger_path, records)
            original_ledger = ledger_path.read_bytes()
            scheduler_path.write_text(
                json.dumps({
                    "timestamp": "2099-01-01T19:00:00+00:00",
                    "mode": "prematch",
                    "status": "completed",
                    "recovery_scan": True,
                    "error": "scheduler detail retained only as a compact field",
                    "output": ["internal scheduler output"],
                    "internal_detail": "must not be exposed",
                }) + "\n",
                encoding="utf-8",
            )

            summary = build_summary(
                ledger_path,
                scheduler_path,
                now=datetime(2099, 1, 1, 20, tzinfo=timezone.utc),
            )

            self.assertEqual(ledger_path.read_bytes(), original_ledger)
            json.dumps(summary)
            self.assertEqual(summary["generated_at"], "2099-01-01T20:00:00+00:00")
            self.assertFalse(summary["calibration"]["available"])
            self.assertEqual(summary["calibration"]["sample_gate"], {
                "settled_records": 2,
                "minimum_settled_records": 50,
                "met": False,
            })
            self.assertEqual(
                [item["strategy"] for item in summary["strategies"]],
                ["market_baseline", "poisson-v1@0.40"],
            )
            self.assertEqual(summary["strategies"][0]["brier_average"], "0.38")
            self.assertEqual(summary["strategies"][0]["top_pick_accuracy"], "1")
            self.assertEqual(summary["operations"], {
                "timestamp": "2099-01-01T19:00:00+00:00",
                "status": "completed",
                "mode": "prematch",
                "recovery_scan": True,
                "error": "scheduler detail retained only as a compact field",
                "age_hours": "1",
                "stale": False,
            })
            self.assertNotIn("output", json.dumps(summary["operations"]))
            self.assertNotIn("internal_detail", json.dumps(summary["operations"]))
            self.assertEqual(summary["evidence"]["event_counts"], {
                "source_observation": 1,
                "collection_failure": 0,
                "by_evidence_kind": {"european_1x2": 1},
            })
            self.assertIn("odds.example.test", summary["evidence"]["source_aggregates"])
            self.assertLessEqual(len(summary["recent_records"]), 10)
            self.assertTrue(all(set(item) == {
                "record_type", "match_identity", "timestamp", "content_hash_prefix"
            } for item in summary["recent_records"]))
            self.assertTrue(all("url" not in json.dumps(item).lower() for item in summary["recent_records"]))
            self.assertNotIn("probability", json.dumps(summary["recent_records"]).lower())

    def test_build_summary_marks_missing_scheduler_log_unavailable_with_warning(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            self._write_records(ledger_path, [])

            summary = build_summary(ledger_path, Path(temporary_directory) / "missing.jsonl")

            self.assertEqual(summary["operations"], {"status": "unavailable"})
            self.assertIn("Scheduler log is unavailable.", summary["warnings"])

    def test_build_summary_marks_malformed_final_scheduler_line_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            ledger_path = temporary_path / "ledger.jsonl"
            scheduler_path = temporary_path / "scheduler.jsonl"
            self._write_records(ledger_path, [])
            scheduler_path.write_text(
                '{"timestamp":"2099-01-01T19:00:00+00:00","mode":"settle","status":"completed"}\n'
                '{not valid json}\n',
                encoding="utf-8",
            )

            summary = build_summary(
                ledger_path,
                scheduler_path,
                now=datetime(2099, 1, 1, 20, tzinfo=timezone.utc),
            )

            self.assertEqual(summary["operations"], {"status": "unavailable"})
            self.assertIn(
                "Scheduler log final nonempty line 2 is malformed JSONL.",
                summary["warnings"],
            )

    def test_build_summary_marks_non_object_final_scheduler_line_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            ledger_path = temporary_path / "ledger.jsonl"
            scheduler_path = temporary_path / "scheduler.jsonl"
            self._write_records(ledger_path, [])
            scheduler_path.write_text(
                '{"timestamp":"2099-01-01T19:00:00+00:00","status":"completed"}\n'
                '[]\n',
                encoding="utf-8",
            )

            summary = build_summary(ledger_path, scheduler_path)

            self.assertEqual(summary["operations"], {"status": "unavailable"})
            self.assertIn(
                "Scheduler log final nonempty line 2 is not an object.",
                summary["warnings"],
            )

    def test_build_summary_rejects_a_missing_ledger(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "missing-ledger.jsonl"

            with self.assertRaisesRegex(FileNotFoundError, "Ledger file is unavailable"):
                build_summary(ledger_path)

    def test_build_summary_defaults_generated_at_to_local_timezone(self):
        class LocalClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2099, 1, 1, 12, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            self._write_records(ledger_path, [])

            with patch("outputs.tai_cai_tracker.dashboard.datetime", LocalClock):
                summary = build_summary(ledger_path)

            expected = LocalClock(2099, 1, 1, 12, tzinfo=timezone.utc).astimezone().isoformat()
            self.assertEqual(summary["generated_at"], expected)

    def test_build_summary_marks_old_scheduler_event_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            ledger_path = temporary_path / "ledger.jsonl"
            scheduler_path = temporary_path / "scheduler.jsonl"
            self._write_records(ledger_path, [])
            scheduler_path.write_text(
                '{"timestamp":"2099-01-01T00:00:00+00:00","mode":"settle","status":"completed"}\n',
                encoding="utf-8",
            )

            summary = build_summary(
                ledger_path,
                scheduler_path,
                now=datetime(2099, 1, 2, 1, tzinfo=timezone.utc),
            )

            self.assertEqual(summary["operations"]["age_hours"], "25")
            self.assertTrue(summary["operations"]["stale"])
            self.assertIn("Scheduler event is stale: 25 hours old.", summary["warnings"])

    def test_build_summary_orders_mixed_offset_recent_records_by_instant(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            captured_times = [
                *(f"2099-01-01T{hour:02d}:00:00+00:00" for hour in range(9)),
                "2099-01-01T20:00:00+08:00",
                "2099-01-01T13:00:00+00:00",
                "2099-01-01T14:00:00+00:00",
            ]
            records = []
            for index, captured_at in enumerate(captured_times, start=1):
                prediction = deepcopy(PREDICTION)
                prediction["match_id"] = f"20990102{index:03d}"
                prediction["captured_at"] = captured_at
                prediction["kickoff_at"] = "2099-01-02T20:00:00+00:00"
                records.append(freeze_prediction(prediction))
            self._write_records(ledger_path, records)

            summary = build_summary(ledger_path)

            recent_ids = [item["match_identity"]["match_id"] for item in summary["recent_records"]]
            self.assertEqual(recent_ids[0], "20990102012")
            self.assertLess(
                recent_ids.index("20990102011"),
                recent_ids.index("20990102010"),
            )
            self.assertEqual(len(recent_ids), 10)
            self.assertIn("20990102011", recent_ids)
            self.assertNotIn("20990102001", recent_ids)
            self.assertNotIn("20990102002", recent_ids)


class DashboardServerTests(unittest.TestCase):
    def _request(self, base_url, path):
        status, content_type, _, body = self._raw_request(base_url, path)
        return status, content_type, body.decode("utf-8")

    def _raw_request(self, base_url, path, method="GET"):
        request = Request(f"{base_url}{path}", method=method)
        with urlopen(request, timeout=3) as response:
            return (
                response.status,
                response.headers.get_content_type(),
                response.headers["Content-Length"],
                response.read(),
            )

    def _running_server(self, ledger_path, scheduler_log_path=None):
        server = create_server(ledger_path, scheduler_log_path, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_summary_endpoint_returns_the_dashboard_summary_as_json(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")

            status, content_type, body = self._request(
                self._running_server(ledger_path), "/api/summary"
            )

            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json")
            self.assertIn("generated_at", json.loads(body))

    def test_health_endpoint_returns_ok_when_summary_builds_successfully(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")

            status, content_type, body = self._request(
                self._running_server(ledger_path), "/api/health"
            )

            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json")
            self.assertEqual(json.loads(body), {"status": "ok"})

    def test_api_endpoints_return_a_json_error_without_traceback_when_ledger_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_ledger_path = Path(temporary_directory) / "missing-ledger.jsonl"

            self._assert_api_error_for_ledger(missing_ledger_path)

    def test_api_endpoints_return_a_json_error_without_traceback_when_ledger_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "invalid-ledger.jsonl"
            ledger_path.write_text("not json\n", encoding="utf-8")

            self._assert_api_error_for_ledger(ledger_path)

    def _assert_api_error_for_ledger(self, ledger_path):
        base_url = self._running_server(ledger_path)

        for path in ("/api/summary", "/api/health"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as raised:
                    self._request(base_url, path)

                response = raised.exception
                self.assertEqual(response.code, 500)
                self.assertEqual(response.headers.get_content_type(), "application/json")
                body = response.read().decode("utf-8")
                payload = json.loads(body)
                self.assertEqual(payload, {"error": "Dashboard data is unavailable."})
                self.assertNotIn(str(ledger_path), body)
                self.assertNotIn("traceback", body.lower())

    def test_root_serves_the_dashboard_html_with_expected_title(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")

            status, content_type, body = self._request(self._running_server(ledger_path), "/")

            self.assertEqual(status, 200)
            self.assertEqual(content_type, "text/html")
            self.assertIn("<title>TaiCaiTracker Dashboard</title>", body)

    def test_head_endpoints_match_get_metadata_without_a_response_body(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            base_url = self._running_server(ledger_path)

            for path in ("/", "/api/summary", "/api/health"):
                with self.subTest(path=path):
                    get_status, get_type, get_length, get_body = self._raw_request(
                        base_url, path
                    )
                    head_status, head_type, head_length, head_body = self._raw_request(
                        base_url, path, method="HEAD"
                    )

                    self.assertEqual(head_status, get_status)
                    self.assertEqual(head_type, get_type)
                    self.assertEqual(head_length, get_length)
                    self.assertEqual(head_length, str(len(get_body)))
                    self.assertEqual(head_body, b"")

    def test_create_server_rejects_non_loopback_hosts(self):
        for host in ("0.0.0.0", "localhost"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "loopback"):
                    create_server("ledger.jsonl", host=host)


class DashboardPageTests(unittest.TestCase):
    def test_dashboard_page_uses_only_local_api_and_exposes_required_sections(self):
        page = (
            Path(__file__).resolve().parents[2]
            / "outputs"
            / "tai_cai_tracker"
            / "static"
            / "dashboard.html"
        ).read_text(encoding="utf-8")

        self.assertIn("/api/summary", page)
        self.assertNotRegex(page, r"https?://")
        for label in (
            "Calibration",
            "Strategy",
            "Operations",
            "Evidence",
            "Agent workflow",
        ):
            self.assertIn(label, page)


if __name__ == "__main__":
    unittest.main()
