"""Dixon-Coles attack/defence ratings for the historical score database.

This is a standalone research module.  It never writes the paper ledger and is
not imported by the prediction CLI.

Implementation notes
--------------------
* Model: independent Poisson goals with the Dixon-Coles low-score correction
  ``tau`` applied to the 0:0, 1:0, 0:1 and 1:1 cells (``rho`` parameter).
* Each division is fitted separately.  Every team gets ``attack`` and
  ``defence``; each division gets a single ``home_adv`` and ``rho``.
* Training weights decay exponentially with match age:
  ``weight = exp(-ln2 * age_days / half_life)``.
* Fitting maximises a penalised log-likelihood with an L2 penalty on the
  attack parameters.  ``scipy`` is not installed in this environment, so the
  optimiser is a hand-written Adam gradient-ascent loop (first-order method)
  rather than ``scipy.optimize.minimize``.  All maths uses the standard
  library only.
* Evaluation is season-rolling: a season is predicted only from matches in
  strictly earlier seasons, so no future result can leak into a prediction.

CLI
---
``python -m outputs.tai_cai_tracker.dixon_coles --fit``
    Fit the ratings on every season before 2026/27 and write ``dc_params.json``.

``python -m outputs.tai_cai_tracker.dixon_coles --eval``
    Rolling evaluation for 2024/25, 2025/26 and 2026/27 (RPS, Brier, top1),
    the market-close subset and 0.2/0.3/0.4/0.5 DC-market blends.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DATA_PATH = (
    Path(__file__).resolve().parent / "data" / "history" / "history_matches.jsonl"
)
PARAMS_PATH = Path(__file__).resolve().parent / "data" / "history" / "dc_params.json"

DIVISIONS: Tuple[str, ...] = ("E0", "E1", "D1", "SP1", "I1", "F1")
OUTCOMES: Tuple[str, ...] = ("home_win", "draw", "away_win")
EVAL_SEASONS: Tuple[str, ...] = ("2024/25", "2025/26", "2026/27")
MARKET_SOURCES: Tuple[str, ...] = ("avg", "b365", "pinnacle")
BLEND_WEIGHTS: Tuple[float, ...] = (0.2, 0.3, 0.4, 0.5)

DEFAULT_HALF_LIFE = 300.0
DEFAULT_REG = 0.01
DEFAULT_ITERATIONS = 350
DEFAULT_MAX_GOALS = 10
_RHO_LOW, _RHO_HIGH = -0.9, 0.9
_ETA_CLIP = 6.0


# ---------------------------------------------------------------------------
# Small probability helpers
# ---------------------------------------------------------------------------


def parse_date(value: object) -> date:
    """Parse an ISO ``YYYY-MM-DD`` date from the history file."""

    return date.fromisoformat(str(value))


def time_weight(
    match_date: date,
    reference_date: date,
    half_life: float = DEFAULT_HALF_LIFE,
) -> float:
    """Exponential time-decay weight (1.0 at age zero, 0.5 at one half-life)."""

    if half_life <= 0:
        raise ValueError("half_life must be positive")
    age = (reference_date - match_date).days
    if age < 0:
        age = 0
    return math.exp(-math.log(2.0) * age / half_life)


def actual_outcome(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home_win"
    if home_goals < away_goals:
        return "away_win"
    return "draw"


def _poisson_pmf(goals: int, mean: float) -> float:
    return math.exp(-mean) * mean**goals / math.factorial(goals)


def _dc_tau(
    home_goals: int, away_goals: int, home_lambda: float, away_lambda: float, rho: float
) -> float:
    """Dixon-Coles correction for the four lowest scorelines."""

    if home_goals == 0 and away_goals == 0:
        return 1.0 - home_lambda * away_lambda * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + home_lambda * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + away_lambda * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(
    home_lambda: float,
    away_lambda: float,
    rho: float,
    max_goals: int = DEFAULT_MAX_GOALS,
    normalize: bool = True,
) -> List[List[float]]:
    """Return the joint scoreline matrix (home goals as rows)."""

    home_pmf = [_poisson_pmf(k, home_lambda) for k in range(max_goals + 1)]
    away_pmf = [_poisson_pmf(k, away_lambda) for k in range(max_goals + 1)]
    matrix = [[home_pmf[i] * away_pmf[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    for i in (0, 1):
        for j in (0, 1):
            tau = _dc_tau(i, j, home_lambda, away_lambda, rho)
            if tau < 0.0:
                tau = 0.0
            matrix[i][j] *= tau
    if normalize:
        total = sum(sum(row) for row in matrix)
        if total > 0.0:
            matrix = [[value / total for value in row] for row in matrix]
    return matrix


def outcome_probabilities(
    home_lambda: float,
    away_lambda: float,
    rho: float,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> Dict[str, float]:
    """1X2 probabilities from a (normalised) Dixon-Coles score matrix."""

    matrix = score_matrix(home_lambda, away_lambda, rho, max_goals=max_goals)
    home = draw = away = 0.0
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            if i > j:
                home += value
            elif i == j:
                draw += value
            else:
                away += value
    total = home + draw + away
    if total > 0.0:
        home, draw, away = home / total, draw / total, away / total
    return {"home_win": home, "draw": draw, "away_win": away}


# ---------------------------------------------------------------------------
# Parametrisation and fitting
# ---------------------------------------------------------------------------


def _team_index(matches: Sequence[Mapping[str, object]]) -> List[str]:
    names = set()
    for match in matches:
        names.add(str(match["home_team"]))
        names.add(str(match["away_team"]))
    return sorted(names)


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _objective_and_gradient(
    rows: Sequence[Tuple[int, int, int, int, float, float]],
    n_teams: int,
    home_adv: float,
    rho: float,
    attack: Sequence[float],
    defence: Sequence[float],
    regulation: float,
) -> Tuple[float, float, float, List[float], List[float]]:
    """Return mean penalised log-likelihood and its gradients.

    ``rows`` items are ``(home_index, away_index, home_goals, away_goals,
    weight, log_factorials)``.
    """

    grad_home = 0.0
    grad_rho = 0.0
    grad_attack = [0.0] * n_teams
    grad_defence = [0.0] * n_teams
    total_weight = 0.0
    log_likelihood = 0.0

    exp = math.exp
    for home_index, away_index, home_goals, away_goals, weight, log_fact in rows:
        home_eta = home_adv + attack[home_index] + defence[away_index]
        away_eta = attack[away_index] + defence[home_index]
        if home_eta > _ETA_CLIP:
            home_eta = _ETA_CLIP
        elif home_eta < -_ETA_CLIP:
            home_eta = -_ETA_CLIP
        if away_eta > _ETA_CLIP:
            away_eta = _ETA_CLIP
        elif away_eta < -_ETA_CLIP:
            away_eta = -_ETA_CLIP
        home_lambda = exp(home_eta)
        away_lambda = exp(away_eta)

        tau = _dc_tau(home_goals, away_goals, home_lambda, away_lambda, rho)
        if tau <= 1e-12:
            tau = 1e-12

        log_likelihood += weight * (
            math.log(tau)
            + home_goals * math.log(home_lambda)
            - home_lambda
            + away_goals * math.log(away_lambda)
            - away_lambda
            - log_fact
        )

        # d log(tau) / d log(lambda)
        tau_home = 0.0
        tau_away = 0.0
        tau_rho = 0.0
        if home_goals == 0 and away_goals == 0:
            tau_home = -away_lambda * rho / tau
            tau_away = -home_lambda * rho / tau
            tau_rho = -home_lambda * away_lambda / tau
        elif home_goals == 0 and away_goals == 1:
            tau_home = rho / tau
            tau_rho = home_lambda / tau
        elif home_goals == 1 and away_goals == 0:
            tau_away = rho / tau
            tau_rho = away_lambda / tau
        elif home_goals == 1 and away_goals == 1:
            tau_rho = -1.0 / tau

        delta_home = (home_goals - home_lambda) + home_lambda * tau_home
        delta_away = (away_goals - away_lambda) + away_lambda * tau_away

        grad_home += weight * delta_home
        grad_attack[home_index] += weight * delta_home
        grad_defence[away_index] += weight * delta_home
        grad_attack[away_index] += weight * delta_away
        grad_defence[home_index] += weight * delta_away
        grad_rho += weight * tau_rho
        total_weight += weight

    if total_weight <= 0.0:
        total_weight = 1.0
    scale = 1.0 / total_weight
    grad_home *= scale
    grad_rho *= scale
    grad_attack = [value * scale - 2.0 * regulation * attack[i] for i, value in enumerate(grad_attack)]
    grad_defence = [value * scale for value in grad_defence]

    penalty = regulation * sum(value * value for value in attack)
    return log_likelihood * scale - penalty, grad_home, grad_rho, grad_attack, grad_defence


def fit_division(
    matches: Sequence[Mapping[str, object]],
    half_life: float = DEFAULT_HALF_LIFE,
    reference_date: Optional[date] = None,
    regulation: float = DEFAULT_REG,
    iterations: int = DEFAULT_ITERATIONS,
    learning_rate: float = 0.06,
) -> Dict[str, object]:
    """Fit one division by penalised maximum likelihood (Adam ascent)."""

    teams = _team_index(matches)
    index = {name: position for position, name in enumerate(teams)}
    if reference_date is None:
        reference_date = max(parse_date(match["date"]) for match in matches)

    rows: List[Tuple[int, int, int, int, float, float]] = []
    for match in matches:
        home_goals = int(match["home_goals"])
        away_goals = int(match["away_goals"])
        log_fact = math.lgamma(home_goals + 1) + math.lgamma(away_goals + 1)
        rows.append(
            (
                index[str(match["home_team"])],
                index[str(match["away_team"])],
                home_goals,
                away_goals,
                time_weight(parse_date(match["date"]), reference_date, half_life),
                log_fact,
            )
        )

    n_teams = len(teams)
    if not rows or n_teams == 0:
        return {"home_adv": 0.0, "rho": -0.05, "teams": {}, "n_matches": 0}

    mean_home = _mean(int(match["home_goals"]) for match in matches)
    mean_away = _mean(int(match["away_goals"]) for match in matches)
    mean_home = max(mean_home, 0.05)
    mean_away = max(mean_away, 0.05)

    home_adv = math.log(mean_home) - math.log(mean_away)
    rho = -0.05
    defence = [math.log(mean_away)] * n_teams
    attack = [0.0] * n_teams

    beta1, beta2, epsilon = 0.9, 0.999, 1e-8
    moment_home = moment_rho = 0.0
    variance_home = variance_rho = 0.0
    moment_attack = [0.0] * n_teams
    variance_attack = [0.0] * n_teams
    moment_defence = [0.0] * n_teams
    variance_defence = [0.0] * n_teams

    best = None
    patience = 0
    for step in range(1, iterations + 1):
        objective, g_home, g_rho, g_attack, g_defence = _objective_and_gradient(
            rows, n_teams, home_adv, rho, attack, defence, regulation
        )
        if best is None or objective > best + 1e-10:
            best = objective
            patience = 0
        else:
            patience += 1
        if patience >= 4 and step > 60:
            break

        moment_home = beta1 * moment_home + (1.0 - beta1) * g_home
        variance_home = beta2 * variance_home + (1.0 - beta2) * g_home * g_home
        moment_rho = beta1 * moment_rho + (1.0 - beta1) * g_rho
        variance_rho = beta2 * variance_rho + (1.0 - beta2) * g_rho * g_rho

        bias1 = 1.0 - beta1**step
        bias2 = 1.0 - beta2**step
        home_adv += learning_rate * (moment_home / bias1) / (math.sqrt(variance_home / bias2) + epsilon)
        rho += learning_rate * (moment_rho / bias1) / (math.sqrt(variance_rho / bias2) + epsilon)

        for i in range(n_teams):
            moment_attack[i] = beta1 * moment_attack[i] + (1.0 - beta1) * g_attack[i]
            variance_attack[i] = beta2 * variance_attack[i] + (1.0 - beta2) * g_attack[i] * g_attack[i]
            moment_defence[i] = beta1 * moment_defence[i] + (1.0 - beta1) * g_defence[i]
            variance_defence[i] = beta2 * variance_defence[i] + (1.0 - beta2) * g_defence[i] * g_defence[i]
            attack[i] += learning_rate * (moment_attack[i] / bias1) / (
                math.sqrt(variance_attack[i] / bias2) + epsilon
            )
            defence[i] += learning_rate * (moment_defence[i] / bias1) / (
                math.sqrt(variance_defence[i] / bias2) + epsilon
            )

        # Identifiability: shift attack/defence in opposite directions keeps
        # every lambda unchanged; pin mean(attack) = 0.
        mean_attack = _mean(attack)
        attack = [value - mean_attack for value in attack]
        defence = [value + mean_attack for value in defence]

        home_adv = max(-_ETA_CLIP, min(_ETA_CLIP, home_adv))
        rho = max(_RHO_LOW, min(_RHO_HIGH, rho))

    return {
        "home_adv": round(home_adv, 6),
        "rho": round(rho, 6),
        "teams": {
            name: {"attack": round(attack[i], 6), "defence": round(defence[i], 6)}
            for i, name in enumerate(teams)
        },
    }


# ---------------------------------------------------------------------------
# Prediction helpers
# ---------------------------------------------------------------------------


def team_parameters(
    division_params: Mapping[str, object], team: str
) -> Tuple[float, float]:
    """Return ``(attack, defence)``; unseen teams fall back to league means."""

    teams = division_params.get("teams") or {}
    if not isinstance(teams, Mapping) or not teams:
        return 0.0, 0.0
    entry = teams.get(team)
    if isinstance(entry, Mapping):
        return float(entry.get("attack", 0.0)), float(entry.get("defence", 0.0))
    mean_attack = _mean(float(item.get("attack", 0.0)) for item in teams.values() if isinstance(item, Mapping))
    mean_defence = _mean(
        float(item.get("defence", 0.0)) for item in teams.values() if isinstance(item, Mapping)
    )
    return mean_attack, mean_defence


def predict_match(
    division_params: Optional[Mapping[str, object]],
    home_team: str,
    away_team: str,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> Dict[str, float]:
    """1X2 probabilities for one match; a missing/empty param set still works."""

    if not division_params:
        division_params = {}
    home_adv = float(division_params.get("home_adv", 0.0) or 0.0)
    rho = float(division_params.get("rho", 0.0) or 0.0)
    home_attack, home_defence = team_parameters(division_params, home_team)
    away_attack, away_defence = team_parameters(division_params, away_team)
    home_lambda = math.exp(max(-_ETA_CLIP, min(_ETA_CLIP, home_adv + home_attack + away_defence)))
    away_lambda = math.exp(max(-_ETA_CLIP, min(_ETA_CLIP, away_attack + home_defence)))
    return outcome_probabilities(home_lambda, away_lambda, rho, max_goals=max_goals)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def rps(probabilities: Mapping[str, float], actual: str) -> float:
    """Normalised rank probability score over home/draw/away."""

    if actual not in OUTCOMES:
        raise ValueError(f"unknown actual outcome: {actual!r}")
    cumulative_probability = 0.0
    cumulative_outcome = 0.0
    score = 0.0
    for outcome in OUTCOMES:
        cumulative_probability += float(probabilities.get(outcome, 0.0))
        cumulative_outcome += 1.0 if outcome == actual else 0.0
        score += (cumulative_probability - cumulative_outcome) ** 2
    return score / 2.0


def brier(probabilities: Mapping[str, float], actual: str) -> float:
    if actual not in OUTCOMES:
        raise ValueError(f"unknown actual outcome: {actual!r}")
    return sum(
        (float(probabilities.get(outcome, 0.0)) - (1.0 if outcome == actual else 0.0)) ** 2
        for outcome in OUTCOMES
    )


def top1(probabilities: Mapping[str, float]) -> str:
    return max(OUTCOMES, key=lambda outcome: float(probabilities.get(outcome, 0.0)))


def summarize(rows: Sequence[Tuple[Mapping[str, float], str]]) -> Dict[str, object]:
    if not rows:
        return {"n": 0, "rps": None, "brier": None, "top1": None}
    return {
        "n": len(rows),
        "rps": sum(rps(prob, actual) for prob, actual in rows) / len(rows),
        "brier": sum(brier(prob, actual) for prob, actual in rows) / len(rows),
        "top1": sum(1 for prob, actual in rows if top1(prob) == actual) / len(rows),
    }


# ---------------------------------------------------------------------------
# Data loading and rolling evaluation
# ---------------------------------------------------------------------------


def load_history(path: Path = DATA_PATH) -> List[dict]:
    matches: List[dict] = []
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            matches.append(json.loads(line))
    return matches


def season_start(season: str) -> int:
    return int(str(season).split("/")[0])


def training_matches(
    matches: Sequence[Mapping[str, object]], target_season: str
) -> List[Mapping[str, object]]:
    """All matches in seasons strictly before ``target_season``."""

    cutoff = season_start(target_season)
    return [match for match in matches if season_start(str(match["season"])) < cutoff]


def season_matches(
    matches: Sequence[Mapping[str, object]], target_season: str
) -> List[Mapping[str, object]]:
    return [match for match in matches if str(match["season"]) == target_season]


def fit_for_season(
    matches: Sequence[Mapping[str, object]],
    target_season: str,
    half_life: float = DEFAULT_HALF_LIFE,
    regulation: float = DEFAULT_REG,
    iterations: int = DEFAULT_ITERATIONS,
) -> Dict[str, Dict[str, object]]:
    """Fit every division on seasons before ``target_season``."""

    train = training_matches(matches, target_season)
    target = season_matches(matches, target_season)
    if target:
        reference_date = min(parse_date(match["date"]) for match in target)
    else:
        reference_date = max((parse_date(match["date"]) for match in train), default=date.today())

    divisions: Dict[str, Dict[str, object]] = {}
    for division in DIVISIONS:
        subset = [match for match in train if str(match["division"]) == division]
        if subset:
            divisions[division] = fit_division(
                subset,
                half_life=half_life,
                reference_date=reference_date,
                regulation=regulation,
                iterations=iterations,
            )
    return divisions


def market_close_probabilities(match: Mapping[str, object]) -> Optional[Dict[str, float]]:
    """Best available closing fair 1X2 probabilities.

    Priority: ``odds.avg`` then ``odds.b365`` then ``odds.pinnacle``.  The
    stored ``fair`` object is the normalised closing-price probability; a
    nested ``close.fair`` shape is also accepted.
    """

    odds = match.get("odds") or {}
    if not isinstance(odds, Mapping):
        return None
    for source in MARKET_SOURCES:
        node = odds.get(source)
        if not isinstance(node, Mapping):
            continue
        fair = None
        close = node.get("close")
        if isinstance(close, Mapping) and isinstance(close.get("fair"), Mapping):
            fair = close["fair"]
        elif isinstance(node.get("fair"), Mapping):
            fair = node["fair"]
        if not isinstance(fair, Mapping):
            continue
        values = {}
        for key in ("home", "draw", "away"):
            value = fair.get(key)
            if value is None:
                values = {}
                break
            values[key] = float(value)
        if len(values) != 3 or any(value <= 0.0 for value in values.values()):
            continue
        total = sum(values.values())
        if total <= 0.0:
            continue
        return {
            "home_win": values["home"] / total,
            "draw": values["draw"] / total,
            "away_win": values["away"] / total,
        }
    return None


def evaluate_rolling(
    matches: Sequence[Mapping[str, object]],
    seasons: Sequence[str] = EVAL_SEASONS,
    half_life: float = DEFAULT_HALF_LIFE,
    regulation: float = DEFAULT_REG,
    iterations: int = DEFAULT_ITERATIONS,
) -> Dict[str, object]:
    """Season-rolling evaluation with market subset and DC/market blends."""

    per_season: Dict[str, dict] = {}
    all_dc: List[Tuple[Mapping[str, float], str]] = []
    all_market: List[Tuple[Mapping[str, float], str]] = []
    all_dc_on_market: List[Tuple[Mapping[str, float], str]] = []
    blend_rows: Dict[float, List[Tuple[Mapping[str, float], str]]] = {
        weight: [] for weight in BLEND_WEIGHTS
    }

    for season in seasons:
        division_params = fit_for_season(
            matches,
            season,
            half_life=half_life,
            regulation=regulation,
            iterations=iterations,
        )
        dc_rows: List[Tuple[Mapping[str, float], str]] = []
        market_rows: List[Tuple[Mapping[str, float], str]] = []
        dc_market_rows: List[Tuple[Mapping[str, float], str]] = []
        season_blends: Dict[float, List[Tuple[Mapping[str, float], str]]] = {
            weight: [] for weight in BLEND_WEIGHTS
        }

        for match in season_matches(matches, season):
            division = str(match["division"])
            params = division_params.get(division)
            if params is None:
                continue
            actual = actual_outcome(int(match["home_goals"]), int(match["away_goals"]))
            dc_prob = predict_match(params, str(match["home_team"]), str(match["away_team"]))
            dc_rows.append((dc_prob, actual))
            market_prob = market_close_probabilities(match)
            if market_prob is None:
                continue
            market_rows.append((market_prob, actual))
            dc_market_rows.append((dc_prob, actual))
            for weight in BLEND_WEIGHTS:
                # weight is the DC share; the rest is the market close.
                blended = {
                    outcome: weight * dc_prob[outcome] + (1.0 - weight) * market_prob[outcome]
                    for outcome in OUTCOMES
                }
                season_blends[weight].append((blended, actual))
                blend_rows[weight].append((blended, actual))

        per_season[season] = {
            "dc": summarize(dc_rows),
            "market": summarize(market_rows),
            "dc_on_market": summarize(dc_market_rows),
            "blends": {weight: summarize(rows) for weight, rows in season_blends.items()},
        }
        all_dc.extend(dc_rows)
        all_market.extend(market_rows)
        all_dc_on_market.extend(dc_market_rows)

    return {
        "seasons": per_season,
        "total": {
            "dc": summarize(all_dc),
            "market": summarize(all_market),
            "dc_on_market": summarize(all_dc_on_market),
            "blends": {weight: summarize(rows) for weight, rows in blend_rows.items()},
        },
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _fmt(value: Optional[float], digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _num(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.5f}"


def print_report(result: Mapping[str, object], half_life: float) -> None:
    print(f"half_life={half_life:g} days")
    print()
    print("[1] DC rolling evaluation (all matches)")
    print(f"{'season':<10}{'n':>7}{'RPS':>10}{'Brier':>10}{'top1':>9}")
    for season, block in result["seasons"].items():
        item = block["dc"]
        print(
            f"{season:<10}{item['n']:>7}{_num(item['rps']):>10}{_num(item['brier']):>10}"
            f"{_fmt(item['top1'], 4):>9}"
        )
    total = result["total"]["dc"]
    print(
        f"{'total':<10}{total['n']:>7}{_num(total['rps']):>10}{_num(total['brier']):>10}"
        f"{_fmt(total['top1'], 4):>9}"
    )

    print()
    print("[2] Market close fair odds (avg > b365 > pinnacle) on the same matches")
    print(f"{'season':<10}{'n':>7}{'market_RPS':>12}{'DC_RPS_subset':>15}")
    for season, block in result["seasons"].items():
        item = block["market"]
        dc_item = block["dc_on_market"]
        print(
            f"{season:<10}{item['n']:>7}{_num(item['rps']):>12}{_num(dc_item['rps']):>15}"
        )
    item = result["total"]["market"]
    dc_item = result["total"]["dc_on_market"]
    print(f"{'total':<10}{item['n']:>7}{_num(item['rps']):>12}{_num(dc_item['rps']):>15}")

    print()
    print("[3] Blend of DC and market close (weight = DC share), same subset")
    market_rps = result["total"]["market"]["rps"]
    print(f"{'weight':<10}{'n':>7}{'RPS':>10}{'vs market':>12}")
    print(f"{'market':<10}{result['total']['market']['n']:>7}{_num(market_rps):>10}{'0.00000':>12}")
    for weight, block in result["total"]["blends"].items():
        delta = "n/a" if market_rps is None or block["rps"] is None else f"{block['rps'] - market_rps:+.5f}"
        print(f"{weight:<10.1f}{block['n']:>7}{_num(block['rps']):>10}{delta:>12}")


def build_params(
    matches: Sequence[Mapping[str, object]],
    half_life: float,
    regulation: float,
    iterations: int,
) -> Dict[str, object]:
    target = "2026/27"
    divisions = fit_for_season(
        matches,
        target,
        half_life=half_life,
        regulation=regulation,
        iterations=iterations,
    )
    start = season_start(target)
    fitted_until = f"{start - 1}/{(start) % 100:02d}"
    return {
        "half_life": int(half_life) if float(half_life).is_integer() else half_life,
        "fitted_until": fitted_until,
        "divisions": divisions,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fit", action="store_true", help="fit and write dc_params.json")
    parser.add_argument("--eval", action="store_true", help="rolling evaluation report")
    parser.add_argument("--half-life", type=float, default=DEFAULT_HALF_LIFE)
    parser.add_argument("--regulation", type=float, default=DEFAULT_REG)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    parser.add_argument("--output", type=Path, default=PARAMS_PATH)
    args = parser.parse_args(argv)

    if not args.fit and not args.eval:
        parser.error("at least one of --fit or --eval is required")

    matches = load_history(args.data)
    print(f"loaded {len(matches)} matches from {args.data}")

    if args.fit:
        started = time.perf_counter()
        params = build_params(matches, args.half_life, args.regulation, args.iterations)
        elapsed = time.perf_counter() - started
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(params, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        size = args.output.stat().st_size
        teams = sum(len(division["teams"]) for division in params["divisions"].values())
        print(
            f"fit done in {elapsed:.2f}s -> {args.output} ({size} bytes, "
            f"{len(params['divisions'])} divisions, {teams} team rows)"
        )

    if args.eval:
        started = time.perf_counter()
        result = evaluate_rolling(
            matches,
            half_life=args.half_life,
            regulation=args.regulation,
            iterations=args.iterations,
        )
        print_report(result, args.half_life)
        print()
        print(f"eval done in {time.perf_counter() - started:.2f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
