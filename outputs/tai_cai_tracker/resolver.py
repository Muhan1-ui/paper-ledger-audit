"""Conservative link candidate resolver for paper-only match evidence."""

import json
import re
import threading
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .sporttery import collect_schedule


ALIASES = {
    "\u9996\u5c14": "seoul",
    "\u9996\u5c14fc": "seoul",
    "\u9996\u5c14\u8db3\u7403\u4ff1\u4e50\u90e8": "seoul",
    "\u851a\u5c71": "ulsan hd",
    "\u851a\u5c71hd": "ulsan hd",
    "\u851a\u5c71\u73b0\u4ee3": "ulsan hd",
    "\u851a\u5c71\u73b0\u4ee3\u8db3\u7403\u4ff1\u4e50\u90e8": "ulsan hd",
    "首尔": "seoul",
    "首尔fc": "seoul",
    "首尔足球俱乐部": "seoul",
    "蔚山": "ulsan hd",
    "蔚山hd": "ulsan hd",
    "蔚山现代": "ulsan hd",
    "蔚山现代足球俱乐部": "ulsan hd",
}

STOP_WORDS = {"fc", "football", "club"}
SPORTTERY_TEMPLATE_FIELDS = (
    "match_id",
    "match_num",
    "competition",
    "home_team",
    "away_team",
    "kickoff_at",
    "official_source_url",
)
SOURCE_DOMAINS = {
    "oddsportal": "oddsportal.com",
    "fotmob": "fotmob.com",
}
_433_DETAIL_URL = re.compile(r"https://www\.433\.cool/zqzhishu/data/\d+\.html")
_XGSCORE_EVENT_URL = re.compile(r"https://xgscore\.io/[a-z0-9-]+/[a-z0-9-]+/xgscore")
_FOTMOB_TEAM_PATH = re.compile(r"^/teams/(\d+)/(?:overview|stats)/[^/]+/?$")
_SOURCE_LINK_REGISTRY_LOCK = threading.RLock()


def _registry_key(value):
    return re.sub(r"\s+", "", str(value).casefold())


def _load_aliases(registry_path):
    if registry_path is None:
        return {}
    try:
        payload = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        aliases = payload["aliases"]
    except (OSError, TypeError, ValueError, KeyError):
        return {}
    if not isinstance(aliases, dict):
        return {}
    return {
        key: entry["source_name"]
        for key, entry in aliases.items()
        if isinstance(key, str)
        and isinstance(entry, dict)
        and isinstance(entry.get("source_name"), str)
        and entry["source_name"].strip()
        and entry.get("source_team_id") is not None
    }


def adopted_source_aliases(registry_path):
    """Return source-team names recorded from strictly adopted event matches."""
    return _load_aliases(registry_path)


def context_source_aliases(registry_path):
    """Return only aliases verified against SofaScore event identities."""
    try:
        payload = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        aliases = payload["aliases"]
    except (OSError, TypeError, ValueError, KeyError):
        return {}
    if not isinstance(aliases, dict):
        return {}
    return {
        key: entry["source_name"]
        for key, entry in aliases.items()
        if isinstance(key, str)
        and isinstance(entry, dict)
        and entry.get("source_provider") in (None, "SofaScore")
        and isinstance(entry.get("source_name"), str)
        and entry["source_name"].strip()
        and entry.get("source_team_id") is not None
    }


def understat_source_aliases(registry_path):
    """Return only aliases whose provider identity is explicitly Understat."""
    try:
        payload = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        aliases = payload["aliases"]
    except (OSError, TypeError, ValueError, KeyError):
        return {}
    if not isinstance(aliases, dict):
        return {}
    return {
        key: entry["source_name"]
        for key, entry in aliases.items()
        if isinstance(key, str)
        and isinstance(entry, dict)
        and entry.get("source_provider") == "Understat"
        and isinstance(entry.get("source_name"), str)
        and entry["source_name"].strip()
        and entry.get("source_team_id") is not None
    }


def reviewed_fotmob_search_aliases(registry_path):
    """Return manually reviewed FotMob search terms, never provider identities."""
    try:
        payload = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        aliases = payload["aliases"]
    except (OSError, TypeError, ValueError, KeyError):
        return {}
    if not isinstance(aliases, dict):
        return {}
    return {
        key: entry["search_name"]
        for key, entry in aliases.items()
        if isinstance(key, str)
        and isinstance(entry, dict)
        and entry.get("provider") == "FotMob"
        and isinstance(entry.get("search_name"), str)
        and entry["search_name"].strip()
    }


def _valid_sporttery_url(source_url):
    if not isinstance(source_url, str):
        return False
    parsed = urlsplit(source_url)
    hostname = parsed.hostname or ""
    return parsed.scheme == "https" and (
        hostname == "sporttery.cn" or hostname.endswith(".sporttery.cn")
    )


def _valid_reviewed_fotmob_search_url(source_url, search_name):
    if not _valid_fotmob_search_url(source_url):
        return False
    terms = parse_qs(urlsplit(source_url).query).get("term")
    return terms == [search_name]


def import_reviewed_fotmob_search_aliases(registry_path, input_path):
    """Import audited bilingual query terms without creating FotMob identities."""
    try:
        payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
        entries = payload["aliases"]
    except (OSError, TypeError, ValueError, KeyError) as error:
        raise ValueError("reviewed FotMob search-alias input is invalid") from error
    if not isinstance(entries, list) or not entries:
        raise ValueError("reviewed FotMob search-alias input requires aliases")

    updates = {}
    for entry in entries:
        required = {
            "official_team", "search_name", "official_source_url", "fotmob_search_url",
            "captured_at", "raw_content_sha256",
        }
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("reviewed FotMob search alias has unexpected fields")
        official_team = entry["official_team"]
        search_name = entry["search_name"]
        if not isinstance(official_team, str) or not official_team.strip():
            raise ValueError("reviewed FotMob search alias requires an official team")
        if not isinstance(search_name, str) or not search_name.strip():
            raise ValueError("reviewed FotMob search alias requires a search name")
        official_team = official_team.strip()
        search_name = search_name.strip()
        if not _valid_sporttery_url(entry["official_source_url"]):
            raise ValueError("reviewed FotMob search alias requires an official Sporttery URL")
        if not _valid_reviewed_fotmob_search_url(entry["fotmob_search_url"], search_name):
            raise ValueError("reviewed FotMob search alias requires its exact public FotMob search URL")
        try:
            captured_at = datetime.fromisoformat(entry["captured_at"])
        except ValueError as error:
            raise ValueError("reviewed FotMob search alias captured_at must be ISO-8601") from error
        if captured_at.tzinfo is None:
            raise ValueError("reviewed FotMob search alias captured_at must include a timezone")
        if not isinstance(entry["raw_content_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", entry["raw_content_sha256"]
        ):
            raise ValueError("reviewed FotMob search alias raw_content_sha256 is invalid")
        key = _registry_key(official_team)
        if key in updates:
            raise ValueError("reviewed FotMob search-alias input duplicates an official team")
        updates[key] = {
            "official_team": official_team,
            "search_name": search_name,
            "provider": "FotMob",
            "official_source_url": entry["official_source_url"],
            "fotmob_search_url": entry["fotmob_search_url"],
            "source_captured_at": entry["captured_at"],
            "raw_content_sha256": entry["raw_content_sha256"],
            "confirmation_method": "manual_bilingual_review",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }

    path = Path(registry_path)
    if path.exists():
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("reviewed FotMob search-alias registry is invalid") from error
    else:
        registry = {"version": 1, "aliases": {}}
    if not isinstance(registry, dict) or not isinstance(registry.get("aliases"), dict):
        raise ValueError("reviewed FotMob search-alias registry is invalid")
    for key, update in updates.items():
        existing = registry["aliases"].get(key)
        if existing and existing.get("search_name") != update["search_name"]:
            raise ValueError("reviewed FotMob search alias conflicts with an existing query keyword")
    registry["aliases"].update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return registry


def normalize_team_name(value, aliases=None):
    """Normalize team names across Chinese, English, and common suffixes."""
    if not isinstance(value, str):
        return ""
    compact = re.sub(r"\s+", " ", value.strip().lower())
    no_space = compact.replace(" ", "")
    if no_space in ALIASES:
        return ALIASES[no_space]
    source_name = aliases.get(_registry_key(value)) if isinstance(aliases, dict) else None
    if source_name is not None:
        return normalize_team_name(source_name)
    compact = compact.replace("足球俱乐部", "")
    tokens = [token for token in re.split(r"[^a-z0-9]+", compact) if token]
    tokens = [token for token in tokens if token not in STOP_WORDS]
    if tokens:
        return " ".join(tokens)
    return compact.replace(" ", "")


def team_similarity(left, right, aliases=None):
    """Return a transparent 0..1 string similarity after team-name normalization."""
    left_norm = normalize_team_name(left, aliases=aliases)
    right_norm = normalize_team_name(right, aliases=aliases)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    token_score = 0.0
    if left_tokens and right_tokens:
        token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence_score = SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(token_score, sequence_score)


def _date_score(left, right):
    if not left or not right:
        return 0.0
    try:
        left_dt = datetime.fromisoformat(left)
        right_dt = datetime.fromisoformat(right)
    except (TypeError, ValueError):
        return 0.0
    delta_seconds = abs((left_dt - right_dt).total_seconds())
    if delta_seconds <= 3600:
        return 1.0
    if delta_seconds <= 86400:
        return 0.5
    return 0.0


def _confidence(score):
    if score >= 0.86:
        return "high"
    if score >= 0.70:
        return "medium"
    return "low"


def _candidate_score(sporttery, candidate, aliases=None):
    home = team_similarity(
        sporttery.get("home_team"), candidate.get("home_team"), aliases=aliases
    )
    away = team_similarity(
        sporttery.get("away_team"), candidate.get("away_team"), aliases=aliases
    )
    date = _date_score(sporttery.get("kickoff_at"), candidate.get("kickoff_at"))
    score = (home * 0.45) + (away * 0.45) + (date * 0.10)
    return score, [
        f"home_similarity={home:.2f}",
        f"away_similarity={away:.2f}",
        f"date_score={date:.2f}",
    ]


def _candidate_source_error(candidate, source_kind):
    if source_kind is None:
        return None
    source_url = candidate.get("source_url") if isinstance(candidate, dict) else None
    if not isinstance(source_url, str):
        return "source_url_missing"
    parsed = urlsplit(source_url)
    expected_domain = SOURCE_DOMAINS[source_kind]
    hostname = parsed.hostname or ""
    if parsed.scheme != "https":
        return "source_url_not_https"
    if hostname != expected_domain and not hostname.endswith("." + expected_domain):
        return "source_url_domain_mismatch"
    if source_kind == "fotmob" and not parsed.path.startswith("/teams/"):
        return "source_url_path_mismatch"
    return None


def rank_match_candidates(
    sporttery,
    candidates,
    tie_margin=0.04,
    source_kind=None,
    aliases=None,
):
    """Rank candidate links and skip unsafe low-confidence or ambiguous matches."""
    scored = []
    rejections = []
    for candidate in candidates:
        error = _candidate_source_error(candidate, source_kind)
        if error is not None:
            rejections.append({"reason": error, "candidate": candidate})
            continue
        score, evidence = _candidate_score(sporttery, candidate, aliases=aliases)
        scored.append((score, evidence, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)

    if not scored:
        result = {"status": "skipped", "reason": "no candidates"}
        if rejections:
            result["reason"] = rejections[0]["reason"]
            result["rejections"] = rejections
        return result
    if len(scored) > 1 and scored[0][0] - scored[1][0] < tie_margin:
        return {
            "status": "skipped",
            "reason": "ambiguous candidates",
            "candidates": [item[2] for item in scored[:2]],
        }

    score, evidence, candidate = scored[0]
    confidence = _confidence(score)
    if confidence == "low":
        return {
            "status": "skipped",
            "reason": "low confidence",
            "score": score,
            "candidate": candidate,
            "evidence": evidence,
        }
    return {
        "status": "candidate",
        "confidence": confidence,
        "score": score,
        "source_url": candidate.get("source_url"),
        "evidence": evidence,
        "candidate": candidate,
        **({"rejections": rejections} if rejections else {}),
    }


def _context_team_anchor_names(official_name, names):
    """Return the official name plus deduplicated, already verified provider names."""
    anchors = []
    for name in [official_name, *(names or [])]:
        if not isinstance(name, str) or not name.strip():
            continue
        key = _registry_key(name)
        if key not in {_registry_key(anchor) for anchor in anchors}:
            anchors.append(name.strip())
    return anchors


def _best_context_team_similarity(official_name, names, candidate_name):
    anchors = _context_team_anchor_names(official_name, names)
    if not anchors:
        return 0.0, None
    score, anchor = max(
        (team_similarity(name, candidate_name), name)
        for name in anchors
    )
    return score, anchor


def rank_context_event_candidates(
    sporttery,
    candidates,
    minimum_score=0.90,
    minimum_margin=0.08,
    home_team_names=None,
    away_team_names=None,
):
    """Adopt one public event only when it is a clearly verified match."""
    scored = []
    for candidate in candidates:
        home, home_anchor = _best_context_team_similarity(
            sporttery.get("home_team"), home_team_names, candidate.get("home_team")
        )
        away, away_anchor = _best_context_team_similarity(
            sporttery.get("away_team"), away_team_names, candidate.get("away_team")
        )
        kickoff = _date_score(sporttery.get("kickoff_at"), candidate.get("kickoff_at"))
        competition = team_similarity(
            sporttery.get("competition"), candidate.get("competition")
        )
        score = (home * 0.40) + (away * 0.40) + (kickoff * 0.10) + (competition * 0.10)
        scored.append((score, {
            "home_similarity": f"{home:.2f}",
            "away_similarity": f"{away:.2f}",
            "kickoff_score": f"{kickoff:.2f}",
            "competition_similarity": f"{competition:.2f}",
            "home_identity_anchor": home_anchor,
            "away_identity_anchor": away_anchor,
        }, candidate))
    scored.sort(key=lambda entry: entry[0], reverse=True)
    if not scored:
        return {"status": "skipped", "reason": "no context-event candidates"}
    score, evidence, candidate = scored[0]
    margin = score - scored[1][0] if len(scored) > 1 else score
    if evidence["kickoff_score"] != "1.00" or score < minimum_score:
        return {"status": "skipped", "reason": "low confidence", "score": score, "candidate": candidate, "evidence": evidence}
    if margin < minimum_margin:
        return {"status": "skipped", "reason": "ambiguous candidates", "score": score, "margin": margin, "candidates": [entry[2] for entry in scored[:2]]}
    if not candidate.get("source_url") or candidate.get("event_id") is None:
        return {"status": "skipped", "reason": "candidate lacks direct event evidence", "score": score, "candidate": candidate, "evidence": evidence}
    return {"status": "adopted", "score": score, "margin": margin, "source_url": candidate["source_url"], "candidate": candidate, "evidence": evidence}


def record_adopted_aliases(registry_path, sporttery, adopted):
    """Persist source aliases only from a uniquely adopted event candidate."""
    path = Path(registry_path)
    if path.exists():
        registry = json.loads(path.read_text(encoding="utf-8"))
    else:
        registry = {"version": 1, "aliases": {}}
    if adopted.get("status") != "adopted":
        return registry
    candidate = adopted.get("candidate", {})
    pairs = (
        (sporttery.get("home_team"), candidate.get("home_team"), candidate.get("home_team_id")),
        (sporttery.get("away_team"), candidate.get("away_team"), candidate.get("away_team_id")),
    )
    updates = {}
    for sporttery_name, source_name, source_team_id in pairs:
        if not isinstance(sporttery_name, str) or not isinstance(source_name, str) or source_team_id is None:
            return registry
        registry_key = re.sub(r"\s+", "", sporttery_name.casefold())
        existing = registry["aliases"].get(registry_key)
        if existing and existing.get("source_team_id") != source_team_id:
            return registry
        updates[registry_key] = {
            "source_name": source_name,
            "source_team_id": source_team_id,
            "source_url": adopted["source_url"],
            "score": adopted["score"],
            "first_seen_at": datetime.now(timezone.utc).isoformat(),
        }
    registry["aliases"].update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return registry


def import_manual_understat_aliases(registry_path, input_payload):
    """Persist manually bilingual-reviewed Understat names with source evidence."""
    if isinstance(input_payload, (str, Path)):
        with Path(input_payload).open("r", encoding="utf-8") as input_file:
            input_payload = json.load(input_file)
    required_payload_fields = {"provider", "confirmation_method", "entries"}
    if (
        not isinstance(input_payload, dict)
        or set(input_payload) != required_payload_fields
        or input_payload.get("provider") != "Understat"
        or input_payload.get("confirmation_method") != "manual_bilingual_review"
        or not isinstance(input_payload.get("entries"), list)
        or not input_payload["entries"]
    ):
        raise ValueError("manual Understat alias import has an invalid payload")

    required_entry_fields = {
        "official_team", "official_source_url", "source_team", "source_team_id",
        "source_url", "captured_at", "raw_content_sha256",
    }
    updates = {}
    for entry in input_payload["entries"]:
        if not isinstance(entry, dict) or set(entry) != required_entry_fields:
            raise ValueError("manual Understat alias entry has invalid fields")
        if any(
            not isinstance(entry[field], str) or not entry[field].strip()
            for field in ("official_team", "official_source_url", "source_team", "source_url", "captured_at", "raw_content_sha256")
        ):
            raise ValueError("manual Understat alias entry has empty fields")
        if isinstance(entry["source_team_id"], bool) or entry["source_team_id"] in (None, ""):
            raise ValueError("manual Understat alias source_team_id is required")
        official_url = urlsplit(entry["official_source_url"])
        if official_url.scheme != "https" or not (official_url.hostname or "").endswith("sporttery.cn"):
            raise ValueError("manual Understat alias official_source_url must be a China Sports Lottery URL")
        source_url = urlsplit(entry["source_url"])
        if (
            source_url.scheme != "https"
            or source_url.hostname != "understat.com"
            or not source_url.path.startswith("/getLeagueData/")
        ):
            raise ValueError("manual Understat source_url must be an Understat league-data URL")
        try:
            captured_at = datetime.fromisoformat(entry["captured_at"])
        except ValueError as error:
            raise ValueError("manual Understat alias captured_at must be ISO-8601") from error
        if captured_at.tzinfo is None:
            raise ValueError("manual Understat alias captured_at must include a timezone")
        if not re.fullmatch(r"[0-9a-f]{64}", entry["raw_content_sha256"]):
            raise ValueError("manual Understat alias raw_content_sha256 is invalid")
        key = _registry_key(entry["official_team"])
        if key in updates:
            raise ValueError("manual Understat alias input duplicates an official team")
        updates[key] = {
            "source_name": entry["source_team"].strip(),
            "source_team_id": str(entry["source_team_id"]),
            "source_provider": "Understat",
            "source_url": entry["source_url"],
            "source_captured_at": entry["captured_at"],
            "raw_content_sha256": entry["raw_content_sha256"],
            "official_source_url": entry["official_source_url"],
            "confirmation_method": "manual_bilingual_review",
            "first_seen_at": datetime.now(timezone.utc).isoformat(),
        }

    path = Path(registry_path)
    if path.exists():
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("source alias registry is invalid") from error
    else:
        registry = {"version": 1, "aliases": {}}
    if not isinstance(registry, dict) or not isinstance(registry.get("aliases"), dict):
        raise ValueError("source alias registry is invalid")
    for key, update in updates.items():
        existing = registry["aliases"].get(key)
        if existing and (
            existing.get("source_provider") == "Understat"
            and (
                str(existing.get("source_team_id")) != update["source_team_id"]
                or existing.get("source_name") != update["source_name"]
            )
        ):
            raise ValueError("manual Understat alias conflicts with an existing provider identity")
    registry["aliases"].update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return registry


def _load_source_link_registry(path):
    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        registry = {"version": 1, "matches": {}, "fotmob_teams": {}, "wikidata_teams": {}}
    if not isinstance(registry, dict):
        registry = {"version": 1, "matches": {}, "fotmob_teams": {}, "wikidata_teams": {}}
    registry.setdefault("version", 1)
    if not isinstance(registry.get("matches"), dict):
        registry["matches"] = {}
    if not isinstance(registry.get("fotmob_teams"), dict):
        registry["fotmob_teams"] = {}
    if not isinstance(registry.get("wikidata_teams"), dict):
        registry["wikidata_teams"] = {}
    return registry


def _valid_433_detail_url(source_url):
    return isinstance(source_url, str) and _433_DETAIL_URL.fullmatch(source_url) is not None


def _valid_xgscore_event_url(source_url):
    return isinstance(source_url, str) and _XGSCORE_EVENT_URL.fullmatch(source_url) is not None


def _valid_fotmob_team_url(source_url):
    if not isinstance(source_url, str):
        return False
    parsed = urlsplit(source_url)
    hostname = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and (hostname == "fotmob.com" or hostname.endswith(".fotmob.com"))
        and parsed.path.startswith("/teams/")
    )


def _fotmob_team_id_from_url(source_url):
    if not _valid_fotmob_team_url(source_url):
        return None
    match = _FOTMOB_TEAM_PATH.fullmatch(urlsplit(source_url).path)
    return match.group(1) if match is not None else None


def _valid_fotmob_search_url(source_url):
    if not isinstance(source_url, str):
        return False
    parsed = urlsplit(source_url)
    hostname = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and (hostname == "fotmob.com" or hostname.endswith(".fotmob.com"))
        and parsed.path == "/api/data/search/suggest"
        and bool(parsed.query)
    )


def _validated_fotmob_team_identity(official_team, candidate):
    if not isinstance(official_team, str) or not official_team.strip():
        raise ValueError("FotMob identity requires an official team name")
    if not isinstance(candidate, dict):
        raise ValueError("FotMob identity candidate must be an object")
    source_team_id = candidate.get("team_id")
    if isinstance(source_team_id, bool) or source_team_id is None:
        raise ValueError("FotMob identity candidate requires a team ID")
    source_team_id = str(source_team_id)
    if not source_team_id.isdigit() or int(source_team_id) <= 0:
        raise ValueError("FotMob identity candidate has an invalid team ID")
    source_name = candidate.get("team_name")
    if not isinstance(source_name, str) or not source_name.strip():
        raise ValueError("FotMob identity candidate requires a source team name")
    source_url = candidate.get("source_url")
    if _fotmob_team_id_from_url(source_url) != source_team_id:
        raise ValueError("FotMob identity URL team ID does not match the candidate team ID")
    search_url = candidate.get("search_url")
    if not _valid_fotmob_search_url(search_url):
        raise ValueError("FotMob identity candidate requires a public search URL")
    score = candidate.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.90 <= score <= 1.0:
        raise ValueError("FotMob identity candidate requires a strict match score")
    return {
        "team_name": official_team.strip(),
        "source_team_id": source_team_id,
        "source_name": source_name.strip(),
        "source_url": source_url,
        "search_url": search_url,
        "score": score,
    }


def record_fotmob_team_identity(registry_path, official_team, candidate):
    """Atomically retain a strict official-team to FotMob-team identity mapping."""
    identity = _validated_fotmob_team_identity(official_team, candidate)
    path = Path(registry_path)
    key = _registry_key(identity["team_name"])
    with _SOURCE_LINK_REGISTRY_LOCK:
        registry = _load_source_link_registry(path)
        existing = registry["fotmob_teams"].get(key)
        if isinstance(existing, dict):
            existing_team_id = existing.get("source_team_id") or _fotmob_team_id_from_url(
                existing.get("source_url")
            )
            if existing_team_id is not None and str(existing_team_id) != identity["source_team_id"]:
                raise ValueError("FotMob identity conflicts with the cached provider team ID")
        registry["fotmob_teams"][key] = {
            **identity,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(registry, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
        return registry


def cached_fotmob_team_identity(registry_path, official_team):
    """Return a complete FotMob team identity only when every stored field revalidates."""
    if not isinstance(official_team, str) or not official_team.strip():
        raise ValueError("FotMob identity requires an official team name")
    registry = _load_source_link_registry(registry_path)
    entry = registry["fotmob_teams"].get(_registry_key(official_team))
    if not isinstance(entry, dict):
        return None
    try:
        return _validated_fotmob_team_identity(official_team, {
            "team_id": entry.get("source_team_id"),
            "team_name": entry.get("source_name"),
            "source_url": entry.get("source_url"),
            "search_url": entry.get("search_url"),
            "score": entry.get("score"),
        })
    except ValueError:
        return None


def _valid_wikidata_api_url(source_url, action):
    if not isinstance(source_url, str):
        return False
    parsed = urlsplit(source_url)
    query = parse_qs(parsed.query)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.wikidata.org"
        and parsed.path == "/w/api.php"
        and query.get("action") == [action]
        and query.get("format") == ["json"]
    )


def _validated_wikidata_team_identity(identity):
    if not isinstance(identity, dict) or identity.get("status") != "resolved":
        raise ValueError("Wikidata identity must be a resolved object")
    required_text = ("official_team", "qid", "chinese_name", "english_name", "search_url", "source_url", "captured_at")
    if any(not isinstance(identity.get(field), str) or not identity[field].strip() for field in required_text):
        raise ValueError("Wikidata identity has an empty required field")
    if not re.fullmatch(r"Q[1-9][0-9]*", identity["qid"]):
        raise ValueError("Wikidata identity QID is invalid")
    if not _valid_wikidata_api_url(identity["search_url"], "wbsearchentities"):
        raise ValueError("Wikidata identity search URL is invalid")
    if not _valid_wikidata_api_url(identity["source_url"], "wbgetentities"):
        raise ValueError("Wikidata identity source URL is invalid")
    source_query = parse_qs(urlsplit(identity["source_url"]).query)
    if source_query.get("ids") != [identity["qid"]]:
        raise ValueError("Wikidata identity source URL QID does not match")
    try:
        captured_at = datetime.fromisoformat(identity["captured_at"])
    except ValueError as error:
        raise ValueError("Wikidata identity captured_at must be ISO-8601") from error
    if captured_at.tzinfo is None:
        raise ValueError("Wikidata identity captured_at must include a timezone")
    validated = {field: identity[field].strip() for field in required_text}
    english_names = identity.get("english_names")
    if english_names is not None:
        if not isinstance(english_names, list) or not english_names:
            raise ValueError("Wikidata identity English names are invalid")
        normalized_names = []
        seen_names = set()
        for name in english_names:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Wikidata identity English names are invalid")
            compact = _registry_key(name)
            if compact in seen_names:
                raise ValueError("Wikidata identity English names are duplicated")
            seen_names.add(compact)
            normalized_names.append(name.strip())
        if normalized_names[0] != validated["english_name"]:
            raise ValueError("Wikidata identity English names must begin with the English label")
        validated["english_names"] = normalized_names
    for field in ("search_raw_content_sha256", "raw_content_sha256"):
        value = identity.get(field)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"Wikidata identity {field} is invalid")
            validated[field] = value
    return validated


def record_wikidata_team_identity(registry_path, identity):
    """Atomically retain a strict official-team to Wikidata identity anchor."""
    validated = _validated_wikidata_team_identity(identity)
    path = Path(registry_path)
    key = _registry_key(validated["official_team"])
    with _SOURCE_LINK_REGISTRY_LOCK:
        registry = _load_source_link_registry(path)
        existing = registry["wikidata_teams"].get(key)
        if isinstance(existing, dict) and existing.get("qid") != validated["qid"]:
            raise ValueError("Wikidata identity conflicts with the cached QID")
        registry["wikidata_teams"][key] = {
            **validated,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(registry, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
        return registry


def cached_wikidata_team_identity(registry_path, official_team):
    """Return a revalidated Wikidata anchor for an official team name."""
    if not isinstance(official_team, str) or not official_team.strip():
        raise ValueError("Wikidata identity requires an official team name")
    registry = _load_source_link_registry(registry_path)
    entry = registry["wikidata_teams"].get(_registry_key(official_team))
    if not isinstance(entry, dict):
        return None
    try:
        return _validated_wikidata_team_identity({"status": "resolved", **entry})
    except ValueError:
        return None


def _source_link_match_identity(match):
    required = ("match_id", "home_team", "away_team", "kickoff_at")
    if not isinstance(match, dict) or any(not isinstance(match.get(field), (str, int)) or not str(match[field]).strip() for field in required):
        raise ValueError("source link registry requires match_id, home_team, away_team, and kickoff_at")
    try:
        datetime.fromisoformat(str(match["kickoff_at"]))
    except ValueError as error:
        raise ValueError("source link registry kickoff_at must be ISO 8601") from error
    return {
        "match_id": str(match["match_id"]),
        "home_team": str(match["home_team"]).strip(),
        "away_team": str(match["away_team"]).strip(),
        "kickoff_at": str(match["kickoff_at"]),
    }


def record_source_links(registry_path, match, links):
    """Atomically retain verified source URLs without storing mutable market data."""
    identity = _source_link_match_identity(match)
    if not isinstance(links, dict):
        raise ValueError("source links must be an object")
    path = Path(registry_path)
    with _SOURCE_LINK_REGISTRY_LOCK:
        registry = _load_source_link_registry(path)
        now = datetime.now(timezone.utc).isoformat()
        odds_url = links.get("european_433_url")
        xgscore_url = links.get("xgscore_url")
        if odds_url is not None and not _valid_433_detail_url(odds_url):
            raise ValueError("european_433_url must be a 433 public detail URL")
        if xgscore_url is not None and not _valid_xgscore_event_url(xgscore_url):
            raise ValueError("xgscore_url must be a public xGscore event URL")
        if odds_url is not None or xgscore_url is not None:
            existing = registry["matches"].get(identity["match_id"])
            existing_identity = {
                field: str(existing.get(field, ""))
                for field in ("match_id", "home_team", "away_team", "kickoff_at")
            } if isinstance(existing, dict) else None
            if existing is None:
                existing = {**identity}
                registry["matches"][identity["match_id"]] = existing
            elif existing_identity != identity:
                existing = None
            if existing is not None:
                if odds_url is not None:
                    existing["european_433_url"] = odds_url
                if xgscore_url is not None:
                    existing["xgscore_url"] = xgscore_url
                existing["verified_at"] = now
        for side, team_field in (("home", "home_team"), ("away", "away_team")):
            source_url = links.get(f"{side}_fotmob_url")
            if source_url is None:
                continue
            if not _valid_fotmob_team_url(source_url):
                raise ValueError(f"{side}_fotmob_url must be a FotMob public team URL")
            key = _registry_key(identity[team_field])
            existing = registry["fotmob_teams"].get(key)
            if existing is None:
                registry["fotmob_teams"][key] = {
                    "team_name": identity[team_field],
                    "source_url": source_url,
                    "verified_at": now,
                }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(registry, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
        return registry


def cached_source_links(registry_path, match):
    """Return only URLs whose stored identity is still valid for this collection run."""
    identity = _source_link_match_identity(match)
    registry = _load_source_link_registry(registry_path)
    cached = {}
    event = registry["matches"].get(identity["match_id"])
    if isinstance(event, dict) and all(event.get(field) == value for field, value in identity.items()):
        source_url = event.get("european_433_url")
        if _valid_433_detail_url(source_url):
            cached["european_433_url"] = source_url
        source_url = event.get("xgscore_url")
        if _valid_xgscore_event_url(source_url):
            cached["xgscore_url"] = source_url
    for side, team_field in (("home", "home_team"), ("away", "away_team")):
        entry = registry["fotmob_teams"].get(_registry_key(identity[team_field]))
        source_url = entry.get("source_url") if isinstance(entry, dict) else None
        if _valid_fotmob_team_url(source_url):
            cached[f"{side}_fotmob_url"] = source_url
    return cached


def candidate_template_from_match(match):
    """Build an editable resolve-links input from official Sporttery identity."""
    return {
        "sporttery": {
            field: match.get(field)
            for field in SPORTTERY_TEMPLATE_FIELDS
            if match.get(field) is not None
        },
        "oddsportal_candidates": [],
        "home_fotmob_candidates": [],
        "away_fotmob_candidates": [],
    }


def collect_candidate_template(match_id, method="concern"):
    """Fetch official schedule identity and build a blank candidate template."""
    schedule = collect_schedule(method=method)
    wanted = str(match_id)
    for match in schedule.get("matches", []):
        if str(match.get("match_id")) == wanted:
            return candidate_template_from_match(match)
    raise ValueError(f"match_id {match_id} was not found in the official Sporttery schedule")


def _team_candidates(team_candidates):
    return [
        {
            "home_team": candidate.get("team_name"),
            "away_team": candidate.get("team_name"),
            "source_url": candidate.get("source_url"),
            "kickoff_at": candidate.get("kickoff_at"),
        }
        for candidate in team_candidates
    ]


def resolve_links(input_path, alias_registry_path=None):
    """Rank user-supplied candidate URLs for one Sporttery match."""
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    sporttery = payload["sporttery"]
    aliases = _load_aliases(alias_registry_path)
    home_team = sporttery.get("home_team")
    away_team = sporttery.get("away_team")
    kickoff_at = sporttery.get("kickoff_at")
    return {
        "sporttery": sporttery,
        "oddsportal": rank_match_candidates(
            sporttery,
            payload.get("oddsportal_candidates", []),
            source_kind="oddsportal",
            aliases=aliases,
        ),
        "fotmob_home": rank_match_candidates(
            {"home_team": home_team, "away_team": home_team, "kickoff_at": kickoff_at},
            _team_candidates(payload.get("home_fotmob_candidates", [])),
            source_kind="fotmob",
            aliases=aliases,
        ),
        "fotmob_away": rank_match_candidates(
            {"home_team": away_team, "away_team": away_team, "kickoff_at": kickoff_at},
            _team_candidates(payload.get("away_fotmob_candidates", [])),
            source_kind="fotmob",
            aliases=aliases,
        ),
    }


def _candidate_url(resolved, section_name):
    section = resolved.get(section_name)
    if not isinstance(section, dict) or section.get("status") != "candidate":
        return None
    return section.get("source_url")


def _powershell_quote(value):
    return f'"{str(value).replace(chr(34), chr(34) + chr(34))}"'


def link_summary_from_resolved(resolved):
    """Extract pregame-bundle-ready URLs from a resolved candidate result."""
    sporttery = resolved.get("sporttery", {})
    match_id = sporttery.get("match_id")
    urls = {
        "oddsportal_url": _candidate_url(resolved, "oddsportal"),
        "home_fotmob_url": _candidate_url(resolved, "fotmob_home"),
        "away_fotmob_url": _candidate_url(resolved, "fotmob_away"),
    }
    missing = [name for name, value in urls.items() if not value]
    result = {
        "sporttery": sporttery,
        "ready": not missing,
        "missing": missing,
        **{name: value for name, value in urls.items() if value},
    }
    if result["ready"]:
        pregame_args = [
            str(match_id),
            urls["oddsportal_url"],
            urls["home_fotmob_url"],
            urls["away_fotmob_url"],
        ]
        result["pregame_bundle_args"] = pregame_args
        result["pregame_bundle_command"] = " ".join(
            [
                "python",
                "-m",
                "outputs.tai_cai_tracker.cli",
                "pregame-bundle",
                pregame_args[0],
                *[_powershell_quote(argument) for argument in pregame_args[1:]],
            ]
        )
    return result


def link_summary(input_path):
    """Read a resolved candidate JSON file and return usable link summary."""
    with Path(input_path).open("r", encoding="utf-8") as input_file:
        return link_summary_from_resolved(json.load(input_file))


def workflow_next(match_id, base_dir="work"):
    """Return the next local candidate-link workflow step for one match."""
    match_id_text = str(match_id)
    base_path = Path(base_dir)
    candidate_path = base_path / "candidates" / f"{match_id_text}.json"
    resolved_path = base_path / "resolved" / f"{match_id_text}.json"
    paths = {
        "candidate_path": str(candidate_path),
        "resolved_path": str(resolved_path),
    }

    if not candidate_path.exists():
        return {
            "ready": False,
            "step": "create_candidate_template",
            **paths,
            "next_command": (
                "python -m outputs.tai_cai_tracker.cli "
                f"candidate-template {match_id_text} --output {_powershell_quote(candidate_path)}"
            ),
        }

    if not resolved_path.exists():
        return {
            "ready": False,
            "step": "resolve_links",
            **paths,
            "next_command": (
                "python -m outputs.tai_cai_tracker.cli "
                f"resolve-links {_powershell_quote(candidate_path)} --output {_powershell_quote(resolved_path)}"
            ),
        }

    summary = link_summary(resolved_path)
    if not summary["ready"]:
        return {
            "ready": False,
            "step": "fix_candidate_links",
            **paths,
            "missing": summary["missing"],
            "next_command": (
                "python -m outputs.tai_cai_tracker.cli "
                f"resolve-links {_powershell_quote(candidate_path)} --output {_powershell_quote(resolved_path)}"
            ),
        }

    return {
        "ready": True,
        "step": "run_pregame_bundle",
        **paths,
        "summary": summary,
        "next_command": summary["pregame_bundle_command"],
    }


def format_workflow_next(result):
    """Render a concise human-readable workflow-next result."""
    lines = [
        f"状态: {result['step']}",
        f"ready: {str(result['ready']).lower()}",
    ]
    if result.get("missing"):
        lines.append(f"缺少: {', '.join(result['missing'])}")
    if result.get("candidate_path"):
        lines.append(f"候选文件: {result['candidate_path']}")
    if result.get("resolved_path"):
        lines.append(f"解析文件: {result['resolved_path']}")
    if result.get("next_command"):
        lines.append("下一步命令:")
        lines.append(result["next_command"])
    return "\n".join(lines)
