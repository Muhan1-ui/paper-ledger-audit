"""Calibration reporting for settled three-outcome predictions."""

import hashlib
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Mapping
from urllib.parse import urlsplit

from .ledger import (
    ALL_MARKET_OPTIONS,
    OUTCOME_LABELS,
    _verify_content_hash,
    verify_all_market_settlement,
    verify_all_market_snapshot,
    verify_evidence_event,
    verify_odds_capture,
    verify_settlement_invalidation,
)


MINIMUM_SETTLED_RECORDS = 50
TUNING_MINIMUM_SETTLED_RECORDS = 200
MINIMUM_COMPARABLE_COHORT_RECORDS = 20
TEMPORAL_TRAINING_FRACTION = Decimal("0.70")
WEIGHT_CANDIDATES = tuple(Decimal(number) / Decimal(10) for number in range(11))


def all_market_breakdown(records):
    """Return descriptive metrics for linked all-market snapshots only."""
    snapshots = {}
    settlements = {}
    for record in records:
        if record.get("record_type") == "all_market_snapshot":
            verify_all_market_snapshot(record)
            snapshots[record["content_hash"]] = record
        elif record.get("record_type") == "all_market_settlement":
            verify_all_market_settlement(record)
            settlements[record["snapshot_hash"]] = record
    result = {}
    for code, options in ALL_MARKET_OPTIONS.items():
        frozen = skipped = settled = hits = 0
        selected_probabilities = []
        brier_values = []
        log_losses = []
        for snapshot in snapshots.values():
            entry = snapshot["markets"][code]
            if entry["status"] == "skipped":
                skipped += 1
                continue
            frozen += 1
            settlement = settlements.get(snapshot["content_hash"])
            if settlement is None or code not in settlement["markets"]:
                continue
            settled += 1
            settled_entry = settlement["markets"][code]
            hits += int(settled_entry["hit"])
            selected_probabilities.append(entry["selected_probability"])
            actual = settled_entry["actual_option"]
            brier_values.append(sum(
                (entry["probabilities"][option] - (Decimal(1) if option == actual else Decimal(0))) ** 2
                for option in options
            ))
            probability = entry["probabilities"][actual]
            log_losses.append(Decimal("Infinity") if probability == 0 else -probability.ln())
        count = Decimal(settled)
        result[code] = {
            "frozen_records": frozen,
            "settled_records": settled,
            "skipped_records": skipped,
            "hits": hits,
            "hit_rate": Decimal(hits) / count if settled else None,
            "average_selected_probability": sum(selected_probabilities, Decimal(0)) / count if settled else None,
            "brier_average": sum(brier_values, Decimal(0)) / count if settled else None,
            "log_loss_average": sum(log_losses, Decimal(0)) / count if settled else None,
        }
    return result


def all_market_snapshot_strategy(snapshot):
    """Return the immutable source tier of a validated five-market snapshot."""
    verify_all_market_snapshot(snapshot)
    return snapshot.get("snapshot_strategy", "model_derived")


def all_market_strategy_breakdown(records):
    """Keep official-market baselines separate from model-derived snapshots."""
    strategy_by_snapshot = {}
    grouped_records = {}
    for record in records:
        if record.get("record_type") != "all_market_snapshot":
            continue
        strategy = all_market_snapshot_strategy(record)
        strategy_by_snapshot[record["content_hash"]] = strategy
        grouped_records.setdefault(strategy, []).append(record)
    for record in records:
        if record.get("record_type") != "all_market_settlement":
            continue
        strategy = strategy_by_snapshot.get(record.get("snapshot_hash"))
        if strategy is not None:
            grouped_records[strategy].append(record)
    return {
        strategy: all_market_breakdown(items)
        for strategy, items in grouped_records.items()
    }


def _probability_context(prediction, settlement):
    if not isinstance(prediction, Mapping) or not isinstance(settlement, Mapping):
        raise ValueError("prediction and settlement must be mappings")
    if prediction.get("record_type") != "prediction" or prediction.get("status") != "frozen":
        raise ValueError("prediction must be frozen")
    if settlement.get("record_type") != "settlement" or settlement.get("status") != "settled":
        raise ValueError("settlement must be settled")
    _verify_content_hash(prediction)
    _verify_content_hash(settlement)
    if settlement.get("prediction_hash") != prediction.get("content_hash"):
        raise ValueError("settlement is not linked to prediction")

    outcomes = prediction.get("outcomes")
    if not isinstance(outcomes, list):
        raise ValueError("prediction must contain outcomes")
    probabilities = {
        outcome.get("label"): outcome.get("probability")
        for outcome in outcomes
        if isinstance(outcome, Mapping)
    }
    if set(probabilities) != set(OUTCOME_LABELS):
        raise ValueError("prediction outcomes are invalid")
    if any(not isinstance(probability, Decimal) for probability in probabilities.values()):
        raise ValueError("prediction probabilities must be Decimal values")

    actual_outcome = settlement.get("actual_outcome")
    if actual_outcome not in OUTCOME_LABELS:
        raise ValueError("settlement actual_outcome is invalid")
    return probabilities, actual_outcome


def brier_score(prediction, settlement):
    """Return the unnormalised three-class Brier contribution for a settlement."""
    probabilities, actual_outcome = _probability_context(prediction, settlement)
    return sum(
        (probabilities[label] - (Decimal(1) if label == actual_outcome else Decimal(0))) ** 2
        for label in OUTCOME_LABELS
    )


def log_loss(prediction, settlement):
    """Return the negative natural log probability for the settled outcome."""
    probabilities, actual_outcome = _probability_context(prediction, settlement)
    probability = probabilities[actual_outcome]
    if probability == 0:
        return Decimal("Infinity")
    return -probability.ln()


def scoreline_log_loss(prediction, settlement):
    """Return exact-score log loss when both a score matrix and final score exist."""
    _probability_context(prediction, settlement)
    final_score = settlement.get("final_score")
    model = prediction.get("model")
    if final_score is None or model is None:
        return None
    if (
        not isinstance(final_score, Mapping)
        or set(final_score) != {"home", "away"}
        or any(
            isinstance(goals, bool) or not isinstance(goals, int) or goals < 0
            for goals in final_score.values()
        )
        or not isinstance(model, Mapping)
        or not isinstance(model.get("scorelines"), list)
    ):
        raise ValueError("scoreline evaluation inputs are invalid")
    probability = next(
        (
            scoreline.get("probability")
            for scoreline in model["scorelines"]
            if isinstance(scoreline, Mapping)
            and scoreline.get("home_goals") == final_score["home"]
            and scoreline.get("away_goals") == final_score["away"]
        ),
        None,
    )
    if not isinstance(probability, Decimal) or not probability.is_finite() or probability < 0:
        raise ValueError("scoreline model probability is invalid")
    if probability == 0:
        return Decimal("Infinity")
    return -probability.ln()


def _confidence_summary(prediction_settlements, bin_count=10):
    buckets = [
        {"records": 0, "confidence_sum": Decimal(0), "correct": 0}
        for _ in range(bin_count)
    ]
    for prediction, settlement in prediction_settlements:
        probabilities, actual_outcome = _probability_context(prediction, settlement)
        predicted_outcome = max(OUTCOME_LABELS, key=lambda label: probabilities[label])
        confidence = probabilities[predicted_outcome]
        index = min(int(confidence * bin_count), bin_count - 1)
        bucket = buckets[index]
        bucket["records"] += 1
        bucket["confidence_sum"] += confidence
        bucket["correct"] += int(predicted_outcome == actual_outcome)

    total_records = sum(bucket["records"] for bucket in buckets)
    expected_calibration_error = Decimal(0)
    summaries = {}
    for index, bucket in enumerate(buckets):
        label = f"{index / bin_count:.1f}-{(index + 1) / bin_count:.1f}"
        if bucket["records"] == 0:
            summaries[label] = {"records": 0}
            continue
        records = Decimal(bucket["records"])
        average_confidence = bucket["confidence_sum"] / records
        accuracy = Decimal(bucket["correct"]) / records
        expected_calibration_error += abs(accuracy - average_confidence) * records / Decimal(total_records)
        summaries[label] = {
            "records": bucket["records"],
            "average_confidence": average_confidence,
            "accuracy": accuracy,
        }
    return expected_calibration_error, summaries


def strategy_label(prediction):
    """Return the frozen probability strategy label for a prediction."""
    model = prediction.get("model")
    role = prediction.get("prediction_role")
    if role not in {None, "primary", "comparison"}:
        raise ValueError("prediction role is invalid")
    if model is None:
        base_label = "market_baseline"
    else:
        if (
            not isinstance(model, Mapping)
            or not isinstance(model.get("version"), str)
            or not isinstance(model.get("weight"), Decimal)
        ):
            raise ValueError("prediction model metadata is invalid")
        base_label = f"{model['version']}@{model['weight']}"
    return base_label if role is None else f"{base_label}@{role}"


def _strategy_breakdown(prediction_settlements):
    grouped = {}
    for prediction, settlement in prediction_settlements:
        grouped.setdefault(strategy_label(prediction), []).append((prediction, settlement))

    breakdown = {}
    for label, pairs in grouped.items():
        brier_values = [brier_score(prediction, settlement) for prediction, settlement in pairs]
        log_losses = [log_loss(prediction, settlement) for prediction, settlement in pairs]
        correct_top_picks = 0
        scoreline_losses = []
        for prediction, settlement in pairs:
            probabilities, actual_outcome = _probability_context(prediction, settlement)
            top_pick = max(OUTCOME_LABELS, key=lambda outcome: probabilities[outcome])
            correct_top_picks += int(top_pick == actual_outcome)
            scoreline_loss = scoreline_log_loss(prediction, settlement)
            if scoreline_loss is not None:
                scoreline_losses.append(scoreline_loss)

        count = Decimal(len(pairs))
        strategy_report = {
            "settled_records": len(pairs),
            "brier_average": sum(brier_values, Decimal(0)) / count,
            "log_loss_average": sum(log_losses, Decimal(0)) / count,
            "top_pick_accuracy": Decimal(correct_top_picks) / count,
        }
        if scoreline_losses:
            strategy_report["scoreline_evaluated_records"] = len(scoreline_losses)
            strategy_report["scoreline_log_loss_average"] = sum(
                scoreline_losses, Decimal(0)
            ) / Decimal(len(scoreline_losses))
        breakdown[label] = strategy_report
    return breakdown


def _competition_breakdown(prediction_settlements):
    grouped = {}
    for prediction, settlement in prediction_settlements:
        match_context = prediction.get("match_context")
        if not isinstance(match_context, Mapping):
            continue
        competition = match_context.get("competition")
        if not isinstance(competition, str) or not competition.strip():
            raise ValueError("prediction match_context competition is invalid")
        grouped.setdefault(competition, []).append((prediction, settlement))

    breakdown = {}
    for competition, pairs in grouped.items():
        count = Decimal(len(pairs))
        brier_values = [brier_score(prediction, settlement) for prediction, settlement in pairs]
        log_losses = [log_loss(prediction, settlement) for prediction, settlement in pairs]
        correct_top_picks = 0
        for prediction, settlement in pairs:
            probabilities, actual_outcome = _probability_context(prediction, settlement)
            top_pick = max(OUTCOME_LABELS, key=lambda outcome: probabilities[outcome])
            correct_top_picks += int(top_pick == actual_outcome)
        breakdown[competition] = {
            "settled_records": len(pairs),
            "brier_average": sum(brier_values, Decimal(0)) / count,
            "log_loss_average": sum(log_losses, Decimal(0)) / count,
            "top_pick_accuracy": Decimal(correct_top_picks) / count,
        }
    return breakdown


def _model_probability_maps(prediction):
    model = prediction.get("model")
    if not isinstance(model, Mapping):
        return None
    market = model.get("market_outcomes")
    model_outcomes = model.get("model_outcomes")
    if not isinstance(market, Mapping) or not isinstance(model_outcomes, Mapping):
        return None
    if set(market) != set(OUTCOME_LABELS) or set(model_outcomes) != set(OUTCOME_LABELS):
        return None
    if any(
        not isinstance(probability, Decimal)
        or not probability.is_finite()
        or probability < 0
        or probability > 1
        for probabilities in (market, model_outcomes)
        for probability in probabilities.values()
    ):
        return None
    if (
        sum(market.values(), Decimal(0)) != Decimal(1)
        or sum(model_outcomes.values(), Decimal(0)) != Decimal(1)
    ):
        return None
    return market, model_outcomes


def _weighted_log_loss(pairs, weight):
    losses = []
    for prediction, settlement in pairs:
        probability_maps = _model_probability_maps(prediction)
        if probability_maps is None:
            raise ValueError("enhanced prediction probability maps are invalid")
        market, model_outcomes = probability_maps
        _, actual_outcome = _probability_context(prediction, settlement)
        probability = (
            market[actual_outcome] * (Decimal(1) - weight)
            + model_outcomes[actual_outcome] * weight
        )
        if probability == 0:
            return Decimal("Infinity")
        losses.append(-probability.ln())
    return sum(losses, Decimal(0)) / Decimal(len(losses))


def temporal_weight_evaluation(prediction_settlements):
    """Select a fixed blend weight on early data and assess it on later data."""
    enhanced_pairs = [
        pair
        for pair in prediction_settlements
        if pair[0].get("prediction_role", "primary") == "primary"
        and _model_probability_maps(pair[0]) is not None
    ]
    if len(enhanced_pairs) < TUNING_MINIMUM_SETTLED_RECORDS:
        return {
            "available": False,
            "enhanced_settled_records": len(enhanced_pairs),
            "minimum_enhanced_settled_records": TUNING_MINIMUM_SETTLED_RECORDS,
        }
    try:
        ordered_pairs = sorted(
            enhanced_pairs,
            key=lambda pair: datetime.fromisoformat(pair[0]["captured_at"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("enhanced prediction captured_at is invalid") from error
    split_index = int(Decimal(len(ordered_pairs)) * TEMPORAL_TRAINING_FRACTION)
    training_pairs = ordered_pairs[:split_index]
    holdout_pairs = ordered_pairs[split_index:]
    with localcontext() as context:
        context.prec = max(context.prec, 50)
        candidate_losses = {
            weight: _weighted_log_loss(training_pairs, weight)
            for weight in WEIGHT_CANDIDATES
        }
        selected_weight = min(WEIGHT_CANDIDATES, key=lambda weight: (candidate_losses[weight], weight))
        return {
            "available": True,
            "enhanced_settled_records": len(ordered_pairs),
            "training_records": len(training_pairs),
            "holdout_records": len(holdout_pairs),
            "selected_weight": selected_weight,
            "training_log_loss": candidate_losses[selected_weight],
            "holdout_log_loss": _weighted_log_loss(holdout_pairs, selected_weight),
            "market_holdout_log_loss": _weighted_log_loss(holdout_pairs, Decimal(0)),
            "model_holdout_log_loss": _weighted_log_loss(holdout_pairs, Decimal(1)),
        }


def _ordered_enhanced_pairs(prediction_settlements):
    enhanced_pairs = [
        pair
        for pair in prediction_settlements
        if pair[0].get("prediction_role", "primary") == "primary"
        and _model_probability_maps(pair[0]) is not None
    ]
    try:
        return sorted(
            enhanced_pairs,
            key=lambda pair: (
                datetime.fromisoformat(pair[0]["captured_at"]),
                pair[0]["content_hash"],
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("enhanced prediction captured_at is invalid") from error


def _training_snapshot_hash(training_pairs):
    prediction_hashes = [pair[0]["content_hash"] for pair in training_pairs]
    return hashlib.sha256("\n".join(prediction_hashes).encode("ascii")).hexdigest()


def walk_forward_weight_evaluation(prediction_settlements):
    """Evaluate candidate blend weights using only predictions frozen before each fold."""
    ordered_pairs = _ordered_enhanced_pairs(prediction_settlements)
    if len(ordered_pairs) < TUNING_MINIMUM_SETTLED_RECORDS:
        return {
            "available": False,
            "enhanced_settled_records": len(ordered_pairs),
            "minimum_enhanced_settled_records": TUNING_MINIMUM_SETTLED_RECORDS,
        }

    initial_training_records = int(
        Decimal(len(ordered_pairs)) * TEMPORAL_TRAINING_FRACTION
    )
    folds = []
    with localcontext() as context:
        context.prec = max(context.prec, 50)
        for test_index in range(initial_training_records, len(ordered_pairs)):
            training_pairs = ordered_pairs[:test_index]
            test_prediction, test_settlement = ordered_pairs[test_index]
            candidate_losses = {
                weight: _weighted_log_loss(training_pairs, weight)
                for weight in WEIGHT_CANDIDATES
            }
            selected_weight = min(
                WEIGHT_CANDIDATES,
                key=lambda weight: (candidate_losses[weight], weight),
            )
            folds.append({
                "training_ends_at": training_pairs[-1][0]["captured_at"],
                "training_prediction_hashes": [
                    prediction["content_hash"] for prediction, _ in training_pairs
                ],
                "training_snapshot_hash": _training_snapshot_hash(training_pairs),
                "test_prediction_hash": test_prediction["content_hash"],
                "test_captured_at": test_prediction["captured_at"],
                "model_version": test_prediction.get("model", {}).get("version"),
                "selected_weight": selected_weight,
                "training_log_loss": candidate_losses[selected_weight],
                "test_log_loss": _weighted_log_loss(
                    [(test_prediction, test_settlement)], selected_weight
                ),
            })

        selected_weight_counts = {
            str(weight): sum(
                fold["selected_weight"] == weight for fold in folds
            )
            for weight in WEIGHT_CANDIDATES
        }
        test_pairs = ordered_pairs[initial_training_records:]
        return {
            "available": True,
            "enhanced_settled_records": len(ordered_pairs),
            "initial_training_records": initial_training_records,
            "candidate_weights": list(WEIGHT_CANDIDATES),
            "fold_count": len(folds),
            "selected_weight_counts": selected_weight_counts,
            "walk_forward_log_loss": sum(
                (fold["test_log_loss"] for fold in folds), Decimal(0)
            ) / Decimal(len(folds)),
            "market_walk_forward_log_loss": _weighted_log_loss(test_pairs, Decimal(0)),
            "model_walk_forward_log_loss": _weighted_log_loss(test_pairs, Decimal(1)),
            "folds": folds,
        }


def _evidence_event_counts(records):
    counts = {
        "source_observation": 0,
        "collection_failure": 0,
        "by_evidence_kind": {},
    }
    for record in records:
        record_type = record.get("record_type")
        if record_type in {"source_observation", "collection_failure"}:
            verify_evidence_event(record)
            counts[record_type] += 1
            evidence_kind = record["evidence_kind"]
            counts["by_evidence_kind"][evidence_kind] = (
                counts["by_evidence_kind"].get(evidence_kind, 0) + 1
            )
    return counts


def _source_hostname(value, fallback):
    if not isinstance(value, str):
        return fallback
    hostname = urlsplit(value).hostname
    return hostname or fallback


def _european_provider_label(prediction):
    european_odds = prediction.get("european_odds")
    if isinstance(european_odds, Mapping):
        provider = european_odds.get("provider")
        if isinstance(provider, str) and provider.strip():
            return provider
    return "unlinked_market"


def _european_provider_family(prediction):
    """Coalesce known legacy provider labels without changing frozen records."""
    label = _european_provider_label(prediction)
    normalized = label.casefold()
    if normalized.startswith("oddsportal"):
        return "OddsPortal"
    if normalized.startswith("433"):
        return "433"
    if normalized.startswith("sgodds"):
        return "sgodds"
    return label.strip()


def _xg_source_label(prediction):
    xg_inputs = prediction.get("xg_inputs")
    if not isinstance(xg_inputs, Mapping):
        return "no_xg"
    evidence = xg_inputs.get("evidence")
    if isinstance(evidence, Mapping):
        label = _source_hostname(evidence.get("source_url"), None)
        if label is not None:
            return label
    return _source_hostname(xg_inputs.get("source_url"), "no_xg")


def _xg_source_family(prediction):
    """Return a derived xG source family while retaining the raw source label."""
    label = _xg_source_label(prediction).casefold()
    if label in {"fotmob.com", "www.fotmob.com", "data.fotmob.com"}:
        return "fotmob"
    return label


def _xg_scope_label(prediction):
    xg_inputs = prediction.get("xg_inputs")
    if not isinstance(xg_inputs, Mapping):
        return "no_xg"
    evidence = xg_inputs.get("evidence")
    scope = evidence.get("scope") if isinstance(evidence, Mapping) else None
    if isinstance(scope, str) and scope in {
        "home_away_split", "league_total", "source_match_model",
    }:
        return scope
    return "scope_unknown"


def _xg_scope_family_label(prediction):
    return f"{_xg_source_family(prediction)}|{_xg_scope_label(prediction)}"


def _context_ceiling_label(prediction):
    context = prediction.get("pregame_context")
    risk = context.get("risk_summary") if isinstance(context, Mapping) else None
    ceiling = risk.get("confidence_ceiling") if isinstance(risk, Mapping) else None
    return ceiling if isinstance(ceiling, str) and ceiling else "no_context"


def _settled_source_breakdown(pairs, labeler, overall_calibration_available):
    groups = {}
    for prediction, settlement in pairs:
        groups.setdefault(labeler(prediction), []).append((prediction, settlement))
    result = {}
    for label, group_pairs in groups.items():
        count = Decimal(len(group_pairs))
        brier_values = [brier_score(prediction, settlement) for prediction, settlement in group_pairs]
        loss_values = [log_loss(prediction, settlement) for prediction, settlement in group_pairs]
        correct_top_picks = 0
        for prediction, settlement in group_pairs:
            probabilities, actual_outcome = _probability_context(prediction, settlement)
            top_pick = max(OUTCOME_LABELS, key=lambda outcome: probabilities[outcome])
            correct_top_picks += int(top_pick == actual_outcome)
        result[label] = {
            "settled_records": len(group_pairs),
            "brier_average": sum(brier_values, Decimal(0)) / count,
            "log_loss_average": sum(loss_values, Decimal(0)) / count,
            "top_pick_accuracy": Decimal(correct_top_picks) / count,
            "calibration_available": (
                overall_calibration_available
                and len(group_pairs) >= MINIMUM_SETTLED_RECORDS
            ),
        }
    return result


def _evidence_source_breakdown(records):
    groups = {}
    for record in records:
        record_type = record.get("record_type")
        if record_type not in {"source_observation", "collection_failure"}:
            continue
        if record_type == "source_observation":
            facts = record.get("observed_facts")
            provider = facts.get("provider") if isinstance(facts, Mapping) else None
            label = provider if isinstance(provider, str) and provider.strip() else _source_hostname(record.get("source_url"), "unknown_source")
        else:
            label = record.get("collector") if isinstance(record.get("collector"), str) else "unknown_collector"
        entry = groups.setdefault(label, {"observations": 0, "failures": 0, "by_evidence_kind": {}})
        if record_type == "source_observation":
            entry["observations"] += 1
        else:
            entry["failures"] += 1
        kind = record.get("evidence_kind", "unknown")
        entry["by_evidence_kind"][kind] = entry["by_evidence_kind"].get(kind, 0) + 1
    return groups


def _comparable_cohort_label(prediction):
    return "|".join((
        f"odds={_european_provider_family(prediction)}",
        f"xg={_xg_source_family(prediction)}",
        f"scope={_xg_scope_label(prediction)}",
        f"strategy={strategy_label(prediction)}",
    ))


def _comparable_cohort_breakdown(pairs, overall_calibration_available):
    items = _settled_source_breakdown(
        pairs,
        _comparable_cohort_label,
        overall_calibration_available,
    )
    for item in items.values():
        item["comparison_ready"] = (
            item["settled_records"] >= MINIMUM_COMPARABLE_COHORT_RECORDS
        )
    return {
        "minimum_settled_records": MINIMUM_COMPARABLE_COHORT_RECORDS,
        "items": items,
    }


def source_performance(records, primary_pairs, overall_calibration_available):
    """Return descriptive source diagnostics from frozen primary prediction inputs."""
    return {
        "evidence_sources": _evidence_source_breakdown(records),
        "european_odds": _settled_source_breakdown(
            primary_pairs, _european_provider_label, overall_calibration_available
        ),
        "xg_source": _settled_source_breakdown(
            primary_pairs, _xg_source_label, overall_calibration_available
        ),
        "context_ceiling": _settled_source_breakdown(
            primary_pairs, _context_ceiling_label, overall_calibration_available
        ),
        "source_families": {
            "european_odds": _settled_source_breakdown(
                primary_pairs, _european_provider_family, overall_calibration_available
            ),
            "xg_source": _settled_source_breakdown(
                primary_pairs, _xg_source_family, overall_calibration_available
            ),
            "xg_scope": _settled_source_breakdown(
                primary_pairs, _xg_scope_family_label, overall_calibration_available
            ),
        },
        "comparable_cohorts": _comparable_cohort_breakdown(
            primary_pairs, overall_calibration_available
        ),
    }


def calibration_report(records):
    """Return a gated calibration report from prediction and settlement ledger records."""
    predictions = {}
    settlements = []
    settlement_hashes = set()
    invalidated_settlement_hashes = set()
    evidence_event_counts = _evidence_event_counts(records)
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("ledger records must be mappings")
        record_type = record.get("record_type")
        if record_type == "prediction":
            _verify_content_hash(record)
            prediction_hash = record["content_hash"]
            if prediction_hash in predictions:
                raise ValueError("duplicate prediction record")
            predictions[prediction_hash] = record
        elif record_type == "settlement":
            _verify_content_hash(record)
            settlements.append(record)
            settlement_hashes.add(record["content_hash"])
        elif record_type == "settlement_invalidation":
            verify_settlement_invalidation(record)
            if record["settlement_hash"] not in settlement_hashes:
                raise ValueError("settlement invalidation has no prior settlement")
            if record["settlement_hash"] in invalidated_settlement_hashes:
                raise ValueError("settlement has more than one invalidation")
            invalidated_settlement_hashes.add(record["settlement_hash"])
        elif record_type == "odds_capture":
            verify_odds_capture(record)
        elif record_type in {"source_observation", "collection_failure"}:
            verify_evidence_event(record)
        elif record_type == "all_market_snapshot":
            verify_all_market_snapshot(record)
        elif record_type == "all_market_settlement":
            verify_all_market_settlement(record)
        else:
            raise ValueError(
                "ledger records must be predictions, settlements, or odds captures"
            )

    aggregate_scores = []
    aggregate_pairs = []
    strategy_pairs = []
    settled_prediction_hashes = set()
    for settlement in settlements:
        if settlement["content_hash"] in invalidated_settlement_hashes:
            continue
        prediction_hash = settlement.get("prediction_hash")
        if prediction_hash in settled_prediction_hashes:
            raise ValueError("prediction cannot be settled more than once")
        try:
            prediction = predictions[prediction_hash]
        except (KeyError, TypeError) as error:
            raise ValueError("settlement has no matching frozen prediction") from error
        settled_prediction_hashes.add(prediction_hash)
        pair = (prediction, settlement)
        strategy_pairs.append(pair)
        if prediction.get("prediction_role", "primary") == "primary":
            aggregate_scores.append(brier_score(prediction, settlement))
            aggregate_pairs.append(pair)

    settled_records = len(aggregate_scores)
    market_breakdown = all_market_breakdown(records)
    market_strategy_breakdown = all_market_strategy_breakdown(records)
    if settled_records < MINIMUM_SETTLED_RECORDS:
        return {
            "available": False,
            "settled_records": settled_records,
            "minimum_settled_records": MINIMUM_SETTLED_RECORDS,
            "all_market_breakdown": market_breakdown,
            "all_market_strategy_breakdown": market_strategy_breakdown,
            "all_market_phase": "observational",
            "strategy_breakdown": _strategy_breakdown(strategy_pairs),
            "evidence_event_counts": evidence_event_counts,
            "temporal_weight_evaluation": temporal_weight_evaluation(aggregate_pairs),
            "walk_forward_weight_evaluation": walk_forward_weight_evaluation(aggregate_pairs),
            "source_performance": source_performance(records, aggregate_pairs, False),
        }

    losses = [
        log_loss(prediction, settlement) for prediction, settlement in aggregate_pairs
    ]
    scoreline_losses = [
        scoreline_log_loss(prediction, settlement)
        for prediction, settlement in aggregate_pairs
    ]
    scoreline_losses = [loss for loss in scoreline_losses if loss is not None]
    with localcontext() as context:
        context.prec = max(context.prec, 50)
        expected_calibration_error, confidence_buckets = _confidence_summary(aggregate_pairs)
        report = {
            "available": True,
            "settled_records": settled_records,
            "brier_average": sum(aggregate_scores, Decimal(0)) / Decimal(settled_records),
            "log_loss_average": sum(losses, Decimal(0)) / Decimal(settled_records),
            "expected_calibration_error": expected_calibration_error,
            "confidence_buckets": confidence_buckets,
            "strategy_breakdown": _strategy_breakdown(strategy_pairs),
            "competition_breakdown": _competition_breakdown(aggregate_pairs),
            "temporal_weight_evaluation": temporal_weight_evaluation(aggregate_pairs),
            "walk_forward_weight_evaluation": walk_forward_weight_evaluation(aggregate_pairs),
            "evidence_event_counts": evidence_event_counts,
            "source_performance": source_performance(records, aggregate_pairs, True),
            "phase": (
                "observational"
                if settled_records < TUNING_MINIMUM_SETTLED_RECORDS
                else "evaluation_ready"
            ),
            "all_market_breakdown": market_breakdown,
            "all_market_strategy_breakdown": market_strategy_breakdown,
            "all_market_phase": "observational" if any(
                item["settled_records"] < MINIMUM_SETTLED_RECORDS
                for item in market_breakdown.values()
            ) else "calibration_ready",
        }
        if scoreline_losses:
            report["scoreline_evaluated_records"] = len(scoreline_losses)
            report["scoreline_log_loss_average"] = sum(
                scoreline_losses, Decimal(0)
            ) / Decimal(len(scoreline_losses))
        return report
