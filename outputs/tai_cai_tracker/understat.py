"""Public Understat xG collectors for paper-only observations."""

import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from http.cookiejar import CookieJar
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from .transport import coordinated_public_request, proxy_mapping


BASE_URL = "https://understat.com"


def _compact_name(value):
    return "".join(str(value).split()).casefold()


def resolve_anchored_team_name(payload, identity):
    """Resolve one unique Understat roster entry from a cached Wikidata anchor."""
    teams = payload.get("teams") if isinstance(payload, dict) else None
    if not isinstance(teams, dict):
        return {"status": "unavailable", "reason": "understat_roster_unavailable"}
    if not isinstance(identity, dict):
        return {"status": "unavailable", "reason": "wikidata_anchor_unavailable"}
    names = identity.get("english_names")
    if not isinstance(names, list):
        names = [identity.get("english_name")]
    expected = {_compact_name(name) for name in names if isinstance(name, str) and name.strip()}
    if not expected:
        return {"status": "unavailable", "reason": "wikidata_anchor_unavailable"}
    matches = []
    for key, team in teams.items():
        if not isinstance(team, dict):
            continue
        title = team.get("title")
        team_id = team.get("id", key)
        if not isinstance(title, str) or not title.strip() or team_id is None:
            continue
        if _compact_name(title) in expected:
            matches.append((title.strip(), str(team_id)))
    if len(matches) != 1:
        return {
            "status": "unavailable",
            "reason": "understat_anchor_ambiguous" if matches else "understat_anchor_not_found",
        }
    source_name, source_team_id = matches[0]
    return {
        "status": "resolved",
        "source_name": source_name,
        "source_team_id": source_team_id,
    }


def _http_get(opener, url, referer=None, ajax=False):
    def load():
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Accept-Encoding": "gzip",
            "User-Agent": "Mozilla/5.0",
        }
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        with opener.open(Request(url, headers=headers), timeout=20) as response:
            raw = response.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8"), dict(response.headers), hashlib.sha256(raw).hexdigest()

    return coordinated_public_request(url, load)


def _team_total(teams, team_name):
    expected = str(team_name).casefold()
    for team in teams.values():
        if str(team.get("title", "")).casefold() != expected:
            continue
        history = team.get("history", [])
        if not isinstance(history, list):
            raise ValueError("Understat team history is invalid")
        xg = sum((Decimal(str(row["xG"])) for row in history), Decimal("0"))
        xga = sum((Decimal(str(row["xGA"])) for row in history), Decimal("0"))
        return {
            "team_id": str(team.get("id", "")),
            "team_name": team["title"],
            "sample_matches": len(history),
            "xg": str(xg),
            "xga": str(xga),
        }
    raise ValueError(f"Understat team was not found: {team_name}")


def _history_date(row):
    value = row.get("date")
    if not isinstance(value, str):
        raise ValueError("Understat history row has no date")
    try:
        return datetime.strptime(value.split()[0], "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError("Understat history row has an invalid date") from error


def _fixture_kickoff_date(value):
    if not isinstance(value, str):
        raise ValueError("fixture kickoff_at is required for rolling Understat xG")
    try:
        kickoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("fixture kickoff_at must be ISO-8601") from error
    if kickoff.tzinfo is None:
        raise ValueError("fixture kickoff_at must include a timezone")
    return kickoff.date()


def _team_venue_total(teams, team_name, venue, cutoff_date=None, rolling_matches=None):
    expected = str(team_name).casefold()
    for team in teams.values():
        if str(team.get("title", "")).casefold() != expected:
            continue
        history = team.get("history", [])
        if not isinstance(history, list):
            raise ValueError("Understat team history is invalid")
        rows = [row for row in history if row.get("h_a") == venue]
        row_dates = None
        if cutoff_date is not None:
            dated_rows = [(_history_date(row), row) for row in rows]
            rows = [row for match_date, row in dated_rows if match_date < cutoff_date]
            row_dates = [match_date for match_date, _row in dated_rows if match_date < cutoff_date]
            rows_and_dates = sorted(zip(rows, row_dates), key=lambda item: item[1])
            if rolling_matches is not None:
                rows_and_dates = rows_and_dates[-rolling_matches:]
            rows = [row for row, _match_date in rows_and_dates]
            row_dates = [match_date for _row, match_date in rows_and_dates]
        xg = sum((Decimal(str(row["xG"])) for row in rows), Decimal("0"))
        xga = sum((Decimal(str(row["xGA"])) for row in rows), Decimal("0"))
        result = {
            "team_id": str(team.get("id", "")),
            "team_name": team["title"],
            "sample_matches": len(rows),
            "xg": str(xg),
            "xga": str(xga),
        }
        if cutoff_date is not None:
            result["history_cutoff_date"] = cutoff_date.isoformat()
            result["sample_first_date"] = row_dates[0].isoformat() if row_dates else None
            result["sample_last_date"] = row_dates[-1].isoformat() if row_dates else None
            result["sample_window_matches"] = rolling_matches
        return result
    raise ValueError(f"Understat team was not found: {team_name}")


def parse_league_xg(payload, home_team, away_team):
    """Extract named teams' cumulative league xG/xGA from one league response."""
    teams = payload.get("teams") if isinstance(payload, dict) else None
    if not isinstance(teams, dict):
        raise ValueError("Understat league teams were not found")
    return {"home": _team_total(teams, home_team), "away": _team_total(teams, away_team)}


def parse_league_venue_xg(payload, home_team, away_team, cutoff_date=None, rolling_matches=None):
    """Extract a home team's home rows and away team's away rows from one response."""
    teams = payload.get("teams") if isinstance(payload, dict) else None
    if not isinstance(teams, dict):
        raise ValueError("Understat league teams were not found")
    return {
        "home": _team_venue_total(teams, home_team, "h", cutoff_date, rolling_matches),
        "away": _team_venue_total(teams, away_team, "a", cutoff_date, rolling_matches),
    }


def league_name_from_slug(league):
    """Map a league page slug to Understat's data-endpoint league name."""
    return str(league).replace("_", " ")


def build_xg_inputs(totals, source_url, competition, season, captured_at, model_weight="0.40"):
    """Build snapshot-compatible league-total evidence without inventing publication time."""
    home = totals["home"]
    away = totals["away"]
    home_matches = Decimal(str(home["sample_matches"]))
    away_matches = Decimal(str(away["sample_matches"]))
    home_xg = (Decimal(str(home["xg"])) / home_matches + Decimal(str(away["xga"])) / away_matches) / 2
    away_xg = (Decimal(str(away["xg"])) / away_matches + Decimal(str(home["xga"])) / home_matches) / 2
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
            "home_attack_xg": str(home["xg"]),
            "home_defense_xga": str(home["xga"]),
            "away_attack_xg": str(away["xg"]),
            "away_defense_xga": str(away["xga"]),
            "formula": (
                "league_total: home_xg=mean(home_attack_xg/home_sample_matches,"
                " away_defense_xga/away_sample_matches); "
                "away_xg=mean(away_attack_xg/away_sample_matches,"
                " home_defense_xga/home_sample_matches)"
            ),
        },
    }


def build_venue_xg_inputs(totals, source_url, competition, season, captured_at, model_weight="0.40"):
    """Build model-ready evidence using observed home-only and away-only samples."""
    home = totals["home"]
    away = totals["away"]
    home_matches = Decimal(str(home["sample_matches"]))
    away_matches = Decimal(str(away["sample_matches"]))
    if home_matches <= 0 or away_matches <= 0:
        raise ValueError("Understat venue samples must be positive")
    home_xg = (Decimal(str(home["xg"])) / home_matches + Decimal(str(away["xga"])) / away_matches) / 2
    away_xg = (Decimal(str(away["xg"])) / away_matches + Decimal(str(home["xga"])) / home_matches) / 2
    evidence = {
        "source_url": source_url,
        "captured_at": captured_at,
        "published_at_unavailable_reason": "source does not expose publication time",
        "competition": competition,
        "season": season,
        "home_sample_matches": home["sample_matches"],
        "away_sample_matches": away["sample_matches"],
        "scope": "home_away_split",
        "home_attack_xg": str(home["xg"]),
        "home_defense_xga": str(home["xga"]),
        "away_attack_xg": str(away["xg"]),
        "away_defense_xga": str(away["xga"]),
        "formula": (
            "home_away_split: home_xg=mean(home_home_xg/home_sample_matches,"
            " away_away_xga/away_sample_matches); "
            "away_xg=mean(away_away_xg/away_sample_matches,"
            " home_home_xga/home_sample_matches)"
        ),
    }
    if home.get("sample_window_matches") is not None:
        evidence.update({
            "sample_window_matches": home["sample_window_matches"],
            "history_cutoff_date": home["history_cutoff_date"],
            "home_sample_first_date": home["sample_first_date"],
            "home_sample_last_date": home["sample_last_date"],
            "away_sample_first_date": away["sample_first_date"],
            "away_sample_last_date": away["sample_last_date"],
        })
        evidence["formula"] += (
            "; selection=last sample_window_matches venue rows with Understat "
            "calendar date strictly before history_cutoff_date"
        )
    return {
        "source_url": source_url,
        "home_xg": str(home_xg),
        "away_xg": str(away_xg),
        "model_version": "poisson-v1",
        "model_weight": str(Decimal(str(model_weight))),
        "evidence": evidence,
    }


def _season_source_block(source):
    """Return the immutable facts used from one Understat season response."""
    required = {
        "source_url", "captured_at", "raw_content_sha256", "competition",
        "season", "totals",
    }
    if not isinstance(source, dict) or not required.issubset(source):
        raise ValueError("season bridge source is incomplete")
    totals = source["totals"]
    if not isinstance(totals, dict) or set(totals) != {"home", "away"}:
        raise ValueError("season bridge venue totals are incomplete")
    block = {
        "source_url": source["source_url"],
        "captured_at": source["captured_at"],
        "raw_content_sha256": source["raw_content_sha256"],
        "competition": source["competition"],
        "season": source["season"],
    }
    for side, values in totals.items():
        if not isinstance(values, dict):
            raise ValueError("season bridge venue totals are incomplete")
        for field in ("team_id", "team_name", "sample_matches", "xg", "xga"):
            if field not in values:
                raise ValueError("season bridge venue totals are incomplete")
        if not isinstance(values["sample_matches"], int) or values["sample_matches"] <= 0:
            raise ValueError("season bridge venue samples must be positive")
        block[f"{side}_team_id"] = values["team_id"]
        block[f"{side}_team_name"] = values["team_name"]
        block[f"{side}_sample_matches"] = values["sample_matches"]
        block[f"{side}_attack_xg"] = str(values["xg"])
        block[f"{side}_defense_xga"] = str(values["xga"])
    return block


def _bridge_rate(block, side, field):
    return Decimal(block[f"{side}_{field}"]) / Decimal(block[f"{side}_sample_matches"])


def build_season_bridge_xg(current, prior, fixture, model_weight="0.40"):
    """Blend traceable current and prior same-league venue xG observations."""
    current_block = _season_source_block(current)
    prior_block = _season_source_block(prior)
    if current_block["competition"] != prior_block["competition"]:
        raise ValueError("season bridge requires the same competition")
    if (
        current_block["home_team_name"] != prior_block["home_team_name"]
        or current_block["away_team_name"] != prior_block["away_team_name"]
    ):
        raise ValueError("season bridge team identity changed between seasons")
    if min(current_block["home_sample_matches"], current_block["away_sample_matches"]) < 2:
        raise ValueError("season bridge current sample is below two matches")
    if min(prior_block["home_sample_matches"], prior_block["away_sample_matches"]) < 12:
        raise ValueError("season bridge prior sample is below twelve matches")
    if not isinstance(fixture, dict) or not fixture.get("home_team") or not fixture.get("away_team"):
        raise ValueError("season bridge fixture identity is incomplete")

    current_effective = Decimal(
        min(current_block["home_sample_matches"], current_block["away_sample_matches"])
    )
    prior_effective = Decimal(
        min(prior_block["home_sample_matches"], prior_block["away_sample_matches"])
    )
    current_weight = min(
        Decimal("0.70"),
        current_effective / (current_effective + prior_effective),
    )
    prior_weight = Decimal(1) - current_weight

    def blended(side, field):
        return (
            _bridge_rate(current_block, side, field) * current_weight
            + _bridge_rate(prior_block, side, field) * prior_weight
        )

    home_attack = blended("home", "attack_xg")
    home_defense = blended("home", "defense_xga")
    away_attack = blended("away", "attack_xg")
    away_defense = blended("away", "defense_xga")
    home_xg = (home_attack + away_defense) / Decimal(2)
    away_xg = (away_attack + home_defense) / Decimal(2)
    formula = (
        "season_bridge home_away_split: current_weight=min(0.70, "
        "min(current_home_sample_matches,current_away_sample_matches)/"
        "(min(current_home_sample_matches,current_away_sample_matches)+"
        "min(prior_home_sample_matches,prior_away_sample_matches))); "
        "prior_weight=1-current_weight; home_xg=mean(blended_home_home_xg,"
        "blended_away_away_xga); away_xg=mean(blended_away_away_xg,"
        "blended_home_home_xga)"
    )
    captured_at = max(current_block["captured_at"], prior_block["captured_at"])
    evidence = {
        "source_url": current_block["source_url"],
        "captured_at": captured_at,
        "published_at_unavailable_reason": "Understat league responses do not expose publication times",
        "competition": current_block["competition"],
        "season": current_block["season"],
        "home_sample_matches": current_block["home_sample_matches"],
        "away_sample_matches": current_block["away_sample_matches"],
        "scope": "home_away_split",
        "home_attack_xg": str(home_attack),
        "home_defense_xga": str(home_defense),
        "away_attack_xg": str(away_attack),
        "away_defense_xga": str(away_defense),
        "formula": formula,
        "current_season": current_block,
        "prior_season": prior_block,
        "current_weight": str(current_weight),
        "prior_weight": str(prior_weight),
    }
    return {
        "provider": "Understat season bridge",
        "home_team": current_block["home_team_name"],
        "away_team": current_block["away_team_name"],
        "captured_at": captured_at,
        "source_url": current_block["source_url"],
        "xg_inputs": {
            "source_url": current_block["source_url"],
            "home_xg": str(home_xg),
            "away_xg": str(away_xg),
            "model_version": "poisson-v1",
            "model_weight": str(Decimal(str(model_weight))),
            "confidence_ceiling": "medium",
            "evidence": evidence,
        },
        "stat_observations": [],
    }


def _collect_league_venue_source(opener, league, season, fixture, competition):
    league_page = f"{BASE_URL}/league/{league}"
    league_name = league_name_from_slug(league)
    data_url = f"{BASE_URL}/getLeagueData/{quote(league_name, safe='')}/{season}"
    response, _headers, raw_content_sha256 = _http_get(
        opener, data_url, referer=league_page, ajax=True
    )
    cutoff_date = _fixture_kickoff_date(fixture["kickoff_at"])
    totals = parse_league_venue_xg(
        json.loads(response), fixture["home_team"], fixture["away_team"], cutoff_date=cutoff_date
    )
    return {
        "source_url": data_url,
        "captured_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "raw_content_sha256": raw_content_sha256,
        "competition": competition,
        "season": f"{season}/{int(season) + 1}",
        "totals": totals,
    }


def collect_season_bridge_xg(league, season, fixture, model_weight="0.40"):
    """Collect current/prior Understat venue data without writing the ledger."""
    if not isinstance(season, str) or not season.isdecimal():
        raise ValueError("season bridge requires a numeric Understat season")
    if not isinstance(fixture, dict) or not all(
        isinstance(fixture.get(field), str) and fixture[field].strip()
        for field in ("competition", "home_team", "away_team", "kickoff_at")
    ):
        raise ValueError("season bridge fixture identity is incomplete")
    current_season = int(season)
    if current_season <= 0:
        raise ValueError("season bridge requires a positive Understat season")
    opener = build_opener(
        ProxyHandler(proxy_mapping(os.environ)),
        HTTPCookieProcessor(CookieJar()),
    )
    current = _collect_league_venue_source(
        opener, league, str(current_season), fixture, fixture["competition"]
    )
    prior = _collect_league_venue_source(
        opener, league, str(current_season - 1), fixture, fixture["competition"]
    )
    return build_season_bridge_xg(current, prior, fixture, model_weight=model_weight)


def _unavailable_fixture(fixture, reason):
    return {
        "match_id": fixture.get("match_id"),
        "home_team": fixture.get("home_team"),
        "away_team": fixture.get("away_team"),
        "status": "unavailable",
        "reason": reason,
    }


def collect_league_xg_batch(league, season, fixtures, model_weight="0.40", rolling_matches=None):
    """Collect one Understat league payload and derive venue-split xG per fixture."""
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("Understat batch fixtures must be a non-empty list")
    if rolling_matches is not None and (isinstance(rolling_matches, bool) or not isinstance(rolling_matches, int) or rolling_matches < 1):
        raise ValueError("rolling_matches must be a positive integer")
    league_page = f"{BASE_URL}/league/{league}"
    league_name = league_name_from_slug(league)
    data_url = f"{BASE_URL}/getLeagueData/{quote(league_name, safe='')}/{season}"
    opener = build_opener(
        ProxyHandler(proxy_mapping(os.environ)),
        HTTPCookieProcessor(CookieJar()),
    )
    response, _headers, raw_content_sha256 = _http_get(
        opener, data_url, referer=league_page, ajax=True
    )
    captured_at = datetime.now(timezone.utc).astimezone().isoformat()
    payload = json.loads(response)
    result = {
        "provider": "Understat",
        "source_url": data_url,
        "captured_at": captured_at,
        "published_at_unavailable_reason": "Understat league response does not expose a publication time",
        "competition": league_name,
        "season": f"{season}/{int(season) + 1}",
        "raw_content_sha256": raw_content_sha256,
        "fixtures": [],
    }
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            result["fixtures"].append(_unavailable_fixture(
                {}, "invalid_fixture_identity"
            ))
            continue
        home_team = fixture.get("home_team")
        away_team = fixture.get("away_team")
        if not isinstance(home_team, str) or not home_team.strip():
            home_resolution = resolve_anchored_team_name(payload, fixture.get("home_wikidata"))
            if home_resolution.get("status") != "resolved":
                result["fixtures"].append(_unavailable_fixture(fixture, home_resolution["reason"]))
                continue
            home_team = home_resolution["source_name"]
        if not isinstance(away_team, str) or not away_team.strip():
            away_resolution = resolve_anchored_team_name(payload, fixture.get("away_wikidata"))
            if away_resolution.get("status") != "resolved":
                result["fixtures"].append(_unavailable_fixture(fixture, away_resolution["reason"]))
                continue
            away_team = away_resolution["source_name"]
        try:
            cutoff_date = _fixture_kickoff_date(fixture.get("kickoff_at")) if rolling_matches is not None else None
        except ValueError as error:
            result["fixtures"].append(_unavailable_fixture(fixture, str(error)))
            continue
        try:
            totals = parse_league_venue_xg(
                payload,
                home_team,
                away_team,
                cutoff_date=cutoff_date,
                rolling_matches=rolling_matches,
            )
        except ValueError as error:
            reason = "understat_team_not_found" if "team was not found" in str(error) else "invalid_understat_response"
            result["fixtures"].append(_unavailable_fixture(fixture, reason))
            continue
        if totals["home"]["sample_matches"] <= 0:
            result["fixtures"].append(_unavailable_fixture(fixture, "missing_understat_home_sample"))
            continue
        if totals["away"]["sample_matches"] <= 0:
            result["fixtures"].append(_unavailable_fixture(fixture, "missing_understat_away_sample"))
            continue
        xg_inputs = build_venue_xg_inputs(
            totals,
            source_url=data_url,
            competition=league_name,
            season=result["season"],
            captured_at=captured_at,
            model_weight=model_weight,
        )
        result["fixtures"].append({
            "match_id": fixture.get("match_id"),
            "home_team": home_team,
            "away_team": away_team,
            "status": "available",
            "scope": "home_away_split",
            "source_url": data_url,
            "captured_at": captured_at,
            "raw_content_sha256": raw_content_sha256,
            **({"history_cutoff_date": cutoff_date.isoformat()} if cutoff_date is not None else {}),
            **totals,
            "xg_inputs": xg_inputs,
        })
    return result


def collect_league_xg(league, season, home_team, away_team):
    """Collect a public league table; no publication time is inferred."""
    league_page = f"{BASE_URL}/league/{league}"
    league_name = league_name_from_slug(league)
    data_url = f"{BASE_URL}/getLeagueData/{quote(league_name, safe='')}/{season}"
    opener = build_opener(
        ProxyHandler(proxy_mapping(os.environ)),
        HTTPCookieProcessor(CookieJar()),
    )
    _http_get(opener, league_page)
    response, _headers, raw_content_sha256 = _http_get(
        opener, data_url, referer=league_page, ajax=True
    )
    totals = parse_league_xg(json.loads(response), home_team, away_team)
    captured_at = datetime.now(timezone.utc).astimezone().isoformat()
    result = {
        "provider": "Understat",
        "source_url": data_url,
        "captured_at": captured_at,
        "published_at_unavailable_reason": "Understat league response does not expose a publication time",
        "competition": league_name,
        "season": f"{season}/{int(season) + 1}",
        "scope": "league_total",
        "raw_content_sha256": raw_content_sha256,
        **totals,
    }
    result["xg_inputs"] = build_xg_inputs(
        totals,
        source_url=data_url,
        competition=league_name,
        season=result["season"],
        captured_at=captured_at,
    )
    return result
