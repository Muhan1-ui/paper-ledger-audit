"""Offline command line interface for the paper-only calibration tracker."""

import argparse
import getpass
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

# Bookmaker whose price is treated as the sharp anchor. Named here rather than
# inline because the odds-refresh stage and the CLV maths must agree on it.
CROWN_BOOKMAKER = "皇冠"

from .ledger import (
    OUTCOME_LABELS,
    ALL_MARKET_OPTIONS,
    freeze_all_market_snapshot,
    freeze_evidence_event,
    freeze_odds_capture,
    freeze_prediction,
    invalidate_settlement,
    settle_all_market_snapshot,
    verify_all_market_settlement,
    verify_all_market_snapshot,
    verify_evidence_event,
    settle_prediction,
    verify_odds_capture,
    verify_prediction,
    verify_settlement,
    verify_settlement_invalidation,
)
from .fotmob import (
    FOTMOB_SEARCH_ALIASES,
    FotMobSearchResolutionError,
    collect_match_xg_evidence,
    collect_team_search_candidates,
    collect_team_stats,
    discover_team_stats_url,
)
from .context import collect_pregame_context
from .context import validate_pregame_context
from .context_resolver import resolve_context_event
from .daily_slip import (
    DEFAULT_LEDGER_PATH as DEFAULT_DAILY_SLIP_LEDGER_PATH,
    build_daily_slip,
    format_daily_slip,
)
from .model import blend_probabilities, normalize_fixed_prizes, poisson_score_matrix
from .markets import derive_official_markets, select_top_option
from .football433 import collect_433, discover_433_detail_urls
from .pregame import (
    DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    UNDERSTAT_LEAGUES,
    _understat_batch_xg,
    collect_auto_pregame_bundle,
    collect_pregame_bundle,
    quality_gate,
)
from .report import calibration_report
from .deepseek_review import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DeepSeekReviewClient,
    save_local_configuration,
)
from .service_config import save_database_configuration
from .service_store import create_configured_store
from .review_agent import build_review, format_review
from .resolver import (
    adopted_source_aliases,
    cached_fotmob_team_identity,
    cached_wikidata_team_identity,
    collect_candidate_template,
    cached_source_links,
    format_workflow_next,
    link_summary,
    import_manual_understat_aliases,
    import_reviewed_fotmob_search_aliases,
    record_adopted_aliases,
    record_fotmob_team_identity,
    record_source_links,
    record_wikidata_team_identity,
    reviewed_fotmob_search_aliases,
    resolve_links,
    workflow_next,
    understat_source_aliases,
)
from .sporttery import collect_fixed_bonus, collect_results, collect_schedule
from .understat import collect_league_xg, collect_league_xg_batch
from .wikidata import resolve_team_identity
from .xgscore import collect_xgscore, discover_xgscore_event_urls
from .transport import source_access_report
from .sales_cutoff import assess_freeze_timing, load_policy


DEFAULT_SALES_CUTOFF_POLICY_PATH = "work/sales-cutoff-policy.json"
DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH = (
    Path(__file__).resolve().parent / "data" / "historical_fotmob_prewarm_progress.json"
)
DEFAULT_FOTMOB_SEARCH_ALIAS_REGISTRY_PATH = (
    Path(__file__).resolve().parent / "data" / "fotmob_search_aliases.json"
)
DEFAULT_HISTORICAL_FOTMOB_ALIAS_CANDIDATE_QUEUE_PATH = (
    Path(__file__).resolve().parent / "data" / "historical_fotmob_alias_candidates.json"
)
DEFAULT_DAILY_FOTMOB_IDENTITY_PROGRESS_PATH = (
    Path(__file__).resolve().parent / "data" / "daily_fotmob_identity_progress.json"
)
HISTORICAL_WIKIDATA_IDENTITY_STRATEGY_VERSION = 2
HISTORICAL_WIKIDATA_IDENTITY_ATTEMPT_BUDGET = 3


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _wikidata_rate_limit_retry_at(error, now):
    """Honor a public source's Retry-After header, with a conservative fallback."""
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if isinstance(retry_after, str) and retry_after.strip().isdigit():
        return now + timedelta(seconds=max(60, int(retry_after.strip())))
    if isinstance(retry_after, str) and retry_after.strip():
        try:
            retry_at = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError, IndexError):
            retry_at = None
        if retry_at is not None:
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(retry_at.astimezone(now.tzinfo), now + timedelta(minutes=5))
    return now + timedelta(hours=1)


def _decimal(value, field_name):
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a finite decimal value") from error
    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal value")
    return decimal_value


def _is_http_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _timestamp(value, field_name):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if timestamp.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return timestamp


def _historical_date(value, field_name):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field_name} must use YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError(f"{field_name} must use YYYY-MM-DD") from error


def _load_historical_fotmob_prewarm_progress(progress_path):
    path = Path(progress_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("historical FotMob prewarm progress file is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("historical FotMob prewarm progress file is invalid")
    return payload


def _write_historical_fotmob_prewarm_progress(progress_path, payload):
    path = Path(progress_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_json_atomically(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _historical_fotmob_official_reference(match, scan_date, official_result_url):
    """Keep only source facts returned with one historical official match."""
    if not isinstance(official_result_url, str) or not official_result_url:
        return None
    reference = {
        "match_date": scan_date,
        "official_result_url": official_result_url,
    }
    for field in ("match_id", "match_num", "kickoff_at"):
        value = match.get(field)
        if isinstance(value, (str, int)) and str(value).strip():
            reference[field] = value
    return reference


def _merge_historical_fotmob_references(existing, additions):
    existing = existing if isinstance(existing, list) else []
    additions = additions if isinstance(additions, list) else []
    merged = []
    seen = set()
    for reference in [*existing, *additions]:
        if not isinstance(reference, Mapping):
            continue
        encoded = json.dumps(reference, ensure_ascii=False, sort_keys=True, default=str)
        if encoded not in seen:
            seen.add(encoded)
            merged.append(dict(reference))
    return merged


def _contains_cjk(value):
    return isinstance(value, str) and re.search(r"[\u3400-\u9fff]", value) is not None


def _historical_fotmob_failure(error, search_name):
    """Classify a failed public lookup without treating it as an identity fact."""
    if isinstance(error, FotMobSearchResolutionError):
        failure_code = error.failure_code
    elif isinstance(error, OSError):
        status_code = getattr(error, "code", None)
        failure_code = "access_restricted" if status_code in {401, 403, 429} else "network_error"
    elif isinstance(error, (json.JSONDecodeError, KeyError, TypeError)):
        failure_code = "parse_error"
    else:
        failure_code = "other"
    if failure_code in {"no_public_candidates", "identity_mismatch"} and _contains_cjk(search_name):
        category = "search_alias_gap_suspected"
        remediation = "review_search_alias"
    elif failure_code == "no_public_candidates":
        category = "no_public_candidates_after_source_query"
        remediation = "check_source_coverage"
    elif failure_code == "identity_mismatch":
        category = "strict_identity_mismatch"
        remediation = "review_search_candidates"
    elif failure_code == "ambiguous_identity":
        category = "strict_identity_ambiguous"
        remediation = "review_search_candidates"
    elif failure_code == "access_restricted":
        category = "public_access_restricted"
        remediation = "wait_for_public_access"
    elif failure_code == "network_error":
        category = "transport_error"
        remediation = "retry_later"
    elif failure_code == "parse_error":
        category = "public_response_parse_error"
        remediation = "inspect_public_response"
    else:
        category = "unclassified"
        remediation = "inspect_failure"
    return {
        "failure_code": failure_code,
        "failure_category": category,
        "remediation": remediation,
        "reason": str(error),
    }


def _xg_evidence(evidence, captured_at):
    common_fields = {
        "source_url",
        "competition",
        "season",
        "scope",
        "home_attack_xg",
        "home_defense_xga",
        "away_attack_xg",
        "away_defense_xga",
        "formula",
    }
    publication_fields = {"published_at", "published_at_unavailable_reason", "captured_at"}
    legacy_sample_fields = {"sample_matches"}
    split_sample_fields = {"home_sample_matches", "away_sample_matches"}
    rolling_fields = {
        "sample_window_matches",
        "history_cutoff_date",
        "home_sample_first_date",
        "home_sample_last_date",
        "away_sample_first_date",
        "away_sample_last_date",
    }
    season_bridge_fields = {
        "current_season", "prior_season", "current_weight", "prior_weight",
    }
    supplied_sample_fields = set(evidence) & (legacy_sample_fields | split_sample_fields) if isinstance(evidence, Mapping) else set()
    supplied_rolling_fields = set(evidence) & rolling_fields if isinstance(evidence, Mapping) else set()
    supplied_publication_fields = set(evidence) & publication_fields if isinstance(evidence, Mapping) else set()
    supplied_season_bridge_fields = set(evidence) & season_bridge_fields if isinstance(evidence, Mapping) else set()
    if (
        not isinstance(evidence, Mapping)
        or not common_fields.issubset(evidence)
        or supplied_sample_fields not in (legacy_sample_fields, split_sample_fields)
        or supplied_rolling_fields not in (set(), rolling_fields)
        or supplied_season_bridge_fields not in (set(), season_bridge_fields)
        or set(evidence) != common_fields | supplied_sample_fields | supplied_rolling_fields | supplied_publication_fields | supplied_season_bridge_fields
        or supplied_publication_fields not in ({"published_at"}, {"published_at_unavailable_reason", "captured_at"})
    ):
        raise ValueError("xg_inputs evidence has invalid fields")
    if not _is_http_url(evidence["source_url"]):
        raise ValueError("xg_inputs evidence source_url must be an HTTP(S) URL")
    if "captured_at" in evidence:
        evidence_captured_at = _timestamp(evidence["captured_at"], "xg_inputs evidence captured_at")
        if captured_at is not None and evidence_captured_at > _timestamp(captured_at, "captured_at"):
            raise ValueError("xg_inputs evidence cannot postdate captured_at")
    if "published_at" in evidence:
        published_at = _timestamp(evidence["published_at"], "xg_inputs evidence published_at")
        if captured_at is not None and published_at > _timestamp(captured_at, "captured_at"):
            raise ValueError("xg_inputs evidence cannot postdate captured_at")
    else:
        if "captured_at" not in evidence:
            raise ValueError("xg_inputs evidence without published_at requires captured_at")
        if not isinstance(evidence["published_at_unavailable_reason"], str) or not evidence["published_at_unavailable_reason"].strip():
            raise ValueError("xg_inputs evidence publication-time reason must be non-empty")
    if any(
        not isinstance(evidence[field], str) or not evidence[field].strip()
        for field in ("competition", "season", "formula")
    ):
        raise ValueError("xg_inputs evidence text fields must be non-empty")
    for field in supplied_sample_fields:
        sample_matches = evidence[field]
        if isinstance(sample_matches, bool) or not isinstance(sample_matches, int) or sample_matches <= 0:
            raise ValueError(f"xg_inputs evidence {field} must be a positive integer")
    if supplied_rolling_fields:
        if supplied_sample_fields != split_sample_fields:
            raise ValueError("rolling xg evidence requires home/away sample counts")
        window = evidence["sample_window_matches"]
        if isinstance(window, bool) or not isinstance(window, int) or window < 1:
            raise ValueError("xg_inputs evidence sample_window_matches must be a positive integer")
        if window < evidence["home_sample_matches"] or window < evidence["away_sample_matches"]:
            raise ValueError("xg_inputs evidence samples cannot exceed sample_window_matches")
        parsed_dates = {}
        for field in rolling_fields - {"sample_window_matches"}:
            value = evidence[field]
            if not isinstance(value, str):
                raise ValueError(f"xg_inputs evidence {field} must be an ISO date")
            try:
                parsed_dates[field] = datetime.fromisoformat(value).date()
            except ValueError as error:
                raise ValueError(f"xg_inputs evidence {field} must be an ISO date") from error
        if (
            parsed_dates["home_sample_first_date"] > parsed_dates["home_sample_last_date"]
            or parsed_dates["away_sample_first_date"] > parsed_dates["away_sample_last_date"]
            or parsed_dates["home_sample_last_date"] >= parsed_dates["history_cutoff_date"]
            or parsed_dates["away_sample_last_date"] >= parsed_dates["history_cutoff_date"]
        ):
            raise ValueError("xg_inputs evidence rolling dates are inconsistent")
    if evidence["scope"] not in {"home_away_split", "league_total", "source_match_model"}:
        raise ValueError("xg_inputs evidence scope is invalid")
    result = {
        "source_url": evidence["source_url"],
        "competition": evidence["competition"].strip(),
        "season": evidence["season"].strip(),
        "scope": evidence["scope"],
        "formula": evidence["formula"].strip(),
    }
    if supplied_season_bridge_fields:
        common_source_fields = {
            "captured_at", "competition", "season",
            "home_team_id", "home_team_name", "home_sample_matches", "home_attack_xg", "home_defense_xga",
            "away_team_id", "away_team_name", "away_sample_matches", "away_attack_xg", "away_defense_xga",
        }
        expected_understat_source_fields = common_source_fields | {
            "source_url", "captured_at", "raw_content_sha256", "competition", "season",
        }
        expected_fotmob_source_fields = common_source_fields | {
            "published_at", "source_urls", "raw_content_sha256",
        }
        fotmob_stat_keys = {
            "home_expected_goals_team", "home_expected_goals_conceded_team",
            "away_expected_goals_team", "away_expected_goals_conceded_team",
        }
        bridge = {}
        for name in ("current_season", "prior_season"):
            source = evidence[name]
            if not isinstance(source, Mapping) or frozenset(source) not in {
                frozenset(expected_understat_source_fields), frozenset(expected_fotmob_source_fields),
            }:
                raise ValueError(f"xg_inputs evidence {name} has invalid fields")
            is_fotmob_source = set(source) == expected_fotmob_source_fields
            if is_fotmob_source:
                source_urls = source["source_urls"]
                raw_hashes = source["raw_content_sha256"]
                if not isinstance(source_urls, Mapping) or set(source_urls) != fotmob_stat_keys or not all(
                    _is_http_url(url) and url.startswith("https://data.fotmob.com/")
                    for url in source_urls.values()
                ):
                    raise ValueError(f"xg_inputs evidence {name} source_urls are invalid")
                if not isinstance(raw_hashes, Mapping) or set(raw_hashes) != fotmob_stat_keys or not all(
                    isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in raw_hashes.values()
                ):
                    raise ValueError(f"xg_inputs evidence {name} raw_content_sha256 is invalid")
                source_published_at = _timestamp(source["published_at"], f"xg_inputs evidence {name} published_at")
                if captured_at is not None and source_published_at > _timestamp(captured_at, "captured_at"):
                    raise ValueError(f"xg_inputs evidence {name} cannot postdate captured_at")
            elif not _is_http_url(source["source_url"]):
                raise ValueError(f"xg_inputs evidence {name} source_url must be an HTTP(S) URL")
            source_capture = _timestamp(source["captured_at"], f"xg_inputs evidence {name} captured_at")
            if captured_at is not None and source_capture > _timestamp(captured_at, "captured_at"):
                raise ValueError(f"xg_inputs evidence {name} cannot postdate captured_at")
            if not is_fotmob_source and (
                not isinstance(source["raw_content_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", source["raw_content_sha256"])
            ):
                raise ValueError(f"xg_inputs evidence {name} raw_content_sha256 is invalid")
            for field in ("competition", "season", "home_team_id", "home_team_name", "away_team_id", "away_team_name"):
                if not isinstance(source[field], str) or not source[field].strip():
                    raise ValueError(f"xg_inputs evidence {name} {field} must be non-empty")
            for field in ("home_sample_matches", "away_sample_matches"):
                if isinstance(source[field], bool) or not isinstance(source[field], int) or source[field] <= 0:
                    raise ValueError(f"xg_inputs evidence {name} {field} must be a positive integer")
            for field in ("home_attack_xg", "home_defense_xga", "away_attack_xg", "away_defense_xga"):
                if _decimal(source[field], f"xg_inputs evidence {name} {field}") < 0:
                    raise ValueError(f"xg_inputs evidence {name} values must be non-negative")
            bridge[name] = dict(source)
        if bridge["current_season"]["competition"] != bridge["prior_season"]["competition"]:
            raise ValueError("xg_inputs evidence season bridge competition changed")
        if (
            bridge["current_season"]["home_team_name"] != bridge["prior_season"]["home_team_name"]
            or bridge["current_season"]["away_team_name"] != bridge["prior_season"]["away_team_name"]
        ):
            raise ValueError("xg_inputs evidence season bridge team identity changed")
        if min(bridge["current_season"]["home_sample_matches"], bridge["current_season"]["away_sample_matches"]) < 2:
            raise ValueError("xg_inputs evidence season bridge current sample is below two matches")
        if min(bridge["prior_season"]["home_sample_matches"], bridge["prior_season"]["away_sample_matches"]) < 12:
            raise ValueError("xg_inputs evidence season bridge prior sample is below twelve matches")
        weights = {
            "current_weight": _decimal(evidence["current_weight"], "xg_inputs evidence current_weight"),
            "prior_weight": _decimal(evidence["prior_weight"], "xg_inputs evidence prior_weight"),
        }
        if any(value < 0 or value > 1 for value in weights.values()) or sum(weights.values(), Decimal(0)) != Decimal(1):
            raise ValueError("xg_inputs evidence season bridge weights must be unit probabilities")
        result.update(bridge)
        result.update(weights)
    for field in supplied_sample_fields:
        result[field] = evidence[field]
    if supplied_rolling_fields:
        for field in rolling_fields:
            result[field] = evidence[field]
    if "published_at" in evidence:
        result["published_at"] = evidence["published_at"]
    else:
        result["captured_at"] = evidence["captured_at"]
        result["published_at_unavailable_reason"] = evidence["published_at_unavailable_reason"].strip()
    for field in (
        "home_attack_xg",
        "home_defense_xga",
        "away_attack_xg",
        "away_defense_xga",
    ):
        value = _decimal(evidence[field], f"xg_inputs evidence {field}")
        if value < 0:
            raise ValueError("xg_inputs evidence values must be non-negative")
        result[field] = value
    return result


def _optional_xg(xg_inputs, captured_at=None):
    if xg_inputs is None:
        return None
    required_fields = {
        "source_url",
        "home_xg",
        "away_xg",
    }
    enhanced_fields = required_fields | {"model_version", "model_weight"}
    enhanced_handicap_fields = enhanced_fields | {"home_handicap"}
    enhanced_evidence_fields = enhanced_fields | {"evidence"}
    enhanced_handicap_evidence_fields = enhanced_handicap_fields | {"evidence"}
    enhanced_evidence_confidence_fields = enhanced_evidence_fields | {"confidence_ceiling"}
    enhanced_handicap_evidence_confidence_fields = (
        enhanced_handicap_evidence_fields | {"confidence_ceiling"}
    )
    if not isinstance(xg_inputs, Mapping) or frozenset(xg_inputs) not in {
        frozenset(required_fields),
        frozenset(enhanced_fields),
        frozenset(enhanced_handicap_fields),
        frozenset(enhanced_evidence_fields),
        frozenset(enhanced_handicap_evidence_fields),
        frozenset(enhanced_evidence_confidence_fields),
        frozenset(enhanced_handicap_evidence_confidence_fields),
    }:
        raise ValueError("xg_inputs must contain source_url, home_xg, and away_xg")
    if not _is_http_url(xg_inputs["source_url"]):
        raise ValueError("xg_inputs source_url must be an HTTP(S) URL")

    home_xg = _decimal(xg_inputs["home_xg"], "xg_inputs home_xg")
    away_xg = _decimal(xg_inputs["away_xg"], "xg_inputs away_xg")
    if home_xg < 0 or away_xg < 0:
        raise ValueError("xg_inputs values must be non-negative")
    result = {
        "source_url": xg_inputs["source_url"],
        "home_xg": home_xg,
        "away_xg": away_xg,
    }
    if "model_version" in xg_inputs:
        model_version = xg_inputs["model_version"]
        if model_version != "poisson-v1":
            raise ValueError("xg_inputs model_version must be poisson-v1")
        model_weight = _decimal(xg_inputs["model_weight"], "xg_inputs model_weight")
        if model_weight < 0 or model_weight > 1:
            raise ValueError("xg_inputs model_weight must be from zero to one")
        result["model_version"] = model_version
        result["model_weight"] = model_weight
        if "home_handicap" in xg_inputs:
            home_handicap = _decimal(xg_inputs["home_handicap"], "xg_inputs home_handicap")
            if home_handicap != home_handicap.to_integral_value():
                raise ValueError("xg_inputs home_handicap must be a whole number")
            result["home_handicap"] = home_handicap
        if "evidence" in xg_inputs:
            evidence = _xg_evidence(xg_inputs["evidence"], captured_at)
            result["evidence"] = evidence
            expected_confidence_ceiling = {
                "league_total": "medium",
                "home_away_split": "high",
                "source_match_model": "medium",
            }.get(evidence["scope"])
            if "current_season" in evidence:
                expected_confidence_ceiling = "medium"
            if "confidence_ceiling" in xg_inputs and (
                xg_inputs["confidence_ceiling"] != expected_confidence_ceiling
            ):
                raise ValueError(
                    "xg_inputs confidence_ceiling does not match evidence scope"
                )
            if "confidence_ceiling" in xg_inputs:
                result["confidence_ceiling"] = expected_confidence_ceiling
            elif evidence["scope"] in {"league_total", "source_match_model"}:
                result["confidence_ceiling"] = "medium"
    return result


def _optional_match_context(match_context):
    if match_context is None:
        return None
    required_fields = {"competition", "home_team", "away_team"}
    if not isinstance(match_context, Mapping) or set(match_context) != required_fields:
        raise ValueError("match_context must contain competition, home_team, and away_team")
    if any(
        not isinstance(value, str) or not value.strip()
        for value in match_context.values()
    ):
        raise ValueError("match_context values must be non-empty strings")
    return {field: match_context[field].strip() for field in required_fields}


def _odds(odds):
    if not isinstance(odds, Mapping) or set(odds) != set(OUTCOME_LABELS):
        raise ValueError("odds must contain home_win, draw, and away_win")
    return {label: _decimal(odds[label], f"odds {label}") for label in OUTCOME_LABELS}


def _prize_mapping(prizes, option_order, field_name):
    if not isinstance(prizes, Mapping) or set(prizes) != set(option_order):
        raise ValueError(f"{field_name} must cover exactly the declared options")
    values = {
        option: _decimal(prizes[option], f"{field_name} {option}")
        for option in option_order
    }
    if any(value <= 0 for value in values.values()):
        raise ValueError(f"{field_name} values must be positive")
    return values


def _resolve_european_odds(payload, odds_captures):
    european_odds = payload.get("european_odds")
    if not isinstance(european_odds, Mapping) or set(european_odds) != {"capture_hash"}:
        raise ValueError("european_odds must contain exactly capture_hash")
    capture_hash = european_odds["capture_hash"]
    if not isinstance(capture_hash, str):
        raise ValueError("european_odds capture_hash must be a string")
    capture = next(
        (record for record in odds_captures if record.get("content_hash") == capture_hash),
        None,
    )
    if capture is None:
        raise ValueError("european_odds capture_hash was not found in the ledger")
    if capture.get("source_quality") != "named_bookmaker":
        raise ValueError("european_odds capture must have named_bookmaker quality")
    if capture.get("match_id") != payload.get("match_id"):
        raise ValueError("european_odds capture match_id must match snapshot match_id")

    source_urls = payload.get("source_urls")
    if not isinstance(source_urls, Mapping) or source_urls.get("S2") != capture.get("source_url"):
        raise ValueError("snapshot S2 must match european_odds capture source_url")
    try:
        capture_time = datetime.fromisoformat(capture["captured_at"])
        snapshot_time = datetime.fromisoformat(payload["captured_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("european_odds timestamps must be ISO-8601") from error
    if capture_time.tzinfo is None or snapshot_time.tzinfo is None:
        raise ValueError("european_odds timestamps must include a timezone")
    if capture_time > snapshot_time:
        raise ValueError("european_odds capture cannot postdate snapshot capture")
    return capture


def prediction_from_input(payload, odds_captures=()):
    """Build an auditable prediction with optional imported European odds."""
    if not isinstance(payload, Mapping):
        raise ValueError("snapshot input must be a JSON object")

    fixed_prizes = payload.get("fixed_prizes")
    if not isinstance(fixed_prizes, Mapping) or set(fixed_prizes) != set(OUTCOME_LABELS):
        raise ValueError("fixed_prizes must contain home_win, draw, and away_win")
    prizes = [_decimal(fixed_prizes[label], f"fixed_prizes {label}") for label in OUTCOME_LABELS]
    european_capture = None
    if "european_odds" in payload:
        european_capture = _resolve_european_odds(payload, odds_captures)
        market_probabilities = normalize_fixed_prizes(
            [european_capture["odds"][label] for label in OUTCOME_LABELS]
        )
    else:
        market_probabilities = normalize_fixed_prizes(prizes)

    xg_inputs = _optional_xg(payload.get("xg_inputs"), payload.get("captured_at"))
    model = None
    probabilities = market_probabilities
    if xg_inputs is not None and "model_version" in xg_inputs:
        if european_capture is None and xg_inputs["model_weight"] != 0:
            raise ValueError("non-zero xG model weight requires a named-bookmaker baseline")
        score_distribution = poisson_score_matrix(
            xg_inputs["home_xg"],
            xg_inputs["away_xg"],
            home_handicap=xg_inputs.get("home_handicap"),
        )
        market_outcomes = dict(zip(OUTCOME_LABELS, market_probabilities))
        blended_outcomes = blend_probabilities(
            market_outcomes,
            score_distribution["one_x_two"],
            xg_inputs["model_weight"],
        )
        probabilities = [blended_outcomes[label] for label in OUTCOME_LABELS]
        model = {
            "version": xg_inputs["model_version"],
            "weight": xg_inputs["model_weight"],
            "market_outcomes": market_outcomes,
            "model_outcomes": score_distribution["one_x_two"],
            "blended_outcomes": blended_outcomes,
            "scorelines": score_distribution["scorelines"],
            "top_scores": score_distribution["top_scores"],
            "total_goals": score_distribution["total_goals"],
            "residual_mass": score_distribution["residual_mass"],
            "half_time_full_time": score_distribution["half_time_full_time"],
            "half_time_full_time_proxy": score_distribution["half_time_full_time_proxy"],
        }
        if "handicap_one_x_two" in score_distribution:
            model["handicap_one_x_two"] = score_distribution["handicap_one_x_two"]

    prediction = {
        "market": payload.get("market"),
        "match_id": payload.get("match_id"),
        "kickoff_at": payload.get("kickoff_at"),
        "captured_at": payload.get("captured_at"),
        "source_urls": payload.get("source_urls"),
        "fixed_prizes": {
            label: prize for label, prize in zip(OUTCOME_LABELS, prizes)
        },
        "outcomes": [
            {"label": label, "probability": probability}
            for label, probability in zip(OUTCOME_LABELS, probabilities)
        ],
        "prediction_role": payload.get("prediction_role", "primary"),
    }
    if xg_inputs is not None:
        prediction["xg_inputs"] = xg_inputs
    if model is not None:
        prediction["model"] = model
    match_context = _optional_match_context(payload.get("match_context"))
    if match_context is not None:
        prediction["match_context"] = match_context
    if "pregame_context" in payload:
        prediction["pregame_context"] = validate_pregame_context(payload["pregame_context"])
    if european_capture is not None:
        prediction["european_odds"] = {
            "capture_hash": european_capture["content_hash"],
            "provider": european_capture["provider"],
            "bookmaker": european_capture["bookmaker"],
            "captured_at": european_capture["captured_at"],
        }
    return prediction


def _append_record(ledger_path, record):
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as ledger_file:
        ledger_file.write(
            json.dumps(record, default=_json_default, ensure_ascii=True, sort_keys=True)
        )
        ledger_file.write("\n")


@contextmanager
def _ledger_operation_lock(ledger_path):
    """Prevent concurrent CLI operations from admitting the same primary record."""
    ledger = Path(ledger_path)
    lock_path = ledger.with_name(f"{ledger.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("ledger is already being updated by another tracker operation") from error
        try:
            yield
        finally:
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _restore_all_market_decimals(record):
    for entry in record.get("markets", {}).values():
        if entry.get("status") != "frozen":
            continue
        entry["probabilities"] = {
            option: _decimal(value, "all-market probability")
            for option, value in entry["probabilities"].items()
        }
        entry["official_fixed_prizes"] = {
            option: _decimal(value, "all-market prize")
            for option, value in entry["official_fixed_prizes"].items()
        }
        entry["selected_probability"] = _decimal(
            entry["selected_probability"], "all-market selected probability"
        )
        if "home_handicap" in entry:
            entry["home_handicap"] = _decimal(entry["home_handicap"], "home handicap")


def _restore_model_decimals(record):
    """Restore serialized Decimal values needed by report diagnostics."""
    model = record.get("model")
    if model is None:
        return
    if not isinstance(model, Mapping):
        raise ValueError("prediction model must be an object")

    restored = dict(model)
    if "weight" in restored:
        restored["weight"] = _decimal(restored["weight"], "model weight")

    for field in (
        "market_outcomes",
        "model_outcomes",
        "blended_outcomes",
        "total_goals",
        "half_time_full_time",
        "handicap_one_x_two",
    ):
        if field in restored:
            values = restored[field]
            if not isinstance(values, Mapping):
                raise ValueError(f"model {field} must be an object")
            restored[field] = {
                key: _decimal(value, f"model {field} probability")
                for key, value in values.items()
            }

    for field in ("scorelines", "top_scores", "half_time_full_time_proxy"):
        if field in restored:
            values = restored[field]
            if not isinstance(values, list):
                raise ValueError(f"model {field} must be a list")
            restored[field] = [
                {**value, "probability": _decimal(value["probability"], f"model {field} probability")}
                for value in values
            ]

    if "residual_mass" in restored:
        restored["residual_mass"] = _decimal(restored["residual_mass"], "model residual mass")
    record["model"] = restored


def _model_for_markets(prediction):
    model = prediction.get("model")
    if not isinstance(model, Mapping) or not isinstance(model.get("scorelines"), list):
        raise ValueError("all-market snapshots require a frozen full score matrix")
    restored = dict(model)
    restored["scorelines"] = [
        {**line, "probability": _decimal(line["probability"], "scoreline probability")}
        for line in model["scorelines"]
    ]
    restored["half_time_full_time"] = {
        key: _decimal(value, "half-time/full-time probability")
        for key, value in model.get("half_time_full_time", {}).items()
    }
    if len(restored["half_time_full_time"]) != 9:
        raise ValueError("all-market snapshots require complete half-time/full-time probabilities")
    return restored


def _market_entry(
    probabilities,
    option_order,
    prizes,
    home_handicap=None,
    published_at=None,
    official_source_url=None,
):
    selected, probability, tied = select_top_option(probabilities, option_order)
    entry = {
        "status": "frozen",
        "probabilities": probabilities,
        "selected_option": selected,
        "selected_probability": probability,
        "tied_options": list(tied),
        "official_fixed_prizes": prizes,
    }
    if home_handicap is not None:
        entry["home_handicap"] = home_handicap
    if published_at is not None:
        entry["published_at"] = published_at
        entry["official_source_url"] = official_source_url
    return entry


def read_records(ledger_path):
    path = Path(ledger_path)
    records = []
    prediction_hashes = set()
    settlement_hashes = set()
    invalidated_settlement_hashes = set()
    with path.open("r", encoding="utf-8") as ledger_file:
        for line_number, line in enumerate(ledger_file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSONL record on line {line_number}") from error
            if not isinstance(record, dict):
                raise ValueError(f"ledger line {line_number} must contain a JSON object")
            if record.get("record_type") == "prediction":
                try:
                    outcomes = record["outcomes"]
                    for outcome in outcomes:
                        outcome["probability"] = _decimal(
                            outcome["probability"], "outcome probability"
                        )
                except (KeyError, TypeError) as error:
                    raise ValueError(
                        f"prediction record on line {line_number} has invalid outcomes"
                    ) from error
                if "xg_inputs" in record:
                    record["xg_inputs"] = _optional_xg(
                        record["xg_inputs"], record.get("captured_at")
                    )
                _restore_model_decimals(record)
                verify_prediction(record)
                prediction_hashes.add(record["content_hash"])
            elif record.get("record_type") == "odds_capture":
                try:
                    record["odds"] = _odds(record["odds"])
                except (KeyError, TypeError) as error:
                    raise ValueError(
                        f"odds capture record on line {line_number} has invalid odds"
                    ) from error
                verify_odds_capture(record)
            elif record.get("record_type") in {"source_observation", "collection_failure"}:
                verify_evidence_event(record)
            elif record.get("record_type") == "settlement":
                verify_settlement(record)
                if record["prediction_hash"] not in prediction_hashes:
                    raise ValueError(
                        f"settlement record on line {line_number} references no prior prediction"
                    )
                settlement_hashes.add(record["content_hash"])
            elif record.get("record_type") == "settlement_invalidation":
                verify_settlement_invalidation(record)
                if record["settlement_hash"] not in settlement_hashes:
                    raise ValueError(
                        f"settlement invalidation on line {line_number} references no prior settlement"
                    )
                if record["settlement_hash"] in invalidated_settlement_hashes:
                    raise ValueError(
                        f"settlement invalidation on line {line_number} duplicates a prior invalidation"
                    )
                invalidated_settlement_hashes.add(record["settlement_hash"])
            elif record.get("record_type") == "all_market_snapshot":
                _restore_all_market_decimals(record)
                verify_all_market_snapshot(record)
                if record["prediction_hash"] not in prediction_hashes:
                    raise ValueError("all-market snapshot references no prior prediction")
            elif record.get("record_type") == "all_market_settlement":
                verify_all_market_settlement(record)
            records.append(record)
    return records


def snapshot(input_path, ledger_path):
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    path = Path(ledger_path)
    records = read_records(ledger_path) if path.exists() else []
    odds_captures = [record for record in records if record.get("record_type") == "odds_capture"]
    record = freeze_prediction(prediction_from_input(payload, odds_captures))
    _ensure_no_primary_conflict(records, record)
    _append_record(ledger_path, record)
    return record


def odds_import(input_path, ledger_path):
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    allowed_fields = {"provider", "captures", "example_notice"}
    if not isinstance(payload, Mapping) or not {"provider", "captures"}.issubset(payload) or not set(payload).issubset(allowed_fields):
        raise ValueError("odds import must contain provider and captures")
    if "example_notice" in payload and not isinstance(payload["example_notice"], str):
        raise ValueError("odds import example_notice must be a string")
    provider = payload["provider"]
    captures = payload["captures"]
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("odds import provider must be a non-empty string")
    if not isinstance(captures, list) or not captures:
        raise ValueError("odds import captures must be a non-empty list")

    records = []
    for capture in captures:
        if not isinstance(capture, Mapping) or "provider" in capture:
            raise ValueError("each odds capture must be an object without provider")
        normalized_capture = dict(capture)
        normalized_capture["provider"] = provider
        normalized_capture["odds"] = _odds(normalized_capture.get("odds"))
        record = freeze_odds_capture(normalized_capture)
        _append_record(ledger_path, record)
        records.append(record)
    return records


def evidence_record(input_path, ledger_path):
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    record = freeze_evidence_event(payload)
    _append_record(ledger_path, record)
    return record


def _invalidated_settlement_hashes(records):
    return {
        record["settlement_hash"]
        for record in records
        if record.get("record_type") == "settlement_invalidation"
    }


def settle(ledger_path, prediction_hash, actual_outcome, official_result_url, final_score=None):
    records = read_records(ledger_path)
    prediction = next(
        (
            record
            for record in records
            if record.get("record_type") == "prediction"
            and record.get("content_hash") == prediction_hash
        ),
        None,
    )
    if prediction is None:
        raise ValueError("no frozen prediction has the supplied content hash")
    invalidated_settlement_hashes = _invalidated_settlement_hashes(records)
    if any(
        record.get("record_type") == "settlement"
        and record.get("prediction_hash") == prediction_hash
        and record.get("content_hash") not in invalidated_settlement_hashes
        for record in records
    ):
        raise ValueError("prediction has already been settled")

    record = settle_prediction(
        prediction,
        actual_outcome,
        official_result_url,
        final_score=final_score,
    )
    _append_record(ledger_path, record)
    return record


def settlement_invalidate(input_path, ledger_path):
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    records = read_records(ledger_path)
    settlement = next(
        (
            record for record in records
            if record.get("record_type") == "settlement"
            and record.get("content_hash") == payload.get("settlement_hash")
        ),
        None,
    )
    if settlement is None:
        raise ValueError("settlement invalidation requires an existing settlement")
    if settlement["content_hash"] in _invalidated_settlement_hashes(records):
        raise ValueError("settlement has already been invalidated")
    record = invalidate_settlement(settlement, payload)
    _append_record(ledger_path, record)
    return record


def _official_market_inputs(payload, prediction, require_complete=False):
    """Validate the source-specific official tables shared by snapshot tiers."""
    required = {"prediction_hash", "captured_at", "official_source_url", "official_markets"}
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ValueError("all-market snapshot input has invalid fields")
    if prediction.get("content_hash") != payload["prediction_hash"]:
        raise ValueError("all-market snapshot prediction hash does not match the frozen prediction")
    supplied_markets = payload["official_markets"]
    if not isinstance(supplied_markets, Mapping) or set(supplied_markets) != set(ALL_MARKET_OPTIONS):
        raise ValueError("official_markets must cover all official markets")
    market_inputs = {}
    for code, options in ALL_MARKET_OPTIONS.items():
        entry = supplied_markets[code]
        if (
            isinstance(entry, Mapping)
            and set(entry) == {"status", "reason"}
            and entry["status"] == "skipped"
            and isinstance(entry["reason"], str)
            and entry["reason"].strip()
        ):
            if require_complete:
                raise ValueError(f"official market {code} must be complete")
            market_inputs[code] = {"status": "skipped", "reason": entry["reason"]}
            continue
        required_entry = {"published_at", "official_source_url", "fixed_prizes"}
        if code == "hhad":
            required_entry.add("home_handicap")
        if not isinstance(entry, Mapping) or set(entry) != required_entry:
            raise ValueError(f"official market {code} has invalid fields")
        published_at = entry["published_at"]
        try:
            parsed_published_at = datetime.fromisoformat(published_at)
        except (TypeError, ValueError) as error:
            raise ValueError(f"official market {code} published_at must be ISO-8601") from error
        if parsed_published_at.tzinfo is None or parsed_published_at >= datetime.fromisoformat(prediction["kickoff_at"]):
            raise ValueError(f"official market {code} published_at must be before kickoff")
        if not _is_http_url(entry["official_source_url"]):
            raise ValueError(f"official market {code} source URL is invalid")
        market_inputs[code] = {
            "status": "frozen",
            "published_at": published_at,
            "official_source_url": entry["official_source_url"],
            "fixed_prizes": _prize_mapping(entry["fixed_prizes"], options, f"official market {code} prizes"),
        }
        if code == "hhad":
            handicap = _decimal(entry["home_handicap"], "home handicap")
            if handicap != handicap.to_integral_value():
                raise ValueError("home handicap must be a whole number")
            market_inputs[code]["home_handicap"] = handicap
    return market_inputs


def _normalized_market_prize_probabilities(prizes, option_order):
    """Derive an auditable baseline from a complete official fixed-prize table."""
    values = [prizes[option] for option in option_order]
    smallest_prize = min(values)
    with localcontext() as context:
        context.prec *= 2
        scaled_reciprocals = [smallest_prize / prize for prize in values]
        total = sum(scaled_reciprocals, Decimal(0))
        raw_probabilities = [value / total for value in scaled_reciprocals]
    largest_index = max(range(len(raw_probabilities)), key=raw_probabilities.__getitem__)
    probabilities = [
        +value if index != largest_index else None
        for index, value in enumerate(raw_probabilities)
    ]
    probabilities[largest_index] = Decimal(1) - sum(
        (value for value in probabilities if value is not None), Decimal(0)
    )
    return dict(zip(option_order, probabilities))


def _build_all_market_snapshot(payload, prediction):
    """Build a model-derived snapshot from complete official market evidence."""
    model = _model_for_markets(prediction)
    market_inputs = _official_market_inputs(payload, prediction)
    handicap = market_inputs["hhad"].get("home_handicap")
    derived = derive_official_markets(model, handicap)
    spf_probabilities = {outcome["label"]: outcome["probability"] for outcome in prediction["outcomes"]}
    markets = {}
    for code in ALL_MARKET_OPTIONS:
        values = market_inputs[code]
        if values["status"] == "skipped":
            markets[code] = {"status": "skipped", "reason": values["reason"]}
            continue
        probabilities = spf_probabilities if code == "spf" else derived[code]
        markets[code] = _market_entry(
            probabilities,
            ALL_MARKET_OPTIONS[code],
            values["fixed_prizes"],
            values.get("home_handicap"),
            values["published_at"],
            values["official_source_url"],
        )
    snapshot = freeze_all_market_snapshot({
        "schema_version": 2,
        "prediction_hash": prediction["content_hash"],
        "match_id": prediction["match_id"],
        "kickoff_at": prediction["kickoff_at"],
        "captured_at": payload["captured_at"],
        "official_source_url": payload["official_source_url"],
        "markets": markets,
    })
    return snapshot


def _build_official_market_baseline_snapshot(payload, prediction):
    """Build a five-market official-prize baseline without inventing model evidence."""
    market_inputs = _official_market_inputs(payload, prediction, require_complete=True)
    spf_probabilities = {outcome["label"]: outcome["probability"] for outcome in prediction["outcomes"]}
    markets = {}
    for code, options in ALL_MARKET_OPTIONS.items():
        values = market_inputs[code]
        probabilities = (
            spf_probabilities
            if code == "spf"
            else _normalized_market_prize_probabilities(values["fixed_prizes"], options)
        )
        markets[code] = _market_entry(
            probabilities,
            options,
            values["fixed_prizes"],
            values.get("home_handicap"),
            values["published_at"],
            values["official_source_url"],
        )
    return freeze_all_market_snapshot({
        "schema_version": 3,
        "snapshot_strategy": "official_market_baseline",
        "prediction_hash": prediction["content_hash"],
        "match_id": prediction["match_id"],
        "kickoff_at": prediction["kickoff_at"],
        "captured_at": payload["captured_at"],
        "official_source_url": payload["official_source_url"],
        "markets": markets,
    })


def all_market_snapshot(input_path, ledger_path):
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    records = read_records(ledger_path)
    prediction = next(
        (
            record for record in records
            if record.get("record_type") == "prediction"
            and record.get("content_hash") == payload.get("prediction_hash")
        ),
        None,
    )
    if prediction is None:
        raise ValueError("all-market snapshot requires an existing frozen prediction")
    snapshot = _build_all_market_snapshot(payload, prediction)
    _append_record(ledger_path, snapshot)
    return snapshot


def _rapid_match(schedule, match_id):
    wanted = str(match_id)
    for match in schedule.get("matches", []):
        if str(match.get("match_id")) == wanted:
            return match
    raise ValueError(f"match_id {match_id} was not found in the official Sporttery schedule")


def _rapid_capture(job, match):
    supplied = job.get("odds_capture")
    required = {"provider", "source_url", "bookmaker", "captured_at", "odds"}
    if not isinstance(supplied, Mapping) or set(supplied) != required:
        raise ValueError("rapid odds_capture must contain provider, source_url, bookmaker, captured_at, and odds")
    match_num = match.get("match_num")
    kickoff_at = match.get("kickoff_at")
    if not isinstance(match_num, str) or not isinstance(kickoff_at, str):
        raise ValueError("official Sporttery identity lacks a weekday sequence or kickoff time")
    return freeze_odds_capture({
        "provider": supplied["provider"],
        "match_id": match_num,
        "kickoff_at": kickoff_at,
        "captured_at": supplied["captured_at"],
        "source_url": supplied["source_url"],
        "bookmaker": supplied["bookmaker"],
        "source_quality": "named_bookmaker",
        "odds": _odds(supplied["odds"]),
    })


def _rapid_official_markets(official):
    markets = official.get("official_markets") if isinstance(official, Mapping) else None
    if not isinstance(markets, Mapping) or set(markets) != set(ALL_MARKET_OPTIONS):
        raise ValueError("official five-market table is incomplete")
    result = {}
    for code in ALL_MARKET_OPTIONS:
        entry = markets[code]
        if not isinstance(entry, Mapping) or entry.get("status") != "frozen":
            raise ValueError(f"official {code} market is unavailable")
        result[code] = {key: value for key, value in entry.items() if key != "status"}
    return result


def _rapid_prediction_payload(job, match, official, xg, capture, captured_at):
    xg_inputs = xg.get("xg_inputs") if isinstance(xg, Mapping) else None
    if not isinstance(xg_inputs, Mapping) or not isinstance(xg_inputs.get("evidence"), Mapping):
        raise ValueError("traceable xG evidence is unavailable")
    official_markets = _rapid_official_markets(official)
    return {
        "market": "SPF",
        "match_id": match["match_num"],
        "kickoff_at": match["kickoff_at"],
        "captured_at": captured_at,
        "source_urls": {
            "S1": official["official_source_url"],
            "S2": capture["source_url"],
        },
        "match_context": {
            "competition": xg_inputs["evidence"]["competition"],
            "home_team": xg.get("home_team"),
            "away_team": xg.get("away_team"),
        },
        "fixed_prizes": official_markets["spf"]["fixed_prizes"],
        "european_odds": {"capture_hash": capture["content_hash"]},
        "xg_inputs": {
            **xg_inputs,
            "model_weight": job.get("model_weight", xg_inputs.get("model_weight", "0.40")),
        },
    }, official_markets


def _validate_rapid_job(job):
    """Validate the source references needed for one rapid paper record."""
    allowed = {
        "match_id", "home_fotmob_url", "away_fotmob_url", "odds_capture",
        "model_weight", "captured_at",
    }
    if not isinstance(job, Mapping) or not {"match_id", "home_fotmob_url", "away_fotmob_url", "odds_capture"}.issubset(job) or not set(job).issubset(allowed):
        raise ValueError("rapid input requires match_id, two FotMob URLs, and a named odds_capture")
    return job


def _rapid_stage(job, schedule):
    """Collect and validate one match entirely in memory before a ledger append."""
    job = _validate_rapid_job(job)
    match = _rapid_match(schedule, job["match_id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        official_future = executor.submit(collect_fixed_bonus, job["match_id"])
        xg_future = executor.submit(
            collect_match_xg_evidence,
            job["home_fotmob_url"],
            job["away_fotmob_url"],
            model_weight=job.get("model_weight", "0.40"),
        )
        official = official_future.result()
        xg = xg_future.result()

    captured_at = job.get("captured_at") or datetime.now(timezone.utc).astimezone().isoformat()
    capture = _rapid_capture(job, match)
    gate = quality_gate(
        match,
        official,
        capture,
        xg,
        {
            "split_xg": {"status": "unavailable"},
            "lineup": {"status": "unavailable"},
        },
        captured_at,
    )
    if gate.get("status") != "review_ready":
        return {
            "records": [],
            "summary": {
                "status": "observation_only" if gate.get("status") == "observation_only" else "blocked",
                "match_id": match["match_num"],
                "captured_at": captured_at,
                "reasons": gate.get("reasons", ["pregame_quality_gate_unavailable"]),
                "quality_gate": gate,
            },
        }
    prediction_payload, official_markets = _rapid_prediction_payload(
        job, match, official, xg, capture, captured_at
    )
    prediction = freeze_prediction(prediction_from_input(prediction_payload, [capture]))
    all_market = _build_all_market_snapshot({
        "prediction_hash": prediction["content_hash"],
        "captured_at": captured_at,
        "official_source_url": official["official_source_url"],
        "official_markets": official_markets,
    }, prediction)
    return {
        "records": [capture, prediction, all_market],
        "summary": {
            "status": "frozen",
            "match_id": prediction["match_id"],
            "captured_at": captured_at,
            "odds_capture_hash": capture["content_hash"],
            "prediction_hash": prediction["content_hash"],
            "all_market_snapshot_hash": all_market["content_hash"],
            "quality_gate": gate,
        },
    }


def _primary_identity_value(record):
    """Return the reusable official sequence plus normalized kickoff identity."""
    match_id = record.get("match_id")
    if match_id is None:
        return None
    normalized_match_id = "".join(str(match_id).split())
    if not normalized_match_id:
        return None
    try:
        kickoff_at = _timestamp(record.get("kickoff_at"), "kickoff_at").isoformat()
    except ValueError:
        kickoff_at = None
    return normalized_match_id, kickoff_at


def _primary_prediction_conflict(records, prediction):
    """Match primary records by sequence and kickoff, preserving legacy safety."""
    candidate_identity = _primary_identity_value(prediction)
    if candidate_identity is None:
        return False
    for record in records:
        if (
            record.get("record_type") != "prediction"
            or record.get("prediction_role", "primary") != "primary"
        ):
            continue
        existing_identity = _primary_identity_value(record)
        if existing_identity is None or existing_identity[0] != candidate_identity[0]:
            continue
        if existing_identity[1] is None or candidate_identity[1] is None:
            return True
        if existing_identity == candidate_identity:
            return True
    return False


def _ensure_no_primary_conflict(records, prediction):
    if prediction["prediction_role"] == "primary" and _primary_prediction_conflict(records, prediction):
        raise ValueError("a primary prediction already exists for this official match identity")


def rapid_freeze(input_path, ledger_path):
    """Collect one verified-source match and append all three frozen paper records."""
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        job = json.load(input_file)

    path = Path(ledger_path)
    existing = read_records(ledger_path) if path.exists() else []
    schedule = collect_schedule()
    staged = _rapid_stage(job, schedule)
    if not staged["records"]:
        return staged["summary"]
    _ensure_no_primary_conflict(existing, staged["records"][1])
    for record in staged["records"]:
        _append_record(ledger_path, record)
    return staged["summary"]


def rapid_freeze_batch(input_path, ledger_path, max_workers=6):
    """Process independent rapid jobs concurrently while preserving append-only order."""
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    if not isinstance(payload, Mapping) or set(payload) != {"jobs"} or not isinstance(payload["jobs"], list) or not payload["jobs"]:
        raise ValueError("rapid batch input must contain a non-empty jobs list")
    if not isinstance(max_workers, int) or max_workers < 1 or max_workers > 12:
        raise ValueError("rapid batch max_workers must be between 1 and 12")

    schedule = collect_schedule()
    with ThreadPoolExecutor(max_workers=min(max_workers, len(payload["jobs"]))) as executor:
        futures = [executor.submit(_rapid_stage, job, schedule) for job in payload["jobs"]]
        staged = []
        for index, future in enumerate(futures):
            try:
                staged.append((index, future.result(), None))
            except (OSError, ValueError, KeyError, TypeError) as error:
                staged.append((index, None, str(error)))

    path = Path(ledger_path)
    existing = read_records(ledger_path) if path.exists() else []
    accepted_primary_predictions = [
        record for record in existing
        if record.get("record_type") == "prediction"
        and record.get("prediction_role", "primary") == "primary"
    ]
    results = []
    for index, stage, error in staged:
        if error is not None:
            results.append({"index": index, "status": "blocked", "reason": error})
            continue
        if not stage["records"]:
            results.append({"index": index, **stage["summary"]})
            continue
        prediction = next(
            record for record in stage["records"]
            if record.get("record_type") == "prediction"
        )
        if _primary_prediction_conflict(accepted_primary_predictions, prediction):
            results.append({
                "index": index,
                "status": "blocked",
                "match_id": prediction["match_id"],
                "reason": "a primary prediction already exists for this official match identity",
            })
            continue
        accepted_primary_predictions.append(prediction)
        for record in stage["records"]:
            _append_record(ledger_path, record)
        results.append({"index": index, **stage["summary"]})
    return {
        "frozen_count": sum(item["status"] == "frozen" for item in results),
        "observation_count": sum(item["status"] == "observation_only" for item in results),
        "blocked_count": sum(item["status"] == "blocked" for item in results),
        "items": results,
    }


def _bundle_collection_diagnostics(bundle, gate):
    """Return read-only collection facts beside a scheduler outcome."""
    if not isinstance(bundle, Mapping):
        return {"quality_gate": gate if isinstance(gate, Mapping) else {}}
    readiness = bundle.get("readiness")
    missing_inputs = readiness.get("missing", []) if isinstance(readiness, Mapping) else []
    cascade = bundle.get("xg_source_cascade")
    resolved_links = bundle.get("resolved_source_links")
    diagnostics = {
        "quality_gate": gate if isinstance(gate, Mapping) else {},
        "missing_inputs": list(missing_inputs) if isinstance(missing_inputs, list) else [],
        "xg_source_cascade": list(cascade) if isinstance(cascade, list) else [],
        "resolved_source_links": dict(resolved_links) if isinstance(resolved_links, Mapping) else {},
    }
    official = bundle.get("evidence", {}).get("official") if isinstance(bundle.get("evidence"), Mapping) else None
    markets = official.get("official_markets") if isinstance(official, Mapping) else None
    if isinstance(markets, Mapping):
        diagnostics["official_market_status"] = {
            code: entry.get("status")
            for code, entry in markets.items()
            if isinstance(entry, Mapping)
        }
    return diagnostics


def _automatic_freeze_stage(
    match,
    european_odds_url=None,
    understat_xg=None,
    xgscore_url=None,
    xgscore_home_team=None,
    xgscore_away_team=None,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
):
    """Collect one eligible fixture without writing until every record validates."""
    bundle_kwargs = {
        "schedule_match": match,
        "allow_433_discovery": False,
    }
    if understat_xg is not None:
        bundle_kwargs["understat_xg"] = understat_xg
    if xgscore_url is not None:
        bundle_kwargs["xgscore_url"] = xgscore_url
    if xgscore_home_team is not None and xgscore_away_team is not None:
        bundle_kwargs["xgscore_home_team"] = xgscore_home_team
        bundle_kwargs["xgscore_away_team"] = xgscore_away_team
    if Path(source_link_registry_path) != Path(DEFAULT_SOURCE_LINK_REGISTRY_PATH):
        bundle_kwargs["source_link_registry_path"] = source_link_registry_path
    if Path(source_alias_registry_path) != Path(DEFAULT_SOURCE_ALIAS_REGISTRY_PATH):
        bundle_kwargs["source_alias_registry_path"] = source_alias_registry_path
    bundle = collect_auto_pregame_bundle(match["match_id"], european_odds_url, **bundle_kwargs)
    gate = bundle.get("quality_gate", {}) if isinstance(bundle, Mapping) else {}
    diagnostics = _bundle_collection_diagnostics(bundle, gate)
    if gate.get("status") != "review_ready":
        if gate.get("reasons") == ["xg_evidence_unavailable"]:
            try:
                baseline = _automatic_market_baseline_stage(
                    match, bundle, gate, freeze_official_markets=True,
                )
                baseline["summary"]["collection_diagnostics"] = diagnostics
                return baseline
            except (KeyError, TypeError, ValueError):
                pass
        try:
            baseline = _automatic_market_baseline_stage(match, bundle, gate)
            baseline["summary"]["collection_diagnostics"] = diagnostics
            return baseline
        except (KeyError, TypeError, ValueError):
            pass
        return {
            "records": [],
            "summary": {
                "status": "observation_only" if gate.get("status") == "observation_only" else "blocked",
                "match_id": match["match_id"],
                "match_num": match.get("match_num"),
                "reasons": gate.get("reasons", ["pregame_quality_gate_unavailable"]),
                "collection_diagnostics": diagnostics,
            },
        }

    match_num = match.get("match_num")
    if not isinstance(match_num, str) or not match_num.strip():
        raise ValueError("official Sporttery identity lacks a weekday sequence")
    try:
        odds_input = bundle["odds_import_input"]
        supplied_capture = odds_input["captures"][0]
        prediction_payload = dict(bundle["prediction_input_template"])
        snapshot_payload = dict(bundle["all_market_snapshot_input_template"])
    except (KeyError, TypeError, IndexError) as error:
        raise ValueError("review-ready bundle lacks freeze inputs") from error

    capture = freeze_odds_capture({
        "provider": odds_input["provider"],
        "match_id": match_num,
        "kickoff_at": match["kickoff_at"],
        "captured_at": supplied_capture["captured_at"],
        "source_url": supplied_capture["source_url"],
        "bookmaker": supplied_capture["bookmaker"],
        "source_quality": "named_bookmaker",
        "odds": _odds(supplied_capture["odds"]),
    })
    prediction_payload["match_id"] = match_num
    prediction_payload["kickoff_at"] = match["kickoff_at"]
    prediction_payload["captured_at"] = bundle["generated_at"]
    prediction_payload["european_odds"] = {"capture_hash": capture["content_hash"]}
    prediction = freeze_prediction(prediction_from_input(prediction_payload, [capture]))
    snapshot_payload["prediction_hash"] = prediction["content_hash"]
    snapshot_payload["captured_at"] = bundle["generated_at"]
    snapshot = _build_all_market_snapshot(snapshot_payload, prediction)
    frozen_markets = [
        code for code, entry in snapshot["markets"].items()
        if entry["status"] == "frozen"
    ]
    skipped_markets = {
        code: entry["reason"]
        for code, entry in snapshot["markets"].items()
        if entry["status"] == "skipped"
    }
    return {
        "records": [capture, prediction, snapshot],
        "summary": {
            "status": "frozen",
            "freeze_level": (
                "five_market"
                if len(frozen_markets) == len(ALL_MARKET_OPTIONS)
                else "partial_markets"
            ),
            "match_id": match["match_id"],
            "match_num": match_num,
            "odds_capture_hash": capture["content_hash"],
            "prediction_hash": prediction["content_hash"],
            "all_market_snapshot_hash": snapshot["content_hash"],
            "frozen_markets": frozen_markets,
            "skipped_markets": skipped_markets,
            "collection_diagnostics": diagnostics,
        },
    }


def _automatic_market_baseline_stage(match, bundle, gate, freeze_official_markets=False):
    """Freeze an auditable SPF market baseline when enhanced evidence is incomplete."""
    match_num = match.get("match_num")
    if not isinstance(match_num, str) or not match_num.strip():
        raise ValueError("official Sporttery identity lacks a weekday sequence")
    odds_input = bundle["odds_import_input"]
    supplied_capture = odds_input["captures"][0]
    prediction_payload = dict(bundle["prediction_input_template"])
    capture = freeze_odds_capture({
        "provider": odds_input["provider"],
        "match_id": match_num,
        "kickoff_at": match["kickoff_at"],
        "captured_at": supplied_capture["captured_at"],
        "source_url": supplied_capture["source_url"],
        "bookmaker": supplied_capture["bookmaker"],
        "source_quality": "named_bookmaker",
        "odds": _odds(supplied_capture["odds"]),
    })
    prediction_payload.pop("xg_inputs", None)
    prediction_payload["match_id"] = match_num
    prediction_payload["kickoff_at"] = match["kickoff_at"]
    prediction_payload["captured_at"] = bundle["generated_at"]
    prediction_payload["european_odds"] = {"capture_hash": capture["content_hash"]}
    prediction = freeze_prediction(prediction_from_input(prediction_payload, [capture]))
    records = [capture, prediction]
    if freeze_official_markets:
        snapshot_payload = dict(bundle["all_market_snapshot_input_template"])
        snapshot_payload["prediction_hash"] = prediction["content_hash"]
        snapshot_payload["captured_at"] = bundle["generated_at"]
        snapshot = _build_official_market_baseline_snapshot(snapshot_payload, prediction)
        records.append(snapshot)
        return {
            "records": records,
            "summary": {
                "status": "frozen",
                "freeze_level": "official_market_five_market",
                "all_market_snapshot_status": "frozen",
                "match_id": match["match_id"],
                "match_num": match_num,
                "odds_capture_hash": capture["content_hash"],
                "prediction_hash": prediction["content_hash"],
                "all_market_snapshot_hash": snapshot["content_hash"],
                "frozen_markets": list(ALL_MARKET_OPTIONS),
                "unresolved_evidence": gate.get("reasons", []),
            },
        }
    return {
        "records": records,
        "summary": {
            "status": "frozen",
            "freeze_level": "market_baseline",
            "all_market_snapshot_status": "not_frozen",
            "match_id": match["match_id"],
            "match_num": match_num,
            "odds_capture_hash": capture["content_hash"],
            "prediction_hash": prediction["content_hash"],
            "unresolved_evidence": gate.get("reasons", []),
        },
    }


def _eligible_schedule_matches(
    schedule,
    existing_records,
    now,
    min_lead_minutes,
    max_lead_minutes,
    sales_cutoff_policy,
    preflight=False,
    include_open_matches=False,
    freeze_window_minutes=0,
):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    if isinstance(freeze_window_minutes, bool) or not isinstance(freeze_window_minutes, int):
        raise ValueError("freeze_window_minutes must be an integer")

    eligible = []
    skipped = {}
    skipped_items = []
    timings = {}
    operator_open_end = None
    if include_open_matches:
        zone = ZoneInfo(sales_cutoff_policy["timezone"])
        local_now = now.astimezone(zone)
        cutoff_value = sales_cutoff_policy[
            "weekend_cutoff" if local_now.weekday() >= 5 else "weekday_cutoff"
        ]
        cutoff_at = datetime.combine(
            local_now.date(),
            datetime.strptime(cutoff_value, "%H:%M").time(),
            tzinfo=zone,
        )
        operator_open_end = cutoff_at + timedelta(hours=12)
    for match in schedule.get("matches", []):
        kickoff_at = match.get("kickoff_at")
        try:
            kickoff = _timestamp(kickoff_at, "kickoff_at")
        except ValueError:
            skipped["invalid_kickoff"] = skipped.get("invalid_kickoff", 0) + 1
            continue
        timing = assess_freeze_timing(
            kickoff,
            now,
            min_lead_minutes,
            max_lead_minutes,
            sales_cutoff_policy,
        )
        timing_metadata = {
            key: timing[key]
            for key in ("timing_basis", "sales_cutoff_at", "window_start_at", "window_end_at")
        }
        timings[str(match.get("match_id"))] = timing_metadata
        within_window = timing["eligible"] or (
            include_open_matches
            and now < kickoff
            and kickoff.astimezone(operator_open_end.tzinfo) <= operator_open_end
        )
        if preflight and timing["timing_basis"] == "sales_cutoff_window":
            sales_cutoff_at = _timestamp(timing["sales_cutoff_at"], "sales_cutoff_at")
            minutes_until_sales_cutoff = (sales_cutoff_at - now).total_seconds() / 60
            within_window = (
                min_lead_minutes
                <= minutes_until_sales_cutoff
                <= max_lead_minutes
            )
        official_status = str(match.get("status", "")).strip()
        if official_status != "2":
            reason = {
                "1": "official_sale_pending",
                "3": "official_sales_suspended",
            }.get(official_status, "official_sale_status_unavailable")
            skipped[reason] = skipped.get(reason, 0) + 1
            if timing["timing_basis"] == "sales_cutoff_window" or timing["eligible"]:
                skipped_items.append({
                    "status": "skipped",
                    "match_id": match.get("match_id"),
                    "match_num": match.get("match_num"),
                    "official_status": official_status or None,
                    "reasons": [reason],
                    **timing_metadata,
                })
            continue
        # A positive freeze window is an additional close-to-kickoff gate: only
        # 0 < (kickoff - now) <= freeze_window_minutes may freeze. Zero or a
        # negative value means "no close gate" and keeps the pre-existing
        # lead/sales-cutoff selection unchanged; zero is not an empty window.
        # Operator --include-open-matches keeps its explicit override.
        if freeze_window_minutes > 0 and not include_open_matches:
            minutes_until_kickoff = (kickoff - now).total_seconds() / 60
            if not 0 < minutes_until_kickoff <= freeze_window_minutes:
                skipped["outside_close_freeze_window"] = (
                    skipped.get("outside_close_freeze_window", 0) + 1
                )
                continue
        if not within_window:
            skipped["outside_freeze_window"] = skipped.get("outside_freeze_window", 0) + 1
            continue
        match_num = match.get("match_num")
        if not isinstance(match_num, str) or not match_num.strip():
            skipped["official_match_num_unavailable"] = skipped.get("official_match_num_unavailable", 0) + 1
            continue
        if _primary_prediction_conflict(existing_records, {
            "match_id": match_num,
            "kickoff_at": kickoff_at,
            "prediction_role": "primary",
        }):
            skipped["primary_prediction_exists"] = skipped.get("primary_prediction_exists", 0) + 1
            continue
        eligible.append(match)
    return eligible, skipped, skipped_items, timings


def _sale_availability_when_no_official_open_match(schedule, now, sales_cutoff_policy):
    """Describe a local recheck time without inferring an official sale-open time."""
    official_open_sale_count = sum(
        str(match.get("status", "")).strip() == "2"
        for match in schedule.get("matches", [])
    )
    if official_open_sale_count:
        return None

    zone = ZoneInfo(sales_cutoff_policy["timezone"])
    local_now = now.astimezone(zone)
    check_date = local_now.date()
    while True:
        cutoff_value = sales_cutoff_policy[
            "weekend_cutoff" if check_date.weekday() >= 5 else "weekday_cutoff"
        ]
        cutoff_at = datetime.combine(
            check_date,
            datetime.strptime(cutoff_value, "%H:%M").time(),
            tzinfo=zone,
        )
        check_at = cutoff_at - timedelta(
            minutes=sales_cutoff_policy["cutoff_window_start_minutes"]
        )
        if local_now < check_at:
            break
        check_date += timedelta(days=1)

    statuses = [str(match.get("status", "")).strip() for match in schedule.get("matches", [])]
    return {
        "official_open_sale_count": 0,
        "official_open_time": None,
        "official_open_time_status": "not_published_by_current_official_schedule",
        "official_sale_pending_count": statuses.count("1"),
        "official_sales_suspended_count": statuses.count("3"),
        "next_system_check_at": check_at.isoformat(),
        "next_system_check_basis": "local_sales_cutoff_window_start_not_official_open_time",
        "timezone": sales_cutoff_policy["timezone"],
    }


def _prefetch_understat_batch_xg(
    matches,
    source_alias_registry_path,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
):
    """Fetch each eligible Understat league once from strict aliases or anchors."""
    aliases = understat_source_aliases(source_alias_registry_path)
    groups = {}
    wikidata_anchor_fixture_count = 0
    for match in matches:
        home_key = re.sub(r"\s+", "", str(match.get("home_team", "")).casefold())
        away_key = re.sub(r"\s+", "", str(match.get("away_team", "")).casefold())
        home_team = aliases.get(home_key)
        away_team = aliases.get(away_key)
        league = UNDERSTAT_LEAGUES.get(match.get("competition"))
        if not league:
            continue
        home_wikidata = away_wikidata = None
        if not home_team:
            try:
                home_wikidata = cached_wikidata_team_identity(
                    source_link_registry_path, match.get("home_team")
                )
            except (TypeError, ValueError):
                home_wikidata = None
        if not away_team:
            try:
                away_wikidata = cached_wikidata_team_identity(
                    source_link_registry_path, match.get("away_team")
                )
            except (TypeError, ValueError):
                away_wikidata = None
        if not home_team and home_wikidata is None:
            continue
        if not away_team and away_wikidata is None:
            continue
        try:
            kickoff = datetime.fromisoformat(match["kickoff_at"])
        except (TypeError, ValueError):
            continue
        season = str(kickoff.year if kickoff.month >= 7 else kickoff.year - 1)
        fixture = {
            "match_id": match["match_id"],
            "kickoff_at": match["kickoff_at"],
        }
        if home_team:
            fixture["home_team"] = home_team
        else:
            fixture["home_wikidata"] = home_wikidata
        if away_team:
            fixture["away_team"] = away_team
        else:
            fixture["away_wikidata"] = away_wikidata
        if home_wikidata is not None or away_wikidata is not None:
            wikidata_anchor_fixture_count += 1
        groups.setdefault((league, season), []).append((match, fixture, home_team, away_team))

    prefetched = {}
    for (league, season), entries in groups.items():
        fixtures = [fixture for _match, fixture, _home_team, _away_team in entries]
        try:
            response = collect_league_xg_batch(league, season, fixtures)
            response_fixtures = {
                str(item.get("match_id")): item
                for item in response.get("fixtures", [])
                if isinstance(item, dict)
            }
            for match, _fixture, home_team, away_team in entries:
                item = response_fixtures.get(str(match["match_id"]))
                expected_home = home_team or (item or {}).get("home_team")
                expected_away = away_team or (item or {}).get("away_team")
                prefetched[str(match["match_id"])] = _understat_batch_xg(
                    item,
                    expected_home,
                    expected_away,
                )
        except (OSError, ValueError, KeyError, TypeError):
            for match, _fixture, _home_team, _away_team in entries:
                prefetched[str(match["match_id"])] = {
                    "status": "unavailable",
                    "reason": "understat_batch_request_unavailable",
                }
    return prefetched, len(groups), {
        "understat_wikidata_anchor_fixture_count": wikidata_anchor_fixture_count,
    }


def _resolve_433_source_links(matches, source_link_registry_path):
    """Use exact cached identities first, then discover and retain new strict matches."""
    resolved = {}
    unresolved = []
    cache_hit_count = 0
    for match in matches:
        try:
            cached_url = cached_source_links(source_link_registry_path, match).get("european_433_url")
        except (TypeError, ValueError):
            cached_url = None
        if cached_url:
            resolved[str(match["match_id"])] = {
                "status": "resolved",
                "source_url": cached_url,
                "source_link_origin": "cache",
            }
            cache_hit_count += 1
        else:
            unresolved.append(match)

    if unresolved:
        try:
            discovered = discover_433_detail_urls(unresolved, max_pages=8)
        except (OSError, ValueError, KeyError, TypeError) as error:
            discovered = {
                str(match["match_id"]): {
                    "status": "unavailable",
                    "reason": f"433 batch discovery failed: {error}",
                }
                for match in unresolved
            }
        for match in unresolved:
            match_id = str(match["match_id"])
            discovery = dict(discovered.get(match_id, {}))
            if discovery.get("status") == "resolved" and discovery.get("source_url"):
                try:
                    record_source_links(
                        source_link_registry_path,
                        match,
                        {"european_433_url": discovery["source_url"]},
                    )
                except (OSError, TypeError, ValueError):
                    pass
                discovery["source_link_origin"] = "fresh_discovery"
            resolved[match_id] = discovery

    fresh_discovery_count = sum(
        item.get("source_link_origin") == "fresh_discovery"
        for item in resolved.values()
    )
    return resolved, {
        "source_link_cache_hit_count": cache_hit_count,
        "source_link_fresh_discovery_count": fresh_discovery_count,
        "source_link_unresolved_count": len(matches) - cache_hit_count - fresh_discovery_count,
    }


def _resolve_xgscore_source_links(matches, source_link_registry_path):
    """Reuse cached xGscore pages or discover candidates from one public homepage."""
    resolved = {}
    pending = []
    pending_identity_names = {}
    cache_hit_count = 0
    fotmob_anchor_fixture_count = 0
    wikidata_anchor_fixture_count = 0
    for match in matches:
        match_id = str(match["match_id"])
        try:
            cached_url = cached_source_links(source_link_registry_path, match).get("xgscore_url")
        except (TypeError, ValueError):
            cached_url = None
        if cached_url:
            resolved[match_id] = {
                "status": "candidate",
                "source_url": cached_url,
                "source_link_origin": "cache",
            }
            cache_hit_count += 1
            continue
        home_team = match.get("home_team")
        away_team = match.get("away_team")
        if not all(isinstance(team, str) and team.strip() for team in (home_team, away_team)):
            resolved[match_id] = {
                "status": "unavailable",
                "reason": "xgscore requires official home and away team names",
            }
            continue
        try:
            home_identity = cached_fotmob_team_identity(source_link_registry_path, home_team)
            away_identity = cached_fotmob_team_identity(source_link_registry_path, away_team)
        except (TypeError, ValueError):
            home_identity = None
            away_identity = None
        if home_identity is None or away_identity is None:
            try:
                home_anchor = cached_wikidata_team_identity(source_link_registry_path, home_team)
                away_anchor = cached_wikidata_team_identity(source_link_registry_path, away_team)
            except (TypeError, ValueError):
                home_anchor = away_anchor = None
            home_names = (
                home_anchor.get("english_names", [home_anchor.get("english_name")])
                if isinstance(home_anchor, Mapping) else []
            )
            away_names = (
                away_anchor.get("english_names", [away_anchor.get("english_name")])
                if isinstance(away_anchor, Mapping) else []
            )
            if not all(
                isinstance(names, list) and names and all(isinstance(name, str) and name.strip() for name in names)
                for names in (home_names, away_names)
            ):
                resolved[match_id] = {
                    "status": "unavailable",
                    "reason": "xgscore requires cached verified provider or Wikidata team identities",
                }
                continue
            pending.append({
                "match_id": match_id,
                "home_team": home_names[0],
                "home_team_aliases": home_names,
                "away_team": away_names[0],
                "away_team_aliases": away_names,
            })
            pending_identity_names[match_id] = (home_names, away_names)
            wikidata_anchor_fixture_count += 1
            continue
        pending.append({
            "match_id": match_id,
            "home_team": home_identity["source_name"],
            "away_team": away_identity["source_name"],
        })
        fotmob_anchor_fixture_count += 1

    if pending:
        try:
            discovered = discover_xgscore_event_urls(pending)
        except (OSError, ValueError, KeyError, TypeError) as error:
            discovered = {
                fixture["match_id"]: {
                    "status": "unavailable",
                    "reason": f"xgscore batch discovery failed: {error}",
                }
                for fixture in pending
            }
        for fixture in pending:
            match_id = fixture["match_id"]
            item = dict(discovered.get(match_id, {}))
            if item.get("status") == "candidate" and item.get("source_url"):
                item["source_link_origin"] = "fresh_discovery"
                source_names = pending_identity_names.get(match_id)
                if source_names is not None:
                    item["xgscore_home_team"] = source_names[0]
                    item["xgscore_away_team"] = source_names[1]
            resolved[match_id] = item

    fresh_discovery_count = sum(
        item.get("source_link_origin") == "fresh_discovery"
        for item in resolved.values()
    )
    return resolved, {
        "xgscore_cache_hit_count": cache_hit_count,
        "xgscore_candidate_count": fresh_discovery_count,
        "xgscore_unavailable_count": len(matches) - cache_hit_count - fresh_discovery_count,
        "xgscore_fotmob_anchor_fixture_count": fotmob_anchor_fixture_count,
        "xgscore_wikidata_anchor_fixture_count": wikidata_anchor_fixture_count,
    }


def prewarm_wikidata_source_identities(
    hours_ahead=48,
    max_workers=2,
    now=None,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    skip_team_names=None,
    network_enabled=True,
):
    """Cache unique public Wikidata name anchors for upcoming open matches."""
    if not isinstance(hours_ahead, int) or hours_ahead < 1 or hours_ahead > 168:
        raise ValueError("hours_ahead must be between 1 and 168")
    if not isinstance(max_workers, int) or max_workers < 1 or max_workers > 4:
        raise ValueError("max_workers must be between 1 and 4")
    if skip_team_names is not None and (
        not isinstance(skip_team_names, set)
        or any(not isinstance(name, str) or not name.strip() for name in skip_team_names)
    ):
        raise ValueError("skip_team_names must be a set of non-empty team names")
    if not isinstance(network_enabled, bool):
        raise ValueError("network_enabled must be a boolean")
    now = now or datetime.now(timezone.utc).astimezone()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")

    horizon = now + timedelta(hours=hours_ahead)
    schedule = collect_schedule(method="concern")
    pending = {}
    skipped_by_reason = {}
    skipped_team_names = {
        re.sub(r"\s+", "", name).casefold()
        for name in (skip_team_names or set())
    }
    cache_hit_team_count = 0
    for match in schedule.get("matches", []):
        if str(match.get("status", "")).strip() != "2":
            skipped_by_reason["official_sale_not_open"] = (
                skipped_by_reason.get("official_sale_not_open", 0) + 1
            )
            continue
        try:
            kickoff = _timestamp(match.get("kickoff_at"), "kickoff_at")
        except ValueError:
            skipped_by_reason["invalid_kickoff"] = skipped_by_reason.get("invalid_kickoff", 0) + 1
            continue
        if kickoff < now or kickoff > horizon:
            skipped_by_reason["outside_prewarm_horizon"] = (
                skipped_by_reason.get("outside_prewarm_horizon", 0) + 1
            )
            continue
        for field in ("home_team", "away_team"):
            team_name = match.get(field)
            if not isinstance(team_name, str) or not team_name.strip():
                skipped_by_reason["official_team_name_invalid"] = (
                    skipped_by_reason.get("official_team_name_invalid", 0) + 1
                )
                continue
            team_name = team_name.strip()
            key = re.sub(r"\s+", "", team_name).casefold()
            if key in pending:
                continue
            if key in skipped_team_names:
                skipped_by_reason["wikidata_network_cooldown"] = (
                    skipped_by_reason.get("wikidata_network_cooldown", 0) + 1
                )
                continue
            try:
                cached = cached_wikidata_team_identity(source_link_registry_path, team_name)
            except ValueError:
                cached = None
            if cached is not None:
                cache_hit_team_count += 1
                continue
            if not network_enabled:
                skipped_by_reason["wikidata_network_circuit_cooldown"] = (
                    skipped_by_reason.get("wikidata_network_circuit_cooldown", 0) + 1
                )
                continue
            pending[key] = team_name

    def resolve(team_name):
        try:
            identity = resolve_team_identity(team_name, captured_at=now.isoformat())
            if identity.get("status") != "resolved":
                return identity
            record_wikidata_team_identity(source_link_registry_path, identity)
            return identity
        except HTTPError as error:
            if error.code in {401, 403, 429}:
                return {
                    "status": "unresolved", "reason": str(error),
                    "failure_category": "access_restricted",
                    "failure_code": f"http_{error.code}",
                }
            return {"status": "unresolved", "reason": str(error)}
        except OSError as error:
            return {
                "status": "unresolved", "reason": str(error),
                "failure_category": "network_error",
            }
        except (ValueError, KeyError, TypeError) as error:
            return {"status": "unresolved", "reason": str(error)}

    resolved = {}
    pending_items = list(pending.items())
    if pending_items:
        first_key, first_name = pending_items.pop(0)
        first_result = resolve(first_name)
        resolved[first_key] = first_result
        failure_category = first_result.get("failure_category")
        if failure_category in {"network_error", "access_restricted"}:
            reason = (
                "wikidata_access_restricted_this_run"
                if failure_category == "access_restricted"
                else "wikidata_network_unavailable_this_run"
            )
            skipped_by_reason[reason] = len(pending_items)
            pending_items = []
    while pending_items:
        batch = pending_items[:max_workers]
        pending_items = pending_items[max_workers:]
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {key: executor.submit(resolve, name) for key, name in batch}
            batch_results = {key: future.result() for key, future in futures.items()}
        resolved.update(batch_results)
        failure_category = next(
            (
                result.get("failure_category")
                for result in batch_results.values()
                if result.get("failure_category") in {"network_error", "access_restricted"}
            ),
            None,
        )
        if failure_category is not None:
            reason = (
                "wikidata_access_restricted_this_run"
                if failure_category == "access_restricted"
                else "wikidata_network_unavailable_this_run"
            )
            skipped_by_reason[reason] = len(pending_items)
            break

    items = [
        {"team_name": pending[key], **result}
        for key, result in sorted(resolved.items(), key=lambda item: pending[item[0]])
    ]
    return {
        "captured_at": now.isoformat(),
        "hours_ahead": hours_ahead,
        "scanned_count": len(schedule.get("matches", [])),
        "cache_hit_team_count": cache_hit_team_count,
        "resolved_team_count": sum(item.get("status") == "resolved" for item in resolved.values()),
        "unresolved_team_count": sum(item.get("status") != "resolved" for item in resolved.values()),
        "network_failure_count": sum(
            item.get("failure_category") == "network_error" for item in resolved.values()
        ),
        "rate_limit_failure_count": sum(
            item.get("failure_code") == "http_429" for item in resolved.values()
        ),
        "skipped_by_reason": skipped_by_reason,
        "items": items,
    }


def _fotmob_search_name(team_name, source_aliases, source_link_registry_path):
    """Prefer reviewed aliases, then a strict Wikidata English identity anchor."""
    team_key = re.sub(r"\s+", "", team_name).casefold()
    reviewed = source_aliases.get(team_key)
    if isinstance(reviewed, str) and reviewed.strip():
        return reviewed.strip(), "reviewed_alias"
    try:
        identity = cached_wikidata_team_identity(source_link_registry_path, team_name)
    except ValueError:
        identity = None
    english_name = identity.get("english_name") if isinstance(identity, dict) else None
    if isinstance(english_name, str) and english_name.strip():
        return english_name.strip(), "wikidata_anchor"
    return team_name, "official_name"


def prewarm_fotmob_source_links(
    hours_ahead=48,
    max_workers=2,
    now=None,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    upgrade_legacy=False,
    require_verified_search_name=False,
):
    """Cache strictly resolved FotMob team pages for upcoming open matches only."""
    if not isinstance(hours_ahead, int) or hours_ahead < 1 or hours_ahead > 168:
        raise ValueError("hours_ahead must be between 1 and 168")
    if not isinstance(max_workers, int) or max_workers < 1 or max_workers > 4:
        raise ValueError("max_workers must be between 1 and 4")
    if not isinstance(upgrade_legacy, bool):
        raise ValueError("upgrade_legacy must be a boolean")
    if not isinstance(require_verified_search_name, bool):
        raise ValueError("require_verified_search_name must be a boolean")
    now = now or datetime.now(timezone.utc).astimezone()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")

    horizon = now + timedelta(hours=hours_ahead)
    schedule = collect_schedule(method="concern")
    source_aliases = adopted_source_aliases(source_alias_registry_path)
    candidates = []
    pending_teams = {}
    skipped_by_reason = {}
    cache_hit_team_count = 0
    legacy_upgrade_requested_count = 0
    wikidata_anchor_team_count = 0

    for match in schedule.get("matches", []):
        if str(match.get("status", "")).strip() != "2":
            skipped_by_reason["official_sale_not_open"] = (
                skipped_by_reason.get("official_sale_not_open", 0) + 1
            )
            continue
        try:
            kickoff = _timestamp(match.get("kickoff_at"), "kickoff_at")
            cached = cached_source_links(source_link_registry_path, match)
        except (TypeError, ValueError):
            skipped_by_reason["official_identity_invalid"] = (
                skipped_by_reason.get("official_identity_invalid", 0) + 1
            )
            continue
        if kickoff < now or kickoff > horizon:
            skipped_by_reason["outside_prewarm_horizon"] = (
                skipped_by_reason.get("outside_prewarm_horizon", 0) + 1
            )
            continue

        candidates.append(match)
        for side, team_field in (("home", "home_team"), ("away", "away_team")):
            team_name = match.get(team_field)
            if not isinstance(team_name, str) or not team_name.strip():
                skipped_by_reason["official_team_name_invalid"] = (
                    skipped_by_reason.get("official_team_name_invalid", 0) + 1
                )
                continue
            if cached.get(f"{side}_fotmob_url") and (
                not upgrade_legacy or cached_fotmob_team_identity(
                    source_link_registry_path,
                    team_name,
                ) is not None
            ):
                cache_hit_team_count += 1
                continue
            if cached.get(f"{side}_fotmob_url"):
                legacy_upgrade_requested_count += 1
            team_key = re.sub(r"\s+", "", team_name).casefold()
            search_name, search_name_origin = _fotmob_search_name(
                team_name.strip(), source_aliases, source_link_registry_path,
            )
            if require_verified_search_name and search_name_origin == "official_name":
                skipped_by_reason["missing_verified_fotmob_search_name"] = (
                    skipped_by_reason.get("missing_verified_fotmob_search_name", 0) + 1
                )
                continue
            if search_name_origin == "wikidata_anchor":
                wikidata_anchor_team_count += 1
            pending_teams.setdefault(team_key, {
                "team_name": team_name.strip(),
                "search_name": search_name,
                "search_name_origin": search_name_origin,
                "matches": [],
            })["matches"].append((match, side))

    def resolve(team):
        try:
            return {"status": "resolved", "candidate": discover_team_stats_url(team["search_name"])}
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {"status": "unresolved", "reason": str(error)}

    resolved = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(pending_teams) or 1)) as executor:
        futures = {
            team_key: executor.submit(resolve, team)
            for team_key, team in pending_teams.items()
        }
        for team_key, future in futures.items():
            resolved[team_key] = future.result()

    resolved_team_count = 0
    unresolved_team_count = 0
    items = []
    for team_key, team in pending_teams.items():
        result = resolved[team_key]
        if result["status"] != "resolved":
            unresolved_team_count += 1
            items.append({
                "team_name": team["team_name"],
                "status": "unresolved",
                "reason": result["reason"],
            })
            continue
        source_url = result["candidate"].get("source_url")
        linked_matches = len(team["matches"])
        try:
            record_fotmob_team_identity(
                source_link_registry_path,
                team["team_name"],
                result["candidate"],
            )
        except (OSError, ValueError, TypeError) as error:
            result = {"status": "unresolved", "reason": str(error)}
        if result["status"] != "resolved":
            unresolved_team_count += 1
            items.append({
                "team_name": team["team_name"],
                "status": "unresolved",
                "reason": result["reason"],
            })
            continue
        resolved_team_count += 1
        items.append({
            "team_name": team["team_name"],
            "status": "resolved",
            "source_url": source_url,
            **({"search_name": team["search_name"]} if team["search_name"] != team["team_name"] else {}),
            "linked_match_count": linked_matches,
        })

    return {
        "captured_at": now.isoformat(),
        "hours_ahead": hours_ahead,
        "scanned_count": len(schedule.get("matches", [])),
        "candidate_match_count": len(candidates),
        "cache_hit_team_count": cache_hit_team_count,
        "wikidata_anchor_team_count": wikidata_anchor_team_count,
        "legacy_upgrade_requested_count": legacy_upgrade_requested_count,
        "resolved_team_count": resolved_team_count,
        "unresolved_team_count": unresolved_team_count,
        "skipped_by_reason": skipped_by_reason,
        "items": items,
    }


def historical_fotmob_source_prewarm(
    days_back=548,
    max_dates_per_run=7,
    max_workers=2,
    now=None,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    reviewed_alias_registry_path=DEFAULT_FOTMOB_SEARCH_ALIAS_REGISTRY_PATH,
    progress_path=DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH,
):
    """Strictly prewarm FotMob identities from bounded official result history."""
    if not isinstance(days_back, int) or days_back < 1 or days_back > 548:
        raise ValueError("days_back must be between 1 and 548")
    if not isinstance(max_dates_per_run, int) or max_dates_per_run < 1 or max_dates_per_run > 14:
        raise ValueError("max_dates_per_run must be between 1 and 14")
    if not isinstance(max_workers, int) or max_workers < 1 or max_workers > 4:
        raise ValueError("max_workers must be between 1 and 4")
    now = now or datetime.now(timezone.utc).astimezone()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")

    progress = _load_historical_fotmob_prewarm_progress(progress_path)
    if progress is None:
        range_end = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
        range_start = range_end - timedelta(days=days_back - 1)
        attempted_teams = {}
        wikidata_attempted_teams = {}
        wikidata_retry_queue = {}
        wikidata_circuit_open_until = None
        last_completed_date = None
    else:
        if progress.get("version") != 1 or progress.get("days_back") != days_back:
            raise ValueError("historical FotMob prewarm progress does not match days_back")
        range_start = _historical_date(progress.get("range_start"), "progress range_start")
        range_end = _historical_date(progress.get("range_end"), "progress range_end")
        if range_start > range_end:
            raise ValueError("historical FotMob prewarm progress has an invalid date range")
        last_completed_date = progress.get("last_completed_date")
        if last_completed_date is not None:
            last_completed_date = _historical_date(last_completed_date, "progress last_completed_date")
            if not range_start <= last_completed_date <= range_end:
                raise ValueError("historical FotMob prewarm progress last_completed_date is outside its range")
        attempted_teams = progress.get("attempted_teams", {})
        if not isinstance(attempted_teams, dict):
            raise ValueError("historical FotMob prewarm progress attempted_teams is invalid")
        wikidata_attempted_teams = progress.get("wikidata_attempted_teams", {})
        if not isinstance(wikidata_attempted_teams, dict):
            raise ValueError("historical FotMob prewarm progress Wikidata attempts are invalid")
        wikidata_retry_queue = progress.get("wikidata_retry_queue", {})
        if not isinstance(wikidata_retry_queue, dict):
            raise ValueError("historical FotMob prewarm progress Wikidata retry queue is invalid")
        for team_key, queued in wikidata_retry_queue.items():
            if not isinstance(team_key, str) or not isinstance(queued, Mapping):
                raise ValueError("historical Wikidata retry queue entry is invalid")
            if not isinstance(queued.get("team_name"), str) or not queued["team_name"].strip():
                raise ValueError("historical Wikidata retry queue team is invalid")
        wikidata_circuit_open_until = progress.get("wikidata_circuit_open_until")
        if wikidata_circuit_open_until is not None:
            try:
                circuit_expiry = datetime.fromisoformat(wikidata_circuit_open_until)
            except (TypeError, ValueError) as error:
                raise ValueError("historical Wikidata circuit timestamp is invalid") from error
            if circuit_expiry.tzinfo is None:
                raise ValueError("historical Wikidata circuit timestamp needs a timezone")
            if circuit_expiry <= now:
                wikidata_circuit_open_until = None

        # Checkpoints created before the persistent retry queue stored only the
        # failed attempt. Recover those retriable entries before the historical
        # date cursor advances any further.
        legacy_retry_at = progress.get("wikidata_circuit_open_until")
        for team_key, previous_attempt in wikidata_attempted_teams.items():
            if (
                not isinstance(team_key, str)
                or not isinstance(previous_attempt, Mapping)
                or previous_attempt.get("status") != "unresolved"
                or previous_attempt.get("failure_category") not in {
                    "access_restricted", "network_error",
                }
                or team_key in wikidata_retry_queue
            ):
                continue
            team_name = previous_attempt.get("team_name")
            if not isinstance(team_name, str) or not team_name.strip():
                continue
            wikidata_retry_queue[team_key] = {
                "team_name": team_name.strip(),
                "queued_at": previous_attempt.get("attempted_at", now.isoformat()),
                **(
                    {"retry_at": legacy_retry_at}
                    if isinstance(legacy_retry_at, str)
                    else {}
                ),
                "retry_reason": previous_attempt.get(
                    "failure_code", "legacy_retriable_failure"
                ),
            }

        # Earlier cooldown runs could advance the historical date cursor while
        # FotMob attempts were recorded but no Wikidata request was permitted.
        # Recover those unresolved official names so the expiry cannot make
        # them permanently invisible to the identity pass.
        for team_key, attempted in attempted_teams.items():
            if (
                not isinstance(team_key, str)
                or not isinstance(attempted, Mapping)
                or attempted.get("status") != "unresolved"
                or team_key in wikidata_attempted_teams
                or team_key in wikidata_retry_queue
            ):
                continue
            team_name = attempted.get("team_name")
            if not isinstance(team_name, str) or not team_name.strip():
                continue
            wikidata_retry_queue[team_key] = {
                "team_name": team_name.strip(),
                "queued_at": attempted.get("attempted_at", now.isoformat()),
                **(
                    {"retry_at": wikidata_circuit_open_until}
                    if wikidata_circuit_open_until is not None
                    else {}
                ),
                "retry_reason": "historical_uncached_attempt",
            }

    current_date = range_end if last_completed_date is None else last_completed_date - timedelta(days=1)
    dates = []
    while current_date >= range_start and len(dates) < max_dates_per_run:
        dates.append(current_date)
        current_date -= timedelta(days=1)
    next_resume_date = current_date.isoformat() if current_date >= range_start else None
    if not dates and not wikidata_retry_queue:
        return {
            "status": "complete",
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
            "scanned_dates": [],
            "next_resume_date": None,
            "official_match_count": 0,
            "unique_team_count": 0,
            "cache_hit_team_count": 0,
            "previously_attempted_team_count": 0,
            "resolved_team_count": 0,
            "unresolved_team_count": 0,
            "items": [],
        }

    official_matches = []
    scanned_dates = []
    for scan_date in dates:
        date_text = scan_date.isoformat()
        try:
            response = collect_results(match_date=date_text)
            matches = response.get("matches", []) if isinstance(response, Mapping) else None
            if not isinstance(matches, list):
                raise ValueError("official result response has no match list")
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {
                "status": "blocked",
                "range_start": range_start.isoformat(),
                "range_end": range_end.isoformat(),
                "scanned_dates": scanned_dates,
                "failed_date": date_text,
                "reason": f"official historical result collection failed: {error}",
                "next_resume_date": dates[0].isoformat(),
                "official_match_count": 0,
                "unique_team_count": 0,
                "cache_hit_team_count": 0,
                "previously_attempted_team_count": 0,
                "resolved_team_count": 0,
                "unresolved_team_count": 0,
                "items": [],
            }
        scanned_dates.append(date_text)
        official_result_url = response.get("official_result_url")
        if not isinstance(official_result_url, str) or not official_result_url:
            official_result_url = None
        official_matches.extend(
            (match, date_text, official_result_url)
            for match in matches if isinstance(match, Mapping)
        )

    source_aliases = adopted_source_aliases(source_alias_registry_path)
    reviewed_aliases = reviewed_fotmob_search_aliases(reviewed_alias_registry_path)
    historical_team_names = {}
    for team_key, queued in wikidata_retry_queue.items():
        historical_team_names[team_key] = queued["team_name"].strip()
    for match, _, _ in official_matches:
        for team_field in ("home_team", "away_team"):
            team_name = match.get(team_field)
            if isinstance(team_name, str) and team_name.strip():
                team_name = team_name.strip()
                historical_team_names.setdefault(
                    re.sub(r"\s+", "", team_name).casefold(), team_name
                )

    wikidata_items = []
    wikidata_cache_hit_team_count = 0
    wikidata_resolved_team_count = 0
    wikidata_unresolved_team_count = 0
    wikidata_skipped_team_count = 0
    wikidata_retried_queue_team_count = 0
    wikidata_budget_skipped_team_count = 0
    wikidata_recovered_team_names = {}
    wikidata_candidates = []
    queued_keys = set(wikidata_retry_queue)
    for team_key, queued in wikidata_retry_queue.items():
        retry_at_text = queued.get("retry_at")
        if isinstance(retry_at_text, str):
            try:
                retry_at = datetime.fromisoformat(retry_at_text)
            except (TypeError, ValueError) as error:
                raise ValueError("historical Wikidata retry timestamp is invalid") from error
            if retry_at.tzinfo is None:
                raise ValueError("historical Wikidata retry timestamp needs a timezone")
            if retry_at > now:
                wikidata_skipped_team_count += 1
                continue
        wikidata_candidates.append((team_key, queued["team_name"].strip(), True))
    for team_key, team_name in historical_team_names.items():
        if team_key not in queued_keys:
            wikidata_candidates.append((team_key, team_name, False))

    wikidata_attempt_count = 0
    for team_key, team_name, from_retry_queue in wikidata_candidates:
        if cached_wikidata_team_identity(source_link_registry_path, team_name) is not None:
            wikidata_cache_hit_team_count += 1
            wikidata_retry_queue.pop(team_key, None)
            continue
        previous_attempt = wikidata_attempted_teams.get(team_key)
        if (
            isinstance(previous_attempt, Mapping)
            and previous_attempt.get("status") == "unresolved"
            and previous_attempt.get("strategy_version")
            == HISTORICAL_WIKIDATA_IDENTITY_STRATEGY_VERSION
            and previous_attempt.get("failure_category") not in {
                "access_restricted", "network_error",
            }
        ):
            wikidata_skipped_team_count += 1
            continue
        if wikidata_circuit_open_until is not None:
            wikidata_retry_queue.setdefault(team_key, {
                "team_name": team_name,
                "queued_at": now.isoformat(),
                "retry_at": wikidata_circuit_open_until,
                "retry_reason": "circuit_cooldown",
            })
            wikidata_skipped_team_count += 1
            continue
        if wikidata_attempt_count >= HISTORICAL_WIKIDATA_IDENTITY_ATTEMPT_BUDGET:
            wikidata_retry_queue.setdefault(team_key, {
                "team_name": team_name,
                "queued_at": now.isoformat(),
                "retry_reason": "request_budget",
            })
            wikidata_budget_skipped_team_count += 1
            continue
        wikidata_attempt_count += 1
        if from_retry_queue:
            wikidata_retried_queue_team_count += 1
        try:
            identity = resolve_team_identity(team_name, captured_at=now.isoformat())
            if identity.get("status") == "resolved":
                record_wikidata_team_identity(source_link_registry_path, identity)
                wikidata_resolved_team_count += 1
                wikidata_retry_queue.pop(team_key, None)
                if from_retry_queue:
                    wikidata_recovered_team_names[team_key] = team_name
                result = {"status": "resolved", "english_name": identity["english_name"]}
            else:
                wikidata_unresolved_team_count += 1
                wikidata_retry_queue.pop(team_key, None)
                result = {
                    "status": "unresolved",
                    "reason": identity.get("reason", "wikidata_identity_unresolved"),
                }
        except HTTPError as error:
            wikidata_unresolved_team_count += 1
            access_restricted = error.code in {401, 403, 429}
            if access_restricted:
                cooldown_until = _wikidata_rate_limit_retry_at(error, now)
                wikidata_circuit_open_until = cooldown_until.isoformat()
                wikidata_retry_queue[team_key] = {
                    "team_name": team_name,
                    "queued_at": now.isoformat(),
                    "retry_at": cooldown_until.isoformat(),
                    "retry_reason": f"http_{error.code}",
                }
            result = {
                "status": "unresolved", "reason": str(error),
                "failure_category": (
                    "access_restricted" if access_restricted else "network_error"
                ),
                "failure_code": f"http_{error.code}",
            }
        except OSError as error:
            wikidata_unresolved_team_count += 1
            wikidata_circuit_open_until = (now + timedelta(hours=12)).isoformat()
            wikidata_retry_queue[team_key] = {
                "team_name": team_name,
                "queued_at": now.isoformat(),
                "retry_at": wikidata_circuit_open_until,
                "retry_reason": "network_error",
            }
            result = {
                "status": "unresolved", "reason": str(error),
                "failure_category": "network_error",
            }
        except (ValueError, KeyError, TypeError) as error:
            wikidata_unresolved_team_count += 1
            result = {"status": "unresolved", "reason": str(error)}
        wikidata_attempted_teams[team_key] = {
            "team_name": team_name,
            "attempted_at": now.isoformat(),
            "strategy_version": HISTORICAL_WIKIDATA_IDENTITY_STRATEGY_VERSION,
            **result,
        }
        wikidata_items.append({"team_name": team_name, **result})
        if result.get("failure_category") == "access_restricted":
            break

    pending_teams = {}
    cache_hit_team_count = 0
    previously_attempted_team_count = 0
    for match, scan_date, official_result_url in official_matches:
        for team_field in ("home_team", "away_team"):
            team_name = match.get(team_field)
            if not isinstance(team_name, str) or not team_name.strip():
                continue
            team_name = team_name.strip()
            team_key = re.sub(r"\s+", "", team_name).casefold()
            requested_search_name = reviewed_aliases.get(team_key)
            if requested_search_name is None:
                requested_search_name = source_aliases.get(team_key)
            if requested_search_name is None:
                wikidata_identity = cached_wikidata_team_identity(
                    source_link_registry_path, team_name
                )
                requested_search_name = (
                    wikidata_identity.get("english_name")
                    if isinstance(wikidata_identity, Mapping)
                    else team_name
                )
            search_name = FOTMOB_SEARCH_ALIASES.get(requested_search_name, requested_search_name)
            reference_url = match.get("official_result_url")
            if not isinstance(reference_url, str) or not reference_url:
                reference_url = official_result_url
            references = [_historical_fotmob_official_reference(match, scan_date, reference_url)]
            if cached_fotmob_team_identity(source_link_registry_path, team_name) is not None:
                cache_hit_team_count += 1
                continue
            previous_attempt = attempted_teams.get(team_key)
            if isinstance(previous_attempt, Mapping) and previous_attempt.get("search_name", team_name) == search_name:
                previous_attempt["official_references"] = _merge_historical_fotmob_references(
                    previous_attempt.get("official_references"), references
                )
                previously_attempted_team_count += 1
                continue
            pending = pending_teams.setdefault(team_key, {
                "team_name": team_name,
                "search_name": search_name,
                "official_references": [],
            })
            pending["official_references"] = _merge_historical_fotmob_references(
                pending.get("official_references"), references
            )

    # A recovered queue item may have no newly scanned historical date. It is
    # still a valid identity-only candidate because its strict Wikidata anchor
    # was just refreshed; keep the official-reference list empty rather than
    # inventing a match reference.
    for team_key, team_name in wikidata_recovered_team_names.items():
        if team_key in pending_teams or cached_fotmob_team_identity(
            source_link_registry_path, team_name
        ) is not None:
            continue
        requested_search_name = reviewed_aliases.get(team_key)
        if requested_search_name is None:
            requested_search_name = source_aliases.get(team_key)
        if requested_search_name is None:
            wikidata_identity = cached_wikidata_team_identity(
                source_link_registry_path, team_name
            )
            requested_search_name = wikidata_identity["english_name"]
        pending_teams[team_key] = {
            "team_name": team_name,
            "search_name": FOTMOB_SEARCH_ALIASES.get(
                requested_search_name, requested_search_name
            ),
            "official_references": [],
        }

    def resolve(team):
        try:
            return {"status": "resolved", "candidate": discover_team_stats_url(team["search_name"])}
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {"status": "unresolved", **_historical_fotmob_failure(error, team["search_name"])}

    resolved = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(pending_teams) or 1)) as executor:
        futures = {
            team_key: executor.submit(resolve, team)
            for team_key, team in pending_teams.items()
        }
        for team_key, future in futures.items():
            resolved[team_key] = future.result()

    captured_at = now.isoformat()
    resolved_team_count = 0
    unresolved_team_count = 0
    items = []
    for team_key, team in pending_teams.items():
        result = resolved[team_key]
        status = result["status"]
        reason = result.get("reason")
        failure_code = result.get("failure_code")
        failure_category = result.get("failure_category")
        remediation = result.get("remediation")
        source_url = None
        if status == "resolved":
            try:
                record_fotmob_team_identity(
                    source_link_registry_path,
                    team["team_name"],
                    result["candidate"],
                )
                source_url = result["candidate"]["source_url"]
            except (OSError, ValueError, TypeError) as error:
                status = "unresolved"
                reason = str(error)
        attempted_teams[team_key] = {
            "team_name": team["team_name"],
            "search_name": team["search_name"],
            "status": status,
            "attempted_at": captured_at,
            "official_references": team["official_references"],
            **({"reason": reason} if status != "resolved" and reason else {}),
            **({"failure_code": failure_code} if status != "resolved" and failure_code else {}),
            **({"failure_category": failure_category} if status != "resolved" and failure_category else {}),
            **({"remediation": remediation} if status != "resolved" and remediation else {}),
        }
        if status == "resolved":
            resolved_team_count += 1
            items.append({
                "team_name": team["team_name"],
                "status": "resolved",
                "source_url": source_url,
                **({"search_name": team["search_name"]} if team["search_name"] != team["team_name"] else {}),
            })
        else:
            unresolved_team_count += 1
            items.append({
                "team_name": team["team_name"],
                "status": "unresolved",
                "reason": reason or "strict FotMob identity could not be recorded",
                **({"failure_code": failure_code} if failure_code else {}),
                **({"failure_category": failure_category} if failure_category else {}),
                **({"remediation": remediation} if remediation else {}),
            })

    last_completed = dates[-1] if dates else last_completed_date
    _write_historical_fotmob_prewarm_progress(progress_path, {
        "version": 1,
        "days_back": days_back,
        "range_start": range_start.isoformat(),
        "range_end": range_end.isoformat(),
        "last_completed_date": last_completed.isoformat(),
        "next_resume_date": next_resume_date,
        "attempted_teams": attempted_teams,
        "wikidata_attempted_teams": wikidata_attempted_teams,
        "wikidata_retry_queue": wikidata_retry_queue,
        **(
            {"wikidata_circuit_open_until": wikidata_circuit_open_until}
            if wikidata_circuit_open_until is not None
            else {}
        ),
    })
    return {
        "status": "complete" if next_resume_date is None else "in_progress",
        "captured_at": captured_at,
        "range_start": range_start.isoformat(),
        "range_end": range_end.isoformat(),
        "scanned_dates": scanned_dates,
        "next_resume_date": next_resume_date,
        "wikidata": {
            "cache_hit_team_count": wikidata_cache_hit_team_count,
            "resolved_team_count": wikidata_resolved_team_count,
            "unresolved_team_count": wikidata_unresolved_team_count,
            "skipped_team_count": wikidata_skipped_team_count,
            "retried_queue_team_count": wikidata_retried_queue_team_count,
            "budget_skipped_team_count": wikidata_budget_skipped_team_count,
            "retry_queue_count": len(wikidata_retry_queue),
            "circuit_open_until": wikidata_circuit_open_until,
            "items": wikidata_items,
        },
        "official_match_count": len(official_matches),
        "unique_team_count": len(pending_teams),
        "cache_hit_team_count": cache_hit_team_count,
        "previously_attempted_team_count": previously_attempted_team_count,
        "resolved_team_count": resolved_team_count,
        "unresolved_team_count": unresolved_team_count,
        "items": items,
    }


def daily_fotmob_identity_map(
    hours_ahead=72,
    result_days=3,
    max_workers=2,
    now=None,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    reviewed_alias_registry_path=DEFAULT_FOTMOB_SEARCH_ALIAS_REGISTRY_PATH,
    historical_progress_path=DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH,
    daily_progress_path=DEFAULT_DAILY_FOTMOB_IDENTITY_PROGRESS_PATH,
    retry_wikidata_now=False,
):
    """Refresh strict team identities from upcoming fixtures and recent results."""
    if not isinstance(hours_ahead, int) or not 1 <= hours_ahead <= 168:
        raise ValueError("daily FotMob identity hours_ahead must be between 1 and 168")
    if not isinstance(result_days, int) or not 1 <= result_days <= 7:
        raise ValueError("daily FotMob identity result_days must be between 1 and 7")
    if not isinstance(max_workers, int) or not 1 <= max_workers <= 4:
        raise ValueError("daily FotMob identity max_workers must be between 1 and 4")
    if not isinstance(retry_wikidata_now, bool):
        raise ValueError("retry_wikidata_now must be a boolean")
    now = now or datetime.now(timezone.utc).astimezone()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("daily FotMob identity now must be a timezone-aware datetime")

    attempted_teams = {}
    active_wikidata_cooldowns = {}
    active_wikidata_rate_limit_cooldowns = {}
    wikidata_network_circuit_open_until = None
    wikidata_rate_limit_circuit_open_until = None
    for progress_path in (historical_progress_path, daily_progress_path):
        progress = _load_historical_fotmob_prewarm_progress(progress_path)
        if progress is None:
            continue
        attempts = progress.get("attempted_teams")
        if progress.get("version") != 1 or not isinstance(attempts, dict):
            raise ValueError("FotMob identity progress attempted_teams is invalid")
        attempted_teams.update(attempts)
        if Path(progress_path) == Path(daily_progress_path):
            cooldowns = progress.get("wikidata_network_cooldowns", {})
            if not isinstance(cooldowns, Mapping):
                raise ValueError("daily Wikidata network cooldowns are invalid")
            for team_name, expires_at in cooldowns.items():
                if not isinstance(team_name, str) or not team_name.strip():
                    raise ValueError("daily Wikidata network cooldown team is invalid")
                try:
                    expiry = datetime.fromisoformat(expires_at)
                except (TypeError, ValueError) as error:
                    raise ValueError("daily Wikidata network cooldown timestamp is invalid") from error
                if expiry.tzinfo is None:
                    raise ValueError("daily Wikidata network cooldown timestamp needs a timezone")
                if expiry > now:
                    active_wikidata_cooldowns[team_name.strip()] = expiry.isoformat()
            rate_limit_cooldowns = progress.get("wikidata_rate_limit_cooldowns", {})
            if not isinstance(rate_limit_cooldowns, Mapping):
                raise ValueError("daily Wikidata rate-limit cooldowns are invalid")
            for team_name, expires_at in rate_limit_cooldowns.items():
                if not isinstance(team_name, str) or not team_name.strip():
                    raise ValueError("daily Wikidata rate-limit cooldown team is invalid")
                try:
                    expiry = datetime.fromisoformat(expires_at)
                except (TypeError, ValueError) as error:
                    raise ValueError("daily Wikidata rate-limit cooldown timestamp is invalid") from error
                if expiry.tzinfo is None:
                    raise ValueError("daily Wikidata rate-limit cooldown timestamp needs a timezone")
                if expiry > now:
                    active_wikidata_rate_limit_cooldowns[team_name.strip()] = expiry.isoformat()
            circuit_open_until = progress.get("wikidata_network_circuit_open_until")
            if circuit_open_until is not None:
                try:
                    circuit_expiry = datetime.fromisoformat(circuit_open_until)
                except (TypeError, ValueError) as error:
                    raise ValueError("daily Wikidata network circuit timestamp is invalid") from error
                if circuit_expiry.tzinfo is None:
                    raise ValueError("daily Wikidata network circuit timestamp needs a timezone")
                if circuit_expiry > now:
                    wikidata_network_circuit_open_until = circuit_expiry.isoformat()
            rate_limit_circuit_open_until = progress.get("wikidata_rate_limit_circuit_open_until")
            if rate_limit_circuit_open_until is not None:
                try:
                    rate_limit_circuit_expiry = datetime.fromisoformat(rate_limit_circuit_open_until)
                except (TypeError, ValueError) as error:
                    raise ValueError("daily Wikidata rate-limit circuit timestamp is invalid") from error
                if rate_limit_circuit_expiry.tzinfo is None:
                    raise ValueError("daily Wikidata rate-limit circuit timestamp needs a timezone")
                if rate_limit_circuit_expiry > now:
                    wikidata_rate_limit_circuit_open_until = rate_limit_circuit_expiry.isoformat()

    range_end = now.astimezone(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    range_start = range_end - timedelta(days=result_days - 1)
    if retry_wikidata_now:
        active_wikidata_cooldowns = {}
        wikidata_network_circuit_open_until = None
    _write_historical_fotmob_prewarm_progress(daily_progress_path, {
        "version": 1,
        "days_back": result_days,
        "range_start": range_start.isoformat(),
        "range_end": range_end.isoformat(),
        "last_completed_date": None,
        "next_resume_date": range_end.isoformat(),
        "attempted_teams": attempted_teams,
    })

    wikidata = prewarm_wikidata_source_identities(
        hours_ahead=hours_ahead,
        max_workers=max_workers,
        now=now,
        source_link_registry_path=source_link_registry_path,
        skip_team_names=(
            set(active_wikidata_cooldowns)
            | set(active_wikidata_rate_limit_cooldowns)
        ),
        network_enabled=(
            wikidata_network_circuit_open_until is None
            and wikidata_rate_limit_circuit_open_until is None
        ),
    )
    upcoming = prewarm_fotmob_source_links(
        hours_ahead=hours_ahead,
        max_workers=max_workers,
        now=now,
        source_link_registry_path=source_link_registry_path,
        source_alias_registry_path=source_alias_registry_path,
        require_verified_search_name=True,
    )
    recent_results = historical_fotmob_source_prewarm(
        days_back=result_days,
        max_dates_per_run=result_days,
        max_workers=max_workers,
        now=now,
        source_link_registry_path=source_link_registry_path,
        source_alias_registry_path=source_alias_registry_path,
        reviewed_alias_registry_path=reviewed_alias_registry_path,
        progress_path=daily_progress_path,
    )
    network_cooldown_until = (now + timedelta(hours=12)).isoformat()
    rate_limit_cooldown_until = (now + timedelta(hours=1)).isoformat()
    for item in wikidata.get("items", []):
        if item.get("failure_category") == "network_error":
            team_name = item.get("team_name")
            if isinstance(team_name, str) and team_name.strip():
                active_wikidata_cooldowns[team_name.strip()] = network_cooldown_until
                wikidata_network_circuit_open_until = network_cooldown_until
        elif item.get("failure_category") == "access_restricted":
            team_name = item.get("team_name")
            if isinstance(team_name, str) and team_name.strip():
                active_wikidata_rate_limit_cooldowns[team_name.strip()] = rate_limit_cooldown_until
                wikidata_rate_limit_circuit_open_until = rate_limit_cooldown_until
    daily_progress = _load_historical_fotmob_prewarm_progress(daily_progress_path)
    if daily_progress is None:
        raise ValueError("daily FotMob identity progress was not created")
    daily_progress["wikidata_network_cooldowns"] = active_wikidata_cooldowns
    daily_progress["wikidata_rate_limit_cooldowns"] = active_wikidata_rate_limit_cooldowns
    if wikidata_network_circuit_open_until is not None:
        daily_progress["wikidata_network_circuit_open_until"] = wikidata_network_circuit_open_until
    else:
        daily_progress.pop("wikidata_network_circuit_open_until", None)
    if wikidata_rate_limit_circuit_open_until is not None:
        daily_progress["wikidata_rate_limit_circuit_open_until"] = wikidata_rate_limit_circuit_open_until
    else:
        daily_progress.pop("wikidata_rate_limit_circuit_open_until", None)
    _write_historical_fotmob_prewarm_progress(daily_progress_path, daily_progress)
    return {
        "captured_at": now.isoformat(),
        "recent_result_window": {
            "start": range_start.isoformat(),
            "end": range_end.isoformat(),
        },
        "wikidata": wikidata,
        "wikidata_network_cooldown_count": len(active_wikidata_cooldowns),
        "wikidata_network_circuit_open": wikidata_network_circuit_open_until is not None,
        "wikidata_network_circuit_open_until": wikidata_network_circuit_open_until,
        "wikidata_rate_limit_cooldown_count": len(active_wikidata_rate_limit_cooldowns),
        "wikidata_rate_limit_circuit_open": wikidata_rate_limit_circuit_open_until is not None,
        "wikidata_rate_limit_circuit_open_until": wikidata_rate_limit_circuit_open_until,
        "wikidata_retry_forced": retry_wikidata_now,
        "upcoming": upcoming,
        "recent_results": recent_results,
        "ledger_action": "none",
    }


def historical_fotmob_alias_candidates(
    max_teams=25,
    now=None,
    progress_path=DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH,
    reviewed_alias_registry_path=DEFAULT_FOTMOB_SEARCH_ALIAS_REGISTRY_PATH,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    current_matches=None,
    output_path=DEFAULT_HISTORICAL_FOTMOB_ALIAS_CANDIDATE_QUEUE_PATH,
):
    """Write a replaceable, read-only review queue for unresolved FotMob names."""
    if not isinstance(max_teams, int) or not 1 <= max_teams <= 100:
        raise ValueError("max_teams must be between 1 and 100")
    now = now or datetime.now(timezone.utc).astimezone()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    progress = _load_historical_fotmob_prewarm_progress(progress_path)
    if progress is None:
        raise ValueError("historical FotMob prewarm progress file does not exist")
    attempted_teams = progress.get("attempted_teams")
    if not isinstance(attempted_teams, dict):
        raise ValueError("historical FotMob prewarm progress attempted_teams is invalid")

    schedule_error = None
    if current_matches is None:
        try:
            schedule = collect_schedule()
            current_matches = schedule.get("matches", []) if isinstance(schedule, Mapping) else []
        except (OSError, ValueError, KeyError, TypeError) as error:
            current_matches = []
            schedule_error = str(error)
    if not isinstance(current_matches, list):
        raise ValueError("current_matches must be a list when provided")
    current_team_keys = {
        re.sub(r"\s+", "", team_name).casefold()
        for match in current_matches if isinstance(match, Mapping)
        for team_name in (match.get("home_team"), match.get("away_team"))
        if isinstance(team_name, str) and team_name.strip()
    }
    reviewed_aliases = reviewed_fotmob_search_aliases(reviewed_alias_registry_path)
    adopted_aliases = adopted_source_aliases(source_alias_registry_path)

    candidates = []
    for team_key, attempt in attempted_teams.items():
        if not isinstance(team_key, str) or not isinstance(attempt, Mapping):
            continue
        team_name = attempt.get("team_name")
        if attempt.get("status") != "unresolved" or not isinstance(team_name, str) or not team_name.strip():
            continue
        team_name = team_name.strip()
        if cached_fotmob_team_identity(source_link_registry_path, team_name) is not None:
            continue
        original_search_name = attempt.get("search_name")
        if not isinstance(original_search_name, str) or not original_search_name.strip():
            original_search_name = team_name
        search_name = (
            reviewed_aliases.get(team_key)
            or adopted_aliases.get(team_key)
            or FOTMOB_SEARCH_ALIASES.get(original_search_name.strip(), original_search_name.strip())
        )
        references = attempt.get("official_references", [])
        references = references if isinstance(references, list) else []
        occurrence_count = max(1, len(references))
        candidates.append({
            "team_key": team_key,
            "official_team": team_name,
            "original_search_name": original_search_name,
            "search_name": search_name,
            "failure_reason": attempt.get("reason") or "strict FotMob identity was not resolved",
            "attempted_at": attempt.get("attempted_at"),
            "official_references": references,
            "occurrence_count": occurrence_count,
            "current_schedule_relevant": team_key in current_team_keys,
        })
    candidates.sort(
        key=lambda item: (
            item["current_schedule_relevant"],
            item["occurrence_count"],
            item["attempted_at"] or "",
            item["official_team"],
        ),
        reverse=True,
    )

    items = []
    for candidate in candidates[:max_teams]:
        try:
            search_evidence = collect_team_search_candidates(candidate["search_name"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            search_evidence = {
                "status": "unavailable",
                "reason": str(error),
                "search_term": candidate["search_name"],
            }
        items.append({
            **candidate,
            "search_evidence": search_evidence,
        })
    queue = {
        "version": 1,
        "generated_at": now.isoformat(),
        "progress_path": str(progress_path),
        "current_schedule_match_count": len(current_matches),
        "current_schedule_unavailable_reason": schedule_error,
        "unresolved_team_count": len(candidates),
        "queued_team_count": len(items),
        "items": items,
    }
    _write_json_atomically(output_path, queue)
    return queue


def _legacy_historical_fotmob_failure(attempt):
    recorded_search_name = attempt.get("search_name")
    search_name = recorded_search_name or attempt.get("team_name") or ""
    reason = str(attempt.get("reason") or "")
    if "urlopen error" in reason or "timed out" in reason or "connection" in reason.lower():
        return {
            "failure_code": "network_error",
            "failure_category": "transport_error",
            "remediation": "retry_later",
        }
    if "ambiguous FotMob" in reason:
        return {
            "failure_code": "ambiguous_identity",
            "failure_category": "strict_identity_ambiguous",
            "remediation": "review_search_candidates",
        }
    if "no high-confidence FotMob" in reason:
        if not isinstance(recorded_search_name, str) or not recorded_search_name.strip():
            return {
                "failure_code": "legacy_search_term_unrecorded",
                "failure_category": "legacy_search_term_unrecorded",
                "remediation": "retry_with_recorded_search_term",
            }
        if FOTMOB_SEARCH_ALIASES.get(search_name, search_name) != search_name:
            return {
                "failure_code": "legacy_search_response_unrecorded",
                "failure_category": "legacy_static_alias_response_unrecorded",
                "remediation": "review_search_candidates",
            }
        if _contains_cjk(search_name):
            return {
                "failure_code": "legacy_strict_search_failure",
                "failure_category": "search_alias_gap_suspected",
                "remediation": "review_search_alias",
            }
        return {
            "failure_code": "legacy_strict_search_failure",
            "failure_category": "strict_identity_mismatch",
            "remediation": "review_search_candidates",
        }
    return {
        "failure_code": "legacy_unclassified",
        "failure_category": "unclassified",
        "remediation": "inspect_failure",
    }


def historical_fotmob_failure_report(progress_path=DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH):
    """Summarize mutable historical lookup failures without re-collecting sources."""
    progress = _load_historical_fotmob_prewarm_progress(progress_path)
    if progress is None:
        raise ValueError("historical FotMob prewarm progress file does not exist")
    attempted_teams = progress.get("attempted_teams")
    if not isinstance(attempted_teams, dict):
        raise ValueError("historical FotMob prewarm progress attempted_teams is invalid")
    items = []
    counts = {}
    for team_key, attempt in attempted_teams.items():
        if not isinstance(team_key, str) or not isinstance(attempt, Mapping):
            continue
        if attempt.get("status") != "unresolved":
            continue
        structured = all(
            isinstance(attempt.get(field), str) and attempt.get(field)
            for field in ("failure_code", "failure_category", "remediation")
        )
        classification = (
            {
                "failure_code": attempt["failure_code"],
                "failure_category": attempt["failure_category"],
                "remediation": attempt["remediation"],
            }
            if structured else _legacy_historical_fotmob_failure(attempt)
        )
        category = classification["failure_category"]
        counts[category] = counts.get(category, 0) + 1
        items.append({
            "team_key": team_key,
            "team_name": attempt.get("team_name"),
            "search_name": attempt.get("search_name"),
            "attempted_at": attempt.get("attempted_at"),
            "reason": attempt.get("reason"),
            "official_references": attempt.get("official_references", []),
            "classification_basis": "structured_failure_code" if structured else "legacy_reason_text",
            **classification,
        })
    items.sort(key=lambda item: (item["failure_category"], item["team_name"] or ""))
    return {
        "version": 1,
        "progress_path": str(progress_path),
        "unresolved_team_count": len(items),
        "failure_category_counts": dict(sorted(counts.items())),
        "items": items,
    }


def _prewarm_fotmob_identities_for_matches(
    matches,
    source_link_registry_path,
    source_alias_registry_path,
    max_workers=2,
):
    """Resolve each missing official team once for a preflight match set."""
    source_aliases = adopted_source_aliases(source_alias_registry_path)
    pending_teams = {}
    match_links = {}
    cache_hit_team_count = 0
    wikidata_anchor_team_count = 0

    for match in matches:
        match_id = str(match.get("match_id"))
        match_links[match_id] = {}
        try:
            cached = cached_source_links(source_link_registry_path, match)
        except (TypeError, ValueError):
            cached = {}
        for side, team_field in (("home", "home_team"), ("away", "away_team")):
            team_name = match.get(team_field)
            source_url = cached.get(f"{side}_fotmob_url")
            if isinstance(source_url, str):
                cache_hit_team_count += 1
                match_links[match_id][side] = {
                    "status": "resolved",
                    "source_url": source_url,
                    "origin": "cache",
                }
                continue
            if not isinstance(team_name, str) or not team_name.strip():
                match_links[match_id][side] = {
                    "status": "unresolved",
                    "reason": "official_team_name_invalid",
                }
                continue
            team_name = team_name.strip()
            team_key = re.sub(r"\s+", "", team_name).casefold()
            search_name, search_name_origin = _fotmob_search_name(
                team_name, source_aliases, source_link_registry_path,
            )
            if search_name_origin == "wikidata_anchor":
                wikidata_anchor_team_count += 1
            pending_teams.setdefault(team_key, {
                "team_name": team_name,
                "search_name": search_name,
            })

    def resolve(team):
        try:
            candidate = discover_team_stats_url(team["search_name"])
            record_fotmob_team_identity(
                source_link_registry_path,
                team["team_name"],
                candidate,
            )
            return {"status": "resolved", "source_url": candidate["source_url"]}
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {"status": "unresolved", "reason": str(error)}

    resolved_teams = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(pending_teams) or 1)) as executor:
        futures = {
            team_key: executor.submit(resolve, team)
            for team_key, team in pending_teams.items()
        }
        for team_key, future in futures.items():
            resolved_teams[team_key] = future.result()

    for match in matches:
        match_id = str(match.get("match_id"))
        for side, team_field in (("home", "home_team"), ("away", "away_team")):
            if side in match_links[match_id]:
                continue
            team_name = str(match.get(team_field, "")).strip()
            team_key = re.sub(r"\s+", "", team_name).casefold()
            match_links[match_id][side] = resolved_teams[team_key]

    return match_links, {
        "fotmob_cache_hit_team_count": cache_hit_team_count,
        "fotmob_wikidata_anchor_team_count": wikidata_anchor_team_count,
        "fotmob_resolved_team_count": sum(
            item.get("status") == "resolved"
            for item in resolved_teams.values()
        ),
        "fotmob_unresolved_team_count": sum(
            item.get("status") != "resolved"
            for item in resolved_teams.values()
        ),
    }


def _identity_preflight_item(match, discovery, fotmob_links, understat_aliases, captured_at):
    """Describe only provider identities that need a strict review before freezing."""
    discovery = discovery if isinstance(discovery, Mapping) else {}
    reasons = []
    european_433 = {
        "status": discovery.get("status", "unavailable"),
        "source_url": discovery.get("source_url"),
        "reason": discovery.get("reason"),
    }
    if european_433["status"] != "resolved" or not european_433["source_url"]:
        reasons.append("european_433_identity_unresolved")

    fotmob = {}
    for side in ("home", "away"):
        item = fotmob_links.get(side, {}) if isinstance(fotmob_links, Mapping) else {}
        item = item if isinstance(item, Mapping) else {}
        fotmob[side] = {
            "status": item.get("status", "unresolved"),
            "source_url": item.get("source_url"),
            "reason": item.get("reason"),
            "origin": item.get("origin"),
        }
        if fotmob[side]["status"] != "resolved" or not fotmob[side]["source_url"]:
            reasons.append(f"fotmob_{side}_identity_unresolved")

    competition = match.get("competition")
    understat = {
        "status": "not_covered",
        "league": None,
    }
    league = UNDERSTAT_LEAGUES.get(competition)
    if league:
        understat = {"status": "resolved", "league": league}
        for side, field in (("home", "home_team"), ("away", "away_team")):
            team_key = re.sub(r"\s+", "", str(match.get(field, "")).casefold())
            source_name = understat_aliases.get(team_key)
            understat[f"{side}_source_name"] = source_name
            if not source_name:
                understat["status"] = "review_required"

    optional_xg_enhancements = []
    if understat.get("status") == "review_required":
        optional_xg_enhancements = [
            f"understat_{side}_alias_unresolved"
            for side in ("home", "away")
            if not understat.get(f"{side}_source_name")
        ]

    if not reasons:
        return None
    return {
        "match_id": match["match_id"],
        "match_num": match.get("match_num"),
        "competition": competition,
        "home_team": match.get("home_team"),
        "away_team": match.get("away_team"),
        "kickoff_at": match.get("kickoff_at"),
        "official_source_url": match.get("official_source_url"),
        "captured_at": captured_at.isoformat(),
        "reasons": reasons,
        "european_433": european_433,
        "fotmob": fotmob,
        "understat": understat,
        "optional_xg_enhancements": optional_xg_enhancements,
    }


def identity_preflight(
    output_path,
    min_lead_minutes=120,
    max_lead_minutes=180,
    now=None,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    sales_cutoff_policy_path=DEFAULT_SALES_CUTOFF_POLICY_PATH,
):
    """Write a replaceable queue of near-term matches needing identity review."""
    now = now or datetime.now(timezone.utc).astimezone()
    sales_cutoff_policy = load_policy(sales_cutoff_policy_path)
    schedule = collect_schedule(method="concern")
    eligible, skipped, _skipped_items, _timings = _eligible_schedule_matches(
        schedule,
        [],
        now,
        min_lead_minutes,
        max_lead_minutes,
        sales_cutoff_policy,
        preflight=True,
    )
    batch_433, source_link_summary = _resolve_433_source_links(
        eligible,
        source_link_registry_path,
    )
    fotmob_links, fotmob_summary = _prewarm_fotmob_identities_for_matches(
        eligible,
        source_link_registry_path,
        source_alias_registry_path,
    )
    aliases = understat_source_aliases(source_alias_registry_path)
    items = []
    for match in eligible:
        discovery = batch_433.get(str(match["match_id"]))
        item = _identity_preflight_item(
            match,
            discovery,
            fotmob_links.get(str(match["match_id"]), {}),
            aliases,
            now,
        )
        if item is not None:
            item.update(_timings.get(str(match.get("match_id")), {}))
            items.append(item)
    payload = {
        "captured_at": now.isoformat(),
        "min_lead_minutes": min_lead_minutes,
        "max_lead_minutes": max_lead_minutes,
        "sales_cutoff_policy": sales_cutoff_policy,
        "scanned_count": len(schedule.get("matches", [])),
        "eligible_count": len(eligible),
        "skipped_by_reason": skipped,
        "review_count": len(items),
        **source_link_summary,
        **fotmob_summary,
        "items": items,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(path)
    return payload


def identity_coverage_report(
    min_lead_minutes=90,
    max_lead_minutes=240,
    now=None,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    sales_cutoff_policy_path=DEFAULT_SALES_CUTOFF_POLICY_PATH,
):
    """Summarize reusable source identities without external discovery or writes."""
    now = now or datetime.now(timezone.utc).astimezone()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    sales_cutoff_policy = load_policy(sales_cutoff_policy_path)
    schedule = collect_schedule(method="concern")
    aliases = understat_source_aliases(source_alias_registry_path)
    competitions = {}
    items = []

    for match in schedule.get("matches", []):
        competition = match.get("competition") or "未标注联赛"
        summary = competitions.setdefault(competition, {
            "scheduled_count": 0,
            "european_433_cached_count": 0,
            "fotmob_both_link_count": 0,
            "fotmob_home_link_missing_count": 0,
            "fotmob_away_link_missing_count": 0,
            "understat_status": "covered" if competition in UNDERSTAT_LEAGUES else "not_covered",
            "understat_both_alias_count": 0,
            "understat_home_alias_missing_count": 0,
            "understat_away_alias_missing_count": 0,
            "understat_not_covered_count": 0,
            "freeze_window_eligible_count": 0,
            "identity_ready_count": 0,
            "block_reason_counts": {},
            "optional_xg_gap_counts": {},
        })
        summary["scheduled_count"] += 1
        required_identity_blockers = []
        optional_xg_enhancements = []
        operational_blockers = []
        timing_metadata = {}
        try:
            links = cached_source_links(source_link_registry_path, match)
        except (TypeError, ValueError):
            links = {}
        has_433 = bool(links.get("european_433_url"))
        has_home_fotmob = bool(links.get("home_fotmob_url"))
        has_away_fotmob = bool(links.get("away_fotmob_url"))
        if has_433:
            summary["european_433_cached_count"] += 1
        else:
            required_identity_blockers.append("433_link_unresolved")
        if has_home_fotmob and has_away_fotmob:
            summary["fotmob_both_link_count"] += 1
        else:
            if not has_home_fotmob:
                summary["fotmob_home_link_missing_count"] += 1
                required_identity_blockers.append("fotmob_home_link_unresolved")
            if not has_away_fotmob:
                summary["fotmob_away_link_missing_count"] += 1
                required_identity_blockers.append("fotmob_away_link_unresolved")

        understat_ready = False
        if competition in UNDERSTAT_LEAGUES:
            home_key = re.sub(r"\s+", "", str(match.get("home_team", "")).casefold())
            away_key = re.sub(r"\s+", "", str(match.get("away_team", "")).casefold())
            has_home_alias = bool(aliases.get(home_key))
            has_away_alias = bool(aliases.get(away_key))
            understat_ready = has_home_alias and has_away_alias
            if understat_ready:
                summary["understat_both_alias_count"] += 1
            else:
                if not has_home_alias:
                    summary["understat_home_alias_missing_count"] += 1
                    optional_xg_enhancements.append("understat_home_alias_unresolved")
                if not has_away_alias:
                    summary["understat_away_alias_missing_count"] += 1
                    optional_xg_enhancements.append("understat_away_alias_unresolved")
        else:
            summary["understat_not_covered_count"] += 1

        operationally_eligible = False
        try:
            kickoff = _timestamp(match.get("kickoff_at"), "kickoff_at")
            timing = assess_freeze_timing(
                kickoff,
                now,
                min_lead_minutes,
                max_lead_minutes,
                sales_cutoff_policy,
            )
            operationally_eligible = timing["eligible"]
            timing_metadata = {
                key: timing[key]
                for key in ("timing_basis", "sales_cutoff_at", "window_start_at", "window_end_at")
                if key in timing
            }
        except ValueError:
            operational_blockers.append("invalid_kickoff")
        official_status = str(match.get("status", "")).strip()
        if official_status != "2":
            operational_blockers.append({
                "1": "official_sale_pending",
                "3": "official_sales_suspended",
            }.get(official_status, "official_sale_status_unavailable"))
        elif not operationally_eligible:
            operational_blockers.append("outside_freeze_window")
        elif not isinstance(match.get("match_num"), str) or not match["match_num"].strip():
            operational_blockers.append("official_match_num_unavailable")
        else:
            summary["freeze_window_eligible_count"] += 1

        if not required_identity_blockers:
            summary["identity_ready_count"] += 1
        for reason in required_identity_blockers + operational_blockers:
            summary["block_reason_counts"][reason] = summary["block_reason_counts"].get(reason, 0) + 1
        for reason in optional_xg_enhancements:
            summary["optional_xg_gap_counts"][reason] = summary["optional_xg_gap_counts"].get(reason, 0) + 1

        if required_identity_blockers:
            priority = "preflight_required"
        elif operational_blockers:
            priority = "operationally_deferred"
        else:
            priority = "ready_for_collection"
        items.append({
            "match_id": match.get("match_id"),
            "match_num": match.get("match_num"),
            "competition": competition,
            "home_team": match.get("home_team"),
            "away_team": match.get("away_team"),
            "kickoff_at": match.get("kickoff_at"),
            "official_status": official_status or None,
            "required_identity_status": "ready" if not required_identity_blockers else "blocked",
            "required_identity_blockers": required_identity_blockers,
            "optional_xg_enhancements": optional_xg_enhancements,
            "operational_blockers": operational_blockers,
            "priority": priority,
            **timing_metadata,
        })

    return {
        "captured_at": now.isoformat(),
        "min_lead_minutes": min_lead_minutes,
        "max_lead_minutes": max_lead_minutes,
        "scanned_count": len(schedule.get("matches", [])),
        "competition_count": len(competitions),
        "competitions": dict(sorted(competitions.items())),
        "items": sorted(items, key=lambda item: (
            {"preflight_required": 0, "ready_for_collection": 1, "operationally_deferred": 2}[item["priority"]],
            item.get("kickoff_at") or "",
            str(item.get("match_id") or ""),
        )),
    }


def schedule_driven_freeze(
    ledger_path,
    min_lead_minutes=30,
    max_lead_minutes=60,
    max_workers=2,
    now=None,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    sales_cutoff_policy_path=DEFAULT_SALES_CUTOFF_POLICY_PATH,
    include_open_matches=False,
    freeze_window_minutes=0,
):
    with _ledger_operation_lock(ledger_path):
        return _schedule_driven_freeze(
            ledger_path,
            min_lead_minutes=min_lead_minutes,
            max_lead_minutes=max_lead_minutes,
            max_workers=max_workers,
            now=now,
            source_alias_registry_path=source_alias_registry_path,
            source_link_registry_path=source_link_registry_path,
            sales_cutoff_policy_path=sales_cutoff_policy_path,
            include_open_matches=include_open_matches,
            freeze_window_minutes=freeze_window_minutes,
        )


def _schedule_driven_freeze(
    ledger_path,
    min_lead_minutes=30,
    max_lead_minutes=60,
    max_workers=2,
    now=None,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    sales_cutoff_policy_path=DEFAULT_SALES_CUTOFF_POLICY_PATH,
    include_open_matches=False,
    freeze_window_minutes=0,
):
    """Freeze only review-ready paper records near kickoff from one schedule read."""
    if not isinstance(max_workers, int) or max_workers < 1 or max_workers > 4:
        raise ValueError("schedule-driven max_workers must be between 1 and 4")
    now = now or datetime.now(timezone.utc).astimezone()
    sales_cutoff_policy = load_policy(sales_cutoff_policy_path)
    path = Path(ledger_path)
    existing = read_records(ledger_path) if path.exists() else []
    schedule = collect_schedule(method="concern")
    eligible, skipped, skipped_items, timings = _eligible_schedule_matches(
        schedule,
        existing,
        now,
        min_lead_minutes,
        max_lead_minutes,
        sales_cutoff_policy,
        include_open_matches=include_open_matches,
        freeze_window_minutes=freeze_window_minutes,
    )
    batch_433, source_link_summary = _resolve_433_source_links(
        eligible,
        source_link_registry_path,
    )
    batch_xgscore, xgscore_summary = _resolve_xgscore_source_links(
        eligible,
        source_link_registry_path,
    )
    understat_batch_xg, understat_batch_groups, understat_summary = _prefetch_understat_batch_xg(
        eligible,
        source_alias_registry_path,
        source_link_registry_path,
    )
    sale_availability = _sale_availability_when_no_official_open_match(
        schedule,
        now,
        sales_cutoff_policy,
    )

    with ThreadPoolExecutor(max_workers=min(max_workers, len(eligible) or 1)) as executor:
        futures = [
            executor.submit(
                _automatic_freeze_stage,
                match,
                batch_433.get(str(match["match_id"]), {}).get("source_url"),
                understat_batch_xg.get(str(match["match_id"])),
                batch_xgscore.get(str(match["match_id"]), {}).get("source_url"),
                batch_xgscore.get(str(match["match_id"]), {}).get("xgscore_home_team"),
                batch_xgscore.get(str(match["match_id"]), {}).get("xgscore_away_team"),
                source_link_registry_path,
                source_alias_registry_path,
            )
            for match in eligible
        ]
        staged = []
        for match, future in zip(eligible, futures):
            try:
                staged.append((match, future.result(), None))
            except (OSError, ValueError, KeyError, TypeError) as error:
                staged.append((match, None, str(error)))

    accepted_primary_predictions = [
        record for record in existing
        if record.get("record_type") == "prediction"
        and record.get("prediction_role", "primary") == "primary"
    ]
    items = []
    for match, stage, error in staged:
        timing_metadata = timings.get(str(match.get("match_id")), {})
        if error is not None:
            items.append({
                "status": "blocked",
                "match_id": match["match_id"],
                "match_num": match.get("match_num"),
                "reasons": [error],
                **timing_metadata,
            })
            continue
        summary = stage["summary"]
        summary.update(timing_metadata)
        if summary["status"] != "frozen":
            items.append(summary)
            continue
        prediction = next(
            record for record in stage["records"]
            if record.get("record_type") == "prediction"
        )
        if _primary_prediction_conflict(accepted_primary_predictions, prediction):
            items.append({
                "status": "blocked",
                "match_id": match["match_id"],
                "match_num": match.get("match_num"),
                "reasons": ["a primary prediction already exists for this official match identity"],
                **timing_metadata,
            })
            continue
        accepted_primary_predictions.append(prediction)
        for record in stage["records"]:
            _append_record(ledger_path, record)
        items.append(summary)

    summary = {
        "scanned_count": len(schedule.get("matches", [])),
        "eligible_count": len(eligible),
        "skipped_by_reason": skipped,
        "skipped_items": skipped_items,
        "sales_cutoff_policy": sales_cutoff_policy,
        "freeze_window_minutes": freeze_window_minutes,
        "selection_mode": "open_matches" if include_open_matches else "scheduled_window",
        "frozen_count": sum(item["status"] == "frozen" for item in items),
        "observation_count": sum(item["status"] == "observation_only" for item in items),
        "blocked_count": sum(item["status"] == "blocked" for item in items),
        "understat_batch_groups": understat_batch_groups,
        **understat_summary,
        **source_link_summary,
        **xgscore_summary,
        "items": items,
    }
    if sale_availability is not None:
        summary["sale_availability"] = sale_availability
    return summary


def _daily_market_view(prediction, snapshot):
    """Render the frozen five-market state without inferring unavailable markets."""
    if isinstance(snapshot, Mapping):
        supplied_markets = snapshot.get("markets")
        if isinstance(supplied_markets, Mapping):
            markets = {}
            for code in ALL_MARKET_OPTIONS:
                entry = supplied_markets.get(code)
                if isinstance(entry, Mapping):
                    markets[code] = dict(entry)
                else:
                    markets[code] = {
                        "status": "not_frozen",
                        "reason": "full_market_snapshot_market_unavailable",
                    }
            return markets

    top = max(
        prediction["outcomes"],
        key=lambda outcome: outcome["probability"],
    )
    markets = {
        "spf": {
            "status": "frozen",
            "selected_option": top["label"],
            "selected_probability": top["probability"],
            "official_source_url": prediction["source_urls"]["S1"],
        },
    }
    for code in ALL_MARKET_OPTIONS:
        if code != "spf":
            markets[code] = {
                "status": "not_frozen",
                "reason": "full_market_snapshot_not_frozen",
            }
    return markets


def _freeze_level(markets):
    """Classify the frozen market surface without inferring missing markets."""
    frozen_markets = [
        code for code in ALL_MARKET_OPTIONS
        if isinstance(markets.get(code), Mapping)
        and markets[code].get("status") == "frozen"
    ]
    if frozen_markets == ["spf"]:
        return "market_baseline"
    if len(frozen_markets) == len(ALL_MARKET_OPTIONS):
        return "five_market"
    return "partial_markets"


def daily_freeze_status(ledger_path, scheduler_log_path, date=None):
    """Combine frozen matches on their kickoff date with non-writing scan outcomes."""
    date = date or datetime.now(timezone.utc).astimezone().date().isoformat()
    if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("date must use YYYY-MM-DD format")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("date must use YYYY-MM-DD format") from error

    records = read_records(ledger_path) if Path(ledger_path).exists() else []
    invalidated_settlement_hashes = _invalidated_settlement_hashes(records)
    settled_hashes = {
        record.get("prediction_hash")
        for record in records
        if record.get("record_type") == "settlement"
        and record.get("content_hash") not in invalidated_settlement_hashes
    }
    snapshots_by_prediction = {
        record.get("prediction_hash"): record
        for record in records
        if record.get("record_type") == "all_market_snapshot"
    }
    frozen = []
    for record in records:
        if (
            record.get("record_type") != "prediction"
            or record.get("prediction_role", "primary") != "primary"
            or not str(record.get("kickoff_at", "")).startswith(date)
        ):
            continue
        snapshot = snapshots_by_prediction.get(record.get("content_hash"))
        markets = _daily_market_view(record, snapshot)
        frozen.append({
            "match_id": record.get("match_id"),
            "captured_at": record.get("captured_at"),
            "kickoff_at": record.get("kickoff_at"),
            "match_context": record.get("match_context"),
            "status": "settled" if record.get("content_hash") in settled_hashes else "pending",
            "all_market_snapshot_status": "frozen" if snapshot is not None else "not_frozen",
            "freeze_level": _freeze_level(markets),
            "markets": markets,
        })
    frozen.sort(key=lambda item: (str(item.get("kickoff_at")), str(item.get("match_id"))))

    latest_scans = {}
    path = Path(scheduler_log_path)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            attempted_at = entry.get("timestamp") if isinstance(entry, Mapping) else None
            if (
                not isinstance(attempted_at, str)
                or not attempted_at.startswith(date)
                or entry.get("mode") != "prematch"
                or entry.get("status") != "completed"
            ):
                continue
            for raw_output in entry.get("output", []):
                try:
                    result = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
                except json.JSONDecodeError:
                    continue
                if not isinstance(result, Mapping):
                    continue
                scan_items = [
                    item for item in result.get("items", [])
                    if isinstance(item, Mapping) and item.get("status") in {"blocked", "observation_only"}
                ]
                scan_items.extend(
                    item for item in result.get("skipped_items", [])
                    if isinstance(item, Mapping) and item.get("status") == "skipped"
                )
                for item in scan_items:
                    item_key = str(item.get("match_id") or item.get("match_num"))
                    if not item_key or item_key == "None":
                        continue
                    latest_scans[item_key] = {
                        "match_id": item.get("match_id"),
                        "match_num": item.get("match_num"),
                        "status": item.get("status"),
                        "reasons": list(item.get("reasons", [])),
                        "attempted_at": attempted_at,
                        **{
                            key: item[key]
                            for key in ("official_status", "timing_basis", "sales_cutoff_at")
                            if key in item
                        },
                    }

    frozen_keys = {str(item["match_id"]) for item in frozen}
    not_frozen = [
        item for key, item in latest_scans.items()
        if key not in frozen_keys
    ]
    not_frozen.sort(key=lambda item: (item["attempted_at"], str(item.get("match_id"))))
    pending = [item for item in frozen if item["status"] == "pending"]
    return {
        "date": date,
        "frozen_count": len(frozen),
        "pending_count": len(pending),
        "not_frozen_count": len(not_frozen),
        "frozen": frozen,
        "pending": pending,
        "not_frozen": not_frozen,
    }


def xg_inputs_is_league_total(xg):
    evidence = xg.get("xg_inputs", {}).get("evidence", {}) if isinstance(xg, Mapping) else {}
    return evidence.get("scope") == "league_total"


def all_market_settle(input_path, ledger_path):
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    if not isinstance(payload, Mapping) or set(payload) != {"snapshot_hash", "official_result_url", "final_score", "markets"}:
        raise ValueError("all-market settlement input has invalid fields")
    records = read_records(ledger_path)
    snapshot = next((record for record in records if record.get("record_type") == "all_market_snapshot" and record.get("content_hash") == payload["snapshot_hash"]), None)
    if snapshot is None:
        raise ValueError("all-market settlement requires an existing snapshot")
    if any(record.get("record_type") == "all_market_settlement" and record.get("snapshot_hash") == snapshot["content_hash"] for record in records):
        raise ValueError("all-market snapshot has already been settled")
    result = settle_all_market_snapshot(snapshot, {
        "official_result_url": payload["official_result_url"],
        "final_score": payload["final_score"],
        "markets": payload["markets"],
    })
    _append_record(ledger_path, result)
    return result


def _result_outcome(score):
    if score["home"] > score["away"]:
        return "home_win"
    if score["home"] < score["away"]:
        return "away_win"
    return "draw"


def _correct_score_result(score):
    option = f"{score['home']}:{score['away']}"
    if option in ALL_MARKET_OPTIONS["crs"]:
        return option
    return {
        "home_win": "H_OTHER",
        "draw": "D_OTHER",
        "away_win": "A_OTHER",
    }[_result_outcome(score)]


def _actual_all_market_results(snapshot, final_score, half_time_score):
    frozen = {
        code for code, entry in snapshot["markets"].items()
        if entry["status"] == "frozen"
    }
    results = {}
    outcome = _result_outcome(final_score)
    if "spf" in frozen:
        results["spf"] = outcome
    if "hhad" in frozen:
        handicap = snapshot["markets"]["hhad"]["home_handicap"]
        results["hhad"] = _result_outcome({
            "home": final_score["home"] + int(handicap),
            "away": final_score["away"],
        })
    if "crs" in frozen:
        results["crs"] = _correct_score_result(final_score)
    if "ttg" in frozen:
        total_goals = final_score["home"] + final_score["away"]
        results["ttg"] = str(total_goals) if total_goals <= 6 else "7+"
    if "hafu" in frozen:
        if half_time_score is None:
            return None
        symbols = {"home_win": "H", "draw": "D", "away_win": "A"}
        results["hafu"] = symbols[_result_outcome(half_time_score)] + symbols[outcome]
    return results


def _settlement_team_key(value):
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"\s+", "", value).casefold()
    return normalized or None


def _official_result_matches_prediction(result, prediction):
    """Prevent a recurring weekday sequence from settling a different fixture."""
    prediction_id = str(prediction.get("match_id", "")).strip()
    result_id = result.get("match_id")
    if prediction_id.isdecimal() and str(result_id).strip() == prediction_id:
        return True
    if not re.fullmatch(r"周[一二三四五六日][0-9]{3}", prediction_id):
        return False
    if re.sub(r"\s+", "", str(result.get("match_num", ""))) != prediction_id:
        return False
    try:
        kickoff_date = _timestamp(prediction.get("kickoff_at"), "kickoff_at").date().isoformat()
    except ValueError:
        return False
    if result.get("match_date") != kickoff_date:
        return False
    context = prediction.get("match_context")
    if not isinstance(context, Mapping):
        return False
    return (
        _settlement_team_key(context.get("home_team")) == _settlement_team_key(result.get("home_team"))
        and _settlement_team_key(context.get("away_team")) == _settlement_team_key(result.get("away_team"))
    )


def _official_result_deduplication_key(result):
    """Identify one completed official fixture across result-query URLs."""
    final_score = result.get("final_score")
    score_key = (
        final_score.get("home") if isinstance(final_score, Mapping) else None,
        final_score.get("away") if isinstance(final_score, Mapping) else None,
    )
    match_id = str(result.get("match_id", "")).strip()
    if match_id:
        return ("match_id", match_id, result.get("match_date"), *score_key)
    return (
        "fixture",
        re.sub(r"\s+", "", str(result.get("match_num", ""))),
        result.get("match_date"),
        _settlement_team_key(result.get("home_team")),
        _settlement_team_key(result.get("away_team")),
        *score_key,
    )


def odds_refresh(ledger_path, max_hours_ahead=24.0):
    with _ledger_operation_lock(ledger_path):
        return _odds_refresh(ledger_path, max_hours_ahead=max_hours_ahead)


def _same_odds(left, right):
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    return all(str(left.get(label)) == str(right.get(label)) for label in OUTCOME_LABELS)


def _odds_refresh(ledger_path, max_hours_ahead=24.0):
    """Re-capture the crown price for frozen fixtures that have not kicked off.

    Predictions are normally frozen several hours before kickoff, not 30-60
    minutes before it. A 6 hour window therefore started capturing only after
    most predictions had already been made, so those fixtures never got an
    odds_capture row after the prediction and CLV could not be computed. The
    24 hour default covers the whole interval from prediction to kickoff.
    CLV needs the price at more than one point in time, so this stage revisits
    every frozen fixture still ahead of us and appends a capture whenever the
    crown price has actually moved. Unchanged prices are skipped so the ledger
    keeps a price trace instead of one row per poll.
    """
    records = read_records(ledger_path)
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=float(max_hours_ahead))

    fixtures = {}
    for record in records:
        if record.get("record_type") != "prediction" or record.get("status") != "frozen":
            continue
        key = (str(record.get("match_id")), str(record.get("kickoff_at")))
        fixtures.setdefault(key, record)

    # Latest capture per fixture, keyed the same way. match_id alone is reused
    # weekly by sporttery, so it must never be the sole key.
    latest = {}
    for record in records:
        if record.get("record_type") != "odds_capture":
            continue
        key = (str(record.get("match_id")), str(record.get("kickoff_at")))
        captured_at = str(record.get("captured_at") or "")
        if key not in latest or captured_at > str(latest[key].get("captured_at") or ""):
            latest[key] = record

    scanned = refreshed = written = no_crown = failed = 0
    for key, prediction in sorted(fixtures.items()):
        kickoff_at = prediction.get("kickoff_at")
        try:
            kickoff = _timestamp(kickoff_at, "kickoff_at")
        except ValueError:
            continue
        if not now < kickoff <= horizon:
            continue
        scanned += 1
        source_url = (prediction.get("source_urls") or {}).get("S2")
        if not isinstance(source_url, str) or not source_url:
            no_crown += 1
            continue
        try:
            capture = collect_433(source_url)
        except Exception:
            failed += 1
            continue
        prices = None
        for row in (capture.get("market") or {}).get("rows") or []:
            if row.get("bookmaker") == CROWN_BOOKMAKER:
                prices = row.get("prices")
                break
        if not isinstance(prices, Mapping):
            no_crown += 1
            continue
        refreshed += 1
        previous = latest.get(key)
        if previous is not None and _same_odds(previous.get("odds"), prices):
            continue
        try:
            decimal_odds = {label: Decimal(str(prices[label])) for label in OUTCOME_LABELS}
        except (KeyError, InvalidOperation):
            no_crown += 1
            continue
        _append_record(ledger_path, freeze_odds_capture({
            "provider": capture["provider"],
            "match_id": prediction.get("match_id"),
            "kickoff_at": kickoff_at,
            "captured_at": capture["captured_at"],
            "source_url": capture["source_url"],
            "bookmaker": CROWN_BOOKMAKER,
            "source_quality": "named_bookmaker",
            "odds": decimal_odds,
        }))
        written += 1
        latest[key] = {"odds": prices, "captured_at": capture["captured_at"]}

    return {
        "scanned": scanned,
        "refreshed": refreshed,
        "written": written,
        "no_crown": no_crown,
        "failed": failed,
    }


def sporttery_settle_batch(ledger_path, match_dates=()):
    with _ledger_operation_lock(ledger_path):
        return _sporttery_settle_batch(ledger_path, match_dates=match_dates)


def _sporttery_settle_batch(ledger_path, match_dates=()):
    """Append only results confirmed by the public official Sporttery API."""
    records = read_records(ledger_path)
    invalidated_settlement_hashes = _invalidated_settlement_hashes(records)
    settled_prediction_hashes = {
        record["prediction_hash"] for record in records
        if record.get("record_type") == "settlement"
        and record["content_hash"] not in invalidated_settlement_hashes
    }
    pending = [
        record for record in records
        if record.get("record_type") == "prediction"
        and record["content_hash"] not in settled_prediction_hashes
    ]
    if not pending:
        return {
            "pending_predictions": 0,
            "settled_count": 0,
            "all_market_settled_count": 0,
            "official_result_page_count": 0,
            "official_result_duplicate_count": 0,
            "items": [],
        }
    snapshots = {
        record["prediction_hash"]: record for record in records
        if record.get("record_type") == "all_market_snapshot"
    }
    settled_snapshots = {
        record["snapshot_hash"] for record in records
        if record.get("record_type") == "all_market_settlement"
    }
    pending_dates = set()
    for prediction in pending:
        try:
            pending_dates.add(_timestamp(prediction.get("kickoff_at"), "kickoff_at").date().isoformat())
        except ValueError:
            continue
    requested_dates = set()
    for match_date in match_dates:
        if isinstance(match_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", match_date):
            requested_dates.add(match_date)
    official_results = []
    seen_official_results = set()
    official_result_page_count = 0
    official_result_duplicate_count = 0
    for match_date in (None, *sorted(pending_dates | requested_dates)):
        collected = collect_results(match_date=match_date)
        page_count = collected.get("page_count", 1)
        if not isinstance(page_count, int) or page_count < 1:
            raise ValueError("official result collection has an invalid page count")
        official_result_page_count += page_count
        for result in collected.get("matches", []):
            match_num = result.get("match_num")
            if isinstance(match_num, str) and result.get("status") == "11":
                result_url = result.get("official_result_url")
                final_score = result.get("final_score")
                result_key = _official_result_deduplication_key(result)
                if result_key in seen_official_results:
                    official_result_duplicate_count += 1
                    continue
                seen_official_results.add(result_key)
                official_results.append(result)

    items = []
    settled_count = 0
    all_market_settled_count = 0
    for prediction in pending:
        match_id = prediction["match_id"]
        matching_results = [
            result for result in official_results
            if _official_result_matches_prediction(result, prediction)
        ]
        result = matching_results[0] if len(matching_results) == 1 else None
        if result is None:
            items.append({"match_id": match_id, "status": "pending", "reason": "official completed result is unavailable"})
            continue
        final_score = result.get("final_score")
        official_result_url = result.get("official_result_url")
        if not isinstance(final_score, Mapping) or not _is_http_url(official_result_url):
            items.append({"match_id": match_id, "status": "pending", "reason": "official result lacks a valid final score or direct URL"})
            continue
        settlement = settle_prediction(
            prediction,
            _result_outcome(final_score),
            official_result_url,
            final_score=final_score,
        )
        _append_record(ledger_path, settlement)
        settled_count += 1
        item = {"match_id": match_id, "status": "settled", "final_score": final_score}
        snapshot = snapshots.get(prediction["content_hash"])
        if snapshot is not None and snapshot["content_hash"] not in settled_snapshots:
            actual_markets = _actual_all_market_results(
                snapshot, final_score, result.get("half_time_score")
            )
            if actual_markets is None:
                item["all_market_status"] = "pending"
                item["all_market_reason"] = "official result lacks a valid half-time score"
            else:
                all_market = settle_all_market_snapshot(snapshot, {
                    "official_result_url": official_result_url,
                    "final_score": final_score,
                    "markets": actual_markets,
                })
                _append_record(ledger_path, all_market)
                all_market_settled_count += 1
                item["all_market_status"] = "settled"
        items.append(item)
    return {
        "pending_predictions": len(pending),
        "settled_count": settled_count,
        "all_market_settled_count": all_market_settled_count,
        "official_result_page_count": official_result_page_count,
        "official_result_duplicate_count": official_result_duplicate_count,
        "items": items,
    }


def settlement_queue(records):
    """Return frozen predictions awaiting a traceable official settlement."""
    invalidated_settlement_hashes = _invalidated_settlement_hashes(records)
    settled_prediction_hashes = {
        record["prediction_hash"]
        for record in records
        if record.get("record_type") == "settlement"
        and record["content_hash"] not in invalidated_settlement_hashes
    }
    snapshots_by_prediction = {
        record["prediction_hash"]: record
        for record in records
        if record.get("record_type") == "all_market_snapshot"
    }
    settled_snapshot_hashes = {
        record["snapshot_hash"]
        for record in records
        if record.get("record_type") == "all_market_settlement"
    }
    items = []
    for prediction in records:
        if (
            prediction.get("record_type") != "prediction"
            or prediction["content_hash"] in settled_prediction_hashes
        ):
            continue
        snapshot = snapshots_by_prediction.get(prediction["content_hash"])
        if snapshot is None:
            snapshot_status = "not_frozen"
            snapshot_hash = None
        elif snapshot["content_hash"] in settled_snapshot_hashes:
            snapshot_status = "settled"
            snapshot_hash = snapshot["content_hash"]
        else:
            snapshot_status = "pending"
            snapshot_hash = snapshot["content_hash"]
        context = prediction.get("match_context", {})
        items.append({
            "match_id": prediction["match_id"],
            "kickoff_at": prediction["kickoff_at"],
            "prediction_hash": prediction["content_hash"],
            "official_match_url": prediction["source_urls"]["S1"],
            "competition": context.get("competition"),
            "home_team": context.get("home_team"),
            "away_team": context.get("away_team"),
            "all_market_snapshot_status": snapshot_status,
            "all_market_snapshot_hash": snapshot_hash,
            "settlement_action": "official_result_required",
        })
    items.sort(key=lambda item: (item["kickoff_at"], item["prediction_hash"]))
    return {"pending_predictions": len(items), "items": items}


def report(ledger_path):
    return calibration_report(read_records(ledger_path))


def format_report(result):
    """Render a concise human-readable calibration report."""
    evidence_events = result.get("evidence_event_counts")
    if not result["available"]:
        lines = [
            "Calibration unavailable: "
            f"{result['settled_records']} settled predictions; "
            f"at least {result['minimum_settled_records']} are required."
        ]
        for code, stats in sorted(result.get("all_market_breakdown", {}).items()):
            lines.append(
                f"{code}: {stats['settled_records']} settled; "
                f"{stats['hits']} hits; hit rate {stats['hit_rate']}."
            )
        if evidence_events is not None:
            lines.append(
                "Evidence events: "
                f"{evidence_events['source_observation']} observations; "
                f"{evidence_events['collection_failure']} failures."
            )
        for key, label in (
            ("temporal_weight_evaluation", "Temporal weight evaluation"),
            ("walk_forward_weight_evaluation", "Walk-forward weight evaluation"),
        ):
            evaluation = result.get(key)
            if isinstance(evaluation, Mapping) and not evaluation.get("available"):
                lines.append(
                    f"{label} unavailable: "
                    f"{evaluation['enhanced_settled_records']} enhanced settlements; "
                    f"at least {evaluation['minimum_enhanced_settled_records']} are required."
                )
        return "\n".join(lines)
    lines = [
        "Calibration available: "
        f"{result['settled_records']} settled predictions; "
        f"Brier {result['brier_average']}; "
        f"Log loss {result['log_loss_average']}; "
        f"ECE {result['expected_calibration_error']}; "
        f"phase {result['phase']}."
    ]
    for label, strategy in sorted(result["strategy_breakdown"].items()):
        lines.append(
            f"{label}: {strategy['settled_records']} settled; "
            f"Brier {strategy['brier_average']}; "
            f"Log loss {strategy['log_loss_average']}; "
            f"top-pick accuracy {strategy['top_pick_accuracy']}."
        )
    for competition, stats in sorted(result.get("competition_breakdown", {}).items()):
        lines.append(
            f"{competition}: {stats['settled_records']} settled; "
            f"Brier {stats['brier_average']}; "
            f"Log loss {stats['log_loss_average']}; "
            f"top-pick accuracy {stats['top_pick_accuracy']}."
        )
    evaluation = result.get("temporal_weight_evaluation")
    if evaluation is not None:
        if evaluation["available"]:
            lines.append(
                "Temporal weight evaluation: "
                f"selected weight {evaluation['selected_weight']}; "
                f"holdout Log loss {evaluation['holdout_log_loss']}; "
                f"market {evaluation['market_holdout_log_loss']}; "
                f"model {evaluation['model_holdout_log_loss']}."
            )
        else:
            lines.append(
                "Temporal weight evaluation unavailable: "
                f"{evaluation['enhanced_settled_records']} enhanced settlements; "
                f"at least {evaluation['minimum_enhanced_settled_records']} are required."
            )
    walk_forward = result.get("walk_forward_weight_evaluation")
    if walk_forward is not None:
        if walk_forward["available"]:
            lines.append(
                "Walk-forward weight evaluation: "
                f"{walk_forward['fold_count']} folds; "
                f"Log loss {walk_forward['walk_forward_log_loss']}; "
                f"market {walk_forward['market_walk_forward_log_loss']}; "
                f"model {walk_forward['model_walk_forward_log_loss']}."
            )
        else:
            lines.append(
                "Walk-forward weight evaluation unavailable: "
                f"{walk_forward['enhanced_settled_records']} enhanced settlements; "
                f"at least {walk_forward['minimum_enhanced_settled_records']} are required."
            )
    if evidence_events is not None:
        lines.append(
            "Evidence events: "
            f"{evidence_events['source_observation']} observations; "
            f"{evidence_events['collection_failure']} failures."
        )
    return "\n".join(lines)


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Offline, paper-only China Sports Lottery football calibration tracker."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = commands.add_parser("snapshot", help="freeze a pre-kickoff JSON input")
    snapshot_parser.add_argument("input_path")
    snapshot_parser.add_argument("ledger_path")

    odds_import_parser = commands.add_parser(
        "odds-import", help="append offline European odds captures from a JSON batch"
    )
    odds_import_parser.add_argument("input_path")
    odds_import_parser.add_argument("ledger_path")

    evidence_record_parser = commands.add_parser(
        "evidence-record", help="append one auditable public-source observation or collection failure"
    )
    evidence_record_parser.add_argument("input_path")
    evidence_record_parser.add_argument("ledger_path")

    settle_parser = commands.add_parser("settle", help="append an official-result settlement")
    settle_parser.add_argument("ledger_path")
    settle_parser.add_argument("prediction_hash")
    settle_parser.add_argument("actual_outcome", choices=OUTCOME_LABELS)
    settle_parser.add_argument("official_result_url")
    settle_parser.add_argument("--final-score", nargs=2, type=int, metavar=("HOME", "AWAY"))

    settlement_invalidate_parser = commands.add_parser(
        "settlement-invalidate",
        help="append an auditable invalidation for a misidentified official settlement",
    )
    settlement_invalidate_parser.add_argument("input_path")
    settlement_invalidate_parser.add_argument("ledger_path")

    report_parser = commands.add_parser("report", help="show calibration from linked records")
    report_parser.add_argument("ledger_path")
    report_parser.add_argument("--json", action="store_true", dest="as_json")

    review_parser = commands.add_parser(
        "review", help="generate a read-only, auditable local review"
    )
    review_parser.add_argument("ledger_path")
    review_parser.add_argument("--match-id")
    review_parser.add_argument("--json", action="store_true", dest="as_json")
    review_parser.add_argument(
        "--ai", action="store_true", help="use the configured DeepSeek API for a read-only narration"
    )
    review_parser.add_argument("--model", help="override DEEPSEEK_MODEL")
    review_parser.add_argument("--base-url", help="override DEEPSEEK_BASE_URL")
    review_parser.add_argument("--timeout", type=int, default=30)

    configure_deepseek_parser = commands.add_parser(
        "configure-deepseek", help="save a local DeepSeek configuration for review --ai"
    )
    configure_deepseek_parser.add_argument("--model", default=DEFAULT_MODEL)
    configure_deepseek_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)

    configure_database_parser = commands.add_parser(
        "configure-database", help="save local MySQL settings for the service projection"
    )
    configure_database_parser.add_argument("--host", default="127.0.0.1")
    configure_database_parser.add_argument("--port", type=int, default=3306)
    configure_database_parser.add_argument("--username", default="taicai_app")
    configure_database_parser.add_argument("--database", default="taicai_tracker")

    sync_projection_parser = commands.add_parser(
        "sync-service-projection", help="project immutable ledger records into the configured MySQL read model"
    )
    sync_projection_parser.add_argument("ledger_path")

    serve_api_parser = commands.add_parser(
        "serve-api", help="serve the configured MySQL read model through FastAPI"
    )
    serve_api_parser.add_argument("ledger_path")
    serve_api_parser.add_argument("--host", default="127.0.0.1")
    serve_api_parser.add_argument("--port", type=int, default=8000)

    all_market_snapshot_parser = commands.add_parser("all-market-snapshot")
    all_market_snapshot_parser.add_argument("input_path")
    all_market_snapshot_parser.add_argument("ledger_path")
    all_market_settle_parser = commands.add_parser("all-market-settle")
    all_market_settle_parser.add_argument("input_path")
    all_market_settle_parser.add_argument("ledger_path")

    settlement_queue_parser = commands.add_parser(
        "settlement-queue",
        help="list frozen predictions waiting for a traceable official result",
    )
    settlement_queue_parser.add_argument("ledger_path")

    sporttery_scan_parser = commands.add_parser(
        "sporttery-scan",
        help="fetch public official China Sports Lottery football schedule JSON",
    )
    sporttery_scan_parser.add_argument("--method", default="concern")
    sporttery_scan_parser.add_argument("--match-date", dest="matchDate")
    sporttery_scan_parser.add_argument("--league-id", dest="leagueId")

    sporttery_fixed_bonus_parser = commands.add_parser(
        "sporttery-fixed-bonus",
        help="fetch public official fixed-prize history for one football match",
    )
    sporttery_fixed_bonus_parser.add_argument("match_id")

    sporttery_settle_batch_parser = commands.add_parser(
        "sporttery-settle-batch",
        help="append completed results from the public official Sporttery result API",
    )
    sporttery_settle_batch_parser.add_argument("ledger_path")
    sporttery_settle_batch_parser.add_argument("--match-date", action="append", default=[])

    odds_refresh_parser = commands.add_parser(
        "odds-refresh",
        help="re-capture crown prices for frozen fixtures that have not kicked off",
    )
    odds_refresh_parser.add_argument("ledger_path")
    odds_refresh_parser.add_argument("--max-hours-ahead", type=float, default=24.0)

    european_433_extract_parser = commands.add_parser(
        "european-433-extract",
        help="fetch one public 433 match-detail page and extract named immediate 1X2 odds",
    )
    european_433_extract_parser.add_argument("source_url")

    fotmob_team_stats_parser = commands.add_parser(
        "fotmob-team-stats",
        help="fetch one public FotMob team stats page and xG/xGA source rows",
    )
    fotmob_team_stats_parser.add_argument("source_url")

    fotmob_xg_evidence_parser = commands.add_parser(
        "fotmob-xg-evidence",
        help="build league-total Poisson xG evidence from two public FotMob team stats pages",
    )
    fotmob_xg_evidence_parser.add_argument("home_url")
    fotmob_xg_evidence_parser.add_argument("away_url")
    fotmob_xg_evidence_parser.add_argument("--model-weight", default="0.40")

    understat_xg_parser = commands.add_parser(
        "understat-league-xg",
        help="collect public Understat league-total xG/xGA evidence without writing the ledger",
    )
    understat_xg_parser.add_argument("league")
    understat_xg_parser.add_argument("season", help="Understat start year, for example 2026")
    understat_xg_parser.add_argument("home_team")
    understat_xg_parser.add_argument("away_team")

    understat_batch_xg_parser = commands.add_parser(
        "understat-batch-xg",
        help="collect one Understat league payload for multiple home/away split xG observations",
        description="Collect one Understat league payload for multiple home/away split xG observations.",
    )
    understat_batch_xg_parser.add_argument("input_path")
    understat_batch_xg_parser.add_argument(
        "--rolling-matches",
        type=int,
        help="use each fixture's most recent N venue rows strictly before its kickoff calendar date",
    )

    xgscore_extract_parser = commands.add_parser(
        "xgscore-extract",
        help="collect one strict public xGscore event page without writing the ledger",
    )
    xgscore_extract_parser.add_argument("source_url")
    xgscore_extract_parser.add_argument("home_team")
    xgscore_extract_parser.add_argument("away_team")
    xgscore_extract_parser.add_argument("kickoff_at")
    xgscore_extract_parser.add_argument("competition")
    xgscore_extract_parser.add_argument("--model-weight", default="0.40")

    pregame_context_parser = commands.add_parser(
        "pregame-context",
        help="collect optional public split-xG, weather, and lineup evidence without writing the ledger",
    )
    pregame_context_parser.add_argument("official_identity_path")
    pregame_context_parser.add_argument("event_url")

    resolve_context_event_parser = commands.add_parser(
        "resolve-context-event",
        help="discover and strictly verify one public event URL for optional pregame context",
    )
    resolve_context_event_parser.add_argument("official_identity_path")
    resolve_context_event_parser.add_argument(
        "--alias-registry",
        help="write confirmed team-name aliases after a uniquely adopted context event",
    )

    pregame_bundle_parser = commands.add_parser(
        "pregame-bundle",
        help="collect one match's official prizes, 433 named 1X2, and public xG cascade into reviewable ledger inputs",
    )
    pregame_bundle_parser.add_argument("match_id")
    pregame_bundle_parser.add_argument("european_433_url")
    pregame_bundle_parser.add_argument("home_fotmob_url")
    pregame_bundle_parser.add_argument("away_fotmob_url")
    pregame_bundle_parser.add_argument("--method", default="concern")
    pregame_bundle_parser.add_argument("--model-weight", default="0.40")
    pregame_bundle_parser.add_argument("--kickoff-at")
    pregame_bundle_parser.add_argument("--home-team")
    pregame_bundle_parser.add_argument("--away-team")
    pregame_bundle_parser.add_argument("--competition")
    pregame_bundle_parser.add_argument(
        "--xgscore-url",
        help="strictly verified public xGscore event URL; no URL discovery is attempted",
    )

    auto_pregame_bundle_parser = commands.add_parser(
        "auto-pregame-bundle",
        help="resolve verified source links then collect one read-only pregame bundle",
    )
    auto_pregame_bundle_parser.add_argument("match_id")
    auto_pregame_bundle_parser.add_argument("european_433_url", nargs="?")
    auto_pregame_bundle_parser.add_argument("--method", default="concern")
    auto_pregame_bundle_parser.add_argument("--model-weight", default="0.40")
    auto_pregame_bundle_parser.add_argument(
        "--xgscore-url",
        help="optional public xGscore event URL; otherwise strict cached-identity discovery may be attempted",
    )

    rapid_freeze_parser = commands.add_parser(
        "rapid-freeze",
        help="collect one verified-source match and append its odds, SPF, and full-market paper records",
    )
    rapid_freeze_parser.add_argument("input_path")
    rapid_freeze_parser.add_argument("ledger_path")

    rapid_freeze_batch_parser = commands.add_parser(
        "rapid-freeze-batch",
        help="collect independent verified-source matches concurrently and append complete paper records",
    )
    rapid_freeze_batch_parser.add_argument("input_path")
    rapid_freeze_batch_parser.add_argument("ledger_path")
    rapid_freeze_batch_parser.add_argument("--max-workers", type=int, default=6)

    schedule_driven_freeze_parser = commands.add_parser(
        "schedule-driven-freeze",
        help="scan one official schedule page and freeze only review-ready matches near kickoff",
    )
    schedule_driven_freeze_parser.add_argument("ledger_path")
    schedule_driven_freeze_parser.add_argument("--min-lead-minutes", type=int, default=30)
    schedule_driven_freeze_parser.add_argument("--max-lead-minutes", type=int, default=60)
    schedule_driven_freeze_parser.add_argument("--max-workers", type=int, default=2)
    schedule_driven_freeze_parser.add_argument(
        "--freeze-window-minutes",
        type=int,
        default=0,
        help=(
            "prematch only: freeze only when 0 < kickoff - now <= this many minutes "
            "(default: %(default)s); 0 or a negative value restores the previous "
            "lead-window behavior"
        ),
    )
    schedule_driven_freeze_parser.add_argument(
        "--include-open-matches",
        action="store_true",
        help="operator-run only: include official open matches before kickoff outside the scheduled window",
    )
    schedule_driven_freeze_parser.add_argument(
        "--sales-cutoff-policy",
        default=DEFAULT_SALES_CUTOFF_POLICY_PATH,
        help="local operator-observed sales cutoff policy; not source evidence",
    )

    identity_preflight_parser = commands.add_parser(
        "identity-preflight",
        help="write a read-only queue of near-term matches needing provider identity review",
    )
    identity_preflight_parser.add_argument("output_path")
    identity_preflight_parser.add_argument("--min-lead-minutes", type=int, default=120)
    identity_preflight_parser.add_argument("--max-lead-minutes", type=int, default=180)
    identity_preflight_parser.add_argument(
        "--sales-cutoff-policy",
        default=DEFAULT_SALES_CUTOFF_POLICY_PATH,
        help="local operator-observed sales cutoff policy; not source evidence",
    )

    identity_coverage_report_parser = commands.add_parser(
        "identity-coverage-report",
        help="summarize read-only source-identity coverage by current competition",
    )
    identity_coverage_report_parser.add_argument("--min-lead-minutes", type=int, default=90)
    identity_coverage_report_parser.add_argument("--max-lead-minutes", type=int, default=240)
    identity_coverage_report_parser.add_argument(
        "--sales-cutoff-policy",
        default=DEFAULT_SALES_CUTOFF_POLICY_PATH,
        help="local operator-observed sales cutoff policy; not source evidence",
    )

    fotmob_source_prewarm_parser = commands.add_parser(
        "fotmob-source-prewarm",
        help="cache strictly resolved FotMob team links for upcoming official open matches",
    )
    fotmob_source_prewarm_parser.add_argument("--hours-ahead", type=int, default=48)
    fotmob_source_prewarm_parser.add_argument("--max-workers", type=int, default=2)
    fotmob_source_prewarm_parser.add_argument(
        "--upgrade-legacy",
        action="store_true",
        help="re-resolve legacy URL-only FotMob cache entries before retaining provider team IDs",
    )
    fotmob_source_prewarm_parser.add_argument(
        "--source-link-registry",
        default=str(DEFAULT_SOURCE_LINK_REGISTRY_PATH),
        help="mutable verified-source-link registry; never the paper ledger",
    )
    fotmob_source_prewarm_parser.add_argument(
        "--source-alias-registry",
        default=str(DEFAULT_SOURCE_ALIAS_REGISTRY_PATH),
        help="strictly adopted source-team aliases used only as FotMob search terms",
    )

    wikidata_source_prewarm_parser = commands.add_parser(
        "wikidata-source-prewarm",
        help="cache unique public Wikidata team-name anchors for upcoming official open matches",
    )
    wikidata_source_prewarm_parser.add_argument("--hours-ahead", type=int, default=48)
    wikidata_source_prewarm_parser.add_argument("--max-workers", type=int, default=2)
    wikidata_source_prewarm_parser.add_argument(
        "--source-link-registry",
        default=str(DEFAULT_SOURCE_LINK_REGISTRY_PATH),
        help="mutable verified-source-link registry; never the paper ledger",
    )

    historical_fotmob_source_prewarm_parser = commands.add_parser(
        "historical-fotmob-source-prewarm",
        help="resumably cache strict FotMob team identities from official result history",
    )
    historical_fotmob_source_prewarm_parser.add_argument("--days-back", type=int, default=548)
    historical_fotmob_source_prewarm_parser.add_argument("--max-dates-per-run", type=int, default=7)
    historical_fotmob_source_prewarm_parser.add_argument("--max-workers", type=int, default=2)
    historical_fotmob_source_prewarm_parser.add_argument(
        "--source-link-registry",
        default=str(DEFAULT_SOURCE_LINK_REGISTRY_PATH),
        help="mutable verified-source-link registry; never the paper ledger",
    )
    historical_fotmob_source_prewarm_parser.add_argument(
        "--source-alias-registry",
        default=str(DEFAULT_SOURCE_ALIAS_REGISTRY_PATH),
        help="strictly adopted source-team aliases used only as FotMob search terms",
    )
    historical_fotmob_source_prewarm_parser.add_argument(
        "--reviewed-alias-registry",
        default=str(DEFAULT_FOTMOB_SEARCH_ALIAS_REGISTRY_PATH),
        help="audited FotMob search-keyword registry; never a provider identity registry",
    )
    historical_fotmob_source_prewarm_parser.add_argument(
        "--progress-path",
        default=str(DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH),
        help="mutable resumable historical-prewarm progress file; never the paper ledger",
    )

    daily_fotmob_identity_map_parser = commands.add_parser(
        "daily-fotmob-identity-map",
        help="incrementally cache strict FotMob identities from upcoming fixtures and recent official results",
    )
    daily_fotmob_identity_map_parser.add_argument("--hours-ahead", type=int, default=72)
    daily_fotmob_identity_map_parser.add_argument("--result-days", type=int, default=3)
    daily_fotmob_identity_map_parser.add_argument("--max-workers", type=int, default=2)
    daily_fotmob_identity_map_parser.add_argument(
        "--retry-wikidata-now",
        action="store_true",
        help="ignore only local Wikidata network cooldowns after a verified transport change",
    )
    daily_fotmob_identity_map_parser.add_argument(
        "--source-link-registry", default=str(DEFAULT_SOURCE_LINK_REGISTRY_PATH),
    )
    daily_fotmob_identity_map_parser.add_argument(
        "--source-alias-registry", default=str(DEFAULT_SOURCE_ALIAS_REGISTRY_PATH),
    )
    daily_fotmob_identity_map_parser.add_argument(
        "--reviewed-alias-registry", default=str(DEFAULT_FOTMOB_SEARCH_ALIAS_REGISTRY_PATH),
    )
    daily_fotmob_identity_map_parser.add_argument(
        "--historical-progress-path", default=str(DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH),
    )
    daily_fotmob_identity_map_parser.add_argument(
        "--daily-progress-path", default=str(DEFAULT_DAILY_FOTMOB_IDENTITY_PROGRESS_PATH),
    )

    historical_fotmob_alias_candidates_parser = commands.add_parser(
        "historical-fotmob-alias-candidates",
        help="build a read-only reviewed-search queue for unresolved historical FotMob teams",
    )
    historical_fotmob_alias_candidates_parser.add_argument("--max-teams", type=int, default=25)
    historical_fotmob_alias_candidates_parser.add_argument(
        "--progress-path",
        default=str(DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH),
    )
    historical_fotmob_alias_candidates_parser.add_argument(
        "--reviewed-alias-registry",
        default=str(DEFAULT_FOTMOB_SEARCH_ALIAS_REGISTRY_PATH),
    )
    historical_fotmob_alias_candidates_parser.add_argument(
        "--source-alias-registry",
        default=str(DEFAULT_SOURCE_ALIAS_REGISTRY_PATH),
    )
    historical_fotmob_alias_candidates_parser.add_argument(
        "--source-link-registry",
        default=str(DEFAULT_SOURCE_LINK_REGISTRY_PATH),
    )
    historical_fotmob_alias_candidates_parser.add_argument(
        "--output-path",
        default=str(DEFAULT_HISTORICAL_FOTMOB_ALIAS_CANDIDATE_QUEUE_PATH),
    )

    historical_fotmob_failure_report_parser = commands.add_parser(
        "historical-fotmob-failure-report",
        help="summarize structured and legacy historical FotMob lookup failures without collecting",
    )
    historical_fotmob_failure_report_parser.add_argument(
        "--progress-path",
        default=str(DEFAULT_HISTORICAL_FOTMOB_PREWARM_PROGRESS_PATH),
    )

    daily_freeze_status_parser = commands.add_parser(
        "daily-freeze-status",
        help="combine same-day frozen records with scheduled pre-match outcomes",
    )
    daily_freeze_status_parser.add_argument("ledger_path")
    daily_freeze_status_parser.add_argument("scheduler_log_path")
    daily_freeze_status_parser.add_argument("--date")

    daily_slip_parser = commands.add_parser(
        "daily-slip",
        help="print a read-only daily paper slip from already-frozen predictions",
    )
    daily_slip_parser.add_argument(
        "--date",
        help=(
            "replay one Asia/Shanghai date (YYYY-MM-DD) with all of its frozen "
            "fixtures; without it only today's not-yet-started fixtures are shown"
        ),
    )
    daily_slip_parser.add_argument(
        "--budget", type=int, default=10, help="total paper budget in yuan (default: 10)"
    )
    daily_slip_parser.add_argument(
        "--ledger", default=str(DEFAULT_DAILY_SLIP_LEDGER_PATH), help="path to paper_ledger.jsonl"
    )

    commands.add_parser(
        "source-access-check",
        help="check public source and explicit-proxy reachability without collecting or writing records",
    )

    understat_alias_import_parser = commands.add_parser(
        "understat-alias-import",
        help="import manually bilingual-reviewed, traceable Understat team aliases",
    )
    understat_alias_import_parser.add_argument("input_path")
    understat_alias_import_parser.add_argument(
        "registry_path", nargs="?", default=str(DEFAULT_SOURCE_ALIAS_REGISTRY_PATH)
    )

    fotmob_search_alias_import_parser = commands.add_parser(
        "fotmob-search-alias-import",
        help="import reviewed, traceable FotMob query keywords without creating identities",
    )
    fotmob_search_alias_import_parser.add_argument("input_path")
    fotmob_search_alias_import_parser.add_argument(
        "--registry-path",
        default=str(DEFAULT_FOTMOB_SEARCH_ALIAS_REGISTRY_PATH),
    )

    match_preflight_parser = commands.add_parser(
        "match-preflight",
        help="run one read-only pregame bundle from a resolved candidate-link JSON file",
    )
    match_preflight_parser.add_argument("resolved_path")
    match_preflight_parser.add_argument("--method", default="concern")
    match_preflight_parser.add_argument("--model-weight", default="0.40")

    resolve_links_parser = commands.add_parser(
        "resolve-links",
        help="rank manually supplied OddsPortal/FotMob candidate links for one Sporttery match",
        description="Rank manually supplied OddsPortal/FotMob candidate links for one Sporttery match.",
    )
    resolve_links_parser.add_argument("input_path", help="JSON file containing Sporttery identity and candidate links")
    resolve_links_parser.add_argument(
        "--alias-registry",
        help="optional JSON registry written only from strictly adopted source aliases",
    )
    resolve_links_parser.add_argument("--output")

    candidate_template_parser = commands.add_parser(
        "candidate-template",
        help="print a blank resolve-links candidate file from one official Sporttery match_id",
        description="Print a blank resolve-links candidate file from one official Sporttery match_id.",
    )
    candidate_template_parser.add_argument("match_id")
    candidate_template_parser.add_argument("--method", default="concern")
    candidate_template_parser.add_argument("--output")

    link_summary_parser = commands.add_parser(
        "link-summary",
        help="extract pregame-bundle-ready URLs from a resolved-links JSON file",
        description="Extract pregame-bundle-ready URLs from a resolved-links JSON file.",
    )
    link_summary_parser.add_argument("input_path", help="resolved links JSON file")

    workflow_next_parser = commands.add_parser(
        "workflow-next",
        help="show the next local candidate-link workflow step for one match_id",
        description="Show the next local candidate-link workflow step for one match_id.",
    )
    workflow_next_parser.add_argument("match_id")
    workflow_next_parser.add_argument("--base-dir", default="work")
    workflow_next_parser.add_argument("--plain", action="store_true")
    return parser


def _configure_utf8_stdout():
    """Keep Chinese output readable through Windows PowerShell pipelines and logs."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def main(argv=None):
    _configure_utf8_stdout()
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "snapshot":
            record = snapshot(arguments.input_path, arguments.ledger_path)
            print(json.dumps(record, default=_json_default, ensure_ascii=True, sort_keys=True))
        elif arguments.command == "odds-import":
            records = odds_import(arguments.input_path, arguments.ledger_path)
            print(json.dumps(records, default=_json_default, ensure_ascii=True, sort_keys=True))
        elif arguments.command == "evidence-record":
            record = evidence_record(arguments.input_path, arguments.ledger_path)
            print(json.dumps(record, default=_json_default, ensure_ascii=True, sort_keys=True))
        elif arguments.command == "settle":
            final_score = None
            if arguments.final_score is not None:
                final_score = {
                    "home": arguments.final_score[0],
                    "away": arguments.final_score[1],
                }
            record = settle(
                arguments.ledger_path,
                arguments.prediction_hash,
                arguments.actual_outcome,
                arguments.official_result_url,
                final_score,
            )
            print(json.dumps(record, default=_json_default, ensure_ascii=True, sort_keys=True))
        elif arguments.command == "settlement-invalidate":
            print(json.dumps(
                settlement_invalidate(arguments.input_path, arguments.ledger_path),
                default=_json_default,
                ensure_ascii=True,
                sort_keys=True,
            ))
        elif arguments.command == "all-market-snapshot":
            print(json.dumps(all_market_snapshot(arguments.input_path, arguments.ledger_path), default=_json_default, ensure_ascii=True, sort_keys=True))
        elif arguments.command == "all-market-settle":
            print(json.dumps(all_market_settle(arguments.input_path, arguments.ledger_path), default=_json_default, ensure_ascii=True, sort_keys=True))
        elif arguments.command == "sporttery-scan":
            print(json.dumps(
                collect_schedule(
                    method=arguments.method,
                    matchDate=arguments.matchDate,
                    leagueId=arguments.leagueId,
                ),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "sporttery-fixed-bonus":
            print(json.dumps(
                collect_fixed_bonus(arguments.match_id),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "sporttery-settle-batch":
            print(json.dumps(
                sporttery_settle_batch(arguments.ledger_path, arguments.match_date),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "odds-refresh":
            print(
                "赔率跟踪: 扫描 {scanned} 场, 刷新成功 {refreshed} 场, "
                "价格变动写入 {written} 条, 无皇冠 {no_crown} 场, 抓取失败 {failed} 场".format(
                    **odds_refresh(
                        arguments.ledger_path,
                        max_hours_ahead=arguments.max_hours_ahead,
                    )
                )
            )
        elif arguments.command == "european-433-extract":
            print(json.dumps(
                collect_433(arguments.source_url),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "fotmob-team-stats":
            print(json.dumps(
                collect_team_stats(arguments.source_url),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "fotmob-xg-evidence":
            print(json.dumps(
                collect_match_xg_evidence(
                    arguments.home_url,
                    arguments.away_url,
                    model_weight=arguments.model_weight,
                ),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "understat-league-xg":
            print(json.dumps(
                collect_league_xg(
                    arguments.league,
                    arguments.season,
                    arguments.home_team,
                    arguments.away_team,
                ),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "understat-batch-xg":
            with Path(arguments.input_path).open("r", encoding="utf-8") as input_file:
                payload = json.load(input_file)
            if not isinstance(payload, Mapping):
                raise ValueError("Understat batch input must be a JSON object")
            required = {"league", "season", "fixtures"}
            if set(payload) != required or not all(isinstance(payload[key], str) and payload[key] for key in ("league", "season")):
                raise ValueError("Understat batch input must contain league, season, and fixtures")
            print(json.dumps(
                collect_league_xg_batch(
                    payload["league"],
                    payload["season"],
                    payload["fixtures"],
                    rolling_matches=arguments.rolling_matches,
                ),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "xgscore-extract":
            print(json.dumps(
                collect_xgscore(
                    arguments.source_url,
                    arguments.home_team,
                    arguments.away_team,
                    arguments.kickoff_at,
                    arguments.competition,
                    model_weight=arguments.model_weight,
                ),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "pregame-context":
            with Path(arguments.official_identity_path).open("r", encoding="utf-8") as identity_file:
                identity = json.load(identity_file)
            print(json.dumps(
                collect_pregame_context(identity, arguments.event_url),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "resolve-context-event":
            with Path(arguments.official_identity_path).open("r", encoding="utf-8") as identity_file:
                identity = json.load(identity_file)
            result = resolve_context_event(identity)
            if result.get("status") == "adopted":
                if arguments.alias_registry:
                    registry = record_adopted_aliases(
                        arguments.alias_registry,
                        identity,
                        result,
                    )
                    result["alias_registry_path"] = arguments.alias_registry
                    result["alias_registry_count"] = len(registry["aliases"])
                result["pregame_context_command"] = " ".join([
                    "python", "-m", "outputs.tai_cai_tracker.cli", "pregame-context",
                    f'"{arguments.official_identity_path}"', f'"{result["event_url"]}"',
                ])
            print(json.dumps(result, default=_json_default, ensure_ascii=False, sort_keys=True))
        elif arguments.command == "pregame-bundle":
            xgscore_kwargs = {"xgscore_url": arguments.xgscore_url} if arguments.xgscore_url else {}
            print(json.dumps(
                collect_pregame_bundle(
                    arguments.match_id,
                    arguments.european_433_url,
                    arguments.home_fotmob_url,
                    arguments.away_fotmob_url,
                    method=arguments.method,
                    model_weight=arguments.model_weight,
                    kickoff_at=arguments.kickoff_at,
                    home_team=arguments.home_team,
                    away_team=arguments.away_team,
                    competition=arguments.competition,
                    **xgscore_kwargs,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "auto-pregame-bundle":
            xgscore_kwargs = {"xgscore_url": arguments.xgscore_url} if arguments.xgscore_url else {}
            print(json.dumps(
                collect_auto_pregame_bundle(
                    arguments.match_id,
                    arguments.european_433_url,
                    method=arguments.method,
                    model_weight=arguments.model_weight,
                    **xgscore_kwargs,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "rapid-freeze":
            print(json.dumps(
                rapid_freeze(arguments.input_path, arguments.ledger_path),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "rapid-freeze-batch":
            print(json.dumps(
                rapid_freeze_batch(
                    arguments.input_path,
                    arguments.ledger_path,
                    max_workers=arguments.max_workers,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "schedule-driven-freeze":
            print(json.dumps(
                schedule_driven_freeze(
                    arguments.ledger_path,
                    min_lead_minutes=arguments.min_lead_minutes,
                    max_lead_minutes=arguments.max_lead_minutes,
                    max_workers=arguments.max_workers,
                    sales_cutoff_policy_path=arguments.sales_cutoff_policy,
                    include_open_matches=arguments.include_open_matches,
                    freeze_window_minutes=arguments.freeze_window_minutes,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "identity-preflight":
            print(json.dumps(
                identity_preflight(
                    arguments.output_path,
                    min_lead_minutes=arguments.min_lead_minutes,
                    max_lead_minutes=arguments.max_lead_minutes,
                    sales_cutoff_policy_path=arguments.sales_cutoff_policy,
                ),
                default=_json_default,
                sort_keys=True,
            ))
        elif arguments.command == "identity-coverage-report":
            print(json.dumps(
                identity_coverage_report(
                    min_lead_minutes=arguments.min_lead_minutes,
                    max_lead_minutes=arguments.max_lead_minutes,
                    sales_cutoff_policy_path=arguments.sales_cutoff_policy,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "fotmob-source-prewarm":
            print(json.dumps(
                prewarm_fotmob_source_links(
                    hours_ahead=arguments.hours_ahead,
                    max_workers=arguments.max_workers,
                    source_link_registry_path=arguments.source_link_registry,
                    source_alias_registry_path=arguments.source_alias_registry,
                    upgrade_legacy=arguments.upgrade_legacy,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "wikidata-source-prewarm":
            print(json.dumps(
                prewarm_wikidata_source_identities(
                    hours_ahead=arguments.hours_ahead,
                    max_workers=arguments.max_workers,
                    source_link_registry_path=arguments.source_link_registry,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "historical-fotmob-source-prewarm":
            print(json.dumps(
                historical_fotmob_source_prewarm(
                    days_back=arguments.days_back,
                    max_dates_per_run=arguments.max_dates_per_run,
                    max_workers=arguments.max_workers,
                    source_link_registry_path=arguments.source_link_registry,
                    source_alias_registry_path=arguments.source_alias_registry,
                    reviewed_alias_registry_path=arguments.reviewed_alias_registry,
                    progress_path=arguments.progress_path,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "daily-fotmob-identity-map":
            print(json.dumps(
                daily_fotmob_identity_map(
                    hours_ahead=arguments.hours_ahead,
                    result_days=arguments.result_days,
                    max_workers=arguments.max_workers,
                    source_link_registry_path=arguments.source_link_registry,
                    source_alias_registry_path=arguments.source_alias_registry,
                    reviewed_alias_registry_path=arguments.reviewed_alias_registry,
                    historical_progress_path=arguments.historical_progress_path,
                    daily_progress_path=arguments.daily_progress_path,
                    retry_wikidata_now=arguments.retry_wikidata_now,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "historical-fotmob-alias-candidates":
            print(json.dumps(
                historical_fotmob_alias_candidates(
                    max_teams=arguments.max_teams,
                    progress_path=arguments.progress_path,
                    reviewed_alias_registry_path=arguments.reviewed_alias_registry,
                    source_alias_registry_path=arguments.source_alias_registry,
                    source_link_registry_path=arguments.source_link_registry,
                    output_path=arguments.output_path,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "historical-fotmob-failure-report":
            print(json.dumps(
                historical_fotmob_failure_report(arguments.progress_path),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "daily-freeze-status":
            print(json.dumps(
                daily_freeze_status(
                    arguments.ledger_path,
                    arguments.scheduler_log_path,
                    date=arguments.date,
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "daily-slip":
            records = read_records(arguments.ledger)
            slip_report = build_daily_slip(
                records,
                target_date=arguments.date,
                budget=arguments.budget,
                now=datetime.now(ZoneInfo("Asia/Shanghai")),
                ledger_path=arguments.ledger,
            )
            print(format_daily_slip(slip_report))
        elif arguments.command == "source-access-check":
            print(json.dumps(
                source_access_report(),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "understat-alias-import":
            print(json.dumps(
                import_manual_understat_aliases(arguments.registry_path, arguments.input_path),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "fotmob-search-alias-import":
            print(json.dumps(
                import_reviewed_fotmob_search_aliases(arguments.registry_path, arguments.input_path),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "match-preflight":
            summary = link_summary(arguments.resolved_path)
            if not summary["ready"]:
                raise ValueError(
                    "resolved candidate links are incomplete: " + ", ".join(summary["missing"])
                )
            identity = summary["sporttery"]
            print(json.dumps(
                collect_pregame_bundle(
                    identity["match_id"],
                    summary["oddsportal_url"],
                    summary["home_fotmob_url"],
                    summary["away_fotmob_url"],
                    method=arguments.method,
                    model_weight=arguments.model_weight,
                    kickoff_at=identity.get("kickoff_at"),
                    home_team=identity.get("home_team"),
                    away_team=identity.get("away_team"),
                    competition=identity.get("competition"),
                ),
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "settlement-queue":
            print(json.dumps(
                settlement_queue(read_records(arguments.ledger_path)),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "resolve-links":
            resolved = resolve_links(
                arguments.input_path,
                alias_registry_path=arguments.alias_registry,
            )
            if arguments.output:
                output_path = Path(arguments.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(resolved, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                print(json.dumps(
                    {
                        "status": "written",
                        "output_path": str(output_path),
                        "match_id": resolved["sporttery"].get("match_id"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ))
            else:
                print(json.dumps(
                    resolved,
                    ensure_ascii=False,
                    sort_keys=True,
                ))
        elif arguments.command == "candidate-template":
            template = collect_candidate_template(arguments.match_id, method=arguments.method)
            if arguments.output:
                output_path = Path(arguments.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(template, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                print(json.dumps(
                    {
                        "status": "written",
                        "output_path": str(output_path),
                        "match_id": template["sporttery"].get("match_id"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ))
            else:
                print(json.dumps(
                    template,
                    ensure_ascii=False,
                    sort_keys=True,
                ))
        elif arguments.command == "link-summary":
            print(json.dumps(
                link_summary(arguments.input_path),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif arguments.command == "workflow-next":
            workflow_result = workflow_next(arguments.match_id, base_dir=arguments.base_dir)
            if arguments.plain:
                print(format_workflow_next(workflow_result))
            else:
                print(json.dumps(
                    workflow_result,
                    ensure_ascii=False,
                    sort_keys=True,
                ))
        elif arguments.command == "review":
            records = read_records(arguments.ledger_path)
            if arguments.ai:
                result = DeepSeekReviewClient.from_environment(
                    model=arguments.model,
                    base_url=arguments.base_url,
                    timeout=arguments.timeout,
                ).review(records, arguments.match_id)
            else:
                result = build_review(records, arguments.match_id)
            if arguments.as_json:
                print(json.dumps(result, default=_json_default, ensure_ascii=False, sort_keys=True))
            elif arguments.ai:
                print(result["ai_review"])
            else:
                print(format_review(result))
        elif arguments.command == "configure-deepseek":
            api_key = getpass.getpass("请输入 DeepSeek API Key（输入不会显示）: ")
            path = save_local_configuration(
                api_key,
                model=arguments.model,
                base_url=arguments.base_url,
            )
            print(f"DeepSeek 本地配置已保存到 {path}。")
        elif arguments.command == "configure-database":
            password = getpass.getpass("请输入 taicai_app 的 MySQL 密码（输入不会显示）: ")
            path = save_database_configuration(
                password,
                host=arguments.host,
                port=arguments.port,
                username=arguments.username,
                database=arguments.database,
            )
            print(f"MySQL 本地配置已保存到 {path}。")
        elif arguments.command == "sync-service-projection":
            result = create_configured_store().sync_records(read_records(arguments.ledger_path))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif arguments.command == "serve-api":
            from uvicorn import run

            from .dashboard import build_summary
            from .service_api import create_app

            run(
                create_app(
                    store=create_configured_store(),
                    record_loader=lambda: read_records(arguments.ledger_path),
                    summary_loader=lambda: build_summary(arguments.ledger_path),
                ),
                host=arguments.host,
                port=arguments.port,
            )
        else:
            result = report(arguments.ledger_path)
            if arguments.as_json:
                print(json.dumps(result, default=_json_default, sort_keys=True))
            else:
                print(format_report(result))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    main()
