"""Auditable optional pregame context for paper-only football predictions."""

import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .transport import proxy_mapping


def _unavailable(reason):
    return {"status": "unavailable", "reason": reason}


def validate_pregame_context(value):
    """Validate the optional context shape before it becomes a frozen record."""
    required_fields = {"split_xg", "weather", "lineup", "risk_summary"}
    if not isinstance(value, dict) or set(value) != required_fields:
        raise ValueError("pregame_context has invalid fields")
    normalized = {}
    allowed = {
        "split_xg": {"available", "unavailable"},
        "weather": {"available", "unavailable"},
        "lineup": {"confirmed", "reported", "unavailable"},
    }
    for name, statuses in allowed.items():
        entry = value[name]
        if not isinstance(entry, dict) or entry.get("status") not in statuses:
            raise ValueError(f"pregame_context {name} has invalid status")
        if entry["status"] == "unavailable":
            if set(entry) != {"status", "reason"} or not isinstance(entry["reason"], str) or not entry["reason"].strip():
                raise ValueError(f"pregame_context {name} unavailable result is invalid")
        normalized[name] = dict(entry)
    risk = value["risk_summary"]
    if (
        not isinstance(risk, dict)
        or set(risk) != {"confidence_ceiling", "split_xg_status", "lineup_status"}
        or risk["confidence_ceiling"] not in {"high", "medium", "low"}
        or risk["split_xg_status"] != normalized["split_xg"]["status"]
        or risk["lineup_status"] != normalized["lineup"]["status"]
    ):
        raise ValueError("pregame_context risk summary is invalid")
    normalized["risk_summary"] = dict(risk)
    return normalized


def _decimal(value):
    decimal = Decimal(str(value))
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("context values must be finite non-negative decimals")
    return decimal


def _positive_integer(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("context sample matches must be positive integers")
    return value


def build_split_xg(event, home_home, away_away):
    """Build home/away xG evidence or an explicit unavailable result."""
    try:
        home_matches = _positive_integer(home_home["matches"])
        away_matches = _positive_integer(away_away["matches"])
        home_attack_xg = _decimal(home_home["xg"])
        home_defense_xga = _decimal(home_home["xga"])
        away_attack_xg = _decimal(away_away["xg"])
        away_defense_xga = _decimal(away_away["xga"])
        required_event = {
            "source_url",
            "captured_at",
            "competition",
            "season",
            "home_team_id",
            "away_team_id",
            "kickoff_at",
        }
        if not required_event.issubset(event):
            raise ValueError("event identity is incomplete")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return _unavailable("home_away_xg_unavailable")

    with localcontext() as context:
        context.prec = max(context.prec, 40)
        home_xg = (home_attack_xg / home_matches + away_defense_xga / away_matches) / 2
        away_xg = (away_attack_xg / away_matches + home_defense_xga / home_matches) / 2
    return {
        "status": "available",
        "scope": "home_away_split",
        "source_url": event["source_url"],
        "captured_at": event["captured_at"],
        "competition": event["competition"],
        "season": event["season"],
        "kickoff_at": event["kickoff_at"],
        "home_team_id": event["home_team_id"],
        "away_team_id": event["away_team_id"],
        "home_sample_matches": home_matches,
        "away_sample_matches": away_matches,
        "home_attack_xg": +home_attack_xg,
        "home_defense_xga": +home_defense_xga,
        "away_attack_xg": +away_attack_xg,
        "away_defense_xga": +away_defense_xga,
        "home_xg": +home_xg,
        "away_xg": +away_xg,
        "formula": (
            "home_away_split: home_xg=mean(home_home_xg/home_sample_matches,"
            " away_away_xga/away_sample_matches); "
            "away_xg=mean(away_away_xg/away_sample_matches,"
            " home_home_xga/home_sample_matches)"
        ),
        "supporting_source_urls": {
            "home_home": home_home["source_url"],
            "away_away": away_away["source_url"],
        },
    }


def build_weather(event, forecast):
    """Return forecast evidence at kickoff or a reason it cannot be used."""
    venue = event.get("venue") if isinstance(event, dict) else None
    try:
        latitude = _decimal(venue["latitude"])
        longitude = _decimal(venue["longitude"])
        if latitude > 90 or longitude > 180:
            raise ValueError("coordinates are out of range")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return _unavailable("venue_coordinates_unavailable")

    try:
        kickoff_at = datetime.fromisoformat(event["kickoff_at"])
        if kickoff_at.tzinfo is None:
            raise ValueError("kickoff must include a timezone")
        hourly = forecast["hourly"]
        selected = next(
            entry
            for entry in hourly
            if datetime.fromisoformat(entry["at"]) == kickoff_at
        )
        temperature = _decimal(selected["temperature_c"])
        precipitation = _decimal(selected["precipitation_mm"])
        wind_speed = _decimal(selected["wind_speed_kmh"])
        source_url = forecast["source_url"]
        captured_at = forecast["captured_at"]
        timezone = forecast["timezone"]
    except (KeyError, TypeError, ValueError, ArithmeticError, StopIteration):
        return _unavailable("weather_forecast_unavailable")

    return {
        "status": "available",
        "source_url": source_url,
        "captured_at": captured_at,
        "forecast_at": selected["at"],
        "timezone": timezone,
        "latitude": +latitude,
        "longitude": +longitude,
        "temperature_c": +temperature,
        "precipitation_mm": +precipitation,
        "wind_speed_kmh": +wind_speed,
    }


def build_lineup(lineup):
    """Classify a provider lineup without guessing absent starters."""
    try:
        home_starters = lineup["home_starters"]
        away_starters = lineup["away_starters"]
        if not isinstance(home_starters, list) or not isinstance(away_starters, list):
            raise ValueError("starters must be lists")
        if len(home_starters) != 11 or len(away_starters) != 11:
            raise ValueError("both sides need eleven starters")
        source_url = lineup["source_url"]
        captured_at = lineup["captured_at"]
        confirmed = lineup["confirmed"]
        if not isinstance(confirmed, bool):
            raise ValueError("confirmation must be boolean")
    except (KeyError, TypeError, ValueError):
        return _unavailable("complete_lineup_unavailable")

    return {
        "status": "confirmed" if confirmed else "reported",
        "source_url": source_url,
        "captured_at": captured_at,
        "home_starter_count": len(home_starters),
        "away_starter_count": len(away_starters),
        "provider_confirmed": confirmed,
    }


def _http_get_json(url, referer="https://www.sofascore.com/"):
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Mozilla/5.0",
            "Referer": referer,
        },
    )
    opener = build_opener(ProxyHandler(proxy_mapping(os.environ)))
    with opener.open(request, timeout=20) as response:
        raw = response.read()
    return _JsonPayload(json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest())


class _JsonPayload(dict):
    def __init__(self, payload, raw_content_sha256):
        super().__init__(payload)
        self.raw_content_sha256 = raw_content_sha256


def _captured_at():
    return datetime.now(timezone.utc).astimezone().isoformat()


def _normalise_name(value):
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _matches_verified_team_name(event_name, official_name, verified_names):
    names = [official_name, *(verified_names or [])]
    return any(
        isinstance(name, str)
        and name.strip()
        and _normalise_name(event_name) == _normalise_name(name)
        for name in names
    )


def _matched_event(identity, event_url, payload, home_team_names=None, away_team_names=None):
    try:
        event = payload["event"]
        kickoff_at = datetime.fromisoformat(identity["kickoff_at"])
        event_kickoff = datetime.fromtimestamp(event["startTimestamp"], timezone.utc)
        home_name = event["homeTeam"]["name"]
        away_name = event["awayTeam"]["name"]
        competition = event["tournament"]["name"]
        if (
            kickoff_at.tzinfo is None
            or event_kickoff.astimezone(kickoff_at.tzinfo) != kickoff_at
            or not _matches_verified_team_name(
                home_name, identity["home_team"], home_team_names
            )
            or not _matches_verified_team_name(
                away_name, identity["away_team"], away_team_names
            )
            or _normalise_name(competition) != _normalise_name(identity["competition"])
        ):
            raise ValueError("event identity does not match")
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return None
    tournament = event.get("tournament", {}).get("uniqueTournament", {})
    season = event.get("season", {})
    venue = event.get("venue", {})
    return {
        "status": "matched",
        "source_url": event_url,
        "captured_at": _captured_at(),
        "event_id": event["id"],
        "home_team_id": event["homeTeam"]["id"],
        "away_team_id": event["awayTeam"]["id"],
        "competition": competition,
        "season": str(season.get("name") or season.get("id") or ""),
        "kickoff_at": identity["kickoff_at"],
        "tournament_id": tournament.get("id"),
        "season_id": season.get("id"),
        "venue": {
            "latitude": venue.get("lat"),
            "longitude": venue.get("lng"),
        },
    }


def _statistics_url(team_id, tournament_id, season_id, scope):
    return (
        f"https://www.sofascore.com/api/v1/team/{team_id}/unique-tournament/"
        f"{tournament_id}/season/{season_id}/statistics/{scope}"
    )


def _split_statistics(source_url, payload):
    stats = payload.get("statistics", {}) if isinstance(payload, dict) else {}
    return {
        "source_url": source_url,
        "captured_at": _captured_at(),
        "matches": stats.get("matches") or stats.get("matchesPlayed"),
        "xg": stats.get("expectedGoals"),
        "xga": stats.get("expectedGoalsConceded"),
    }


def _lineup_from_payload(event_url, payload):
    lineups = payload.get("lineups", payload) if isinstance(payload, dict) else {}
    home = lineups.get("home", {})
    away = lineups.get("away", {})
    return {
        "source_url": f"{event_url}/lineups",
        "captured_at": _captured_at(),
        "confirmed": lineups.get("confirmed", False),
        "home_starters": [
            row for row in home.get("players", []) if not row.get("substitute", True)
        ],
        "away_starters": [
            row for row in away.get("players", []) if not row.get("substitute", True)
        ],
    }


def _risk_summary(split_xg, lineup):
    if split_xg.get("status") == "available" and lineup.get("status") == "confirmed":
        ceiling = "high"
    else:
        ceiling = "medium"
    return {
        "confidence_ceiling": ceiling,
        "split_xg_status": split_xg["status"],
        "lineup_status": lineup["status"],
    }


def collect_weather_forecast(event):
    """Fetch Open-Meteo weather only for coordinates supplied by the matched event."""
    venue = event.get("venue") if isinstance(event, dict) else None
    try:
        latitude = _decimal(venue["latitude"])
        longitude = _decimal(venue["longitude"])
        if latitude > 90 or longitude > 180:
            raise ValueError("coordinates are out of range")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return _unavailable("venue_coordinates_unavailable")
    source_url = "https://api.open-meteo.com/v1/forecast?" + urlencode({
        "latitude": str(latitude),
        "longitude": str(longitude),
        "hourly": "temperature_2m,precipitation,wind_speed_10m",
        "timezone": "auto",
    })
    try:
        payload = _http_get_json(source_url, "https://open-meteo.com/")
        hourly = payload["hourly"]
        timezone_name = payload["timezone"]
        timezone_info = ZoneInfo(timezone_name)
        rows = [
            {
                "at": datetime.fromisoformat(value).replace(tzinfo=timezone_info).isoformat(),
                "temperature_c": hourly["temperature_2m"][index],
                "precipitation_mm": hourly["precipitation"][index],
                "wind_speed_kmh": hourly["wind_speed_10m"][index],
            }
            for index, value in enumerate(hourly["time"])
        ]
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        IndexError,
        ZoneInfoNotFoundError,
        json.JSONDecodeError,
    ):
        return _unavailable("weather_forecast_unavailable")
    return build_weather(event, {
        "source_url": source_url,
        "captured_at": _captured_at(),
        "timezone": timezone_name,
        "hourly": rows,
    })


def collect_pregame_context(identity, event_url, home_team_names=None, away_team_names=None):
    """Collect optional public context without writing a prediction ledger record."""
    try:
        event_payload = _http_get_json(event_url)
        event = _matched_event(
            identity,
            event_url,
            event_payload,
            home_team_names=home_team_names,
            away_team_names=away_team_names,
        )
        raw_content_sha256 = getattr(event_payload, "raw_content_sha256", None)
        if event is not None and isinstance(raw_content_sha256, str):
            event["raw_content_sha256"] = raw_content_sha256
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        event = None
    if event is None:
        unavailable_event = _unavailable("event_identity_unverified")
        unavailable_split = _unavailable("venue_or_team_statistics_unavailable")
        unavailable_weather = _unavailable("venue_coordinates_unavailable")
        unavailable_lineup = _unavailable("complete_lineup_unavailable")
        return {
            "event": unavailable_event,
            "split_xg": unavailable_split,
            "weather": unavailable_weather,
            "lineup": unavailable_lineup,
            "risk_summary": _risk_summary(unavailable_split, unavailable_lineup),
        }

    split_xg = _unavailable("venue_or_team_statistics_unavailable")
    if event["tournament_id"] is not None and event["season_id"] is not None:
        try:
            home_url = _statistics_url(event["home_team_id"], event["tournament_id"], event["season_id"], "home")
            away_url = _statistics_url(event["away_team_id"], event["tournament_id"], event["season_id"], "away")
            split_xg = build_split_xg(
                event,
                _split_statistics(home_url, _http_get_json(home_url, event_url)),
                _split_statistics(away_url, _http_get_json(away_url, event_url)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    if split_xg.get("status") != "available":
        split_xg = _unavailable("venue_or_team_statistics_unavailable")

    weather = collect_weather_forecast(event)
    lineup = _unavailable("complete_lineup_unavailable")
    try:
        lineup = build_lineup(_lineup_from_payload(event_url, _http_get_json(f"{event_url}/lineups", event_url)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return {
        "event": event,
        "split_xg": split_xg,
        "weather": weather,
        "lineup": lineup,
        "risk_summary": _risk_summary(split_xg, lineup),
    }
