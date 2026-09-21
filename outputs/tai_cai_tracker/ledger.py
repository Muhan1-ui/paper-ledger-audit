"""Immutable-by-convention prediction and settlement ledger records."""

import copy
import hashlib
import json
import re
from decimal import Decimal
from datetime import datetime
from typing import Mapping
from urllib.parse import urlsplit

from .markets import CORRECT_SCORE_OPTIONS, HALF_FULL_OPTIONS, OUTCOME_OPTIONS, TOTAL_GOAL_OPTIONS
from .context import validate_pregame_context


OUTCOME_LABELS = ("home_win", "draw", "away_win")
PREDICTION_ROLES = frozenset({"primary", "comparison"})
WEEKDAY_SEQUENCE_MATCH_ID = re.compile(r"^周[一二三四五六日][0-9]{3}$")
ODDS_SOURCE_QUALITIES = ("named_bookmaker", "unattributed")
EVIDENCE_KINDS = frozenset({"official_market", "european_1x2", "xg", "pregame_context"})
COLLECTION_FAILURE_REASONS = frozenset({
    "access_restricted",
    "not_found",
    "identity_mismatch",
    "missing_publication_time",
    "incomplete_data",
    "parse_error",
    "network_error",
    "other",
})
SETTLEMENT_INVALIDATION_REASONS = frozenset({"identity_mismatch"})
ALL_MARKET_OPTIONS = {
    "spf": OUTCOME_OPTIONS,
    "hhad": OUTCOME_OPTIONS,
    "crs": CORRECT_SCORE_OPTIONS,
    "ttg": TOTAL_GOAL_OPTIONS,
    "hafu": HALF_FULL_OPTIONS,
}


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _content_hash(record):
    encoded = json.dumps(
        record,
        default=_json_default,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_content_hash(record):
    content_hash = record.get("content_hash") if isinstance(record, Mapping) else None
    if not isinstance(content_hash, str):
        raise ValueError("record must include a content_hash")
    unhashed_record = dict(record)
    unhashed_record.pop("content_hash", None)
    if _content_hash(unhashed_record) != content_hash:
        raise ValueError("record content_hash does not match its contents")


def _parse_timestamp(value, field_name):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if timestamp.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return timestamp


def _is_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_valid_match_id(match_id):
    return (
        isinstance(match_id, str)
        and (
            (match_id.isdecimal() and len(match_id) == 11)
            or WEEKDAY_SEQUENCE_MATCH_ID.fullmatch(match_id)
        )
    )


def _validate_match_context(match_context):
    required_fields = {"competition", "home_team", "away_team"}
    if not isinstance(match_context, Mapping) or set(match_context) != required_fields:
        raise ValueError("match_context must contain competition, home_team, and away_team")
    if any(
        not isinstance(value, str) or not value.strip()
        for value in match_context.values()
    ):
        raise ValueError("match_context values must be non-empty strings")


def _validate_prediction(prediction):
    if prediction.get("market") != "SPF":
        raise ValueError("market must be SPF")

    match_id = prediction.get("match_id")
    if not _is_valid_match_id(match_id):
        raise ValueError("match_id must be an 11-digit ID or a Chinese Sports Lottery weekday sequence")

    kickoff_at = _parse_timestamp(prediction.get("kickoff_at"), "kickoff_at")
    captured_at = _parse_timestamp(prediction.get("captured_at"), "captured_at")
    if captured_at >= kickoff_at:
        raise ValueError("captured_at must be strictly before kickoff_at")

    source_urls = prediction.get("source_urls")
    if not isinstance(source_urls, Mapping) or set(source_urls) != {"S1", "S2"}:
        raise ValueError("source_urls must contain exactly S1 and S2")
    if not all(_is_url(source_urls[source]) for source in ("S1", "S2")):
        raise ValueError("S1 and S2 must be HTTP(S) URLs")

    outcomes = prediction.get("outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != len(OUTCOME_LABELS):
        raise ValueError("outcomes must contain exactly three outcomes")
    if any(not isinstance(outcome, Mapping) for outcome in outcomes):
        raise ValueError("outcomes must contain mappings")
    labels = [outcome.get("label") for outcome in outcomes]
    if set(labels) != set(OUTCOME_LABELS) or len(set(labels)) != len(OUTCOME_LABELS):
        raise ValueError("outcomes must use home_win, draw, and away_win exactly once")
    probabilities = [outcome.get("probability") for outcome in outcomes]
    if any(
        not isinstance(probability, Decimal)
        or not probability.is_finite()
        or probability < 0
        or probability > 1
        for probability in probabilities
    ):
        raise ValueError("outcome probabilities must be finite Decimal values between zero and one")
    if sum(probabilities, Decimal(0)) != Decimal(1):
        raise ValueError("outcome probabilities must sum to one")

    if "match_context" in prediction:
        _validate_match_context(prediction["match_context"])

    if "pregame_context" in prediction:
        validate_pregame_context(prediction["pregame_context"])

    if "prediction_role" in prediction and prediction["prediction_role"] not in PREDICTION_ROLES:
        raise ValueError("prediction_role must be primary or comparison")

    if "actual_outcome" in prediction:
        raise ValueError("frozen predictions cannot contain an actual_outcome")


def freeze_prediction(prediction):
    """Return a frozen prediction record with a hash of its contents."""
    if not isinstance(prediction, Mapping):
        raise ValueError("prediction must be a mapping")
    _validate_prediction(prediction)

    record = copy.deepcopy(dict(prediction))
    record["record_type"] = "prediction"
    record["status"] = "frozen"
    record["content_hash"] = _content_hash(record)
    return record


def verify_prediction(record):
    """Verify a frozen prediction's identity, schema, and content hash."""
    if not isinstance(record, Mapping):
        raise ValueError("prediction record must be a mapping")
    if record.get("record_type") != "prediction" or record.get("status") != "frozen":
        raise ValueError("prediction record must be frozen")
    _verify_content_hash(record)
    prediction = dict(record)
    for field in ("record_type", "status", "content_hash"):
        prediction.pop(field)
    _validate_prediction(prediction)


def _validate_odds_capture(capture):
    if not isinstance(capture, Mapping):
        raise ValueError("odds capture must be a mapping")
    required_fields = {
        "provider",
        "match_id",
        "kickoff_at",
        "captured_at",
        "source_url",
        "bookmaker",
        "source_quality",
        "odds",
    }
    if set(capture) != required_fields:
        raise ValueError("odds capture must contain exactly the required fields")
    if not isinstance(capture["provider"], str) or not capture["provider"].strip():
        raise ValueError("odds capture provider must be a non-empty string")
    if not _is_valid_match_id(capture["match_id"]):
        raise ValueError("match_id must be an 11-digit ID or a Chinese Sports Lottery weekday sequence")

    kickoff_at = _parse_timestamp(capture["kickoff_at"], "kickoff_at")
    captured_at = _parse_timestamp(capture["captured_at"], "captured_at")
    if captured_at >= kickoff_at:
        raise ValueError("captured_at must be strictly before kickoff_at")
    if not _is_url(capture["source_url"]):
        raise ValueError("odds capture source_url must be an HTTP(S) URL")

    source_quality = capture["source_quality"]
    if source_quality not in ODDS_SOURCE_QUALITIES:
        raise ValueError("odds capture source_quality is invalid")
    bookmaker = capture["bookmaker"]
    if source_quality == "named_bookmaker":
        if not isinstance(bookmaker, str) or not bookmaker.strip():
            raise ValueError("named_bookmaker capture requires a bookmaker name")
    elif bookmaker is not None:
        raise ValueError("unattributed capture bookmaker must be null")

    odds = capture["odds"]
    if not isinstance(odds, Mapping) or set(odds) != set(OUTCOME_LABELS):
        raise ValueError("odds capture odds must contain home_win, draw, and away_win")
    if any(
        not isinstance(odd, Decimal) or not odd.is_finite() or odd <= 0
        for odd in odds.values()
    ):
        raise ValueError("odds capture odds must be finite positive Decimal values")


def freeze_odds_capture(capture):
    """Return a hashed, immutable-by-convention European odds observation."""
    _validate_odds_capture(capture)
    record = copy.deepcopy(dict(capture))
    record["record_type"] = "odds_capture"
    record["status"] = "captured"
    record["content_hash"] = _content_hash(record)
    return record


def verify_odds_capture(record):
    """Validate an imported capture record before it can become a baseline."""
    if not isinstance(record, Mapping):
        raise ValueError("odds capture record must be a mapping")
    if record.get("record_type") != "odds_capture" or record.get("status") != "captured":
        raise ValueError("record is not a captured odds record")
    _verify_content_hash(record)
    capture = dict(record)
    capture.pop("record_type", None)
    capture.pop("status", None)
    capture.pop("content_hash", None)
    _validate_odds_capture(capture)
    return record


def _non_empty_string(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _validate_json_value(value, field_name):
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"{field_name} must contain only JSON values")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, field_name)
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{field_name} object keys must be strings")
        for item in value.values():
            _validate_json_value(item, field_name)
        return
    raise ValueError(f"{field_name} must contain only JSON values")


def _validate_evidence_event(event):
    if not isinstance(event, Mapping):
        raise ValueError("evidence event must be a mapping")
    record_type = event.get("record_type")
    if record_type not in {"source_observation", "collection_failure"}:
        raise ValueError("evidence event record_type is invalid")
    if not _is_valid_match_id(event.get("match_id")):
        raise ValueError("evidence event match_id is invalid")
    if event.get("evidence_kind") not in EVIDENCE_KINDS:
        raise ValueError("evidence event evidence_kind is invalid")
    _non_empty_string(event.get("collector"), "collector")
    _non_empty_string(event.get("parser_version"), "parser_version")

    if record_type == "source_observation":
        base_fields = {
            "record_type",
            "match_id",
            "evidence_kind",
            "source_url",
            "captured_at",
            "collector",
            "parser_version",
            "raw_content_sha256",
            "observed_facts",
        }
        publication_fields = {"published_at", "published_at_unavailable_reason"}
        if not base_fields.issubset(event) or not set(event).issubset(base_fields | publication_fields):
            raise ValueError("source observation has invalid fields")
        if len(set(event) & publication_fields) != 1:
            raise ValueError("source observation requires published_at or published_at_unavailable_reason")
        if not _is_url(event["source_url"]):
            raise ValueError("source observation source_url must be an HTTP(S) URL")
        _parse_timestamp(event["captured_at"], "captured_at")
        if "published_at" in event:
            _parse_timestamp(event["published_at"], "published_at")
        else:
            _non_empty_string(
                event["published_at_unavailable_reason"],
                "published_at_unavailable_reason",
            )
        raw_content_sha256 = event["raw_content_sha256"]
        if not isinstance(raw_content_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_content_sha256):
            raise ValueError("raw_content_sha256 must be a lowercase SHA-256 hash")
        observed_facts = event["observed_facts"]
        if not isinstance(observed_facts, Mapping) or not observed_facts:
            raise ValueError("observed_facts must be a non-empty object")
        _validate_json_value(observed_facts, "observed_facts")
        return

    required = {
        "record_type",
        "match_id",
        "evidence_kind",
        "attempted_at",
        "collector",
        "parser_version",
        "reason_code",
        "reason",
    }
    optional = {"attempted_source_url"}
    if not required.issubset(event) or not set(event).issubset(required | optional):
        raise ValueError("collection failure has invalid fields")
    _parse_timestamp(event["attempted_at"], "attempted_at")
    if "attempted_source_url" in event and not _is_url(event["attempted_source_url"]):
        raise ValueError("attempted_source_url must be an HTTP(S) URL")
    if event["reason_code"] not in COLLECTION_FAILURE_REASONS:
        raise ValueError("collection failure reason_code is invalid")
    _non_empty_string(event["reason"], "reason")


def freeze_evidence_event(event):
    """Return a hashed public-source observation or collection-failure record."""
    _validate_evidence_event(event)
    record = copy.deepcopy(dict(event))
    record["status"] = "observed" if record["record_type"] == "source_observation" else "unavailable"
    record["content_hash"] = _content_hash(record)
    return record


def verify_evidence_event(record):
    """Verify a hashed immutable public-source evidence event."""
    if not isinstance(record, Mapping):
        raise ValueError("evidence event record must be a mapping")
    expected_status = {
        "source_observation": "observed",
        "collection_failure": "unavailable",
    }.get(record.get("record_type"))
    if expected_status is None or record.get("status") != expected_status:
        raise ValueError("evidence event record has invalid type or status")
    _verify_content_hash(record)
    event = dict(record)
    event.pop("status", None)
    event.pop("content_hash", None)
    _validate_evidence_event(event)
    return record


def _all_market_options(code):
    try:
        return ALL_MARKET_OPTIONS[code]
    except KeyError as error:
        raise ValueError("all-market code is invalid") from error


def _validate_all_market_frozen_entry(code, entry, kickoff_at, schema_version):
    options = _all_market_options(code)
    if not isinstance(entry, Mapping):
        raise ValueError("all-market entry must be a mapping")
    status = entry.get("status")
    if status == "skipped":
        if set(entry) != {"status", "reason"} or not isinstance(entry["reason"], str) or not entry["reason"].strip():
            raise ValueError("skipped market must contain a non-empty reason")
        return
    expected = {"status", "probabilities", "selected_option", "selected_probability", "tied_options", "official_fixed_prizes"}
    if code == "hhad":
        expected.add("home_handicap")
    if schema_version in {2, 3}:
        expected.update({"published_at", "official_source_url"})
    if status != "frozen" or set(entry) != expected:
        raise ValueError("frozen all-market entry has invalid fields")
    probabilities = entry["probabilities"]
    prizes = entry["official_fixed_prizes"]
    if not isinstance(probabilities, Mapping) or set(probabilities) != set(options):
        raise ValueError("all-market probabilities use invalid options")
    if not isinstance(prizes, Mapping) or set(prizes) != set(options):
        raise ValueError("all-market prizes use invalid options")
    if any(not isinstance(value, Decimal) or not value.is_finite() or value < 0 for value in probabilities.values()):
        raise ValueError("all-market probabilities are invalid")
    # JSONL serialization sorts mapping keys, so preserve the declared market
    # order when summing high-precision Decimal probabilities.
    if sum((probabilities[option] for option in options), Decimal(0)) != Decimal(1):
        raise ValueError("all-market probabilities must sum to one")
    if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0 for value in prizes.values()):
        raise ValueError("all-market prizes are invalid")
    selected = entry["selected_option"]
    selected_probability = entry["selected_probability"]
    tied = entry["tied_options"]
    if selected not in options or not isinstance(selected_probability, Decimal) or selected_probability != probabilities[selected]:
        raise ValueError("all-market selected option is invalid")
    if not isinstance(tied, list) or not tied or any(option not in options for option in tied):
        raise ValueError("all-market tied options are invalid")
    maximum = max(probabilities.values())
    if selected_probability != maximum or tied != [option for option in options if probabilities[option] == maximum] or selected != tied[0]:
        raise ValueError("all-market selection does not match probabilities")
    if code == "hhad":
        handicap = entry["home_handicap"]
        if not isinstance(handicap, Decimal) or not handicap.is_finite() or handicap != handicap.to_integral_value():
            raise ValueError("home handicap must be a whole-number Decimal")
    if schema_version in {2, 3}:
        published_at = _parse_timestamp(entry["published_at"], "market published_at")
        if published_at >= kickoff_at:
            raise ValueError("market published_at must be before kickoff_at")
        if not _is_url(entry["official_source_url"]):
            raise ValueError("market official_source_url is invalid")


def _validate_all_market_snapshot(snapshot):
    required = {"prediction_hash", "match_id", "kickoff_at", "captured_at", "official_source_url", "markets"}
    if not isinstance(snapshot, Mapping):
        raise ValueError("all-market snapshot has invalid fields")
    schema_version = snapshot.get("schema_version", 1)
    expected_fields = required | (
        {"schema_version", "snapshot_strategy"}
        if schema_version == 3
        else {"schema_version"} if schema_version == 2 else set()
    )
    if schema_version not in {1, 2, 3} or set(snapshot) != expected_fields:
        raise ValueError("all-market snapshot has invalid fields")
    if schema_version == 3 and snapshot["snapshot_strategy"] != "official_market_baseline":
        raise ValueError("all-market snapshot strategy is invalid")
    if not isinstance(snapshot["prediction_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot["prediction_hash"]):
        raise ValueError("all-market prediction_hash is invalid")
    if not _is_valid_match_id(snapshot["match_id"]):
        raise ValueError("all-market match_id is invalid")
    kickoff_at = _parse_timestamp(snapshot["kickoff_at"], "kickoff_at")
    captured_at = _parse_timestamp(snapshot["captured_at"], "captured_at")
    if captured_at >= kickoff_at:
        raise ValueError("all-market captured_at must be before kickoff_at")
    if not _is_url(snapshot["official_source_url"]):
        raise ValueError("all-market official_source_url is invalid")
    markets = snapshot["markets"]
    if not isinstance(markets, Mapping) or set(markets) != set(ALL_MARKET_OPTIONS):
        raise ValueError("all-market snapshot must include all official markets")
    for code, entry in markets.items():
        _validate_all_market_frozen_entry(code, entry, kickoff_at, schema_version)


def freeze_all_market_snapshot(snapshot):
    _validate_all_market_snapshot(snapshot)
    record = copy.deepcopy(dict(snapshot))
    record["record_type"] = "all_market_snapshot"
    record["status"] = "frozen"
    record["content_hash"] = _content_hash(record)
    return record


def verify_all_market_snapshot(record):
    if not isinstance(record, Mapping) or record.get("record_type") != "all_market_snapshot" or record.get("status") != "frozen":
        raise ValueError("record is not a frozen all-market snapshot")
    _verify_content_hash(record)
    snapshot = dict(record)
    for field in ("record_type", "status", "content_hash"):
        snapshot.pop(field)
    _validate_all_market_snapshot(snapshot)
    return record


def _validate_all_market_result(snapshot, result):
    if not isinstance(result, Mapping) or set(result) != {"official_result_url", "final_score", "markets"}:
        raise ValueError("all-market result has invalid fields")
    if not _is_url(result["official_result_url"]):
        raise ValueError("all-market official result URL is invalid")
    _validated_final_score(result["final_score"])
    expected = {code for code, entry in snapshot["markets"].items() if entry["status"] == "frozen"}
    if not isinstance(result["markets"], Mapping) or set(result["markets"]) != expected:
        raise ValueError("all-market result must cover frozen markets exactly")
    for code, option in result["markets"].items():
        if option not in _all_market_options(code):
            raise ValueError("all-market result option is invalid")


def _market_hits(markets, actual_markets):
    return {
        code: {
            "selected_option": entry["selected_option"],
            "actual_option": actual_markets[code],
            "hit": entry["selected_option"] == actual_markets[code],
        }
        for code, entry in markets.items()
        if entry["status"] == "frozen"
    }


def settle_all_market_snapshot(snapshot, result):
    verify_all_market_snapshot(snapshot)
    _validate_all_market_result(snapshot, result)
    record = {
        "record_type": "all_market_settlement",
        "status": "settled",
        "snapshot_hash": snapshot["content_hash"],
        "official_result_url": result["official_result_url"],
        "final_score": _validated_final_score(result["final_score"]),
        "markets": _market_hits(snapshot["markets"], result["markets"]),
    }
    record["content_hash"] = _content_hash(record)
    return record


def verify_all_market_settlement(record):
    required = {"record_type", "status", "snapshot_hash", "official_result_url", "final_score", "markets", "content_hash"}
    if not isinstance(record, Mapping) or set(record) != required or record.get("record_type") != "all_market_settlement" or record.get("status") != "settled":
        raise ValueError("record is not an all-market settlement")
    if not isinstance(record["snapshot_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["snapshot_hash"]):
        raise ValueError("all-market snapshot_hash is invalid")
    if not _is_url(record["official_result_url"]):
        raise ValueError("all-market official result URL is invalid")
    _validated_final_score(record["final_score"])
    if not isinstance(record["markets"], Mapping) or not record["markets"]:
        raise ValueError("all-market settlement markets are invalid")
    for code, entry in record["markets"].items():
        options = _all_market_options(code)
        if not isinstance(entry, Mapping) or set(entry) != {"selected_option", "actual_option", "hit"}:
            raise ValueError("all-market settlement entry is invalid")
        if entry["selected_option"] not in options or entry["actual_option"] not in options or not isinstance(entry["hit"], bool) or entry["hit"] != (entry["selected_option"] == entry["actual_option"]):
            raise ValueError("all-market settlement result is invalid")
    _verify_content_hash(record)
    return record


def _validated_final_score(final_score):
    if not isinstance(final_score, Mapping) or set(final_score) != {"home", "away"}:
        raise ValueError("final_score must contain integer home and away goals")
    if any(
        isinstance(goals, bool) or not isinstance(goals, int) or goals < 0
        for goals in final_score.values()
    ):
        raise ValueError("final_score must contain integer home and away goals")
    return {"home": final_score["home"], "away": final_score["away"]}


def settle_prediction(frozen_record, actual_outcome, official_result_url, final_score=None):
    """Return a settlement record linked to an unchanged frozen prediction."""
    if not isinstance(frozen_record, Mapping):
        raise ValueError("frozen_record must be a mapping")
    if frozen_record.get("record_type") != "prediction" or frozen_record.get("status") != "frozen":
        raise ValueError("settlements require a frozen prediction record")
    if "actual_outcome" in frozen_record:
        raise ValueError("frozen predictions cannot contain an actual_outcome")
    _verify_content_hash(frozen_record)

    if actual_outcome not in OUTCOME_LABELS:
        raise ValueError("actual_outcome must be home_win, draw, or away_win")
    if not _is_url(official_result_url):
        raise ValueError("official_result_url must be an HTTP(S) URL")

    record = {
        "record_type": "settlement",
        "status": "settled",
        "prediction_hash": frozen_record["content_hash"],
        "actual_outcome": actual_outcome,
        "official_result_url": official_result_url,
    }
    if final_score is not None:
        record["final_score"] = _validated_final_score(final_score)
    record["content_hash"] = _content_hash(record)
    return record


def verify_settlement(record):
    """Validate a hashed settlement before it can affect calibration."""
    if not isinstance(record, Mapping):
        raise ValueError("settlement record must be a mapping")
    required_fields = {
        "record_type",
        "status",
        "prediction_hash",
        "actual_outcome",
        "official_result_url",
        "content_hash",
    }
    if not required_fields.issubset(record) or not set(record).issubset(
        required_fields | {"final_score"}
    ):
        raise ValueError("settlement record has invalid fields")
    if record.get("record_type") != "settlement" or record.get("status") != "settled":
        raise ValueError("record is not a settled record")
    if not isinstance(record["prediction_hash"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", record["prediction_hash"]
    ):
        raise ValueError("settlement prediction_hash is invalid")
    if record["actual_outcome"] not in OUTCOME_LABELS:
        raise ValueError("settlement actual_outcome is invalid")
    if not _is_url(record["official_result_url"]):
        raise ValueError("settlement official_result_url must be an HTTP(S) URL")
    if "final_score" in record:
        _validated_final_score(record["final_score"])
    _verify_content_hash(record)
    return record


def _settlement_hash(value, field_name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field_name} is invalid")
    return value


def _official_result_identity(identity):
    required_fields = {"match_id", "match_num", "match_date", "home_team", "away_team"}
    if not isinstance(identity, Mapping) or set(identity) != required_fields:
        raise ValueError("official_result_identity has invalid fields")
    if isinstance(identity["match_id"], bool) or not str(identity["match_id"]).strip():
        raise ValueError("official_result_identity match_id is invalid")
    if not isinstance(identity["match_num"], str) or not WEEKDAY_SEQUENCE_MATCH_ID.fullmatch(identity["match_num"]):
        raise ValueError("official_result_identity match_num is invalid")
    if any(
        not isinstance(identity[field], str) or not identity[field].strip()
        for field in ("home_team", "away_team")
    ):
        raise ValueError("official_result_identity teams are invalid")
    try:
        datetime.strptime(identity["match_date"], "%Y-%m-%d")
    except (TypeError, ValueError) as error:
        raise ValueError("official_result_identity match_date is invalid") from error
    return dict(identity)


def invalidate_settlement(settlement_record, payload):
    """Append-only correction for a settlement linked to the wrong official fixture."""
    verify_settlement(settlement_record)
    required_fields = {
        "settlement_hash",
        "prediction_hash",
        "invalidated_at",
        "reason_code",
        "reason",
        "official_result_url",
        "official_result_identity",
    }
    if not isinstance(payload, Mapping) or set(payload) != required_fields:
        raise ValueError("settlement invalidation input has invalid fields")
    settlement_hash = _settlement_hash(payload["settlement_hash"], "settlement_hash")
    prediction_hash = _settlement_hash(payload["prediction_hash"], "prediction_hash")
    if settlement_hash != settlement_record["content_hash"]:
        raise ValueError("settlement_hash does not match the supplied settlement")
    if prediction_hash != settlement_record["prediction_hash"]:
        raise ValueError("prediction_hash does not match the supplied settlement")
    _parse_timestamp(payload["invalidated_at"], "invalidated_at")
    if payload["reason_code"] not in SETTLEMENT_INVALIDATION_REASONS:
        raise ValueError("settlement invalidation reason_code is invalid")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise ValueError("settlement invalidation reason is invalid")
    if not _is_url(payload["official_result_url"]):
        raise ValueError("settlement invalidation official_result_url must be an HTTP(S) URL")
    identity = _official_result_identity(payload["official_result_identity"])
    record = {
        "record_type": "settlement_invalidation",
        "status": "invalidated",
        "settlement_hash": settlement_hash,
        "prediction_hash": prediction_hash,
        "invalidated_at": payload["invalidated_at"],
        "reason_code": payload["reason_code"],
        "reason": payload["reason"],
        "official_result_url": payload["official_result_url"],
        "official_result_identity": identity,
    }
    record["content_hash"] = _content_hash(record)
    return record


def verify_settlement_invalidation(record):
    required_fields = {
        "record_type",
        "status",
        "settlement_hash",
        "prediction_hash",
        "invalidated_at",
        "reason_code",
        "reason",
        "official_result_url",
        "official_result_identity",
        "content_hash",
    }
    if not isinstance(record, Mapping) or set(record) != required_fields:
        raise ValueError("settlement invalidation record has invalid fields")
    if record.get("record_type") != "settlement_invalidation" or record.get("status") != "invalidated":
        raise ValueError("record is not a settlement invalidation")
    _settlement_hash(record["settlement_hash"], "settlement_hash")
    _settlement_hash(record["prediction_hash"], "prediction_hash")
    _parse_timestamp(record["invalidated_at"], "invalidated_at")
    if record["reason_code"] not in SETTLEMENT_INVALIDATION_REASONS:
        raise ValueError("settlement invalidation reason_code is invalid")
    if not isinstance(record["reason"], str) or not record["reason"].strip():
        raise ValueError("settlement invalidation reason is invalid")
    if not _is_url(record["official_result_url"]):
        raise ValueError("settlement invalidation official_result_url must be an HTTP(S) URL")
    _official_result_identity(record["official_result_identity"])
    _verify_content_hash(record)
    return record
