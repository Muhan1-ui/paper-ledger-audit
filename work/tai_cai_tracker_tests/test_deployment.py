import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DockerDeploymentTests(unittest.TestCase):
    def test_docker_dependency_install_uses_network_timeout_and_retries(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("PIP_DEFAULT_TIMEOUT=120", dockerfile)
        self.assertIn("pip install --no-cache-dir --retries 5 -r requirements.txt", dockerfile)

    def test_service_verification_script_checks_health_and_read_only_review(self):
        script = (PROJECT_ROOT / "work" / "verify-service.ps1").read_text(encoding="utf-8")

        self.assertIn("param([int]$Port = 8000)", script)
        self.assertIn('"/api/health"', script)
        self.assertIn('"/api/reviews"', script)
        self.assertIn('"read_only"', script)
        self.assertIn("ConvertTo-Json", script)


if __name__ == "__main__":
    unittest.main()
