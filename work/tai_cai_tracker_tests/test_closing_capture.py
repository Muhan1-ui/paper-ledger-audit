"""Closing-line capture selection for paper bets (``paper_bet.py``).

The closing price is the latest capture strictly after the prediction. It must
also be strictly before kickoff, otherwise a row stamped at kickoff would be
read as a closing line. 皇冠 rows win over other bookmakers when both exist.

All timestamps are timezone-aware ISO strings with an explicit +08:00 offset.
"""

import unittest
from datetime import datetime

from outputs.tai_cai_tracker.paper_bet import _closing_capture, _clv_fields


ODDS = {"home_win": "2.0", "draw": "3.5", "away_win": "4.0"}


def capture(captured_at, bookmaker="澳门", odds=None):
    """Build one odds_capture row."""
    return {
        "captured_at": captured_at,
        "bookmaker": bookmaker,
        "odds": dict(ODDS if odds is None else odds),
    }


def at(hour, minute=0):
    """A +08:00 timestamp on the match day."""
    return f"2026-07-24T{hour:02d}:{minute:02d}:00+08:00"


def _dt(text):
    return datetime.fromisoformat(text)


class ClosingCaptureTests(unittest.TestCase):
    def test_latest_capture_after_prediction(self):
        """A: one before the prediction, two after -> latest of the two."""
        after = _dt("2026-07-24T10:00:00+08:00")
        captures = [
            capture(at(9)),
            capture(at(11)),
            capture(at(12)),
        ]

        chosen = _closing_capture(captures, after)

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["captured_at"], at(12))

    def test_crown_preferred_even_when_older(self):
        """B: crown at 11:00 beats a later non-crown at 12:00."""
        after = _dt("2026-07-24T10:00:00+08:00")
        captures = [
            capture(at(11), bookmaker="皇冠"),
            capture(at(12), bookmaker="澳门"),
        ]

        chosen = _closing_capture(captures, after)

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["bookmaker"], "皇冠")
        self.assertEqual(chosen["captured_at"], at(11))

    def test_no_capture_after_prediction(self):
        """C: everything is before the prediction -> None."""
        after = _dt("2026-07-24T10:00:00+08:00")
        captures = [capture(at(8)), capture(at(9))]

        self.assertIsNone(_closing_capture(captures, after))

    def test_unusable_timestamps_are_ignored(self):
        """D: missing and malformed timestamps neither win nor block others."""
        after = _dt("2026-07-24T10:00:00+08:00")
        captures = [
            capture(None),
            capture(""),
            capture("not-a-timestamp"),
            capture("2026-07-24T12:00:00"),  # naive, ambiguous
            capture(at(11)),
        ]

        chosen = _closing_capture(captures, after)

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["captured_at"], at(11))

    def test_capture_at_or_after_before_is_excluded(self):
        """E: a row at kickoff is not a closing price; an earlier one wins."""
        after = _dt("2026-07-24T10:00:00+08:00")
        before = _dt("2026-07-24T13:00:00+08:00")
        captures = [capture(at(13))]

        self.assertIsNone(_closing_capture(captures, after, before))

        captures.append(capture(at(12)))
        chosen = _closing_capture(captures, after, before)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["captured_at"], at(12))

    def test_before_none_keeps_previous_behaviour(self):
        """F: without ``before`` the kickoff-time row is still selected."""
        after = _dt("2026-07-24T10:00:00+08:00")
        captures = [capture(at(13))]

        chosen = _closing_capture(captures, after, None)

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["captured_at"], at(13))


    def test_clv_fields_uses_kickoff_as_upper_bound(self):
        """The bet's kickoff_at reaches _closing_capture as ``before``."""
        bet = {
            "content_hash": "h1",
            "match_id": "M1",
            "kickoff_at": at(13),
            "label": "home_win",
            "odds": "2.10",
        }
        predicted_at = {"h1": at(10)}
        key = ("M1", at(13))

        fields = _clv_fields(bet, predicted_at, {key: [capture(at(13))]})
        self.assertIsNone(fields["closing_captured_at"])

        fields = _clv_fields(
            bet, predicted_at, {key: [capture(at(13)), capture(at(12))]}
        )
        self.assertEqual(fields["closing_captured_at"], at(12))


if __name__ == "__main__":
    unittest.main()
