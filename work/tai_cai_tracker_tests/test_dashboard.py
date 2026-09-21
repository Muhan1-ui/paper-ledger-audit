import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from outputs.tai_cai_tracker.dashboard import build_summary
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
            self.assertEqual(summary["operations"]["status"], "completed")
            self.assertEqual(summary["operations"]["age_hours"], "1")
            self.assertFalse(summary["operations"]["stale"])
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

    def test_build_summary_warns_on_malformed_final_log_line_without_crashing(self):
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

            self.assertEqual(summary["operations"]["status"], "completed")
            self.assertIn("Scheduler log contains malformed JSONL on line 2.", summary["warnings"])

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


if __name__ == "__main__":
    unittest.main()
