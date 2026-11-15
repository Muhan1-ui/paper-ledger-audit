import unittest
from pathlib import Path


class SchedulerScriptTests(unittest.TestCase):
    def test_launchers_resolve_python_without_machine_specific_path(self):
        root = Path(__file__).resolve().parents[2]
        task = (root / "work" / "run-tai-cai-task.ps1").read_text(encoding="utf-8")
        dashboard = root / "work" / "start-dashboard.ps1"
        self.assertNotIn("D:\\python\\python.exe", task)
        self.assertIn("TAICAI_PYTHON", task)
        self.assertIn("Get-Command python", task)
        self.assertTrue(dashboard.exists())
    def test_prematch_window_matches_the_automation_contract(self):
        script = (Path(__file__).resolve().parents[1] / "run-tai-cai-task.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("$MinimumLeadMinutes = 30", script)
        self.assertIn("--max-lead-minutes 60", script)

    def test_scheduler_preserves_utf8_cli_output_in_audit_log(self):
        script = (Path(__file__).resolve().parents[1] / "run-tai-cai-task.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)", script)
        self.assertIn("$OutputEncoding = [Console]::OutputEncoding", script)
        self.assertIn('$env:PYTHONIOENCODING = "utf-8"', script)

    def test_cycle_mode_runs_and_summarizes_all_required_stages(self):
        script = (Path(__file__).resolve().parents[1] / "run-tai-cai-task.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('"cycle"', script)
        self.assertIn('("preflight", "prematch", "odds-refresh", "settle")', script)
        self.assertIn('"completed_with_failures"', script)

    def test_scheduler_writes_the_completed_result_to_standard_output(self):
        script = (Path(__file__).resolve().parents[1] / "run-tai-cai-task.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '$Result | ConvertTo-Json -Compress -Depth 8 | Write-Output',
            script,
        )
        self.assertIn(
            '$CycleEntry | ConvertTo-Json -Compress -Depth 8 | Write-Output',
            script,
        )


if __name__ == "__main__":
    unittest.main()
