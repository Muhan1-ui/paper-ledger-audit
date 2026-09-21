"""Local sales-cutoff timing policy for paper-collection scheduling."""

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_POLICY = {
    "version": 1,
    "timezone": "Asia/Shanghai",
    "sales_day_basis": "run_date",
    "weekday_cutoff": "22:00",
    "weekend_cutoff": "23:00",
    "cutoff_window_start_minutes": 60,
    "cutoff_window_end_minutes": 30,
    "policy_source": "operator_observed_schedule",
}

_REQUIRED_FIELDS = {
    "version",
    "timezone",
    "sales_day_basis",
    "weekday_cutoff",
    "weekend_cutoff",
    "cutoff_window_start_minutes",
    "cutoff_window_end_minutes",
}
_OPTIONAL_FIELDS = {"policy_source"}


def default_policy():
    """Return a copy of the built-in local operational policy."""
    return dict(DEFAULT_POLICY)


def _cutoff_time(value, field_name):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must use HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must use HH:MM") from error
    if parsed.second or parsed.microsecond or len(value) != 5:
        raise ValueError(f"{field_name} must use HH:MM")
    return parsed


def _validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) - _OPTIONAL_FIELDS != _REQUIRED_FIELDS:
        raise ValueError("sales cutoff policy has invalid fields")
    if policy["version"] != 1:
        raise ValueError("sales cutoff policy version must be 1")
    if policy["sales_day_basis"] != "run_date":
        raise ValueError("sales_day_basis must be run_date")
    try:
        ZoneInfo(policy["timezone"])
    except (TypeError, ZoneInfoNotFoundError) as error:
        raise ValueError("timezone must be a valid IANA timezone") from error
    _cutoff_time(policy["weekday_cutoff"], "weekday_cutoff")
    _cutoff_time(policy["weekend_cutoff"], "weekend_cutoff")
    for field in ("cutoff_window_start_minutes", "cutoff_window_end_minutes"):
        value = policy[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
    if policy["cutoff_window_start_minutes"] <= policy["cutoff_window_end_minutes"]:
        raise ValueError("cutoff window start must be earlier than cutoff window end")
    if "policy_source" in policy and (
        not isinstance(policy["policy_source"], str) or not policy["policy_source"].strip()
    ):
        raise ValueError("policy_source must be a non-empty string")
    return dict(policy)


def load_policy(path):
    """Load and validate an explicit, non-evidence local scheduling policy."""
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read sales cutoff policy: {error}") from error
    return _validate_policy(policy)


def assess_freeze_timing(kickoff, now, min_lead_minutes, max_lead_minutes, policy):
    """Assess one fixture against either its regular or sales-cutoff window."""
    if not isinstance(kickoff, datetime) or kickoff.tzinfo is None:
        raise ValueError("kickoff must be a timezone-aware datetime")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    if not isinstance(min_lead_minutes, int) or min_lead_minutes < 0:
        raise ValueError("min_lead_minutes must be a non-negative integer")
    if not isinstance(max_lead_minutes, int) or max_lead_minutes < min_lead_minutes:
        raise ValueError("max_lead_minutes must be an integer no less than min_lead_minutes")

    policy = _validate_policy(policy)
    zone = ZoneInfo(policy["timezone"])
    local_now = now.astimezone(zone)
    local_kickoff = kickoff.astimezone(zone)
    cutoff_time = _cutoff_time(
        policy["weekend_cutoff"] if local_now.weekday() >= 5 else policy["weekday_cutoff"],
        "sales cutoff",
    )
    cutoff_at = datetime.combine(local_now.date(), cutoff_time, tzinfo=zone)
    is_late_fixture = local_kickoff > cutoff_at

    if is_late_fixture:
        basis = "sales_cutoff_window"
        window_start = cutoff_at - timedelta(minutes=policy["cutoff_window_start_minutes"])
        window_end = cutoff_at - timedelta(minutes=policy["cutoff_window_end_minutes"])
        sales_cutoff_at = cutoff_at
    else:
        basis = "kickoff_window"
        window_start = local_now + timedelta(minutes=min_lead_minutes)
        window_end = local_now + timedelta(minutes=max_lead_minutes)
        sales_cutoff_at = None

    eligible = window_start <= local_now <= window_end if is_late_fixture else local_now + timedelta(minutes=min_lead_minutes) <= local_kickoff <= local_now + timedelta(minutes=max_lead_minutes)
    if not is_late_fixture:
        window_start = local_kickoff - timedelta(minutes=max_lead_minutes)
        window_end = local_kickoff - timedelta(minutes=min_lead_minutes)
    return {
        "eligible": eligible and local_now < local_kickoff,
        "timing_basis": basis,
        "sales_cutoff_at": sales_cutoff_at.isoformat() if sales_cutoff_at else None,
        "window_start_at": window_start.isoformat(),
        "window_end_at": window_end.isoformat(),
        "reason": None if eligible and local_now < local_kickoff else "outside_freeze_window",
    }
