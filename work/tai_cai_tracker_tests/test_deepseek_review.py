import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from outputs.tai_cai_tracker.deepseek_review import (
    DeepSeekReviewClient,
    load_local_environment,
    save_local_configuration,
)
from outputs.tai_cai_tracker.ledger import freeze_prediction


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _Opener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return _Response(self.payload)


def _prediction():
    return freeze_prediction({
        "market": "SPF",
        "match_id": "20990101001",
        "kickoff_at": "2099-01-01T20:00:00+08:00",
        "captured_at": "2099-01-01T18:00:00+08:00",
        "source_urls": {
            "S1": "https://official.example.test/matches/1",
            "S2": "https://odds.example.test/matches/1",
        },
        "outcomes": [
            {"label": "home_win", "probability": Decimal("0.50")},
            {"label": "draw", "probability": Decimal("0.30")},
            {"label": "away_win", "probability": Decimal("0.20")},
        ],
    })


class DeepSeekReviewClientTests(unittest.TestCase):
    def test_from_environment_requires_api_key(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_config_path = Path(temporary_directory) / ".env"

            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                DeepSeekReviewClient.from_environment({}, config_path=missing_config_path)

    def test_review_sends_only_structured_read_only_context_to_chat_endpoint(self):
        opener = _Opener({
            "choices": [{"message": {"content": "## 已验证事实\n暂无官方结算。"}}],
        })
        client = DeepSeekReviewClient(
            "test-key",
            model="deepseek-v4",
            base_url="https://api.example.test/v1",
            opener=opener,
        )

        result = client.review([_prediction()], match_id="20990101001")

        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["model"], "deepseek-v4")
        self.assertEqual(result["ai_review"], "## 已验证事实\n暂无官方结算。")
        self.assertEqual(opener.request.full_url, "https://api.example.test/v1/chat/completions")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer test-key")
        request_body = json.loads(opener.request.data.decode("utf-8"))
        self.assertEqual(request_body["model"], "deepseek-v4")
        self.assertIn("只读", request_body["messages"][0]["content"])
        self.assertIn("official_settlement_missing", request_body["messages"][1]["content"])
        self.assertNotIn("test-key", request_body["messages"][1]["content"])

    def test_review_rejects_response_without_text_content(self):
        client = DeepSeekReviewClient("test-key", opener=_Opener({"choices": []}))

        with self.assertRaisesRegex(ValueError, "响应"):
            client.review([_prediction()], match_id="20990101001")

    def test_local_env_file_configures_client_without_shell_environment(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / ".env"
            config_path.write_text("OTHER_SETTING=keep\n", encoding="utf-8")

            save_local_configuration("local-key", "deepseek-v4", config_path=config_path)
            client = DeepSeekReviewClient.from_environment({}, config_path=config_path)

            self.assertEqual(client.api_key, "local-key")
            self.assertEqual(client.model, "deepseek-v4")
            self.assertEqual(load_local_environment({}, config_path)["OTHER_SETTING"], "keep")
            self.assertIn("OTHER_SETTING=keep", config_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
