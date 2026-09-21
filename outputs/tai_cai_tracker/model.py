from decimal import Decimal, localcontext


OUTCOME_LABELS = ("home_win", "draw", "away_win")


def _non_negative_decimal(value, field_name):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field_name} must be a finite non-negative Decimal")
    return value


def _probability_distribution(value, field_name):
    if not isinstance(value, dict) or set(value) != set(OUTCOME_LABELS):
        raise ValueError(f"{field_name} must contain home_win, draw, and away_win")
    if any(
        not isinstance(probability, Decimal)
        or not probability.is_finite()
        or probability < 0
        or probability > 1
        for probability in value.values()
    ):
        raise ValueError(f"{field_name} values must be finite Decimal probabilities")
    if sum(value.values(), Decimal(0)) != Decimal(1):
        raise ValueError(f"{field_name} probabilities must sum to one")
    return value


def _poisson_marginal(expected_goals, max_goals):
    probabilities = [(-expected_goals).exp()]
    for goals in range(1, max_goals + 1):
        probabilities.append(probabilities[-1] * expected_goals / Decimal(goals))
    return probabilities


def _outcome_label(home_goals, away_goals):
    if home_goals > away_goals:
        return "home_win"
    if home_goals < away_goals:
        return "away_win"
    return "draw"


def poisson_score_matrix(home_xg, away_xg, max_goals=10, home_handicap=None):
    """Return a normalised bounded Poisson score distribution and derived markets."""
    home_xg = _non_negative_decimal(home_xg, "home_xg")
    away_xg = _non_negative_decimal(away_xg, "away_xg")
    if isinstance(max_goals, bool) or not isinstance(max_goals, int) or max_goals < 1:
        raise ValueError("max_goals must be an integer of at least one")
    if home_handicap is not None:
        if (
            not isinstance(home_handicap, Decimal)
            or not home_handicap.is_finite()
            or home_handicap != home_handicap.to_integral_value()
        ):
            raise ValueError("home_handicap must be a finite whole-number Decimal")

    with localcontext() as context:
        context.prec = max(context.prec, 50)
        home_marginal = _poisson_marginal(home_xg, max_goals)
        away_marginal = _poisson_marginal(away_xg, max_goals)
        raw_scorelines = [
            {
                "home_goals": home_goals,
                "away_goals": away_goals,
                "probability": home_probability * away_probability,
            }
            for home_goals, home_probability in enumerate(home_marginal)
            for away_goals, away_probability in enumerate(away_marginal)
        ]
        captured_mass = sum(
            (scoreline["probability"] for scoreline in raw_scorelines), Decimal(0)
        )
        scorelines = [
            {
                **scoreline,
                "probability": scoreline["probability"] / captured_mass,
            }
            for scoreline in raw_scorelines
        ]
        outcomes = {label: Decimal(0) for label in OUTCOME_LABELS}
        total_goals = {"0_1": Decimal(0), "2_3": Decimal(0), "4_plus": Decimal(0)}
        for scoreline in scorelines:
            home_goals = scoreline["home_goals"]
            away_goals = scoreline["away_goals"]
            probability = scoreline["probability"]
            outcomes[_outcome_label(home_goals, away_goals)] += probability
            goal_total = home_goals + away_goals
            if goal_total <= 1:
                total_goals["0_1"] += probability
            elif goal_total <= 3:
                total_goals["2_3"] += probability
            else:
                total_goals["4_plus"] += probability

        outcomes["away_win"] = Decimal(1) - outcomes["home_win"] - outcomes["draw"]
        total_goals["4_plus"] = Decimal(1) - total_goals["0_1"] - total_goals["2_3"]
        top_scores = sorted(
            scorelines,
            key=lambda scoreline: (
                -scoreline["probability"],
                scoreline["home_goals"],
                scoreline["away_goals"],
            ),
        )[:5]
        residual_mass = Decimal(1) - captured_mass

        handicap_outcomes = None
        if home_handicap is not None:
            handicap_outcomes = {label: Decimal(0) for label in OUTCOME_LABELS}
            for scoreline in scorelines:
                adjusted_home_goals = Decimal(scoreline["home_goals"]) + home_handicap
                handicap_outcomes[
                    _outcome_label(adjusted_home_goals, scoreline["away_goals"])
                ] += scoreline["probability"]
            handicap_outcomes["away_win"] = (
                Decimal(1)
                - handicap_outcomes["home_win"]
                - handicap_outcomes["draw"]
            )

        half_home_marginal = _poisson_marginal(home_xg / Decimal(2), max_goals)
        half_away_marginal = _poisson_marginal(away_xg / Decimal(2), max_goals)
        half_raw_scorelines = [
            {
                "home_goals": home_goals,
                "away_goals": away_goals,
                "probability": home_probability * away_probability,
            }
            for home_goals, home_probability in enumerate(half_home_marginal)
            for away_goals, away_probability in enumerate(half_away_marginal)
        ]
        half_captured_mass = sum(
            (scoreline["probability"] for scoreline in half_raw_scorelines), Decimal(0)
        )
        half_scorelines = [
            {**scoreline, "probability": scoreline["probability"] / half_captured_mass}
            for scoreline in half_raw_scorelines
        ]
        half_time_full_time = {}
        for first_half in half_scorelines:
            half_time = _outcome_label(
                first_half["home_goals"], first_half["away_goals"]
            )
            for second_half in half_scorelines:
                full_time = _outcome_label(
                    first_half["home_goals"] + second_half["home_goals"],
                    first_half["away_goals"] + second_half["away_goals"],
                )
                key = (half_time, full_time)
                half_time_full_time[key] = half_time_full_time.get(key, Decimal(0)) + (
                    first_half["probability"] * second_half["probability"]
                )
        half_time_full_time_proxy = [
            {
                "half_time": half_time,
                "full_time": full_time,
                "probability": +probability,
            }
            for (half_time, full_time), probability in sorted(
                half_time_full_time.items(),
                key=lambda item: (-item[1], item[0]),
            )[:2]
        ]

    rounded_outcomes = {
        "home_win": +outcomes["home_win"],
        "draw": +outcomes["draw"],
    }
    # Unary plus applies the caller's decimal precision after the high-precision
    # matrix work, so derive the residual last to retain an exact unit sum.
    rounded_outcomes["away_win"] = (
        Decimal(1) - rounded_outcomes["home_win"] - rounded_outcomes["draw"]
    )

    return {
        "one_x_two": rounded_outcomes,
        "scorelines": [
            {**scoreline, "probability": +scoreline["probability"]}
            for scoreline in scorelines
        ],
        "top_scores": [
            {**scoreline, "probability": +scoreline["probability"]}
            for scoreline in top_scores
        ],
        "total_goals": {bucket: +probability for bucket, probability in total_goals.items()},
        "residual_mass": +residual_mass,
        "half_time_full_time": {
            f"{half_time}:{full_time}": +probability
            for (half_time, full_time), probability in half_time_full_time.items()
        },
        "half_time_full_time_proxy": half_time_full_time_proxy,
        **(
            {"handicap_one_x_two": {label: +handicap_outcomes[label] for label in OUTCOME_LABELS}}
            if handicap_outcomes is not None
            else {}
        ),
    }


def blend_probabilities(market, model, model_weight):
    """Blend two validated 1X2 distributions using a fixed model weight."""
    market = _probability_distribution(market, "market")
    model = _probability_distribution(model, "model")
    if (
        not isinstance(model_weight, Decimal)
        or not model_weight.is_finite()
        or model_weight < 0
        or model_weight > 1
    ):
        raise ValueError("model_weight must be a finite Decimal from zero to one")

    with localcontext() as context:
        context.prec = max(context.prec, 50)
        market_weight = Decimal(1) - model_weight
        result = {
            label: market[label] * market_weight + model[label] * model_weight
            for label in OUTCOME_LABELS
        }
    rounded = {
        "home_win": +result["home_win"],
        "draw": +result["draw"],
    }
    rounded["away_win"] = Decimal(1) - rounded["home_win"] - rounded["draw"]
    return rounded


def normalize_fixed_prizes(prizes):
    """Return normalized reciprocal probabilities for three fixed prizes."""
    try:
        prizes = list(prizes)
    except TypeError as error:
        raise ValueError("prizes must contain exactly three positive Decimal values") from error

    if len(prizes) != 3:
        raise ValueError("prizes must contain exactly three positive Decimal values")
    if any(
        not isinstance(prize, Decimal) or not prize.is_finite() or prize <= 0
        for prize in prizes
    ):
        raise ValueError("prizes must contain exactly three positive Decimal values")

    smallest_prize = min(prizes)
    with localcontext() as context:
        context.prec *= 2
        scaled_reciprocals = [smallest_prize / prize for prize in prizes]
        total = sum(scaled_reciprocals, Decimal(0))
        probabilities = [reciprocal / total for reciprocal in scaled_reciprocals]

    largest_index = max(range(len(probabilities)), key=probabilities.__getitem__)
    normalized = [
        +probability if index != largest_index else None
        for index, probability in enumerate(probabilities)
    ]
    normalized[largest_index] = Decimal(1) - sum(
        (probability for probability in normalized if probability is not None),
        Decimal(0),
    )
    return [probability.normalize() for probability in normalized]
