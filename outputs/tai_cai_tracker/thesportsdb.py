"""TheSportsDB fallback for auditable pregame identity, venue, and weather."""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .context import _risk_summary, _unavailable, collect_weather_forecast
from .resolver import rank_context_event_candidates
from .transport import coordinated_public_request, proxy_mapping


API_ROOT = "https://www.thesportsdb.com/api/v1/json/123"


class _JsonPayload(dict):
    def __init__(self, payload, raw_content_sha256):
        super().__init__(payload)
        self.raw_content_sha256 = raw_content_sha256


def _captured_at():
    return datetime.now(timezone.utc).astimezone().isoformat()


def _http_get_json(url):
    def load():
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "TaiCaiTracker/1.0"})
        opener = build_opener(ProxyHandler(proxy_mapping(os.environ)))
        with opener.open(request, timeout=20) as response:
            raw = response.read()
        return _JsonPayload(json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest())

    return coordinated_public_request(url, load)


def _normalise_name(value):
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _matches_verified_team_name(event_name, official_name, verified_names):
    return any(
        isinstance(name, str)
        and name.strip()
        and _normalise_name(event_name) == _normalise_name(name)
        for name in [official_name, *(verified_names or [])]
    )


def _timestamp(value, target_timezone):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("TheSportsDB candidate has no timestamp with timezone")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("TheSportsDB candidate timestamp has no timezone")
    return parsed.astimezone(target_timezone).isoformat()


def _search_names(official_name, verified_names):
    candidates = [official_name, *(verified_names or [])]
    selected = []
    for name in candidates:
        if not isinstance(name, str) or not name.strip():
            continue
        # English aliases are normally the searchable form; retain the official
        # name as a final fallback when no such alias has been verified.
        if not re.search(r"[A-Za-z]", name) and any(re.search(r"[A-Za-z]", item) for item in candidates if isinstance(item, str)):
            continue
        if name not in selected:
            selected.append(name)
    return selected[:2]


def _event_search_url(home_team, away_team, date):
    return API_ROOT + "/searchevents.php?" + urlencode({
        "e": f"{home_team}_vs_{away_team}",
        "d": date,
    })


def collect_thesportsdb_event_candidates(identity, home_team_names=None, away_team_names=None):
    """Collect candidate events. Only timezone-bearing source timestamps are usable."""
    try:
        kickoff = datetime.fromisoformat(identity["kickoff_at"])
        if kickoff.tzinfo is None:
            raise ValueError("kickoff needs timezone")
        home_names = _search_names(identity["home_team"], home_team_names)
        away_names = _search_names(identity["away_team"], away_team_names)
        if not home_names or not away_names:
            raise ValueError("team names unavailable")
    except (KeyError, TypeError, ValueError):
        return {"status": "skipped", "reason": "official identity incomplete"}

    source_urls = []
    events = []
    for home_name in home_names:
        for away_name in away_names:
            source_url = _event_search_url(home_name, away_name, kickoff.date().isoformat())
            source_urls.append(source_url)
            try:
                events.extend(_http_get_json(source_url).get("event", []))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    if not events:
        return {"status": "skipped", "reason": "no public event candidates", "source_url": source_urls}

    captured_at = _captured_at()
    candidates = []
    seen = set()
    for event in events:
        try:
            event_id = str(event["idEvent"])
            if event_id in seen:
                continue
            seen.add(event_id)
            candidates.append({
                "event_id": event_id,
                "home_team": event["strHomeTeam"],
                "away_team": event["strAwayTeam"],
                "home_team_id": event.get("idHomeTeam"),
                "away_team_id": event.get("idAwayTeam"),
                "competition": event["strLeague"],
                "kickoff_at": _timestamp(event["strTimestamp"], kickoff.tzinfo),
                "source_url": f"{API_ROOT}/lookupevent.php?id={event_id}",
                "captured_at": captured_at,
            })
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
    if not candidates:
        return {
            "status": "skipped",
            "reason": "no candidates with timezone-bearing kickoff",
            "source_url": source_urls,
            "captured_at": captured_at,
        }
    return {"status": "collected", "source_url": source_urls, "captured_at": captured_at, "candidates": candidates}


def resolve_thesportsdb_event(identity, home_team_names=None, away_team_names=None):
    """Adopt at most one strictly matched TheSportsDB event candidate."""
    collected = collect_thesportsdb_event_candidates(identity, home_team_names, away_team_names)
    if collected.get("status") != "collected":
        return {**collected, "provider": "TheSportsDB"}
    try:
        official_kickoff = datetime.fromisoformat(identity["kickoff_at"])
        strict_candidates = [
            candidate
            for candidate in collected["candidates"]
            if datetime.fromisoformat(candidate["kickoff_at"]) == official_kickoff
        ]
    except (KeyError, TypeError, ValueError):
        strict_candidates = []
    ranked = rank_context_event_candidates(
        identity,
        strict_candidates,
        home_team_names=home_team_names,
        away_team_names=away_team_names,
    )
    result = {**ranked, "provider": "TheSportsDB", "captured_at": collected.get("captured_at")}
    if ranked.get("status") == "adopted":
        result["event_url"] = ranked["source_url"]
    else:
        result.setdefault("source_url", collected["source_url"])
    return result


def _venue_coordinates(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)°([NS])\s+([0-9]+(?:\.[0-9]+)?)°([EW])\s*",
        value,
    )
    if match is None:
        return None
    latitude = match.group(1)
    longitude = match.group(3)
    if match.group(2) == "S":
        latitude = "-" + latitude
    if match.group(4) == "W":
        longitude = "-" + longitude
    return {"latitude": latitude, "longitude": longitude}


def _matched_event(identity, event_url, payload, home_team_names=None, away_team_names=None):
    try:
        event = payload["events"][0]
        kickoff = datetime.fromisoformat(identity["kickoff_at"])
        if kickoff.tzinfo is None or _timestamp(event["strTimestamp"], kickoff.tzinfo) != kickoff.isoformat():
            raise ValueError("kickoff does not match")
        if not _matches_verified_team_name(event["strHomeTeam"], identity["home_team"], home_team_names):
            raise ValueError("home team does not match")
        if not _matches_verified_team_name(event["strAwayTeam"], identity["away_team"], away_team_names):
            raise ValueError("away team does not match")
        if _normalise_name(event["strLeague"]) != _normalise_name(identity["competition"]):
            raise ValueError("competition does not match")
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    return {
        "status": "matched",
        "provider": "TheSportsDB",
        "source_url": event_url,
        "captured_at": _captured_at(),
        "event_id": str(event["idEvent"]),
        "home_team_id": event.get("idHomeTeam"),
        "away_team_id": event.get("idAwayTeam"),
        "competition": event["strLeague"],
        "season": str(event.get("strSeason") or ""),
        "kickoff_at": identity["kickoff_at"],
        "venue": {},
        "venue_id": event.get("idVenue"),
    }


def collect_thesportsdb_pregame_context(identity, event_url, home_team_names=None, away_team_names=None):
    """Collect venue/weather context; TheSportsDB is never treated as an xG source."""
    try:
        payload = _http_get_json(event_url)
        event = _matched_event(identity, event_url, payload, home_team_names, away_team_names)
        raw_content_sha256 = getattr(payload, "raw_content_sha256", None)
        if event is not None and isinstance(raw_content_sha256, str):
            event["raw_content_sha256"] = raw_content_sha256
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        event = None
    split_xg = _unavailable("the_sports_db_does_not_publish_home_away_xg")
    lineup = _unavailable("complete_lineup_unavailable")
    if event is None:
        return {
            "event": _unavailable("event_identity_unverified"),
            "split_xg": split_xg,
            "weather": _unavailable("venue_coordinates_unavailable"),
            "lineup": lineup,
            "risk_summary": _risk_summary(split_xg, lineup),
        }
    venue_id = event.pop("venue_id", None)
    if venue_id:
        venue_url = f"{API_ROOT}/lookupvenue.php?id={venue_id}"
        try:
            venues = _http_get_json(venue_url).get("venues", [])
            coordinates = _venue_coordinates(venues[0].get("strMap")) if venues else None
            if coordinates is not None:
                event["venue"] = coordinates
        except (OSError, ValueError, TypeError, json.JSONDecodeError, IndexError):
            pass
    try:
        # Fetch only to preserve an auditable unavailable result when the public
        # endpoint is empty. Its schema does not establish a confirmed starting XI.
        _http_get_json(f"{API_ROOT}/lookuplineup.php?id={event['event_id']}")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    weather = collect_weather_forecast(event)
    return {
        "event": event,
        "split_xg": split_xg,
        "weather": weather,
        "lineup": lineup,
        "risk_summary": _risk_summary(split_xg, lineup),
    }
