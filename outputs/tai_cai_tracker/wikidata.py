"""Public, read-only Wikidata team-identity lookup.

Wikidata is an identity anchor only. It never supplies match evidence, odds,
xG, kickoff time, or settlement data.
"""

import gzip
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .transport import coordinated_public_request, proxy_mapping


API_URL = "https://www.wikidata.org/w/api.php"
HTTP_TIMEOUT_SECONDS = 5
MIN_REQUEST_INTERVAL_SECONDS = 5
_QID = re.compile(r"Q[1-9][0-9]*")
_GENERIC_CHINESE_FOOTBALL_SUFFIXES = (
    "足球俱乐部", "足球俱樂部", "足球队", "足球隊",
)
_REQUEST_INTERVAL_LOCK = Lock()
_last_request_started_at = None


def _wait_for_request_slot():
    """Keep public Wikidata requests below a conservative sustained rate."""
    global _last_request_started_at
    with _REQUEST_INTERVAL_LOCK:
        now = time.monotonic()
        if _last_request_started_at is not None:
            delay = MIN_REQUEST_INTERVAL_SECONDS - (now - _last_request_started_at)
            if delay > 0:
                time.sleep(delay)
        _last_request_started_at = time.monotonic()


def _compact_name(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", value).casefold()


def _canonical_chinese_team_name(value):
    """Ignore only generic football-club suffixes visible in source labels."""
    compact = _compact_name(value)
    for suffix in _GENERIC_CHINESE_FOOTBALL_SUFFIXES:
        if compact.endswith(suffix):
            return compact[:-len(suffix)]
    return compact


def search_url(team_name):
    return API_URL + "?" + urlencode([
        ("action", "wbsearchentities"),
        ("search", team_name),
        ("language", "zh"),
        ("format", "json"),
        ("limit", "10"),
    ])


def entity_url(qid):
    return API_URL + "?" + urlencode([
        ("action", "wbgetentities"),
        ("ids", qid),
        ("props", "labels|aliases"),
        ("languages", "zh|en"),
        ("format", "json"),
    ])


def _http_get_json(url):
    def load():
        _wait_for_request_slot()
        request = Request(url, headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "TaiCaiTracker/1.0 (paper-only identity resolver)",
        })
        opener = build_opener(ProxyHandler(proxy_mapping(__import__("os").environ)))
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Wikidata response is not an object")
        return payload, hashlib.sha256(raw).hexdigest()

    return coordinated_public_request(url, load)


def _payload_and_hash(value):
    if isinstance(value, tuple) and len(value) == 2:
        payload, raw_hash = value
        if not isinstance(payload, dict):
            raise ValueError("Wikidata response is not an object")
        if not isinstance(raw_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_hash):
            raise ValueError("Wikidata response hash is invalid")
        return payload, raw_hash
    if not isinstance(value, dict):
        raise ValueError("Wikidata response is not an object")
    return value, None


def _exact_search_qids(payload, official_team):
    results = payload.get("search")
    if not isinstance(results, list):
        raise ValueError("Wikidata search response has no search list")
    expected = _canonical_chinese_team_name(official_team)
    candidates = []
    for result in results:
        if not isinstance(result, dict) or not _QID.fullmatch(str(result.get("id", ""))):
            continue
        names = [result.get("label")]
        aliases = result.get("aliases")
        if isinstance(aliases, list):
            names.extend(aliases)
        match = result.get("match")
        if isinstance(match, dict):
            names.append(match.get("text"))
        if any(_canonical_chinese_team_name(name) == expected for name in names):
            qid = result["id"]
            if qid not in {candidate[0] for candidate in candidates}:
                candidates.append((qid, result))
    qids = [candidate[0] for candidate in candidates]
    football_qids = [
        qid for qid, result in candidates
        if isinstance(result.get("description"), str)
        and re.search(r"足球|football\s+club|association\s+football", result["description"], re.IGNORECASE)
    ]
    if len(football_qids) == 1:
        return football_qids
    return qids


def _entity_names(entity):
    labels = entity.get("labels") if isinstance(entity, dict) else None
    aliases = entity.get("aliases") if isinstance(entity, dict) else None
    if not isinstance(labels, dict):
        return [], None, None
    chinese_names = []
    zh_label = labels.get("zh")
    if isinstance(zh_label, dict) and isinstance(zh_label.get("value"), str):
        chinese_names.append(zh_label["value"])
    zh_aliases = aliases.get("zh") if isinstance(aliases, dict) else None
    if isinstance(zh_aliases, list):
        chinese_names.extend(
            entry["value"] for entry in zh_aliases
            if isinstance(entry, dict) and isinstance(entry.get("value"), str)
        )
    en_label = labels.get("en")
    english_name = en_label.get("value") if isinstance(en_label, dict) else None
    return chinese_names, zh_label.get("value") if isinstance(zh_label, dict) else None, english_name


def _english_entity_names(entity, english_name):
    aliases = entity.get("aliases") if isinstance(entity, dict) else None
    english_aliases = aliases.get("en") if isinstance(aliases, dict) else None
    values = [english_name]
    if isinstance(english_aliases, list):
        values.extend(
            entry.get("value") for entry in english_aliases
            if isinstance(entry, dict)
        )
    names = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = _compact_name(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        names.append(value.strip())
    return names


def resolve_team_identity(official_team, fetch_json=None, captured_at=None):
    """Resolve one exact official Chinese team name to a reusable Wikidata QID.

    A match is accepted only when the public search has one exact candidate and
    the entity's Chinese label or alias repeats that exact official name.
    """
    if not isinstance(official_team, str) or not official_team.strip():
        raise ValueError("Wikidata identity requires an official team name")
    official_team = official_team.strip()
    fetch_json = _http_get_json if fetch_json is None else fetch_json
    if captured_at is None:
        captured_at = datetime.now(timezone.utc).astimezone().isoformat()
    try:
        captured = datetime.fromisoformat(captured_at)
    except (TypeError, ValueError) as error:
        raise ValueError("Wikidata identity captured_at must be ISO-8601") from error
    if captured.tzinfo is None:
        raise ValueError("Wikidata identity captured_at must include a timezone")

    searched_url = search_url(official_team)
    search_payload, search_hash = _payload_and_hash(fetch_json(searched_url))
    qids = _exact_search_qids(search_payload, official_team)
    if not qids:
        return {
            "status": "unresolved",
            "reason": "wikidata_exact_name_not_found",
            "search_url": searched_url,
        }
    if len(qids) != 1:
        return {
            "status": "unresolved",
            "reason": "wikidata_ambiguous_exact_name",
            "candidate_qids": qids,
            "search_url": searched_url,
        }

    qid = qids[0]
    source_url = entity_url(qid)
    entity_payload, entity_hash = _payload_and_hash(fetch_json(source_url))
    entity = entity_payload.get("entities", {}).get(qid)
    chinese_names, chinese_name, english_name = _entity_names(entity)
    if _canonical_chinese_team_name(official_team) not in {
        _canonical_chinese_team_name(name) for name in chinese_names
    }:
        return {
            "status": "unresolved",
            "reason": "wikidata_entity_name_mismatch",
            "qid": qid,
            "search_url": searched_url,
            "source_url": source_url,
        }
    if not isinstance(english_name, str) or not english_name.strip():
        return {
            "status": "unresolved",
            "reason": "wikidata_english_label_unavailable",
            "qid": qid,
            "search_url": searched_url,
            "source_url": source_url,
        }
    english_names = _english_entity_names(entity, english_name)
    result = {
        "status": "resolved",
        "official_team": official_team,
        "qid": qid,
        "chinese_name": chinese_name or official_team,
        "english_name": english_name.strip(),
        "english_names": english_names,
        "search_url": searched_url,
        "source_url": source_url,
        "captured_at": captured.isoformat(),
    }
    if search_hash is not None:
        result["search_raw_content_sha256"] = search_hash
    if entity_hash is not None:
        result["raw_content_sha256"] = entity_hash
    return result
