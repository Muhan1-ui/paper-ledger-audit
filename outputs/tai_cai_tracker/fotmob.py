"""Public FotMob team-stat extraction for paper-only xG evidence."""

import gzip
import hashlib
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from .transport import coordinated_public_request, proxy_mapping


REQUIRED_XG_STATS = ("expected_goals_team", "expected_goals_conceded_team")
FOTMOB_TEAM_REGISTRY_PATHS = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "source_aliases.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "source_link_registry.json"),
)
FOTMOB_SEARCH_URL = "https://www.fotmob.com/api/data/search/suggest?hits=50&lang=en&term={}"
FOTMOB_SEARCH_ALIASES = {
    "朴次茅斯": "Portsmouth",
    "德比郡": "Derby County",
    "林肯城": "Lincoln City",
    "布莱克本": "Blackburn Rovers",
    "普雷斯顿": "Preston North End",
    "布城": "Bristol City",
    "谢菲联": "Sheffield United",
    "博尔顿": "Bolton Wanderers",
    "伯明翰": "Birmingham City",
    "南安普敦": "Southampton",
    "斯托克城": "Stoke City",
    "诺维奇": "Norwich City",
    "都灵": "Torino",
    "蒙扎": "Monza",
    "卡利亚里": "Cagliari",
    "莱切": "Lecce",
    "赫塔费": "Getafe",
    "塞尔塔": "Celta Vigo",
    "马尔默": "Malmo FF",
    "索尔纳": "AIK",
    "卡尔马": "Kalmar FF",
    "佐加顿斯": "Djurgarden",
    "乌迪内斯": "Udinese",
    "Atlético Mineiro": "Atlético-MG",
    "Atletico Mineiro": "Atlético-MG",
    "拉齐奥": "Lazio",
    "埃斯托里": "Estoril",
    "阿罗卡": "Arouca",
    "埃尔切": "Elche",
    "皇家社会": "Real Sociedad",
    "维多利亚": "Vitoria",
    "格雷米奥": "Gremio",
    "蔚山现代": "Ulsan HD FC",
    "首尔FC": "FC Seoul",
    "雅典AEK": "AEK Athens",
    "LASK林茨": "LASK",
    "国际米兰": "Inter",
    "布鲁日": "Club Brugge",
    "维拉": "Aston Villa",
    "奈梅亨": "NEC Nijmegen",
    "SBV精英": "Excelsior",
    "斯旺西": "Swansea City",
    "桑德兰": "Sunderland",
    "赫尔城": "Hull City",
    "皇马": "Real Madrid",
    "多特蒙德": "Borussia Dortmund",
    "比利亚雷": "Villarreal",
    "里尔": "Lille",
    "贝蒂斯": "Real Betis",
    "波尔图": "Porto",
    "曼城": "Manchester City",
    "弗鲁米嫩": "Fluminense",
    "普拉滕斯": "Platense",
    "江原FC": "Gangwon FC",
    "全北现代": "Jeonbuk Hyundai Motors",
    "费耶诺德": "Feyenoord",
    "斯图加特": "VfB Stuttgart",
    "维京": "Viking",
    "特温特": "FC Twente",
    "特尔斯达": "Telstar",
    "里斯本": "Sporting CP",
    "加拉塔萨": "Galatasaray",
    "那不勒斯": "Napoli",
    "利物浦": "Liverpool",
    "马竞": "Atletico Madrid",
    "巴黎圣曼": "Paris Saint-Germain",
    "布拉迪斯": "Slovan Bratislava",
    "胡巴卡德": "Al Kholood",
    "吉达国民": "Al Ahli",
    "切尔西": "Chelsea",
    "利兹联": "Leeds United",
    "帕梅拉斯": "Palmeiras",
    "基多体大": "LDU de Quito",
    "拉普大学": "Universidad de Chile",
    "科林蒂安": "Corinthians",
}


class FotMobSearchResolutionError(ValueError):
    """A strict public-search failure with a stable, non-adopting reason code."""

    def __init__(self, failure_code, message):
        super().__init__(message)
        self.failure_code = failure_code


def _http_get(url, referer="https://www.fotmob.com/"):
    def load():
        request = Request(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                "User-Agent": "Mozilla/5.0",
                "Referer": referer,
            },
        )
        opener = build_opener(ProxyHandler(proxy_mapping(os.environ)))
        with opener.open(request, timeout=20) as response:
            raw = response.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            headers = dict(response.headers)
            headers["X-Tai-Cai-Raw-Content-SHA256"] = hashlib.sha256(raw).hexdigest()
            return raw.decode("utf-8"), headers

    return coordinated_public_request(url, load)


def _team_slug(name):
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", name.lower())) or "team"


def parse_team_search_results(payload):
    """Parse public FotMob team suggestions into candidate team-page URLs."""
    if isinstance(payload, dict):
        suggestions = payload.get("suggestions", [])
    elif isinstance(payload, list):
        suggestions = [
            suggestion
            for group in payload
            if isinstance(group, dict)
            for suggestion in group.get("suggestions", [])
        ]
    else:
        suggestions = []
    results = []
    seen_team_ids = set()
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        kind = str(suggestion.get("type") or suggestion.get("entityType") or "").lower()
        entity = suggestion.get("entity") if isinstance(suggestion.get("entity"), dict) else suggestion
        if kind != "team":
            continue
        team_id = entity.get("id")
        team_name = entity.get("name")
        if team_id is None or not isinstance(team_name, str) or not team_name.strip():
            continue
        try:
            team_id = int(team_id)
        except (TypeError, ValueError):
            continue
        if team_id in seen_team_ids:
            continue
        seen_team_ids.add(team_id)
        name = team_name.strip()
        results.append({
            "team_id": team_id,
            "team_name": name,
            "competition": entity.get("leagueName") or entity.get("competitionName"),
            "source_url": f"https://www.fotmob.com/teams/{team_id}/overview/{_team_slug(name)}",
            **(
                {"source_search_score": suggestion.get("score")}
                if isinstance(suggestion.get("score"), (int, float))
                and not isinstance(suggestion.get("score"), bool)
                else {}
            ),
        })
    return results


def collect_team_search_candidates(search_term):
    """Capture public FotMob search candidates without adopting an identity."""
    if not isinstance(search_term, str) or not search_term.strip():
        raise ValueError("FotMob team search requires a non-empty team name")
    search_term = search_term.strip()
    search_url = FOTMOB_SEARCH_URL.format(quote(search_term))
    response_text, headers = _http_get(search_url)
    raw_content_sha256 = headers.get("X-Tai-Cai-Raw-Content-SHA256")
    if not isinstance(raw_content_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_content_sha256):
        raise ValueError("FotMob team search response is missing its raw-content hash")
    return {
        "provider": "FotMob",
        "search_term": search_term,
        "search_url": search_url,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "raw_content_sha256": raw_content_sha256,
        "candidates": parse_team_search_results(json.loads(response_text)),
    }


def _registry_team_entry(team_name):
    """Return the curated Chinese→FotMob team page for a name, when one exists.

    FotMob's public search only indexes Latin team names: every Chinese name
    came back with zero candidates, so the FotMob xG tier was skipped for most
    fixtures and only Understat's six leagues ever produced xG. The local
    registry already holds the resolved page for 277 Chinese names, so consult
    it before spending a network round-trip that cannot succeed.
    """
    if not isinstance(team_name, str) or not team_name.strip():
        return None
    name = team_name.strip()
    keys = (name, name.lower())
    for path in FOTMOB_TEAM_REGISTRY_PATHS:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        teams = payload.get("aliases") if isinstance(payload.get("aliases"), dict) else payload.get("fotmob_teams")
        if not isinstance(teams, dict):
            continue
        for key in keys:
            entry = teams.get(key)
            if isinstance(entry, dict) and entry.get("source_url"):
                return dict(entry)
    return None


def discover_team_stats_url(team_name, minimum_score=0.90, tie_margin=0.04):
    """Resolve one public FotMob team page, refusing loose or tied name matches."""
    if not isinstance(team_name, str) or not team_name.strip():
        raise ValueError("FotMob team search requires a non-empty team name")
    registry_entry = _registry_team_entry(team_name)
    if registry_entry is not None:
        score = registry_entry.get("score")
        return {
            **registry_entry,
            "score": score if isinstance(score, (int, float)) and not isinstance(score, bool) else 1.0,
            "search_url": registry_entry.get("search_url"),
        }
    from .resolver import team_similarity

    search_name = FOTMOB_SEARCH_ALIASES.get(team_name.strip(), team_name.strip())
    search_url = FOTMOB_SEARCH_URL.format(quote(search_name))
    response_text, _headers = _http_get(search_url)
    candidates = parse_team_search_results(json.loads(response_text))
    scored = sorted(
        (
            (
                team_similarity(search_name, candidate["team_name"]),
                candidate.get("source_search_score"),
                candidate,
            )
            for candidate in candidates
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored or scored[0][0] < minimum_score:
        if not candidates:
            raise FotMobSearchResolutionError(
                "no_public_candidates", "FotMob public search returned no team candidates"
            )
        raise FotMobSearchResolutionError(
            "identity_mismatch", "no high-confidence FotMob team search result"
        )
    contenders = [item for item in scored if scored[0][0] - item[0] < tie_margin]
    if len(contenders) > 1:
        prioritized = sorted(
            contenders,
            key=lambda item: item[1] if isinstance(item[1], (int, float)) and not isinstance(item[1], bool) else -1,
            reverse=True,
        )
        first_priority = prioritized[0][1]
        second_priority = prioritized[1][1]
        if (
            not isinstance(first_priority, (int, float))
            or isinstance(first_priority, bool)
            or not isinstance(second_priority, (int, float))
            or isinstance(second_priority, bool)
            or first_priority - second_priority < 100
        ):
            raise FotMobSearchResolutionError(
                "ambiguous_identity", "ambiguous FotMob team search result"
            )
        score, _priority, candidate = prioritized[0]
    else:
        score, _priority, candidate = scored[0]
    return {**candidate, "score": score, "search_url": search_url}


def discover_match_team_stats_urls(home_team, away_team):
    """Resolve both teams' FotMob pages concurrently for one match."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        home_future = executor.submit(discover_team_stats_url, home_team)
        away_future = executor.submit(discover_team_stats_url, away_team)
        return {"home": home_future.result(), "away": away_future.result()}


def _next_data(page_html):
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        page_html,
        re.S,
    )
    if match is None:
        raise ValueError("FotMob __NEXT_DATA__ block was not found")
    return json.loads(html.unescape(match.group(1)))


def _team_payload(next_data):
    fallback = next_data.get("props", {}).get("pageProps", {}).get("fallback", {})
    for key, value in fallback.items():
        if key.startswith("team-") and isinstance(value, dict):
            return value
    raise ValueError("FotMob team payload was not found")


def _team_table_row(team):
    team_id = team.get("details", {}).get("id")
    for table_group in (team.get("table") or []):
        rows = table_group.get("data", {}).get("table", {}).get("all", [])
        for row in rows:
            if row.get("id") == team_id:
                return row
    return {}


def _competition(team):
    stats = team.get("stats", {})
    seasons = stats.get("tournamentSeasons", [])
    if seasons:
        return seasons[0].get("leagueName")
    for table_group in (team.get("table") or []):
        league_name = table_group.get("data", {}).get("leagueName")
        if league_name:
            return league_name
    return None


def parse_team_page(page_html, source_url):
    """Extract FotMob team identity, season, sample size, and stat endpoints."""
    next_data = _next_data(page_html)
    team = _team_payload(next_data)
    details = team.get("details", {})
    stats = team.get("stats", {})
    table_row = _team_table_row(team)
    stat_urls = {}
    page_stats = {}
    for row in stats.get("teams", []):
        stat_name = row.get("participant", {}).get("stat", {}).get("name")
        if not stat_name:
            continue
        stat_urls[stat_name] = row.get("fetchAllUrl")
        page_stats[stat_name] = {
            "header": row.get("header"),
            "value": str(row.get("participant", {}).get("value")),
            "rank": row.get("participant", {}).get("rank"),
        }
    return {
        "source_url": source_url,
        "team_id": details.get("id"),
        "team_name": details.get("name"),
        "country": details.get("country"),
        "competition": _competition(team),
        "season": details.get("latestSeason"),
        "primary_league_id": stats.get("primaryLeagueId"),
        "primary_season_id": stats.get("primarySeasonId"),
        "season_catalog": stats.get("tournamentSeasons", []),
        "sample_matches": table_row.get("played"),
        "table": {
            key: table_row.get(key)
            for key in ("played", "wins", "draws", "losses", "scoresStr", "pts", "idx")
            if key in table_row
        },
        "stat_urls": {
            stat_name: stat_urls[stat_name]
            for stat_name in REQUIRED_XG_STATS
            if stat_urls.get(stat_name)
        },
        "page_stats": page_stats,
    }


def _published_at(last_modified):
    if not last_modified:
        return None
    parsed = parsedate_to_datetime(last_modified)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_stat_payload(
    payload,
    team_id,
    stat_name,
    source_url,
    last_modified=None,
    raw_content_sha256=None,
):
    """Extract one team row from a FotMob stats JSON payload."""
    for top_list in payload.get("TopLists", []):
        if top_list.get("StatName") != stat_name:
            continue
        for row in top_list.get("StatList", []):
            if row.get("TeamId") == team_id:
                sample_matches = row.get("StatValueCount") or row.get("MatchesPlayed")
                result = {
                    "source_url": source_url,
                    "published_at": _published_at(last_modified),
                    "stat_name": stat_name,
                    "team_id": team_id,
                    "team_name": row.get("ParticipantName"),
                    "value": str(Decimal(str(row.get("StatValue")))),
                    "rank": row.get("Rank"),
                    "sample_matches": int(sample_matches) if sample_matches is not None else None,
                    "league_name": payload.get("LeagueName"),
                }
                if isinstance(raw_content_sha256, str):
                    result["raw_content_sha256"] = raw_content_sha256
                return result
    raise ValueError(f"FotMob stat {stat_name} was not found for team {team_id}")


FOTMOB_STAT_URL = "https://data.fotmob.com/stats/{league_id}/season/{season_id}/{stat}.json"
ROLLING_SEASON_LIMIT = 3
ROLLING_SAMPLE_TARGET = 12


def _rolling_stat_entry(team, contest, stat_name):
    """Fetch one team's stat row for a past contest, or None when unavailable."""
    league_id = contest.get("parentLeagueId")
    season_id = contest.get("tournamentId")
    if not league_id or not season_id:
        return None
    url = FOTMOB_STAT_URL.format(league_id=league_id, season_id=season_id, stat=stat_name)
    try:
        text, headers = _http_get(url, referer=team["source_url"])
        return parse_stat_payload(
            json.loads(text),
            team_id=team["team_id"],
            stat_name=stat_name,
            source_url=url,
            last_modified=headers.get("Last-Modified"),
        )
    except (OSError, ValueError, KeyError):
        return None


def _merge_stat_samples(base, extra):
    """Fold a second contest's totals into the first, keeping both windows visible."""
    merged = dict(base)
    merged["value"] = str(_decimal(base["value"]) + _decimal(extra["value"]))
    merged["sample_matches"] = (base.get("sample_matches") or 0) + (extra.get("sample_matches") or 0)
    windows = list(base.get("sample_windows") or ([base["league_name"]] if base.get("league_name") else []))
    windows.append(extra.get("league_name"))
    merged["sample_windows"] = [name for name in windows if name]
    merged["source_urls"] = list(base.get("source_urls") or [base.get("source_url")]) + [extra.get("source_url")]
    return merged


def collect_team_stats(source_url, rolling_seasons=ROLLING_SEASON_LIMIT, sample_target=ROLLING_SAMPLE_TARGET):
    """Fetch one FotMob team stats page and required public xG stat rows.

    A single contest often carries too few matches to clear the xG quality gate
    (a national team four games into a Nations League season, for example).
    When the primary season is short, fold the team's most recent prior contests
    into the same totals so the sample reflects recent form rather than one
    barely-started tournament.
    """
    page_html, _headers = _http_get(source_url, referer="https://www.fotmob.com/")
    team = parse_team_page(page_html, source_url)
    def collect_stat(stat_name, stat_url):
        stat_text, headers = _http_get(stat_url, referer=source_url)
        result = parse_stat_payload(
            json.loads(stat_text),
            team_id=team["team_id"],
            stat_name=stat_name,
            source_url=stat_url,
            last_modified=headers.get("Last-Modified"),
            raw_content_sha256=headers.get("X-Tai-Cai-Raw-Content-SHA256"),
        )
        result["captured_at"] = datetime.now(timezone.utc).astimezone().isoformat()
        return stat_name, result

    with ThreadPoolExecutor(max_workers=len(team["stat_urls"]) or 1) as executor:
        stats = dict(executor.map(
            lambda item: collect_stat(*item),
            team["stat_urls"].items(),
        ))
    past_contests = list(team.get("season_catalog") or [])[1:rolling_seasons]
    if any((row.get("sample_matches") or 0) < sample_target for row in stats.values()):
        for contest in past_contests:
            extra = {name: _rolling_stat_entry(team, contest, name) for name in stats}
            # Advance every stat together: a contest that only covers one of them
            # would leave attack and defence with different denominators, which
            # build_match_xg_evidence rejects.
            if any(row is None or not row.get("sample_matches") for row in extra.values()):
                continue
            # FotMob can report different StatValueCount coverage per stat within
            # one contest. Merging those would leave attack and defence on
            # different denominators, which build_match_xg_evidence rejects.
            if len({row["sample_matches"] for row in extra.values()}) != 1:
                continue
            stats = {name: _merge_stat_samples(stats[name], extra[name]) for name in stats}
            if all((row.get("sample_matches") or 0) >= sample_target for row in stats.values()):
                break
    team["stats"] = stats
    team["captured_at"] = datetime.now(timezone.utc).astimezone().isoformat()
    return team


def _decimal(value):
    return Decimal(str(value))


def _max_iso(timestamps):
    values = [datetime.fromisoformat(value) for value in timestamps if value]
    if not values:
        return None
    return max(values).isoformat()


def build_match_xg_evidence(home, away, model_weight="0.40"):
    """Build snapshot-compatible league-total xG model inputs from two FotMob teams."""
    if home["competition"] != away["competition"]:
        raise ValueError("FotMob teams must use the same competition")
    if home["season"] != away["season"]:
        raise ValueError("FotMob teams must use the same season")
    home_attack = home["stats"]["expected_goals_team"]
    home_defense = home["stats"]["expected_goals_conceded_team"]
    away_attack = away["stats"]["expected_goals_team"]
    away_defense = away["stats"]["expected_goals_conceded_team"]
    home_sample_counts = {home_attack["sample_matches"], home_defense["sample_matches"]}
    away_sample_counts = {away_attack["sample_matches"], away_defense["sample_matches"]}
    if len(home_sample_counts) != 1 or None in home_sample_counts:
        raise ValueError("FotMob home xG statistics must have one sample size")
    if len(away_sample_counts) != 1 or None in away_sample_counts:
        raise ValueError("FotMob away xG statistics must have one sample size")
    home_sample_matches = home_sample_counts.pop()
    away_sample_matches = away_sample_counts.pop()
    with localcontext() as context:
        context.prec = max(context.prec, 40)
        home_xg = (
            _decimal(home_attack["value"]) / Decimal(home_sample_matches)
            + _decimal(away_defense["value"]) / Decimal(away_sample_matches)
        ) / Decimal(2)
        away_xg = (
            _decimal(away_attack["value"]) / Decimal(away_sample_matches)
            + _decimal(home_defense["value"]) / Decimal(home_sample_matches)
        ) / Decimal(2)
    published_at = _max_iso(
        [
            home_attack.get("published_at"),
            home_defense.get("published_at"),
            away_attack.get("published_at"),
            away_defense.get("published_at"),
        ]
    )
    evidence_source_url = home.get("source_url")
    xg_inputs = {
        "source_url": evidence_source_url,
        "home_xg": str(+home_xg),
        "away_xg": str(+away_xg),
        "model_version": "poisson-v1",
        "model_weight": str(Decimal(str(model_weight))),
        "evidence": {
            "source_url": evidence_source_url,
            "published_at": published_at,
            "competition": home["competition"],
            "season": home["season"],
            "scope": "league_total",
            "home_attack_xg": home_attack["value"],
            "home_defense_xga": home_defense["value"],
            "away_attack_xg": away_attack["value"],
            "away_defense_xga": away_defense["value"],
            "formula": "league_total: home_xg=mean(home_attack_xg/home_sample_matches, away_defense_xga/away_sample_matches); away_xg=mean(away_attack_xg/away_sample_matches, home_defense_xga/home_sample_matches)",
        },
        "confidence_ceiling": "medium",
    }
    if home_sample_matches == away_sample_matches:
        xg_inputs["evidence"]["sample_matches"] = home_sample_matches
        xg_inputs["evidence"]["formula"] = (
            "league_total: home_xg=mean(home_attack_xg/sample_matches,"
            " away_defense_xga/sample_matches); "
            "away_xg=mean(away_attack_xg/sample_matches,"
            " home_defense_xga/sample_matches)"
        )
    else:
        xg_inputs["evidence"]["home_sample_matches"] = home_sample_matches
        xg_inputs["evidence"]["away_sample_matches"] = away_sample_matches
    observations = []
    for side, team in (("home", home), ("away", away)):
        for stat_name, stat in team["stats"].items():
            raw_content_sha256 = stat.get("raw_content_sha256")
            captured_at = stat.get("captured_at")
            if not isinstance(raw_content_sha256, str) or not isinstance(captured_at, str):
                continue
            observations.append({
                "source_url": stat["source_url"],
                "captured_at": captured_at,
                "published_at": stat.get("published_at"),
                "raw_content_sha256": raw_content_sha256,
                "team": team["team_name"],
                "side": side,
                "stat_name": stat_name,
                "value": stat["value"],
                "sample_matches": stat["sample_matches"],
            })
    result = {
        "provider": "FotMob",
        "home_team": home["team_name"],
        "away_team": away["team_name"],
        "xg_inputs": xg_inputs,
        "supporting_source_urls": {
            "home_team_page": home["source_url"],
            "away_team_page": away["source_url"],
            "expected_goals_team": home_attack["source_url"],
            "expected_goals_conceded_team": home_defense["source_url"],
        },
    }
    if observations:
        result["stat_observations"] = observations
    return result


def collect_match_xg_evidence(home_url, away_url, model_weight="0.40"):
    with ThreadPoolExecutor(max_workers=2) as executor:
        home_future = executor.submit(collect_team_stats, home_url)
        away_future = executor.submit(collect_team_stats, away_url)
        return build_match_xg_evidence(
            home_future.result(),
            away_future.result(),
            model_weight=model_weight,
        )


def _season_start(season):
    match = re.fullmatch(r"(\d{4})/\d{4}", str(season or ""))
    if match is None:
        raise ValueError("FotMob season is not a year range")
    return int(match.group(1))


def _prior_same_league_season(team):
    competition = team.get("competition")
    parent_league_id = team.get("primary_league_id")
    current_season_id = str(team.get("primary_season_id"))
    current_start = _season_start(team.get("season"))
    candidates = team.get("season_catalog", [])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if (
            candidate.get("leagueName") == competition
            and str(candidate.get("parentLeagueId")) == str(parent_league_id)
            and str(candidate.get("tournamentId")) != current_season_id
            and _season_start(candidate.get("season")) == current_start - 1
        ):
            return candidate
    raise ValueError("FotMob prior same-league season is unavailable")


def _collect_team_stats_for_season(team, season_ref):
    """Read both xG tables for one already verified team and league season."""
    if not isinstance(season_ref, dict):
        raise ValueError("FotMob season reference is invalid")
    league_id = season_ref.get("parentLeagueId")
    season_id = season_ref.get("tournamentId")
    if league_id is None or season_id is None:
        raise ValueError("FotMob season reference is incomplete")

    def collect_stat(stat_name):
        source_url = f"https://data.fotmob.com/stats/{league_id}/season/{season_id}/{stat_name}.json"
        stat_text, headers = _http_get(source_url, referer=team["source_url"])
        result = parse_stat_payload(
            json.loads(stat_text),
            team_id=team["team_id"],
            stat_name=stat_name,
            source_url=source_url,
            last_modified=headers.get("Last-Modified"),
            raw_content_sha256=headers.get("X-Tai-Cai-Raw-Content-SHA256"),
        )
        result["captured_at"] = datetime.now(timezone.utc).astimezone().isoformat()
        return stat_name, result

    with ThreadPoolExecutor(max_workers=2) as executor:
        stats = dict(executor.map(collect_stat, REQUIRED_XG_STATS))
    return {
        "team_id": team["team_id"],
        "team_name": team["team_name"],
        "competition": season_ref.get("leagueName"),
        "season": season_ref.get("season"),
        "captured_at": max(stat["captured_at"] for stat in stats.values()),
        "stats": stats,
    }


def _bridge_season_block(home, away):
    if home.get("competition") != away.get("competition") or home.get("season") != away.get("season"):
        raise ValueError("FotMob bridge teams must share one competition and season")
    block = {
        "competition": home["competition"],
        "season": home["season"],
        "captured_at": max(home["captured_at"], away["captured_at"]),
        "published_at": _max_iso([
            home["stats"]["expected_goals_team"].get("published_at"),
            home["stats"]["expected_goals_conceded_team"].get("published_at"),
            away["stats"]["expected_goals_team"].get("published_at"),
            away["stats"]["expected_goals_conceded_team"].get("published_at"),
        ]),
        "source_urls": {},
        "raw_content_sha256": {},
    }
    for side, team in (("home", home), ("away", away)):
        block[f"{side}_team_id"] = str(team["team_id"])
        block[f"{side}_team_name"] = team["team_name"]
        samples = set()
        for stat_name, short_name in (("expected_goals_team", "attack_xg"), ("expected_goals_conceded_team", "defense_xga")):
            stat = team["stats"].get(stat_name)
            if not isinstance(stat, dict) or stat.get("sample_matches") is None:
                raise ValueError("FotMob bridge xG stat is incomplete")
            samples.add(stat["sample_matches"])
            key = f"{side}_{stat_name}"
            block["source_urls"][key] = stat.get("source_url")
            block["raw_content_sha256"][key] = stat.get("raw_content_sha256")
            block[f"{side}_{short_name}"] = str(stat.get("value"))
        if len(samples) != 1 or not isinstance(next(iter(samples)), int):
            raise ValueError("FotMob bridge xG sample is inconsistent")
        block[f"{side}_sample_matches"] = samples.pop()
    return block


def build_fotmob_season_bridge_xg(current_home, current_away, prior_home, prior_away, model_weight="0.40"):
    """Blend traceable same-league FotMob league-total xG across two seasons."""
    current = _bridge_season_block(current_home, current_away)
    prior = _bridge_season_block(prior_home, prior_away)
    if current["competition"] != prior["competition"]:
        raise ValueError("FotMob bridge requires the same competition")
    if (
        current["home_team_id"] != prior["home_team_id"]
        or current["away_team_id"] != prior["away_team_id"]
        or current["home_team_name"] != prior["home_team_name"]
        or current["away_team_name"] != prior["away_team_name"]
    ):
        raise ValueError("FotMob bridge team identity changed between seasons")
    current_effective = Decimal(min(current["home_sample_matches"], current["away_sample_matches"]))
    prior_effective = Decimal(min(prior["home_sample_matches"], prior["away_sample_matches"]))
    if current_effective < 2:
        raise ValueError("FotMob bridge current sample is below two matches")
    if prior_effective < 12:
        raise ValueError("FotMob bridge prior sample is below twelve matches")
    current_weight = min(Decimal("0.70"), current_effective / (current_effective + prior_effective))
    prior_weight = Decimal(1) - current_weight

    def blended(side, field):
        return (
            _decimal(current[f"{side}_{field}"]) / Decimal(current[f"{side}_sample_matches"]) * current_weight
            + _decimal(prior[f"{side}_{field}"]) / Decimal(prior[f"{side}_sample_matches"]) * prior_weight
        )

    home_attack = blended("home", "attack_xg")
    home_defense = blended("home", "defense_xga")
    away_attack = blended("away", "attack_xg")
    away_defense = blended("away", "defense_xga")
    home_xg = (home_attack + away_defense) / Decimal(2)
    away_xg = (away_attack + home_defense) / Decimal(2)
    captured_at = max(current["captured_at"], prior["captured_at"])
    published_at = _max_iso([current["published_at"], prior["published_at"]])
    evidence = {
        "source_url": current["source_urls"]["home_expected_goals_team"],
        "published_at": published_at,
        "competition": current["competition"],
        "season": current["season"],
        "home_sample_matches": current["home_sample_matches"],
        "away_sample_matches": current["away_sample_matches"],
        "scope": "league_total",
        "home_attack_xg": str(home_attack),
        "home_defense_xga": str(home_defense),
        "away_attack_xg": str(away_attack),
        "away_defense_xga": str(away_defense),
        "formula": "league_total_season_bridge: current_weight=min(0.70, min(current_home_sample_matches,current_away_sample_matches)/(min(current_home_sample_matches,current_away_sample_matches)+min(prior_home_sample_matches,prior_away_sample_matches))); prior_weight=1-current_weight; home_xg=mean(blended_home_attack_xg,blended_away_defense_xga); away_xg=mean(blended_away_attack_xg,blended_home_defense_xga)",
        "current_season": current,
        "prior_season": prior,
        "current_weight": str(current_weight),
        "prior_weight": str(prior_weight),
    }
    return {
        "provider": "FotMob season bridge",
        "home_team": current["home_team_name"],
        "away_team": current["away_team_name"],
        "captured_at": captured_at,
        "source_url": evidence["source_url"],
        "xg_inputs": {
            "source_url": evidence["source_url"],
            "home_xg": str(home_xg),
            "away_xg": str(away_xg),
            "model_version": "poisson-v1",
            "model_weight": str(Decimal(str(model_weight))),
            "confidence_ceiling": "medium",
            "evidence": evidence,
        },
        "stat_observations": [],
    }


def collect_fotmob_season_bridge_xg(home_url, away_url, model_weight="0.40"):
    """Collect a same-league FotMob season bridge without writing the ledger."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        home_future = executor.submit(collect_team_stats, home_url)
        away_future = executor.submit(collect_team_stats, away_url)
        current_home = home_future.result()
        current_away = away_future.result()
    home_ref = _prior_same_league_season(current_home)
    away_ref = _prior_same_league_season(current_away)
    with ThreadPoolExecutor(max_workers=2) as executor:
        prior_home_future = executor.submit(_collect_team_stats_for_season, current_home, home_ref)
        prior_away_future = executor.submit(_collect_team_stats_for_season, current_away, away_ref)
        return build_fotmob_season_bridge_xg(
            current_home,
            current_away,
            prior_home_future.result(),
            prior_away_future.result(),
            model_weight=model_weight,
        )
