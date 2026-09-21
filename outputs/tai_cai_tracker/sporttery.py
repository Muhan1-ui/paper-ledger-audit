"""Public China Sports Lottery football data collection helpers."""

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from .transport import coordinated_public_request


CLIENT_CODE = "3001"
WEB_API_BASE = "https://webapi.sporttery.cn"
SCHEDULE_PAGE_URL = "https://m.sporttery.cn/mjc/zqsj/?tab=concern"
FIXED_BONUS_PAGE_TEMPLATE = "https://m.sporttery.cn/mjc/zqgdjjv1/?mid={match_id}"

_TZ_SUFFIX = "+08:00"
MAX_RESULT_PAGES = 20
_STATUS_NAMES = {
    "1": "待开售",
    "2": "已开售",
    "3": "暂停销售",
    "4": "进行中",
    "5": "进行中",
    "6": "进行中",
    "7": "进行中",
    "8": "进行中",
    "9": "进行中",
    "10": "赛果开奖",
    "11": "赛果开奖",
    "12": "无效场次",
    "13": "销售取消",
}

_SPF_MAP = {"h": "home_win", "d": "draw", "a": "away_win"}
_CRS_MAP = {
    "s01s00": "1:0",
    "s02s00": "2:0",
    "s02s01": "2:1",
    "s03s00": "3:0",
    "s03s01": "3:1",
    "s03s02": "3:2",
    "s04s00": "4:0",
    "s04s01": "4:1",
    "s04s02": "4:2",
    "s05s00": "5:0",
    "s05s01": "5:1",
    "s05s02": "5:2",
    "s-1sh": "H_OTHER",
    "s00s00": "0:0",
    "s01s01": "1:1",
    "s02s02": "2:2",
    "s03s03": "3:3",
    "s-1sd": "D_OTHER",
    "s00s01": "0:1",
    "s00s02": "0:2",
    "s01s02": "1:2",
    "s00s03": "0:3",
    "s01s03": "1:3",
    "s02s03": "2:3",
    "s00s04": "0:4",
    "s01s04": "1:4",
    "s02s04": "2:4",
    "s00s05": "0:5",
    "s01s05": "1:5",
    "s02s05": "2:5",
    "s-1sa": "A_OTHER",
}
_TTG_MAP = {
    "s0": "0",
    "s1": "1",
    "s2": "2",
    "s3": "3",
    "s4": "4",
    "s5": "5",
    "s6": "6",
    "s7": "7+",
}
_HAFU_MAP = {
    "hh": "HH",
    "hd": "HD",
    "ha": "HA",
    "dh": "DH",
    "dd": "DD",
    "da": "DA",
    "ah": "AH",
    "ad": "AD",
    "aa": "AA",
}


def _published_at(row):
    update_date = str(row.get("updateDate", "")).strip()
    update_time = str(row.get("updateTime", "")).strip()
    if not update_date or not update_time:
        return None
    return f"{update_date}T{update_time}{_TZ_SUFFIX}"


def _latest(rows):
    if not isinstance(rows, list) or not rows:
        return None
    valid_rows = [row for row in rows if isinstance(row, dict) and _published_at(row)]
    if not valid_rows:
        return None
    return max(valid_rows, key=_published_at)


def _prizes(row, mapping):
    fixed_prizes = {}
    for raw_key, option in mapping.items():
        value = str(row.get(raw_key, "")).strip()
        if value:
            fixed_prizes[option] = value
    return fixed_prizes


def _market_from_latest(rows, mapping, official_source_url, extra=None):
    row = _latest(rows)
    if row is None:
        return {"status": "skipped", "reason": "official market history is unavailable"}
    result = {
        "status": "frozen",
        "published_at": _published_at(row),
        "official_source_url": official_source_url,
        "fixed_prizes": _prizes(row, mapping),
    }
    if extra:
        result.update(extra(row))
    return result


def schedule_url(method="concern", **params):
    query = {"method": method, "clientCode": CLIENT_CODE}
    query.update({key: value for key, value in params.items() if value not in (None, "")})
    return f"{WEB_API_BASE}/gateway/uniform/fb/getMatchDataPageListV1.qry?{urlencode(query)}"


def fixed_bonus_url(match_id):
    query = urlencode({"clientCode": CLIENT_CODE, "matchId": str(match_id)})
    return f"{WEB_API_BASE}/gateway/uniform/football/getFixedBonusV1.qry?{query}"


class _JsonPayload(dict):
    def __init__(self, payload, raw_content_sha256):
        super().__init__(payload)
        self.raw_content_sha256 = raw_content_sha256


def fetch_json(url, referer):
    def load():
        request = Request(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "User-Agent": "Mozilla/5.0",
                "Referer": referer,
            },
        )
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=20) as response:
            raw = response.read()
        return _JsonPayload(json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest())

    return coordinated_public_request(url, load)


def parse_schedule_matches(payload):
    if str(payload.get("errorCode")) != "0":
        raise ValueError(f"sporttery schedule API error: {payload.get('errorMessage')}")
    groups = payload.get("value", {}).get("matchInfoList", [])
    matches = []
    for group in groups:
        for raw in group.get("subMatchList", []):
            match_id = raw.get("matchId")
            match_date = str(raw.get("matchDate", "")).strip()
            match_time = str(raw.get("matchTime", "")).strip()
            kickoff_at = f"{match_date}T{match_time}:00{_TZ_SUFFIX}" if match_date and match_time else None
            status = str(raw.get("matchStatus", "")).strip()
            match = {
                "match_id": match_id,
                "match_num": raw.get("matchNumStr"),
                "competition": raw.get("leagueAbbName") or raw.get("leagueAllName"),
                "home_team": raw.get("homeTeamAbbName") or raw.get("homeTeamAllName"),
                "away_team": raw.get("awayTeamAbbName") or raw.get("awayTeamAllName"),
                "kickoff_at": kickoff_at,
                "status": status,
                "status_name": raw.get("matchStatusName") or _STATUS_NAMES.get(status, ""),
                "official_source_url": FIXED_BONUS_PAGE_TEMPLATE.format(match_id=match_id),
            }
            prizes = {
                label: str(raw.get(source_key, "")).strip()
                for source_key, label in _SPF_MAP.items()
                if str(raw.get(source_key, "")).strip()
            }
            if prizes:
                match["spf_fixed_prizes"] = prizes
            matches.append(match)
    return matches


def _score(value):
    """Return an official score only when the result field is unambiguous."""
    if not isinstance(value, str) or value.count(":") != 1:
        return None
    home, away = (part.strip() for part in value.split(":"))
    if not home.isdigit() or not away.isdigit():
        return None
    return {"home": int(home), "away": int(away)}


def parse_result_matches(payload, official_result_url):
    """Extract completed official results without inferring absent score fields."""
    if str(payload.get("errorCode")) != "0":
        raise ValueError(f"sporttery result API error: {payload.get('errorMessage')}")
    groups = payload.get("value", {}).get("matchInfoList", [])
    matches = []
    for group in groups:
        for raw in group.get("subMatchList", []):
            status = str(raw.get("matchStatus", "")).strip()
            final_score = _score(raw.get("sectionsNo999"))
            if status != "11" or final_score is None:
                continue
            matches.append({
                "match_id": raw.get("matchId"),
                "match_num": raw.get("matchNumStr"),
                "match_date": str(raw.get("matchDate", "")).strip() or None,
                "match_time": str(raw.get("matchTime", "")).strip() or None,
                "competition": raw.get("leagueAbbName") or raw.get("leagueAllName"),
                "home_team": raw.get("homeTeamAbbName") or raw.get("homeTeamAllName"),
                "away_team": raw.get("awayTeamAbbName") or raw.get("awayTeamAllName"),
                "status": status,
                "status_name": raw.get("matchStatusName") or _STATUS_NAMES[status],
                "half_time_score": _score(raw.get("sectionsNo1")),
                "final_score": final_score,
                "official_result_url": official_result_url,
            })
    return matches


def extract_latest_fixed_bonus(payload, official_source_url):
    if str(payload.get("errorCode")) != "0":
        raise ValueError(f"sporttery fixed-bonus API error: {payload.get('errorMessage')}")
    history = payload.get("value", {}).get("oddsHistory", {})
    if not isinstance(history, dict):
        raise ValueError("sporttery fixed-bonus payload is missing oddsHistory")
    return {
        "spf": _market_from_latest(history.get("hadList"), _SPF_MAP, official_source_url),
        "hhad": _market_from_latest(
            history.get("hhadList"),
            _SPF_MAP,
            official_source_url,
            extra=lambda row: {"home_handicap": str(row.get("goalLine", "")).strip()},
        ),
        "crs": _market_from_latest(history.get("crsList"), _CRS_MAP, official_source_url),
        "ttg": _market_from_latest(history.get("ttgList"), _TTG_MAP, official_source_url),
        "hafu": _market_from_latest(history.get("hafuList"), _HAFU_MAP, official_source_url),
    }


def collect_schedule(method="concern", **params):
    payload = fetch_json(schedule_url(method, **params), SCHEDULE_PAGE_URL)
    return {
        "source_url": SCHEDULE_PAGE_URL,
        "api_url": schedule_url(method, **params),
        "method": method,
        "matches": parse_schedule_matches(payload),
    }


def collect_results(match_date=None, max_pages=MAX_RESULT_PAGES):
    """Collect every page indicated by the public official result endpoint."""
    if not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_RESULT_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_RESULT_PAGES}")
    page_no = 1
    visited_pages = set()
    matches = []
    page_evidence = []
    while True:
        if page_no in visited_pages:
            raise ValueError("sporttery result API repeated an official page number")
        if len(visited_pages) >= max_pages:
            raise ValueError("sporttery result API exceeded the configured official page limit")
        visited_pages.add(page_no)
        official_result_url = schedule_url(
            "result", matchDate=match_date, pageSize=100, pageNo=page_no
        )
        payload = fetch_json(official_result_url, SCHEDULE_PAGE_URL)
        matches.extend(parse_result_matches(payload, official_result_url))
        value = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(value, dict):
            raise ValueError("sporttery result API response has no value object")
        next_page = str(value.get("nextPage", "0")).strip()
        raw_content_sha256 = getattr(payload, "raw_content_sha256", None)
        page_evidence.append({
            "page_no": page_no,
            "official_result_url": official_result_url,
            **({"raw_content_sha256": raw_content_sha256} if isinstance(raw_content_sha256, str) else {}),
        })
        if next_page in {"", "0"}:
            break
        if not next_page.isdigit() or int(next_page) < 1:
            raise ValueError("sporttery result API returned an invalid nextPage value")
        page_no = int(next_page)
    collected = {
        "method": "result",
        "official_result_url": page_evidence[0]["official_result_url"],
        "matches": matches,
        "page_count": len(page_evidence),
        "page_evidence": page_evidence,
    }
    if len(page_evidence) == 1 and "raw_content_sha256" in page_evidence[0]:
        collected["raw_content_sha256"] = page_evidence[0]["raw_content_sha256"]
    collected["captured_at"] = datetime.now(timezone.utc).astimezone().isoformat()
    return collected


def collect_fixed_bonus(match_id):
    official_source_url = FIXED_BONUS_PAGE_TEMPLATE.format(match_id=match_id)
    payload = fetch_json(fixed_bonus_url(match_id), official_source_url)
    collected = {
        "match_id": int(match_id) if str(match_id).isdigit() else str(match_id),
        "official_source_url": official_source_url,
        "api_url": fixed_bonus_url(match_id),
        "official_markets": extract_latest_fixed_bonus(payload, official_source_url),
    }
    raw_content_sha256 = getattr(payload, "raw_content_sha256", None)
    if isinstance(raw_content_sha256, str):
        collected["raw_content_sha256"] = raw_content_sha256
        collected["captured_at"] = datetime.now(timezone.utc).astimezone().isoformat()
    return collected
