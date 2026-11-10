"""FotMob per-match xG collector for paper-only evidence backfill."""

import gzip
import json
import os
import time
from urllib.request import ProxyHandler, Request, build_opener

from .transport import proxy_mapping


BASE_URL = "https://www.fotmob.com/api/data"
MATCHES_URL = BASE_URL + "/matches?date={day}"
MATCH_DETAILS_URL = BASE_URL + "/matchDetails?matchId={match_id}"
REQUEST_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}
TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 3
RETRY_DELAYS = (1, 2, 4)

_XG_TITLES = {
    "expected goals (xg)": ("home_xg", "away_xg"),
    "xg on target (xgot)": ("home_xgot", "away_xgot"),
    "xg open play": ("home_xg_open_play", "away_xg_open_play"),
    "xg set play": ("home_xg_set_play", "away_xg_set_play"),
    "xg non-penalty": ("home_xg_non_penalty", "away_xg_non_penalty"),
}

_XG_KEYS = (
    "home_xg",
    "away_xg",
    "home_xgot",
    "away_xgot",
    "home_xg_open_play",
    "away_xg_open_play",
    "home_xg_set_play",
    "away_xg_set_play",
    "home_xg_non_penalty",
    "away_xg_non_penalty",
)


def _opener():
    mapping = proxy_mapping(os.environ)
    # An explicit empty mapping would disable environment proxies, so let
    # ProxyHandler fall back to HTTP(S)_PROXY when the project proxy is unset.
    return build_opener(ProxyHandler(mapping if mapping else None))


def _http_json(url):
    """Fetch one JSON object with bounded retries; raises after the last try."""
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            opener = _opener()
            with opener.open(Request(url, headers=REQUEST_HEADERS), timeout=TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise OSError(f"FotMob returned HTTP {status}")
                raw = response.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("FotMob response was not a JSON object")
            return payload
        except (OSError, ValueError, UnicodeDecodeError) as error:
            last_error = error
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(RETRY_DELAYS[attempt])
    raise last_error


def _to_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def list_matches_by_date(day):
    """List FotMob fixtures for one YYYYMMDD calendar date."""
    payload = _http_json(MATCHES_URL.format(day=day))
    matches = []
    for league in payload.get("leagues") or []:
        if not isinstance(league, dict):
            continue
        league_name = league.get("name")
        ccode = league.get("ccode")
        for match in league.get("matches") or []:
            if not isinstance(match, dict) or match.get("id") is None:
                continue
            home = match.get("home") if isinstance(match.get("home"), dict) else {}
            away = match.get("away") if isinstance(match.get("away"), dict) else {}
            status = match.get("status") if isinstance(match.get("status"), dict) else {}
            matches.append({
                "match_id": str(match["id"]),
                "home": home.get("name"),
                "away": away.get("name"),
                "utc_time": status.get("utcTime"),
                "league": league_name,
                "ccode": ccode,
                "finished": bool(status.get("finished")),
                "score": status.get("scoreStr"),
            })
    return matches


def match_xg(match_id):
    """Extract FotMob per-match xG fields; coverage may be ratings/lower."""
    payload = _http_json(MATCH_DETAILS_URL.format(match_id=match_id))
    if not isinstance(payload, dict):
        return None
    general = payload.get("general") if isinstance(payload.get("general"), dict) else {}
    coverage = general.get("coverageLevel")
    values = {key: None for key in _XG_KEYS}

    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    stats = content.get("stats") if isinstance(content.get("stats"), dict) else {}
    periods = stats.get("Periods") if isinstance(stats.get("Periods"), dict) else {}
    all_period = periods.get("All") if isinstance(periods.get("All"), dict) else {}
    groups = all_period.get("stats") if isinstance(all_period.get("stats"), list) else []

    seen = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        rows = group.get("stats") if isinstance(group.get("stats"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = row.get("title")
            if not isinstance(title, str):
                continue
            keys = _XG_TITLES.get(" ".join(title.split()).casefold())
            if keys is None or keys[0] in seen:
                continue
            pair = row.get("stats")
            if not isinstance(pair, list) or len(pair) < 2:
                continue
            values[keys[0]] = _to_float(pair[0])
            values[keys[1]] = _to_float(pair[1])
            seen.add(keys[0])

    return {"coverage": coverage, **values}
