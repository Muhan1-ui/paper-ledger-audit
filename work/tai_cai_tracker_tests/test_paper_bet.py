"""Regression tests for paper-only bet selection and P&L (``paper_bet.py``).

The bet selection criterion is expected value at the official fixed prize
(``p * odds - 1``), not the de-vigged probability gap. ``SelectionCriterionTests``
pins that definition down; see the negative-EV-with-positive-edge case below.

Everything here is paper accounting. No test places, funds, or recommends a
lottery bet.
"""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from outputs.tai_cai_tracker.paper_bet import (
    _devig_probabilities,
    _money_text,
    _select_bet,
    _strategy_name,
    settle_bets,
)


def make_record(prizes, probabilities, content_hash="h1"):
    """Build the selection-time projection of a frozen SPF prediction."""
    return {
        "content_hash": content_hash,
        "match_id": "20260724001",
        "kickoff_at": "2026-07-24T20:00:00+08:00",
        "match_context": {
            "competition": "Test League",
            "home_team": "Home FC",
            "away_team": "Away FC",
        },
        "outcomes": [
            {"label": label, "probability": probability}
            for label, probability in probabilities.items()
        ],
        "fixed_prizes": dict(prizes),
        "model": {},
    }


# Fixture A: EV(home) = 0.52*2.0-1 = +0.04, EV(draw) = -0.02, EV(away) = -0.20.
FIXTURE_A_PRIZES = {"home_win": "2.0", "draw": "3.5", "away_win": "4.0"}
FIXTURE_A_PROBS = {"home_win": 0.52, "draw": 0.28, "away_win": 0.20}


class SelectionCriterionTests(unittest.TestCase):
    def test_positive_ev_outcome_is_selected(self):
        bet, skip_reason = _select_bet(
            make_record(FIXTURE_A_PRIZES, FIXTURE_A_PROBS),
            Decimal("0"),
            10.0,
        )

        self.assertIsNone(skip_reason)
        self.assertIsNotNone(bet)
        self.assertEqual(bet["label"], "home_win")
        self.assertAlmostEqual(float(bet["ev"]), 0.04, places=4)
        self.assertEqual(bet["odds"], "2.0")

    def test_negative_ev_is_skipped_even_when_devig_edge_exceeds_five_points(self):
        """Pins EV, not the de-vigged probability edge, as the criterion.

        prizes 1.2 / 6.0 / 12.0, probabilities 0.82 / 0.12 / 0.06.
        overround = 1/1.2 + 1/6 + 1/12 = 1.08333, so the de-vigged home
        implied probability is 0.76923 and edge(home) = 0.82 - 0.76923 =
        +0.05077, which is above 0.05. The old edge-based criterion would
        therefore have taken this bet. EV(home) = 0.82*1.2 - 1 = -0.016 is
        negative, so the EV criterion must skip it.

        If this test fails, the selection criterion has been reverted to the
        de-vigged probability edge. The test arithmetic is not wrong.
        """
        record = make_record(
            {"home_win": "1.2", "draw": "6.0", "away_win": "12.0"},
            {"home_win": 0.82, "draw": 0.12, "away_win": 0.06},
        )

        bet, skip_reason = _select_bet(record, Decimal("0"), 10.0)

        self.assertIsNone(bet)
        self.assertEqual(skip_reason, "ev_not_above_threshold")

    def test_ev_equal_to_threshold_is_not_selected(self):
        # EV(home) is exactly 0.04 and the comparison is ``ev <= threshold``.
        bet, skip_reason = _select_bet(
            make_record(FIXTURE_A_PRIZES, FIXTURE_A_PROBS),
            Decimal("0.04"),
            10.0,
        )

        self.assertIsNone(bet)
        self.assertEqual(skip_reason, "ev_not_above_threshold")

    def test_ev_just_above_threshold_is_selected(self):
        bet, skip_reason = _select_bet(
            make_record(FIXTURE_A_PRIZES, FIXTURE_A_PROBS),
            Decimal("0.039"),
            10.0,
        )

        self.assertIsNone(skip_reason)
        self.assertIsNotNone(bet)
        self.assertEqual(bet["label"], "home_win")
        self.assertEqual(bet["strategy"], "ev_gt_0.039")


class StrategyNameTests(unittest.TestCase):
    def test_zero_threshold_uses_ev_prefix(self):
        # _threshold_label appends ".0" to a bare integer, hence ev_gt_0.0.
        self.assertEqual(_strategy_name(Decimal("0")), "ev_gt_0.0")
        self.assertTrue(_strategy_name(Decimal("0")).startswith("ev_gt_"))
        self.assertFalse(_strategy_name(Decimal("0")).startswith("edge_gt_"))

    def test_five_point_threshold_uses_ev_prefix(self):
        self.assertEqual(_strategy_name(Decimal("0.05")), "ev_gt_0.05")
        self.assertTrue(_strategy_name(Decimal("0.05")).startswith("ev_gt_"))
        self.assertFalse(_strategy_name(Decimal("0.05")).startswith("edge_gt_"))


class DevigTests(unittest.TestCase):
    def test_normalizes_three_way_prices_to_one(self):
        implied = _devig_probabilities(
            {"home_win": "2.0", "draw": "3.5", "away_win": "4.0"}
        )

        self.assertIsNotNone(implied)
        self.assertEqual(set(implied), {"home_win", "draw", "away_win"})
        total = sum(implied.values(), Decimal(0))
        self.assertAlmostEqual(total, Decimal(1), places=10)
        self.assertAlmostEqual(float(implied["home_win"]), 0.4828, places=4)

    def test_returns_none_when_a_price_is_missing(self):
        self.assertIsNone(
            _devig_probabilities({"home_win": "2.0", "draw": "3.5"})
        )

    def test_returns_none_when_a_price_is_not_positive(self):
        self.assertIsNone(
            _devig_probabilities(
                {"home_win": "2.0", "draw": "0", "away_win": "4.0"}
            )
        )

    def test_prices_with_no_overround_are_still_normalized(self):
        # Three 4.0 prices give an inverse total of 0.75. The implementation
        # does not reject a sub-unit overround; it only requires usable prices
        # and a positive inverse sum, so the result is a uniform 1/3 each.
        implied = _devig_probabilities(
            {"home_win": "4.0", "draw": "4.0", "away_win": "4.0"}
        )

        self.assertIsNotNone(implied)
        for label in ("home_win", "draw", "away_win"):
            self.assertAlmostEqual(float(implied[label]), 1.0 / 3.0, places=10)


class MalformedInputTests(unittest.TestCase):
    def test_missing_fixed_prizes_is_skipped(self):
        record = make_record(FIXTURE_A_PRIZES, FIXTURE_A_PROBS)
        del record["fixed_prizes"]

        bet, skip_reason = _select_bet(record, Decimal("0"), 10.0)

        self.assertIsNone(bet)
        self.assertEqual(skip_reason, "missing_fixed_prizes")

    def test_non_positive_prize_is_skipped(self):
        record = make_record(
            {"home_win": "2.0", "draw": "0", "away_win": "4.0"},
            FIXTURE_A_PROBS,
        )

        bet, skip_reason = _select_bet(record, Decimal("0"), 10.0)

        self.assertIsNone(bet)
        self.assertEqual(skip_reason, "non_positive_prize")

    def test_missing_content_hash_is_skipped(self):
        record = make_record(FIXTURE_A_PRIZES, FIXTURE_A_PROBS, content_hash="")

        bet, skip_reason = _select_bet(record, Decimal("0"), 10.0)

        self.assertIsNone(bet)
        self.assertEqual(skip_reason, "missing_content_hash")


class SettleBetsTests(unittest.TestCase):
    def _run_settlement(self, label):
        """Settle one bet in a throwaway ledger; return (updated, bet)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "ledger.jsonl"
            bets_path = tmp_path / "bets.jsonl"

            ledger = [
                {"record_type": "prediction", "content_hash": "h1"},
                {
                    "record_type": "settlement",
                    "content_hash": "s1",
                    "prediction_hash": "h1",
                    "actual_outcome": "home_win",
                },
            ]
            ledger_path.write_text(
                "\n".join(json.dumps(row) for row in ledger) + "\n",
                encoding="utf-8",
            )
            bets_path.write_text(
                json.dumps(
                    {
                        "content_hash": "h1",
                        "label": label,
                        "stake": "10",
                        "odds": "2.5",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            updated = settle_bets(ledger_path, bets_path)
            bet = json.loads(bets_path.read_text(encoding="utf-8").splitlines()[0])

        return updated, bet

    def test_winning_bet_is_settled_with_profit(self):
        updated, bet = self._run_settlement("home_win")

        self.assertEqual(updated, 1)
        self.assertTrue(bet["won"])
        self.assertEqual(bet["actual_outcome"], "home_win")
        # _money_text quantizes to two decimals: 10 * (2.5 - 1) = 15 -> "15.00".
        self.assertEqual(bet["pnl"], "15.00")
        self.assertEqual(bet["pnl"], _money_text(Decimal("15")))
        self.assertTrue(bet["settled_at"])

    def test_losing_bet_is_settled_with_stake_loss(self):
        updated, bet = self._run_settlement("draw")

        self.assertEqual(updated, 1)
        self.assertFalse(bet["won"])
        self.assertEqual(bet["actual_outcome"], "home_win")
        # _money_text quantizes to two decimals: -10 -> "-10.00".
        self.assertEqual(bet["pnl"], "-10.00")
        self.assertEqual(bet["pnl"], _money_text(Decimal("-10")))
        self.assertTrue(bet["settled_at"])


if __name__ == "__main__":
    unittest.main()
