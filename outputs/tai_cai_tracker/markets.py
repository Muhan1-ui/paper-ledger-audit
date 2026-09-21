"""Derive China Sports Lottery market probabilities from a frozen score matrix."""

from decimal import Decimal


OUTCOME_OPTIONS = ("home_win", "draw", "away_win")
TOTAL_GOAL_OPTIONS = ("0", "1", "2", "3", "4", "5", "6", "7+")
HALF_FULL_OPTIONS = ("HH", "HD", "HA", "DH", "DD", "DA", "AH", "AD", "AA")
CORRECT_SCORE_OPTIONS = (
    "1:0", "2:0", "2:1", "3:0", "3:1", "3:2", "4:0", "4:1", "4:2", "5:0", "5:1", "5:2", "H_OTHER",
    "0:0", "1:1", "2:2", "3:3", "D_OTHER",
    "0:1", "0:2", "1:2", "0:3", "1:3", "2:3", "0:4", "1:4", "2:4", "0:5", "1:5", "2:5", "A_OTHER",
)

_OUTCOME_SYMBOL = {"home_win": "H", "draw": "D", "away_win": "A"}


def select_top_option(probabilities, option_order):
    """Return the declared-order winner, probability, and all tied top options."""
    if not isinstance(probabilities, dict) or tuple(probabilities) != tuple(option_order):
        raise ValueError("probabilities must use the declared option order")
    if not probabilities or any(
        not isinstance(value, Decimal) or not value.is_finite() or value < 0
        for value in probabilities.values()
    ):
        raise ValueError("probabilities must be finite non-negative Decimals")
    maximum = max(probabilities.values())
    tied = tuple(option for option in option_order if probabilities[option] == maximum)
    return tied[0], maximum, tied


def _outcome(home_goals, away_goals):
    if home_goals > away_goals:
        return "home_win"
    if home_goals < away_goals:
        return "away_win"
    return "draw"


def _correct_score_option(home_goals, away_goals):
    option = f"{home_goals}:{away_goals}"
    if option in CORRECT_SCORE_OPTIONS:
        return option
    return {
        "home_win": "H_OTHER",
        "draw": "D_OTHER",
        "away_win": "A_OTHER",
    }[_outcome(home_goals, away_goals)]


def _unit_sum(probabilities, option_order):
    last_option = option_order[-1]
    probabilities[last_option] = Decimal(1) - sum(
        (probabilities[option] for option in option_order[:-1]), Decimal(0)
    )
    return probabilities


def derive_official_markets(score_matrix, home_handicap):
    """Return HHAD, CRS, TTG and HAFU distributions from an immutable matrix."""
    if home_handicap is not None and (
        not isinstance(home_handicap, Decimal)
        or not home_handicap.is_finite()
        or home_handicap != home_handicap.to_integral_value()
    ):
        raise ValueError("home_handicap must be a finite whole-number Decimal")
    if not isinstance(score_matrix, dict) or not isinstance(score_matrix.get("scorelines"), list):
        raise ValueError("score_matrix must contain scorelines")
    if not isinstance(score_matrix.get("half_time_full_time"), dict):
        raise ValueError("score_matrix must contain full half_time_full_time probabilities")

    hhad = (
        {option: Decimal(0) for option in OUTCOME_OPTIONS}
        if home_handicap is not None
        else None
    )
    crs = {option: Decimal(0) for option in CORRECT_SCORE_OPTIONS}
    ttg = {option: Decimal(0) for option in TOTAL_GOAL_OPTIONS}
    for line in score_matrix["scorelines"]:
        if not isinstance(line, dict):
            raise ValueError("scorelines must contain mappings")
        home_goals = line.get("home_goals")
        away_goals = line.get("away_goals")
        probability = line.get("probability")
        if (
            isinstance(home_goals, bool)
            or isinstance(away_goals, bool)
            or not isinstance(home_goals, int)
            or not isinstance(away_goals, int)
            or home_goals < 0
            or away_goals < 0
            or not isinstance(probability, Decimal)
            or not probability.is_finite()
            or probability < 0
        ):
            raise ValueError("scorelines are invalid")
        if hhad is not None:
            hhad[_outcome(Decimal(home_goals) + home_handicap, Decimal(away_goals))] += probability
        crs[_correct_score_option(home_goals, away_goals)] += probability
        ttg[str(home_goals + away_goals) if home_goals + away_goals <= 6 else "7+"] += probability

    hafu = {option: Decimal(0) for option in HALF_FULL_OPTIONS}
    for key, probability in score_matrix["half_time_full_time"].items():
        if not isinstance(key, str) or key.count(":") != 1:
            raise ValueError("half_time_full_time keys are invalid")
        half_time, full_time = key.split(":")
        if half_time not in _OUTCOME_SYMBOL or full_time not in _OUTCOME_SYMBOL:
            raise ValueError("half_time_full_time outcomes are invalid")
        if not isinstance(probability, Decimal) or not probability.is_finite() or probability < 0:
            raise ValueError("half_time_full_time probabilities are invalid")
        hafu[_OUTCOME_SYMBOL[half_time] + _OUTCOME_SYMBOL[full_time]] += probability

    result = {
        "crs": _unit_sum(crs, CORRECT_SCORE_OPTIONS),
        "ttg": _unit_sum(ttg, TOTAL_GOAL_OPTIONS),
        "hafu": _unit_sum(hafu, HALF_FULL_OPTIONS),
    }
    if hhad is not None:
        result = {"hhad": _unit_sum(hhad, OUTCOME_OPTIONS), **result}
    return result
