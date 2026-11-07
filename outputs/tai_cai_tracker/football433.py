"""Public 433 football European 1X2 collector for paper-only evidence."""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .transport import coordinated_public_request, proxy_mapping


PROVIDER_NAME = "433足球网"
COMPENSATE_URL = "https://www.433.cool/zqzhishu/compensate"
MATCH_LIST_URL = "https://www.433.cool/odds/GetOddsSimple?pageindex=1&lastupdatetime="
MAX_LIST_PAGES = 8
TEAM_ALIASES = {
    frozenset(("纽卡斯尔", "纽卡斯尔联")),
    frozenset(("西布罗姆", "西布朗")),
    frozenset(("赫塔费", "赫塔菲")),
    frozenset(("第戎", "迪卓恩")),
    frozenset(("圣埃蒂安", "圣伊天")),
    frozenset(("埃斯托里", "伊斯托里尔")),
    frozenset(("吉马良斯", "甘马雷斯")),
    frozenset(("巴萨", "巴塞罗那")),
    frozenset(("布莱克本", "布力般流浪")),
    frozenset(("布城", "布里斯托城")),
    frozenset(("谢菲联", "谢菲尔德联")),
    frozenset(("博尔顿", "保顿")),
    frozenset(("水户蜀葵", "水户霍利克")),
    frozenset(("山形山神", "山形蒙迪奥")),
    frozenset(("济州SK", "济州联队")),
    frozenset(("萨索洛", "森索罗")),
    frozenset(("弗洛西诺", "弗洛辛诺内")),
    frozenset(("卡尔马", "卡马亚")),
    frozenset(("埃斯托里", "伊斯托里尔")),
    frozenset(("阿罗卡", "阿鲁卡")),
    frozenset(("格雷米奥", "甘美奥RS")),
    frozenset(("雅典AEK", "AEK雅典")),
    frozenset(("奈梅亨", "NEC尼美根")),
    frozenset(("SBV精英", "SBV精英队")),
    frozenset(("皇马", "皇家马德里")),
    frozenset(("里尔", "利尔")),
    frozenset(("贝蒂斯", "皇家贝蒂斯")),
    frozenset(("曼城", "曼彻斯特城")),
    frozenset(("斯旺西", "史云斯")),
    frozenset(("费耶诺德", "飞燕诺")),
    frozenset(("特温特", "川迪")),
    frozenset(("特尔斯达", "特士达")),
    frozenset(("里斯本", "葡萄牙体育")),
    frozenset(("加拉塔萨", "加拉塔沙雷")),
    frozenset(("马竞", "马德里竞技")),
    frozenset(("巴黎圣曼", "巴黎圣日耳曼")),
    frozenset(("布拉迪斯", "斯洛云")),
    frozenset(("帕梅拉斯", "帕尔梅拉斯")),
    frozenset(("基多体大", "奎托")),
}


def _text(markup):
    value = " ".join(unescape(re.sub(r"<[^>]+>", " ", markup)).split())
    words = value.split()
    midpoint = len(words) // 2
    if len(words) % 2 == 0 and words[:midpoint] == words[midpoint:]:
        return " ".join(words[:midpoint])
    return value


def _required_match(pattern, page_html, label):
    match = re.search(pattern, page_html, re.IGNORECASE | re.DOTALL)
    if match is None:
        raise ValueError(f"433 match identity is missing {label}")
    value = _text(match.group(1))
    if not value:
        raise ValueError(f"433 match identity is missing {label}")
    return value


def parse_match_detail(page_html):
    """Parse visible named-bookmaker immediate 1X2 rows from a 433 match page."""
    if not isinstance(page_html, str) or not page_html.strip():
        raise ValueError("433 match detail response is empty")
    event = {
        "home_team": _required_match(
            r'<div[^>]*class=["\'][^"\']*homeTeamName[^"\']*["\'][^>]*>.*?<p[^>]*>(.*?)</p>',
            page_html,
            "home team",
        ),
        "away_team": _required_match(
            r'<div[^>]*class=["\'][^"\']*guestTeamName[^"\']*["\'][^>]*>.*?<p[^>]*>(.*?)</p>',
            page_html,
            "away team",
        ),
        "kickoff_local": _required_match(
            r'<p[^>]*class=["\'][^"\']*dataTime[^"\']*["\'][^>]*>(.*?)</p>',
            page_html,
            "kickoff time",
        ),
    }
    rows = []
    company = None
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page_html, re.IGNORECASE | re.DOTALL):
        cells = [_text(cell) for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, re.IGNORECASE | re.DOTALL)]
        if len(cells) < 2:
            continue
        if cells[1] == "初盘" and cells[0] not in {"", "公司"}:
            company = cells[0]
            continue
        if cells[0] != "即时" or company is None or len(cells) < 7:
            continue
        prices = cells[4:7]
        if all(re.fullmatch(r"\d+(?:\.\d+)?", price) for price in prices):
            rows.append({
                "bookmaker": company,
                "prices": {
                    "home_win": prices[0],
                    "draw": prices[1],
                    "away_win": prices[2],
                },
            })
    if not rows:
        raise ValueError("433 match detail has no complete named immediate 1X2 row")
    return {
        "event": event,
        "market": {
            "market_name": "1X2",
            "rows": rows,
        },
    }


def _http_get(url):
    def load():
        request = Request(url, headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"})
        opener = build_opener(ProxyHandler(proxy_mapping(os.environ)))
        with opener.open(request, timeout=20) as response:
            return response.read().decode("utf-8")

    return coordinated_public_request(url, load)


def _team_score(official, candidate):
    official = re.sub(r"[\s\-_.]", "", official).replace("足球俱乐部", "").replace("FC", "")
    candidate = re.sub(r"[\s\-_.]", "", candidate).replace("足球俱乐部", "").replace("FC", "")
    if frozenset((official, candidate)) in TEAM_ALIASES:
        return 1.0
    if official in candidate or candidate in official:
        return 1.0
    return SequenceMatcher(None, official, candidate).ratio()


def find_match_detail_url(matches, home_team, away_team, kickoff_at):
    """Resolve one 433 detail URL from public list rows using strict time and team checks."""
    kickoff = datetime.fromisoformat(kickoff_at)
    candidates = []
    for row in matches:
        if not isinstance(row, list) or len(row) < 23:
            continue
        try:
            source_time = datetime.fromtimestamp(int(row[1]) / 1000, timezone.utc).astimezone(kickoff.tzinfo)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if abs((source_time - kickoff).total_seconds()) > 60:
            continue
        home_score = _team_score(str(home_team), str(row[14]))
        away_score = _team_score(str(away_team), str(row[21]))
        if home_score >= 0.70 and away_score >= 0.70:
            candidates.append((home_score + away_score, row[0]))
    unique_candidates = {}
    for score, event_id in candidates:
        unique_candidates[event_id] = max(score, unique_candidates.get(event_id, 0.0))
    if len(unique_candidates) != 1:
        raise ValueError("433 match list has no uniquely verified team and kickoff match")
    event_id = next(iter(unique_candidates))
    return f"https://www.433.cool/zqzhishu/data/{int(event_id)}.html"


def event_matches_official_identity(event, home_team, away_team, kickoff_at):
    """Confirm a parsed 433 event against the official teams and kickoff."""
    if not isinstance(event, dict):
        return False
    try:
        kickoff = datetime.fromisoformat(kickoff_at)
        source_time = datetime.strptime(event["kickoff_local"], "%Y-%m-%d %H:%M")
    except (TypeError, ValueError, KeyError):
        return False
    source_time = source_time.replace(tzinfo=kickoff.tzinfo)
    return (
        abs((source_time - kickoff).total_seconds()) <= 60
        and _team_score(str(home_team), str(event.get("home_team", ""))) >= 0.70
        and _team_score(str(away_team), str(event.get("away_team", ""))) >= 0.70
    )


def _433_event_days(kickoff_at):
    """Return bounded official-day-first 433 list keys for one fixture."""
    try:
        kickoff = datetime.fromisoformat(kickoff_at)
    except (TypeError, ValueError) as error:
        raise ValueError("433 kickoff must be an ISO-8601 datetime") from error
    if kickoff.tzinfo is None:
        raise ValueError("433 kickoff must include a timezone")
    event_days = [kickoff.date()]
    if kickoff.hour < 12:
        event_days.append(kickoff.date() - timedelta(days=1))
    else:
        event_days.append(kickoff.date() + timedelta(days=1))
    return [event_day.isoformat() for event_day in event_days]


def _433_event_day(kickoff_at):
    """Return the preferred official-day 433 list key for one fixture."""
    return _433_event_days(kickoff_at)[0]


def _collect_match_list_page(page_index, search_date=None):
    """Read one bounded public 433 list page without attempting access-control evasion."""
    if type(page_index) is not int or not 1 <= page_index <= MAX_LIST_PAGES:
        raise ValueError(f"433 page index must be an integer from 1 through {MAX_LIST_PAGES}")
    if search_date is not None:
        if not isinstance(search_date, str):
            raise ValueError("433 search date must be an ISO-8601 date")
        try:
            datetime.strptime(search_date, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError("433 search date must be an ISO-8601 date") from error
    payload_items = [
        ("pageindex", str(page_index)),
        ("isOddsPriority", "false"),
        ("companyIDs", "4"),
        ("companyIDs", "2"),
        ("companyIDs", "8"),
        ("companyIDs", "14"),
    ]
    if search_date is not None:
        payload_items.append(("searchdate", search_date))
    payload = urlencode(payload_items).encode("ascii")
    source_url = re.sub(r"pageindex=\d+", f"pageindex={page_index}", MATCH_LIST_URL)

    def load():
        request = Request(
            source_url,
            data=payload,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": COMPENSATE_URL,
                "User-Agent": "Mozilla/5.0",
            },
        )
        opener = build_opener(ProxyHandler(proxy_mapping(os.environ)))
        with opener.open(request, timeout=20) as response:
            return response.read().decode("utf-8")

    raw = coordinated_public_request(source_url, load)
    response = json.loads(raw)
    if not isinstance(response, list) or len(response) < 6 or response[0] != 1:
        raise ValueError("433 match list returned an invalid response")
    data = response[5]
    if not isinstance(data, list) or len(data) < 3:
        raise ValueError("433 match list is incomplete")
    if not isinstance(data[2], list):
        raise ValueError("433 match list rows are incomplete")
    return data[2]


def discover_433_detail_url(home_team, away_team, kickoff_at, max_pages=3):
    """Resolve one 433 detail URL after a bounded, strict public list search."""
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_LIST_PAGES:
        raise ValueError(f"433 max_pages must be an integer from 1 through {MAX_LIST_PAGES}")
    last_error = None
    for search_date in _433_event_days(kickoff_at):
        matches = []
        for page_index in range(1, max_pages + 1):
            matches.extend(_collect_match_list_page(page_index, search_date))
        try:
            return find_match_detail_url(matches, home_team, away_team, kickoff_at)
        except ValueError as error:
            last_error = error
    raise last_error


def discover_433_detail_urls(fixtures, max_pages=3):
    """Resolve many official fixtures from one bounded 433 list-page collection."""
    if not isinstance(fixtures, list):
        raise ValueError("433 batch fixtures must be a list")
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_LIST_PAGES:
        raise ValueError(f"433 max_pages must be an integer from 1 through {MAX_LIST_PAGES}")
    resolved = {}
    unresolved = [fixture for fixture in fixtures if isinstance(fixture, dict) and fixture.get("match_id") is not None]
    errors = {}
    for attempt_index in range(2):
        fixtures_by_day = {}
        for fixture in unresolved:
            try:
                event_days = _433_event_days(fixture["kickoff_at"])
            except (KeyError, TypeError, ValueError) as error:
                errors[str(fixture["match_id"])] = str(error)
                continue
            if attempt_index < len(event_days):
                fixtures_by_day.setdefault(event_days[attempt_index], []).append(fixture)
        if not fixtures_by_day:
            break
        next_unresolved = []
        for event_day, day_fixtures in fixtures_by_day.items():
            matches = []
            for page_index in range(1, max_pages + 1):
                matches.extend(_collect_match_list_page(page_index, event_day))
            for fixture in day_fixtures:
                match_id = str(fixture["match_id"])
                try:
                    source_url = find_match_detail_url(
                        matches,
                        fixture["home_team"],
                        fixture["away_team"],
                        fixture["kickoff_at"],
                    )
                except (KeyError, TypeError, ValueError) as error:
                    errors[match_id] = str(error)
                    next_unresolved.append(fixture)
                    continue
                resolved[match_id] = {
                    "status": "resolved",
                    "source_url": source_url,
                }
        unresolved = next_unresolved
    for fixture in unresolved:
        match_id = str(fixture["match_id"])
        resolved[match_id] = {
            "status": "unavailable",
            "reason": errors.get(match_id, "433 match list has no uniquely verified team and kickoff match"),
        }
    return resolved


def collect_433(source_url):
    """Collect one public 433 detail page without writing the ledger."""
    if not isinstance(source_url, str) or not re.fullmatch(r"https://www\.433\.cool/zqzhishu/data/\d+\.html", source_url):
        raise ValueError("433 source URL must be a public https://www.433.cool/zqzhishu/data/<id>.html detail URL")
    raw = _http_get(source_url)
    parsed = parse_match_detail(raw)
    return {
        "provider": PROVIDER_NAME,
        "source_url": source_url,
        "captured_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "market_raw_content_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        **parsed,
    }
