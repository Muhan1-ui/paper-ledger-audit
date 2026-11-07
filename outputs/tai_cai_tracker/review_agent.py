"""Read-only, auditable Chinese review views over the immutable paper ledger."""

from collections.abc import Mapping

from .report import calibration_report


OUTCOME_NAMES = {
    "home_win": "主胜",
    "draw": "平局",
    "away_win": "客胜",
}


def _unique(values):
    return list(dict.fromkeys(value for value in values if value))


def _calibration_status(records):
    report = calibration_report(records)
    status = {
        "available": report["available"],
        "settled_records": report["settled_records"],
        "minimum_settled_records": report.get("minimum_settled_records", 50),
        "phase": report.get("phase", "样本不足"),
    }
    for key in ("brier_average", "log_loss_average", "expected_calibration_error"):
        if key in report:
            status[key] = report[key]
    return status


def _prediction_view(prediction):
    return {
        "prediction_hash": prediction["content_hash"],
        "captured_at": prediction["captured_at"],
        "prediction_role": prediction.get("prediction_role", "primary"),
        "outcomes": [
            {
                "label": outcome["label"],
                "label_zh": OUTCOME_NAMES[outcome["label"]],
                "probability": outcome["probability"],
            }
            for outcome in prediction["outcomes"]
        ],
    }


def _match_facts(predictions):
    if not predictions:
        return {}
    prediction = predictions[0]
    context = prediction.get("match_context", {})
    return {
        "match_id": prediction["match_id"],
        "kickoff_at": prediction["kickoff_at"],
        "competition": context.get("competition"),
        "home_team": context.get("home_team"),
        "away_team": context.get("away_team"),
        "official_match_url": prediction["source_urls"]["S1"],
    }


def _evidence_view(record):
    view = {
        "record_hash": record["content_hash"],
        "record_type": record["record_type"],
        "evidence_kind": record["evidence_kind"],
        "collector": record["collector"],
        "parser_version": record["parser_version"],
    }
    if record["record_type"] == "source_observation":
        view.update({
            "source_url": record["source_url"],
            "captured_at": record["captured_at"],
            "published_at": record.get("published_at"),
            "published_at_unavailable_reason": record.get("published_at_unavailable_reason"),
            "observed_facts": record["observed_facts"],
        })
    else:
        view.update({
            "attempted_at": record["attempted_at"],
            "attempted_source_url": record.get("attempted_source_url"),
            "reason_code": record["reason_code"],
            "reason": record["reason"],
        })
    return view


def _global_summary(records):
    return {
        "prediction_count": sum(record.get("record_type") == "prediction" for record in records),
        "evidence_observation_count": sum(
            record.get("record_type") == "source_observation" for record in records
        ),
        "collection_failure_count": sum(
            record.get("record_type") == "collection_failure" for record in records
        ),
    }


def build_review(records, match_id=None):
    """Return a JSON-safe, read-only Chinese review derived from ledger records.

    ``records`` is never mutated.  The calibration function validates every
    immutable record and provides the project-wide, sample-gated status.
    """
    if not all(isinstance(record, Mapping) for record in records):
        raise ValueError("ledger records must be mappings")
    calibration = _calibration_status(records)
    predictions = [record for record in records if record.get("record_type") == "prediction"]
    selected_predictions = (
        [record for record in predictions if record["match_id"] == match_id]
        if match_id is not None else []
    )
    selected_hashes = {record["content_hash"] for record in selected_predictions}
    invalidations = {
        record["settlement_hash"]: record
        for record in records
        if record.get("record_type") == "settlement_invalidation"
    }
    selected_settlements = [
        record for record in records
        if record.get("record_type") == "settlement"
        and record["prediction_hash"] in selected_hashes
    ]
    active_settlements = [
        record for record in selected_settlements
        if record["content_hash"] not in invalidations
    ]
    relevant_evidence = [
        record for record in records
        if record.get("record_type") in {"source_observation", "collection_failure"}
        and match_id is not None and record["match_id"] == match_id
    ]

    data_gaps = []
    active_prediction_hashes = {record["prediction_hash"] for record in active_settlements}
    for prediction in selected_predictions:
        if prediction["content_hash"] not in active_prediction_hashes:
            data_gaps.append({
                "code": "official_settlement_missing",
                "message": "缺少可追溯的官方结果结算，未对赛事结果作推断。",
                "prediction_hash": prediction["content_hash"],
                "official_match_url": prediction["source_urls"]["S1"],
            })
    for settlement in selected_settlements:
        invalidation = invalidations.get(settlement["content_hash"])
        if invalidation is not None:
            data_gaps.append({
                "code": "settlement_invalidated",
                "message": "关联结算已失效，不能作为已验证事实。",
                "settlement_hash": settlement["content_hash"],
                "invalidation_hash": invalidation["content_hash"],
                "reason": invalidation["reason"],
            })
    for evidence in relevant_evidence:
        if evidence["record_type"] == "collection_failure":
            data_gaps.append({
                "code": "collection_failure",
                "message": "证据采集未完成，相关事实保持缺失。",
                "record_hash": evidence["content_hash"],
                "evidence_kind": evidence["evidence_kind"],
                "reason_code": evidence["reason_code"],
                "reason": evidence["reason"],
            })

    settlement_views = [
        {
            "settlement_hash": settlement["content_hash"],
            "prediction_hash": settlement["prediction_hash"],
            "actual_outcome": settlement["actual_outcome"],
            "actual_outcome_zh": OUTCOME_NAMES[settlement["actual_outcome"]],
            "official_result_url": settlement["official_result_url"],
            **({"final_score": settlement["final_score"]} if "final_score" in settlement else {}),
        }
        for settlement in active_settlements
    ]
    reference_records = (
        selected_predictions + selected_settlements + relevant_evidence
        + [invalidation for invalidation in invalidations.values()
           if invalidation["prediction_hash"] in selected_hashes]
    )
    source_urls = []
    for record in reference_records:
        source_urls.extend(record.get("source_urls", {}).values())
        for key in ("official_result_url", "official_source_url", "source_url", "attempted_source_url"):
            source_urls.append(record.get(key))

    return {
        "agent": {
            "name": "本地复盘 Agent",
            "mode": "read_only",
            "external_access": False,
        },
        "scope": {
            "match_id": match_id,
            "matched_predictions": len(selected_predictions),
            **(_global_summary(records) if match_id is None else {}),
        },
        "facts": {
            "match": _match_facts(selected_predictions),
            "evidence": [_evidence_view(record) for record in relevant_evidence],
        },
        "model_output": [_prediction_view(record) for record in selected_predictions],
        "settlements": settlement_views,
        "data_gaps": data_gaps,
        "calibration": calibration,
        "references": {
            "ledger_hashes": _unique(record.get("content_hash") for record in reference_records),
            "source_urls": _unique(source_urls),
        },
    }


def format_review(review):
    """Render the derived review as a concise Chinese terminal report."""
    scope = review["scope"]
    if scope["match_id"] is None:
        lines = [
            "本地复盘 Agent（只读）",
            f"台账汇总：{scope['prediction_count']} 条预测，"
            f"{scope['evidence_observation_count']} 条证据观察，"
            f"{scope['collection_failure_count']} 条采集失败。",
        ]
    else:
        lines = [
            "本地复盘 Agent（只读）",
            f"赛事：{scope['match_id']}，匹配 {scope['matched_predictions']} 条冻结预测。",
            f"已验证结算：{len(review['settlements'])} 条。",
            f"数据缺口：{len(review['data_gaps'])} 项。",
        ]
    calibration = review["calibration"]
    if calibration["available"]:
        lines.append(
            f"校准报告：可用，已结算 {calibration['settled_records']} 条，"
            f"Brier {calibration['brier_average']}，Log Loss {calibration['log_loss_average']}。"
        )
    else:
        lines.append(
            f"校准报告：样本不足，已结算 {calibration['settled_records']} 条，"
            f"至少需要 {calibration['minimum_settled_records']} 条。"
        )
    return "\n".join(lines)
