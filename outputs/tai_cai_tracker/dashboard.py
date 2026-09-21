"""Read-only dashboard projections for the paper calibration tracker."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from .cli import read_records
from .report import calibration_report


def _json_value(value):
    """Return a standard-JSON-compatible copy of a report value."""
    if isinstance(value, Decimal):
        rendered = format(value.normalize(), "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _sample_gate(report, count_key, minimum_key):
    count = report.get(count_key, 0)
    minimum = report.get(minimum_key)
    return {
        count_key: count,
        minimum_key: minimum,
        "met": isinstance(minimum, int) and count >= minimum,
    }


def _calibration_summary(report):
    result = {
        "available": report["available"],
        "settled_records": report["settled_records"],
        "minimum_settled_records": report["minimum_settled_records"],
        "sample_gate": _sample_gate(
            report, "settled_records", "minimum_settled_records"
        ),
    }
    for key in (
        "phase",
        "brier_average",
        "log_loss_average",
        "expected_calibration_error",
        "scoreline_evaluated_records",
        "scoreline_log_loss_average",
    ):
        if key in report:
            result[key] = report[key]
    for source_key, target_key in (
        ("temporal_weight_evaluation", "temporal_weight_sample_gate"),
        ("walk_forward_weight_evaluation", "walk_forward_weight_sample_gate"),
    ):
        evaluation = report.get(source_key, {})
        if isinstance(evaluation, Mapping):
            result[target_key] = _sample_gate(
                evaluation,
                "enhanced_settled_records",
                "minimum_enhanced_settled_records",
            )
    return _json_value(result)


def _strategy_summary(report):
    strategies = report.get("strategy_breakdown", {})
    items = []
    for name in sorted(strategies):
        values = strategies[name]
        items.append({
            "strategy": name,
            "settled_records": values["settled_records"],
            "brier_average": values["brier_average"],
            "log_loss_average": values["log_loss_average"],
            "top_pick_accuracy": values["top_pick_accuracy"],
        })
    return _json_value(items)


def _parse_scheduler_event(scheduler_log_path, warnings):
    if scheduler_log_path is None:
        warnings.append("Scheduler log is unavailable.")
        return None
    path = Path(scheduler_log_path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        warnings.append("Scheduler log is unavailable.")
        return None

    latest = None
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"Scheduler log contains malformed JSONL on line {line_number}.")
            continue
        if not isinstance(candidate, Mapping):
            warnings.append(f"Scheduler log entry on line {line_number} is not an object.")
            continue
        latest = dict(candidate)
    if latest is None:
        warnings.append("Scheduler log contains no usable events.")
    return latest


def _operation_summary(event, now, warnings):
    if event is None:
        return {"status": "unavailable"}

    status = event.get("status")
    if not isinstance(status, str) or not status.strip():
        status = "unavailable"
        warnings.append("Latest scheduler event has no usable status.")
    result = {"status": status, "event": _json_value(event)}

    timestamp = event.get("timestamp")
    try:
        event_time = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
        if event_time is None or event_time.tzinfo is None:
            raise ValueError
    except ValueError:
        warnings.append("Latest scheduler event has no usable timestamp.")
        return result

    age = Decimal(str((now - event_time).total_seconds())) / Decimal(3600)
    if age < 0:
        warnings.append("Latest scheduler event is in the future.")
        age = Decimal(0)
    result["age_hours"] = _json_value(age.normalize())
    result["stale"] = age > Decimal(24)
    if result["stale"]:
        warnings.append(f"Scheduler event is stale: {result['age_hours']} hours old.")
    return result


def _record_timestamp(record):
    for field in ("captured_at", "attempted_at", "invalidated_at", "settled_at"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _timestamp_for_sort(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _identity_for_record(record, predictions, snapshots):
    source = record
    record_type = record.get("record_type")
    if record_type in {"settlement", "settlement_invalidation"}:
        source = predictions.get(record.get("prediction_hash"), record)
    elif record_type == "all_market_settlement":
        source = snapshots.get(record.get("snapshot_hash"), record)
    elif record_type == "all_market_snapshot":
        source = predictions.get(record.get("prediction_hash"), record)

    identity = {}
    if source.get("match_id") is not None:
        identity["match_id"] = source["match_id"]
    context = source.get("match_context")
    if isinstance(context, Mapping):
        for field in ("competition", "home_team", "away_team"):
            if isinstance(context.get(field), str) and context[field].strip():
                identity[field] = context[field]
    return identity


def _recent_records(records):
    predictions = {
        record.get("content_hash"): record
        for record in records
        if record.get("record_type") == "prediction"
    }
    snapshots = {
        record.get("content_hash"): record
        for record in records
        if record.get("record_type") == "all_market_snapshot"
    }
    items = []
    for position, record in enumerate(records):
        content_hash = record.get("content_hash", "")
        timestamp = _record_timestamp(record)
        items.append({
            "record_type": record.get("record_type", "unknown"),
            "match_identity": _identity_for_record(record, predictions, snapshots),
            "timestamp": timestamp,
            "content_hash_prefix": content_hash[:12] if isinstance(content_hash, str) else "",
            "_sort_timestamp": _timestamp_for_sort(timestamp),
            "_position": position,
        })
    items.sort(
        key=lambda item: (
            item["_sort_timestamp"] is not None,
            item["_sort_timestamp"] or datetime.min.replace(tzinfo=timezone.utc),
            item["timestamp"] or "",
            item["_position"],
        ),
        reverse=True,
    )
    for item in items:
        item.pop("_sort_timestamp")
        item.pop("_position")
    return items[:10]


def build_summary(ledger_path, scheduler_log_path=None, now=None):
    """Build a compact dashboard view without writing ledger or scheduler data."""
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")

    warnings = []
    ledger = Path(ledger_path)
    if ledger.exists():
        records = read_records(ledger)
    else:
        records = []
        warnings.append("Ledger is unavailable.")
    report = calibration_report(records)
    if not report["available"]:
        warnings.append(
            "Calibration is unavailable: "
            f"{report['settled_records']} settled predictions; "
            f"{report['minimum_settled_records']} required."
        )
    event = _parse_scheduler_event(scheduler_log_path, warnings)
    operations = _operation_summary(event, now, warnings)
    source_performance = report.get("source_performance", {})

    return {
        "generated_at": now.isoformat(),
        "calibration": _calibration_summary(report),
        "strategies": _strategy_summary(report),
        "operations": operations,
        "evidence": _json_value({
            "event_counts": report.get("evidence_event_counts", {}),
            "source_aggregates": source_performance.get("evidence_sources", {}),
        }),
        "recent_records": _recent_records(records),
        "warnings": warnings,
    }
