"""Strict public xGscore event collector for paper-only evidence."""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from html import unescape
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .transport import coordinated_public_request, proxy_mapping


BASE_URL = "https://xgscore.io"
_PREVIEW_PATH = re.compile(r"^/([a-z0-9-]+)/([a-z0-9-]+)/preview$")


def _text(markup):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _event_team(page_html, side):
    pattern = (
        r"<xgs-game_team\b[^>]*class=[\"'][^\"']*"
        + re.escape(f"-{side}")
        + r"[^\"']*[\"'][^>]*>(.*?)</xgs-game_team>"
    )
    blocks = re.findall(pattern, page_html, re.IGNORECASE | re.DOTALL)
    if not blocks:
        raise ValueError(f"xGscore event identity is missing {side} team")
    values = [_text(block) for block in blocks]
    candidates = [value for value in values if value and len(value) <= 80]
    if not candidates:
        raise ValueError(f"xGscore event identity is missing {side} team")
    return candidates[0]


def _kickoff_local(page_html):
    pattern = (
        r"\b([01]?\d|2[0-3]):[0-5]\d\s+"
        r"(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},\s+\d{4}\b"
    )
    matches = re.findall(pattern, _text(page_html), re.IGNORECASE)
    if not matches:
        raise ValueError("xGscore event identity is missing kickoff time")
    # Re-run against text to preserve the full matched timestamp rather than its first group.
    full = re.search(
        r"\b(?:[01]?\d|2[0-3]):[0-5]\d\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
        _text(page_html),
        re.IGNORECASE,
    )
    return full.group(0) if full else None


def _bookmaker_rows(page_html):
    rows = []
    for block in re.findall(r"<xgs-odds\b[^>]*>(.*?)</xgs-odds>", page_html, re.IGNORECASE | re.DOTALL):
        if not re.search(r"<img\b[^>]*\balt=[\"']1xbet[\"']", block, re.IGNORECASE):
            continue
        if not re.search(r"<small\b[^>]*>\s*Result\s*</small>", block, re.IGNORECASE):
            continue
        prices = {}
        for cell in re.findall(r"<xgs-odds-cell\b[^>]*>(.*?)</xgs-odds-cell>", block, re.IGNORECASE | re.DOTALL):
            match = re.fullmatch(r"([12xX])\s+(\d+(?:\.\d+)?)", _text(cell))
            if match and match.group(1).casefold() not in prices:
                prices[match.group(1).casefold()] = match.group(2)
            if set(prices) == {"1", "x", "2"}:
                break
        if set(prices) == {"1", "x", "2"}:
            rows.append({
                "label": "1X2",
                "bookmaker": "1xbet",
                "prices": {
                    "home_win": prices["1"],
                    "draw": prices["x"],
                    "away_win": prices["2"],
                },
            })
    for block in re.findall(r"<(?:section|div)\b[^>]*>(.*?)</(?:section|div)>", page_html, re.IGNORECASE | re.DOTALL):
        if not re.search(r"<img\b[^>]*\balt=[\"']1xbet[\"']", block, re.IGNORECASE):
            continue
        if not re.search(r"<small\b[^>]*>\s*Result\s*</small>", block, re.IGNORECASE):
            continue
        labels_and_prices = re.findall(
            r"<span\b[^>]*>\s*([12xX])\s*</span>\s*<(?:strong|b|span)\b[^>]*>\s*(\d+(?:\.\d+)?)\s*</(?:strong|b|span)>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        prices = {label.casefold(): price for label, price in labels_and_prices}
        if set(prices) == {"1", "x", "2"}:
            rows.append({
                "label": "1X2",
                "bookmaker": "1xbet",
                "prices": {
                    "home_win": prices["1"],
                    "draw": prices["x"],
                    "away_win": prices["2"],
                },
            })
    if len(rows) != 1:
        raise ValueError("xGscore event has no uniquely complete explicit 1xbet Result row")
    return rows


def _metadata_block(page_html, side):
    pattern = (
        r"<xgs-team-metadata\b[^>]*class=[\"'][^\"']*"
        + re.escape(f"-{side}")
        + r"[^\"']*[\"'][^>]*>(.*?)</xgs-team-metadata>"
    )
    blocks = re.findall(pattern, page_html, re.IGNORECASE | re.DOTALL)
    blocks = [block for block in blocks if "All Season" in _text(block)]
    if len(blocks) != 1:
        raise ValueError(f"xGscore season metadata is missing {side} aggregate")
    return _text(blocks[0])


def _after_label(text, label):
    match = re.search(
        re.escape(label) + r"(?:\s+[A-Za-z ]+)?\s+(\d+(?:\.\d+)?)\b",
        text,
        re.IGNORECASE,
    )
    if match is None:
        raise ValueError(f"xGscore season metadata is missing {label}")
    return match.group(1)


def _season_xg(page_html, side):
    text = _metadata_block(page_html, side)
    sample_match = re.search(r"\bMP\s+(\d+)\b", text, re.IGNORECASE)
    if sample_match is None or int(sample_match.group(1)) <= 0:
        raise ValueError(f"xGscore season metadata is missing {side} match count")
    xg_match = re.search(r"\bxG\s+(\d+(?:\.\d+)?)\b", text)
    xga_match = re.search(r"\bxGA\s+(\d+(?:\.\d+)?)\b", text)
    if xg_match is None or xga_match is None:
        raise ValueError(f"xGscore season metadata is missing {side} xG/xGA")
    return {
        "sample_matches": int(sample_match.group(1)),
        "xg_per_match": xg_match.group(1),
        "xga_per_match": xga_match.group(1),
    }


def _structured_event(page_html):
    for raw in re.findall(r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", page_html, re.IGNORECASE | re.DOTALL):
        try:
            payload = json.loads(_text(raw))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("@type") != "SportsEvent":
            continue
        home = payload.get("homeTeam", {}).get("name") if isinstance(payload.get("homeTeam"), dict) else None
        away = payload.get("awayTeam", {}).get("name") if isinstance(payload.get("awayTeam"), dict) else None
        kickoff = payload.get("startDate")
        try:
            kickoff_at = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
        except ValueError:
            continue
        if not isinstance(home, str) or not home.strip() or not isinstance(away, str) or not away.strip() or kickoff_at.tzinfo is None:
            continue
        return {
            "home_team": home.strip(),
            "away_team": away.strip(),
            "kickoff_at": kickoff_at.isoformat(),
        }
    raise ValueError("xGscore event structured identity is missing")


def _server_state(page_html):
    match = re.search(
        r"<script\b[^>]*\bid=[\"']serverApp-state[\"'][^>]*>(.*?)</script>",
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise ValueError("xGscore event state is missing")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ValueError("xGscore event state is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("xGscore event state is invalid")
    return payload


def _season_xg_from_state(page_html, side):
    metadata_name = f"{side}TeamMetadata"
    candidates = []
    for entry in _server_state(page_html).values():
        block = entry.get("b") if isinstance(entry, dict) else None
        metadata = block.get(metadata_name) if isinstance(block, dict) else None
        season_table = metadata.get("seasonTable") if isinstance(metadata, dict) else None
        rows = season_table.get("rows") if isinstance(season_table, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            continue
        stats = rows[0].get("stats")
        if isinstance(stats, dict):
            candidates.append(stats)
    if len(candidates) != 1:
        raise ValueError(f"xGscore season state is missing {side} aggregate")
    stats = candidates[0]
    games = stats.get("games")
    if isinstance(games, bool) or not isinstance(games, int) or games <= 0:
        raise ValueError(f"xGscore season state is missing {side} match count")
    try:
        xg_total = Decimal(str(stats["xgScored"]))
        xga_total = Decimal(str(stats["xgConceded"]))
    except (KeyError, ArithmeticError) as error:
        raise ValueError(f"xGscore season state is missing {side} xG/xGA") from error
    season = stats.get("season", {}).get("start") if isinstance(stats.get("season"), dict) else None
    if not isinstance(season, int) or season < 1900:
        raise ValueError(f"xGscore season state is missing {side} season")
    team = stats.get("team", {}).get("name") if isinstance(stats.get("team"), dict) else None
    if not isinstance(team, str) or not team.strip():
        raise ValueError(f"xGscore season state is missing {side} team")
    return {
        "team": team.strip(),
        "season": str(season),
        "sample_matches": games,
        "xg_per_match": str(xg_total / games),
        "xga_per_match": str(xga_total / games),
    }


def _decimal_text(value, label, allow_zero=True):
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError) as error:
        raise ValueError(f"xGscore source match state is missing {label}") from error
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise ValueError(f"xGscore source match state is missing {label}")
    return str(number)


def _source_match_season(block):
    values = []

    def walk(value):
        if isinstance(value, dict):
            season_id = value.get("seasonId")
            if isinstance(season_id, int) and not isinstance(season_id, bool):
                values.append(str(season_id))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(block.get("standings"))
    unique = set(values)
    if not unique:
        return "source_not_exposed"
    if len(unique) != 1 or not re.fullmatch(r"\d{8}", next(iter(unique), "")):
        raise ValueError("xGscore source match state is ambiguous")
    season = next(iter(unique))
    return f"{season[:4]}/{season[4:]}"


def _source_match_venue_sample(metadata, expected_type, side):
    tables = metadata.get("gameTables") if isinstance(metadata, dict) else None
    candidates = [
        table for table in tables if isinstance(table, dict) and table.get("type") == expected_type
    ] if isinstance(tables, list) else []
    if len(candidates) != 1 or not isinstance(candidates[0].get("rows"), list):
        raise ValueError(f"xGscore source match state is missing {side} venue sample")
    rows = candidates[0]["rows"]
    if not rows:
        raise ValueError(f"xGscore source match state is missing {side} venue sample")
    for row in rows:
        game = row.get("game") if isinstance(row, dict) and isinstance(row.get("game"), dict) else row
        xg = game.get("xG") if isinstance(game, dict) else None
        if (
            not isinstance(game.get("datetime") if isinstance(game, dict) else None, str)
            or not isinstance(xg, dict)
        ):
            raise ValueError(f"xGscore source match state has invalid {side} venue sample")
        try:
            observed_at = datetime.fromisoformat(game["datetime"].replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"xGscore source match state has invalid {side} venue sample") from error
        if observed_at.tzinfo is None:
            raise ValueError(f"xGscore source match state has invalid {side} venue sample")
        _decimal_text(xg.get("s"), f"{side} venue xG")
        _decimal_text(xg.get("c"), f"{side} venue xGA")
    return len(rows)


def _source_match_xg_from_state(page_html):
    """Extract current xGscore's explicitly rendered pre-match source model."""
    candidates = []
    for entry in _server_state(page_html).values():
        block = entry.get("b") if isinstance(entry, dict) else None
        if not isinstance(block, dict):
            continue
        if {"forecastScore", "homeTeamMetadata", "awayTeamMetadata"}.issubset(block):
            candidates.append(block)
    if len(candidates) != 1:
        raise ValueError("xGscore source match state is missing unique model")
    block = candidates[0]
    forecast = block["forecastScore"]
    home = block["homeTeamMetadata"]
    away = block["awayTeamMetadata"]
    if not isinstance(forecast, dict) or not isinstance(home, dict) or not isinstance(away, dict):
        raise ValueError("xGscore source match state is invalid")
    home_goals = home.get("forecastGoals")
    away_goals = away.get("forecastGoals")
    if not isinstance(home_goals, dict) or not isinstance(away_goals, dict):
        raise ValueError("xGscore source match state is missing team aggregates")
    return {
        "home_xg": _decimal_text(forecast.get("h"), "home forecast", allow_zero=False),
        "away_xg": _decimal_text(forecast.get("a"), "away forecast", allow_zero=False),
        "home_attack_xg": _decimal_text(home_goals.get("s"), "home attack xG"),
        "home_defense_xga": _decimal_text(home_goals.get("c"), "home defense xGA"),
        "away_attack_xg": _decimal_text(away_goals.get("s"), "away attack xG"),
        "away_defense_xga": _decimal_text(away_goals.get("c"), "away defense xGA"),
        "home_sample_matches": _source_match_venue_sample(home, "home", "home"),
        "away_sample_matches": _source_match_venue_sample(away, "away", "away"),
        "home_impact": _decimal_text(home.get("impact"), "home impact"),
        "away_impact": _decimal_text(away.get("impact"), "away impact"),
        "season": _source_match_season(block),
    }


def parse_event_page(page_html):
    """Parse a public xGscore event page without inferring missing fields."""
    if not isinstance(page_html, str) or not page_html.strip():
        raise ValueError("xGscore event response is empty")
    event = None
    try:
        event = _structured_event(page_html)
        source_match_xg = _source_match_xg_from_state(page_html)
        try:
            market_rows = _bookmaker_rows(page_html)
        except ValueError:
            market_rows = []
        return {
            "event": event,
            "market": {"market_name": "1X2", "rows": market_rows},
            "source_match_xg": {
                key: source_match_xg[key]
                for key in (
                    "home_xg", "away_xg", "home_attack_xg", "home_defense_xga",
                    "away_attack_xg", "away_defense_xga", "home_sample_matches",
                    "away_sample_matches", "home_impact", "away_impact",
                )
            },
            "season": source_match_xg["season"],
        }
    except ValueError as source_match_error:
        if event is not None and "forecastScore" in page_html:
            raise source_match_error
    try:
        event = _structured_event(page_html)
        home = _season_xg_from_state(page_html, "home")
        away = _season_xg_from_state(page_html, "away")
        if home["season"] != away["season"]:
            raise ValueError("xGscore event season is ambiguous")
        if _normal_name(event["home_team"]) != _normal_name(home["team"]) or _normal_name(event["away_team"]) != _normal_name(away["team"]):
            raise ValueError("xGscore event state identity mismatch")
        return {
            "event": event,
            "market": {"market_name": "1X2", "rows": _bookmaker_rows(page_html)},
            "season_xg": {
                "home": {key: home[key] for key in ("sample_matches", "xg_per_match", "xga_per_match")},
                "away": {key: away[key] for key in ("sample_matches", "xg_per_match", "xga_per_match")},
            },
            "season": home["season"],
        }
    except ValueError as structured_error:
        if "serverApp-state" in page_html or "SportsEvent" in page_html:
            raise structured_error
    season_values = re.findall(r"\bSeason\s+(\d{4})\b", _text(page_html), re.IGNORECASE)
    if not season_values or len(set(season_values)) != 1:
        raise ValueError("xGscore event season is missing or ambiguous")
    return {
        "event": {
            "home_team": _event_team(page_html, "home"),
            "away_team": _event_team(page_html, "away"),
            "kickoff_local": _kickoff_local(page_html),
        },
        "market": {"market_name": "1X2", "rows": _bookmaker_rows(page_html)},
        "season_xg": {
            "home": _season_xg(page_html, "home"),
            "away": _season_xg(page_html, "away"),
        },
        "season": season_values[0],
    }


def _valid_event_url(source_url):
    parsed = urlsplit(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    return (
        parsed.scheme == "https"
        and parsed.netloc == "xgscore.io"
        and len(parts) == 3
        and parts[-1] == "xgscore"
        and all(re.fullmatch(r"[a-z0-9-]+", part) for part in parts)
    )


def _preview_slug_matches_source_name(event_slug, source_name):
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", source_name.casefold())
        if len(token) >= 3 and token not in {"the", "club", "football", "soccer", "fc"}
    ]
    slug_tokens = set(event_slug.split("-"))
    return bool(tokens) and any(token in slug_tokens for token in tokens)


def discover_xgscore_event_url(home_source_name, away_source_name):
    """Locate one public xGscore detail candidate from its homepage previews.

    The returned locator remains a candidate until ``collect_xgscore`` verifies
    exact source teams and kickoff on the detail page.
    """
    result = discover_xgscore_event_urls([{
        "match_id": "single",
        "home_team": home_source_name,
        "away_team": away_source_name,
    }])["single"]
    if result["status"] != "candidate":
        raise ValueError(result["reason"])
    return result["source_url"]


def _discovery_names(primary_name, aliases):
    if not isinstance(primary_name, str) or not primary_name.strip():
        raise ValueError("xGscore discovery requires verified provider team names")
    values = [primary_name]
    if aliases is not None:
        if not isinstance(aliases, list) or not all(isinstance(value, str) and value.strip() for value in aliases):
            raise ValueError("xGscore discovery aliases must be non-empty strings")
        values.extend(aliases)
    normalized = []
    seen = set()
    for value in values:
        key = _normal_name(value)
        if key and key not in seen:
            seen.add(key)
            normalized.append(value.strip())
    return normalized


def discover_xgscore_event_urls(fixtures):
    """Find strict xGscore candidates for multiple verified provider name pairs.

    The public homepage is fetched once per batch. Returned URLs are candidates
    only; ``collect_xgscore`` must still verify team identity and kickoff from
    each detail page before the source is retained or used as evidence.
    """
    if not isinstance(fixtures, list):
        raise ValueError("xGscore batch discovery requires a fixture list")
    normalized = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise ValueError("xGscore batch discovery fixture must be an object")
        match_id = fixture.get("match_id")
        home_team = fixture.get("home_team")
        away_team = fixture.get("away_team")
        if not isinstance(match_id, (str, int)) or not str(match_id).strip():
            raise ValueError("xGscore batch discovery fixture requires match_id")
        normalized.append((
            str(match_id),
            _discovery_names(home_team, fixture.get("home_team_aliases")),
            _discovery_names(away_team, fixture.get("away_team_aliases")),
        ))

    homepage, _raw_content_sha256 = _http_get(BASE_URL)
    previews = []
    for href in re.findall(r"\bhref=[\"']([^\"']+)[\"']", homepage, re.IGNORECASE):
        match = _PREVIEW_PATH.fullmatch(urlsplit(href).path)
        if match is not None:
            previews.append(match.groups())

    results = {}
    for match_id, home_names, away_names in normalized:
        urls = {
            f"{BASE_URL}/{league_slug}/{event_slug}/xgscore"
            for league_slug, event_slug in previews
            if any(_preview_slug_matches_source_name(event_slug, home_name) for home_name in home_names)
            and any(_preview_slug_matches_source_name(event_slug, away_name) for away_name in away_names)
        }
        results[match_id] = (
            {"status": "candidate", "source_url": next(iter(urls))}
            if len(urls) == 1
            else {
                "status": "unavailable",
                "reason": "xgscore homepage has no uniquely matched public preview",
            }
        )
    return results


def _normal_name(value):
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _matches_expected_team_name(observed, expected):
    values = expected if isinstance(expected, (list, tuple, set)) else [expected]
    observed_name = _normal_name(observed)
    return any(
        isinstance(value, str) and observed_name == _normal_name(value)
        for value in values
    )


def _kickoff_matches(displayed, kickoff_at):
    try:
        expected = datetime.fromisoformat(kickoff_at)
        observed = datetime.fromisoformat(displayed)
    except (TypeError, ValueError):
        try:
            observed = datetime.strptime(displayed, "%H:%M %B %d, %Y")
        except (TypeError, ValueError):
            return False
    if expected.tzinfo is None:
        return False
    if observed.tzinfo is not None:
        return expected.astimezone(timezone.utc) == observed.astimezone(timezone.utc)
    return (expected.hour, expected.minute, expected.date()) == (
        observed.hour, observed.minute, observed.date()
    )


def _http_get(source_url):
    def load():
        request = Request(source_url, headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"})
        opener = build_opener(ProxyHandler(proxy_mapping(os.environ)))
        with opener.open(request, timeout=20) as response:
            raw = response.read()
        return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()

    return coordinated_public_request(source_url, load)


def build_xg_inputs(season_xg, source_url, competition, season, captured_at, model_weight="0.40"):
    """Build traceable league-total xG evidence from per-game season aggregates."""
    home = season_xg["home"]
    away = season_xg["away"]
    home_xg = (Decimal(home["xg_per_match"]) + Decimal(away["xga_per_match"])) / 2
    away_xg = (Decimal(away["xg_per_match"]) + Decimal(home["xga_per_match"])) / 2
    return {
        "source_url": source_url,
        "home_xg": str(home_xg),
        "away_xg": str(away_xg),
        "model_version": "poisson-v1",
        "model_weight": str(Decimal(str(model_weight))),
        "confidence_ceiling": "medium",
        "evidence": {
            "source_url": source_url,
            "captured_at": captured_at,
            "published_at_unavailable_reason": "source does not expose publication time",
            "competition": competition,
            "season": season,
            "sample_matches": min(home["sample_matches"], away["sample_matches"]),
            "scope": "league_total",
            "home_attack_xg": str(Decimal(home["xg_per_match"]) * home["sample_matches"]),
            "home_defense_xga": str(Decimal(home["xga_per_match"]) * home["sample_matches"]),
            "away_attack_xg": str(Decimal(away["xg_per_match"]) * away["sample_matches"]),
            "away_defense_xga": str(Decimal(away["xga_per_match"]) * away["sample_matches"]),
            "formula": (
                "league_total: home_xg=mean(home_season_xg_per_game, "
                "away_season_xga_per_game); away_xg=mean(away_season_xg_per_game, "
                "home_season_xga_per_game)"
            ),
        },
    }


def build_source_match_xg_inputs(source_match_xg, source_url, competition, season, captured_at, model_weight="0.40"):
    """Build medium-confidence evidence from xGscore's displayed pre-match model."""
    return {
        "source_url": source_url,
        "home_xg": source_match_xg["home_xg"],
        "away_xg": source_match_xg["away_xg"],
        "model_version": "poisson-v1",
        "model_weight": str(Decimal(str(model_weight))),
        "confidence_ceiling": "medium",
        "evidence": {
            "source_url": source_url,
            "captured_at": captured_at,
            "published_at_unavailable_reason": "source does not expose publication time",
            "competition": competition,
            "season": season,
            "home_sample_matches": source_match_xg["home_sample_matches"],
            "away_sample_matches": source_match_xg["away_sample_matches"],
            "scope": "source_match_model",
            "home_attack_xg": source_match_xg["home_attack_xg"],
            "home_defense_xga": source_match_xg["home_defense_xga"],
            "away_attack_xg": source_match_xg["away_attack_xg"],
            "away_defense_xga": source_match_xg["away_defense_xga"],
            "formula": (
                "source_match_model: home_xg=xGscore forecastScore.home; "
                "away_xg=xGscore forecastScore.away; "
                f"home_impact={source_match_xg['home_impact']}; "
                f"away_impact={source_match_xg['away_impact']}; "
                "team aggregate xG/xGA are preserved as displayed source inputs"
            ),
        },
    }


def collect_xgscore(source_url, home_team, away_team, kickoff_at, competition, model_weight="0.40"):
    """Collect one pre-verified public event URL; this function never writes the ledger."""
    if not _valid_event_url(source_url):
        raise ValueError("xGscore source URL must be one https://xgscore.io/<league>/<event>/xgscore page")
    html, raw_content_sha256 = _http_get(source_url)
    parsed = parse_event_page(html)
    event = parsed["event"]
    if (
        not _matches_expected_team_name(event["home_team"], home_team)
        or not _matches_expected_team_name(event["away_team"], away_team)
        or not _kickoff_matches(event.get("kickoff_at") or event.get("kickoff_local"), kickoff_at)
    ):
        raise ValueError("xGscore event identity mismatch")
    captured_at = datetime.now(timezone.utc).astimezone().isoformat()
    source_match_xg = parsed.get("source_match_xg")
    xg_inputs = (
        build_source_match_xg_inputs(
            source_match_xg, source_url, competition, parsed["season"], captured_at, model_weight
        )
        if isinstance(source_match_xg, dict)
        else build_xg_inputs(
            parsed["season_xg"], source_url, competition, parsed["season"], captured_at, model_weight
        )
    )
    if isinstance(source_match_xg, dict):
        stat_observations = [
            {
                "source_url": source_url,
                "captured_at": captured_at,
                "raw_content_sha256": raw_content_sha256,
                "team": event["home_team"],
                "side": "home",
                "stat_name": stat_name,
                "value": source_match_xg[value_key],
                "sample_matches": source_match_xg["home_sample_matches"],
            }
            for stat_name, value_key in (
                ("source_match_model_attack_xg", "home_attack_xg"),
                ("source_match_model_defense_xga", "home_defense_xga"),
            )
        ] + [
            {
                "source_url": source_url,
                "captured_at": captured_at,
                "raw_content_sha256": raw_content_sha256,
                "team": event["away_team"],
                "side": "away",
                "stat_name": stat_name,
                "value": source_match_xg[value_key],
                "sample_matches": source_match_xg["away_sample_matches"],
            }
            for stat_name, value_key in (
                ("source_match_model_attack_xg", "away_attack_xg"),
                ("source_match_model_defense_xga", "away_defense_xga"),
            )
        ]
    else:
        stat_observations = [{
            "source_url": source_url,
            "captured_at": captured_at,
            "raw_content_sha256": raw_content_sha256,
            "team": event["home_team"],
            "side": "home",
            "stat_name": "season_xg_xga_per_game",
            "value": parsed["season_xg"]["home"]["xg_per_match"],
            "sample_matches": parsed["season_xg"]["home"]["sample_matches"],
        }, {
            "source_url": source_url,
            "captured_at": captured_at,
            "raw_content_sha256": raw_content_sha256,
            "team": event["away_team"],
            "side": "away",
            "stat_name": "season_xg_xga_per_game",
            "value": parsed["season_xg"]["away"]["xg_per_match"],
            "sample_matches": parsed["season_xg"]["away"]["sample_matches"],
        }]
    return {
        "provider": "xGscore",
        "source_url": source_url,
        "captured_at": captured_at,
        "raw_content_sha256": raw_content_sha256,
        "event": event,
        "market": parsed["market"],
        "xg_inputs": xg_inputs,
        "stat_observations": stat_observations,
    }
