import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from outputs.tai_cai_tracker.sales_cutoff import (
    assess_freeze_timing,
    default_policy,
    load_policy,
)


class SalesCutoffTests(unittest.TestCase):
    def test_assess_freeze_timing_uses_weekday_sales_cutoff_window_for_late_fixture(self):
        policy = default_policy()
        now = datetime.fromisoformat("2099-01-05T21:00:00+08:00")
        kickoff = datetime.fromisoformat("2099-01-05T23:55:00+08:00")

        result = assess_freeze_timing(kickoff, now, 60, 75, policy)

        self.assertTrue(result["eligible"])
        self.assertEqual(result["timing_basis"], "sales_cutoff_window")
        self.assertEqual(result["sales_cutoff_at"], "2099-01-05T22:00:00+08:00")
        self.assertEqual(result["window_start_at"], "2099-01-05T21:00:00+08:00")
        self.assertEqual(result["window_end_at"], "2099-01-05T21:30:00+08:00")

    def test_assess_freeze_timing_keeps_normal_window_before_weekday_cutoff(self):
        result = assess_freeze_timing(
            datetime.fromisoformat("2099-01-09T21:00:00+08:00"),
            datetime.fromisoformat("2099-01-09T19:50:00+08:00"),
            60,
            75,
            default_policy(),
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["timing_basis"], "kickoff_window")
        self.assertIsNone(result["sales_cutoff_at"])

    def test_assess_freeze_timing_uses_weekend_cutoff(self):
        result = assess_freeze_timing(
            datetime.fromisoformat("2099-01-10T23:30:00+08:00"),
            datetime.fromisoformat("2099-01-10T22:20:00+08:00"),
            60,
            75,
            default_policy(),
        )

        self.assertEqual(result["timing_basis"], "sales_cutoff_window")
        self.assertEqual(result["sales_cutoff_at"], "2099-01-10T23:00:00+08:00")

    def test_load_policy_rejects_invalid_weekday_cutoff(self):
        policy = default_policy()
        policy["weekday_cutoff"] = "tomorrow"
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "weekday_cutoff"):
                load_policy(path)
