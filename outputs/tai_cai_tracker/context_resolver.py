"""Public event-candidate collection for the optional pregame context flow."""

import json
import os
from datetime import datetime, timezone
from urllib.request import ProxyHandler, Request, build_opener

from .resolver import rank_context_event_candidates
from .transport import proxy_mapping


SCHEDULE_URL = "https://www.sofascore.com/api/v1/sport/football/scheduled-events/{date}"
EVENT_URL = "https://www.sofascore.com/api/v1/event/{event_id}"


def _http_get_json(url):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    opener = build_opener(ProxyHandler(proxy_mapping(os.environ)))
    with opener.open(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def collect_context_event_candidates(identity):
    """Collect public candidates for one official kickoff date without matching them."""
    try:
        kickoff = datetime.fromisoformat(identity["kickoff_at"])
        if kickoff.tzinfo is None:
            raise ValueError("kickoff needs timezone")
        local_date = kickoff.date()
        utc_date = kickoff.astimezone(timezone.utc).date()
        source_urls = [SCHEDULE_URL.format(date=local_date.isoformat())]
        if utc_date != local_date:
            source_urls.append(SCHEDULE_URL.format(date=utc_date.isoformat()))
        events = []
        for source_url in source_urls:
            events.extend(_http_get_json(source_url).get("events", []))
        captured_at = datetime.now(timezone.utc).astimezone().isoformat()
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return {"status": "skipped", "reason": "public event source unavailable"}
    candidates = []
    for event in events:
        try:
            candidates.append({
                "event_id": event["id"],
                "home_team": event["homeTeam"]["name"],
                "away_team": event["awayTeam"]["name"],
                "home_team_id": event["homeTeam"]["id"],
                "away_team_id": event["awayTeam"]["id"],
                "competition": event["tournament"]["name"],
                "kickoff_at": datetime.fromtimestamp(event["startTimestamp"], timezone.utc).astimezone(kickoff.tzinfo).isoformat(),
                "source_url": EVENT_URL.format(event_id=event["id"]),
                "captured_at": captured_at,
            })
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
    if not candidates:
        return {"status": "skipped", "reason": "no public event candidates"}
    return {"status": "collected", "source_url": source_urls, "captured_at": captured_at, "candidates": candidates}


def resolve_context_event(identity, home_team_names=None, away_team_names=None):
    """Return one adopted event URL or an auditable skip result."""
    collected = collect_context_event_candidates(identity)
    if collected.get("status") != "collected":
        return collected
    ranked = rank_context_event_candidates(
        identity,
        collected["candidates"],
        home_team_names=home_team_names,
        away_team_names=away_team_names,
    )
    if ranked.get("status") != "adopted":
        return {**ranked, "source_url": collected["source_url"], "captured_at": collected["captured_at"]}
    return {**ranked, "event_url": ranked["source_url"], "captured_at": collected.get("captured_at")}
