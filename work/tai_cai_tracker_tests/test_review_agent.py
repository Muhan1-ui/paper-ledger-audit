import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from outputs.tai_cai_tracker.ledger import (
    freeze_evidence_event,
    freeze_prediction,
    invalidate_settlement,
    settle_prediction,
)
from outputs.tai_cai_tracker.review_agent import build_review


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


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


class ReviewAgentTests(unittest.TestCase):
    def test_settled_match_keeps_verified_result_and_audit_references(self):
        prediction = _prediction()
        settlement = settle_prediction(
            prediction,
            "home_win",
            "https://official.example.test/results/20990101001",
            final_score={"home": 2, "away": 0},
        )
        evidence = freeze_evidence_event({
            "record_type": "source_observation",
            "match_id": prediction["match_id"],
            "evidence_kind": "xg",
            "source_url": "https://stats.example.test/20990101001",
            "captured_at": "2099-01-01T17:00:00+08:00",
            "published_at_unavailable_reason": "页面未显示发布时间",
            "collector": "test-collector",
            "parser_version": "1",
            "raw_content_sha256": "a" * 64,
            "observed_facts": {"scope": "league_total"},
        })

        review = build_review([prediction, settlement, evidence], match_id=prediction["match_id"])

        self.assertEqual(review["scope"]["matched_predictions"], 1)
        self.assertEqual(review["settlements"][0]["actual_outcome"], "home_win")
        self.assertEqual(review["settlements"][0]["official_result_url"], settlement["official_result_url"])
        self.assertEqual(review["settlements"][0]["final_score"], {"home": 2, "away": 0})
        self.assertEqual(review["model_output"][0]["prediction_hash"], prediction["content_hash"])
        self.assertIn(prediction["content_hash"], review["references"]["ledger_hashes"])
        self.assertIn(evidence["source_url"], review["references"]["source_urls"])

    def test_unsettled_match_reports_missing_official_settlement_without_inference(self):
        prediction = _prediction()

        review = build_review([prediction], match_id=prediction["match_id"])

        self.assertEqual(review["settlements"], [])
        self.assertTrue(any(
            gap["code"] == "official_settlement_missing"
            for gap in review["data_gaps"]
        ))
        self.assertNotIn("actual_outcome", review["model_output"][0])

    def test_invalidated_settlement_is_not_reported_as_verified_fact(self):
        prediction = _prediction()
        settlement = settle_prediction(
            prediction, "away_win", "https://official.example.test/results/wrong"
        )
        invalidation = invalidate_settlement(settlement, {
            "settlement_hash": settlement["content_hash"],
            "prediction_hash": prediction["content_hash"],
            "invalidated_at": "2099-01-02T12:00:00+08:00",
            "reason_code": "identity_mismatch",
            "reason": "官方赛事身份不一致",
            "official_result_url": "https://official.example.test/results/correct",
            "official_result_identity": {
                "match_id": prediction["match_id"],
                "match_num": "周六001",
                "match_date": "2099-01-01",
                "home_team": "主队",
                "away_team": "客队",
            },
        })

        review = build_review([prediction, settlement, invalidation], match_id=prediction["match_id"])

        self.assertEqual(review["settlements"], [])
        self.assertTrue(any(
            gap["code"] == "settlement_invalidated"
            for gap in review["data_gaps"]
        ))

    def test_review_is_read_only_and_cli_json_is_parseable(self):
        prediction = _prediction()
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "ledger.jsonl"
            ledger_path.write_text(
                json.dumps(prediction, default=str, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
            original_bytes = ledger_path.read_bytes()

            review = build_review([prediction], match_id=prediction["match_id"])
            result = subprocess.run(
                [
                    sys.executable, "-m", "outputs.tai_cai_tracker.cli", "review",
                    str(ledger_path), "--match-id", prediction["match_id"], "--json",
                ],
                cwd=WORKSPACE_ROOT,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )

            self.assertEqual(ledger_path.read_bytes(), original_bytes)
            self.assertEqual(review["agent"]["mode"], "read_only")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["scope"]["match_id"], prediction["match_id"])


if __name__ == "__main__":
    unittest.main()
