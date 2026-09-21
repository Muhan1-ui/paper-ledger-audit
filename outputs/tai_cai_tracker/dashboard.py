"""Read-only dashboard projections for the paper calibration tracker."""

import argparse
import json
import socket
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping

from .cli import read_records
from .report import MINIMUM_SETTLED_RECORDS, brier_score, calibration_report, log_loss


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


def _visual_pairs(records):
    predictions = {
        record.get("content_hash"): record
        for record in records
        if record.get("record_type") == "prediction"
    }
    invalidated_settlements = {
        record.get("settlement_hash")
        for record in records
        if record.get("record_type") == "settlement_invalidation"
    }
    pairs = []
    for settlement in records:
        if (
            settlement.get("record_type") != "settlement"
            or settlement.get("content_hash") in invalidated_settlements
        ):
            continue
        prediction = predictions.get(settlement.get("prediction_hash"))
        if prediction and prediction.get("prediction_role", "primary") == "primary":
            pairs.append((prediction, settlement))
    return pairs


def _visual_calibration_summary(records, report):
    grouped = {}
    for prediction, settlement in _visual_pairs(records):
        captured_at = _timestamp_for_sort(prediction.get("captured_at"))
        if captured_at is not None:
            grouped.setdefault(captured_at.date().isoformat(), []).append(
                (prediction, settlement)
            )

    trend = []
    for date in sorted(grouped):
        pairs = grouped[date]
        count = Decimal(len(pairs))
        trend.append({
            "date": date,
            "settled_records": len(pairs),
            "brier_average": sum(
                (brier_score(prediction, settlement) for prediction, settlement in pairs),
                Decimal(0),
            ) / count,
            "log_loss_average": sum(
                (log_loss(prediction, settlement) for prediction, settlement in pairs),
                Decimal(0),
            ) / count,
            "expected_calibration_error": None,
        })

    reliability = []
    for bucket in report.get("confidence_buckets", {}).values():
        if bucket.get("records", 0) and "accuracy" in bucket:
            reliability.append({
                "confidence": bucket["average_confidence"],
                "accuracy": bucket["accuracy"],
                "records": bucket["records"],
            })
    reliability.sort(key=lambda item: item["confidence"])
    return _json_value({"trend": trend, "reliability": reliability})


def _calibration_summary(report, records=None):
    minimum_settled_records = report.get(
        "minimum_settled_records", MINIMUM_SETTLED_RECORDS
    )
    result = {
        "available": report["available"],
        "settled_records": report["settled_records"],
        "minimum_settled_records": minimum_settled_records,
        "sample_gate": {
            "settled_records": report["settled_records"],
            "minimum_settled_records": minimum_settled_records,
            "met": report["settled_records"] >= minimum_settled_records,
        },
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
    result.update(_visual_calibration_summary(records or [], report))
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

    nonempty_lines = [
        (line_number, line)
        for line_number, line in enumerate(lines, start=1)
        if line.strip()
    ]
    if not nonempty_lines:
        warnings.append("Scheduler log contains no usable events.")
        return None

    line_number, line = nonempty_lines[-1]
    try:
        candidate = json.loads(line)
    except json.JSONDecodeError:
        warnings.append(
            f"Scheduler log final nonempty line {line_number} is malformed JSONL."
        )
        return None
    if not isinstance(candidate, Mapping):
        warnings.append(
            f"Scheduler log final nonempty line {line_number} is not an object."
        )
        return None
    return dict(candidate)


def _operation_summary(event, now, warnings):
    if event is None:
        return {"status": "unavailable"}

    status = event.get("status")
    if not isinstance(status, str) or not status.strip():
        status = "unavailable"
        warnings.append("Latest scheduler event has no usable status.")
    result = {"status": status}

    for field in ("mode", "error"):
        value = event.get(field)
        if isinstance(value, str) and value.strip():
            result[field] = value
    if isinstance(event.get("recovery_scan"), bool):
        result["recovery_scan"] = event["recovery_scan"]

    timestamp = event.get("timestamp")
    try:
        event_time = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
        if event_time is None or event_time.tzinfo is None:
            raise ValueError
    except ValueError:
        warnings.append("Latest scheduler event has no usable timestamp.")
        return result

    result["timestamp"] = timestamp

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
        now = datetime.now(timezone.utc).astimezone()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")

    warnings = []
    ledger = Path(ledger_path)
    if not ledger.exists():
        raise FileNotFoundError(f"Ledger file is unavailable: {ledger}")
    records = read_records(ledger)
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
        "calibration": _calibration_summary(report, records),
        "strategies": _strategy_summary(report),
        "operations": operations,
        "evidence": _json_value({
            "event_counts": report.get("evidence_event_counts", {}),
            "source_aggregates": source_performance.get("evidence_sources", {}),
        }),
        "recent_records": _recent_records(records),
        "warnings": warnings,
    }


_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
_DASHBOARD_HTML_PATH = Path(__file__).with_name("static") / "dashboard.html"


def _validate_loopback_host(host):
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            "Dashboard server host must be a loopback address: "
            "127.0.0.1 or ::1"
        )


def _dashboard_handler(ledger_path, scheduler_log_path):
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_json(self, status, payload, include_body):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _send_dashboard_error(self, include_body):
            self._send_json(
                500, {"error": "Dashboard data is unavailable."}, include_body
            )

        def _handle_request(self, include_body):
            if self.path == "/api/summary":
                try:
                    self._send_json(
                        200, build_summary(ledger_path, scheduler_log_path), include_body
                    )
                except Exception:
                    self._send_dashboard_error(include_body)
                return

            if self.path == "/api/health":
                try:
                    build_summary(ledger_path, scheduler_log_path)
                except Exception:
                    self._send_dashboard_error(include_body)
                else:
                    self._send_json(200, {"status": "ok"}, include_body)
                return

            if self.path == "/":
                try:
                    body = _DASHBOARD_HTML_PATH.read_bytes()
                except OSError:
                    self._send_dashboard_error(include_body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if include_body:
                    self.wfile.write(body)
                return

            self._send_json(404, {"error": "Not found."}, include_body)

        def do_GET(self):
            self._handle_request(include_body=True)

        def do_HEAD(self):
            self._handle_request(include_body=False)

        def log_message(self, format, *args):
            return

    return DashboardHandler


def create_server(ledger_path, scheduler_log_path=None, host="127.0.0.1", port=8765):
    """Create a loopback-only HTTP server for read-only dashboard views."""
    _validate_loopback_host(host)
    server_class = ThreadingHTTPServer
    if host == "::1":
        class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_class = IPv6ThreadingHTTPServer
    return server_class((host, port), _dashboard_handler(ledger_path, scheduler_log_path))


def main(argv=None):
    """Run the dashboard HTTP server until interrupted."""
    parser = argparse.ArgumentParser(description="Serve the local TaiCaiTracker dashboard.")
    parser.add_argument("--ledger-path", required=True)
    parser.add_argument("--scheduler-log-path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args(argv)
    try:
        server = create_server(
            arguments.ledger_path,
            arguments.scheduler_log_path,
            host=arguments.host,
            port=arguments.port,
        )
    except ValueError as error:
        parser.error(str(error))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
