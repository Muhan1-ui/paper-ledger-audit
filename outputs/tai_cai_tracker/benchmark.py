"""Post-match closing-line benchmark for settled paper predictions.

Closing lines are used only for post-match evaluation. They must never enter a
prediction or freeze workflow, because the closing line already contains
information about the eventual result direction: it is the last price after the
market has absorbed late team news and money. This module reads settled primary
predictions from the immutable paper ledger, matches them to football-data.co.uk
history by date and team name, and writes a separate benchmark JSONL. It never
writes to the paper ledger and never changes a frozen prediction.

Only competitions whose teams are covered by the local football-data history are
eligible. ``英冠`` maps to the collected ``E1`` history.  Chinese team names are
linked through Wikidata identities resolved by ``wikidata.resolve_team_identity``
and cached locally; the English identity is then compared with football-data
names by exact equality after normalization, never by a similarity threshold.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.request import ProxyHandler, Request, build_opener

from . import wikidata
from .resolver import cached_wikidata_team_identity, normalize_team_name

DATA_DIR = Path(__file__).resolve().parent / "data"
LEDGER_PATH = DATA_DIR / "paper_ledger.jsonl"
HISTORY_DIR = DATA_DIR / "history"
HISTORY_PATH = HISTORY_DIR / "history_matches.jsonl"
SOURCE_LINK_REGISTRY_PATH = DATA_DIR / "source_link_registry.json"
TEAM_ALIAS_PATH = HISTORY_DIR / "team_alias_fd.json"
CHINESE_TEAM_MAP_PATH = HISTORY_DIR / "chinese_team_map.json"
OUTPUT_PATH = HISTORY_DIR / "ledger_benchmark.jsonl"
WIKIDATA_CACHE_PATH = HISTORY_DIR / "wikidata_team_cache.json"

OUTCOMES: Tuple[str, ...] = ("home_win", "draw", "away_win")
DIVISIONS: Tuple[str, ...] = ("E0", "D1", "SP1", "I1", "F1", "E1")
TARGET_COMPETITIONS = frozenset({"英超", "西甲", "德甲", "意甲", "法甲", "英冠"})
COMPETITION_DIVISION = {
    "英超": "E0",
    "西甲": "SP1",
    "德甲": "D1",
    "意甲": "I1",
    "法甲": "F1",
    "英冠": "E1",
}
REASONS = ("competition_unmapped", "team_unmapped", "no_date_candidate", "ambiguous")

# One pass, token level, no fuzzy matching.  Words are dropped or rewritten
# exactly as listed; nothing is inferred from spelling similarity.
COMMON_WORD_MAP = {
    "fc": "",
    "cf": "",
    "sc": "",
    "afc": "",
    "ac": "",
    "calcio": "",
    "club": "",
    "de": "",
    "futbol": "",
    "football": "",
    "the": "",
    "united": "utd",
    "utd": "utd",
}

_KEYS = {
    "home_win": ("home_win", "home", "H"),
    "draw": ("draw", "D"),
    "away_win": ("away_win", "away", "A"),
}

# football-data.co.uk abbreviates club names.  Each entry below is either the
# club's full name or a standard abbreviation expansion.  This is a fixed
# table, not a similarity measure: an alias matches only by exact normalized
# equality.  Names without a confident expansion keep only themselves.
FD_ALIAS_EXPANSIONS: Dict[str, Tuple[str, ...]] = {
    # England
    "Bournemouth": ("AFC Bournemouth",),
    "Brighton": ("Brighton & Hove Albion",),
    "Cardiff": ("Cardiff City",),
    "Coventry": ("Coventry City",),
    "Huddersfield": ("Huddersfield Town",),
    "Hull": ("Hull City",),
    "Ipswich": ("Ipswich Town",),
    "Leeds": ("Leeds United",),
    "Leicester": ("Leicester City",),
    "Luton": ("Luton Town",),
    "Man City": ("Manchester City",),
    "Man United": ("Manchester United",),
    "Newcastle": ("Newcastle United",),
    "Norwich": ("Norwich City",),
    "Nott'm Forest": ("Nottingham Forest",),
    "QPR": ("Queens Park Rangers",),
    "Sheffield Weds": ("Sheffield Wednesday",),
    "Stoke": ("Stoke City",),
    "Swansea": ("Swansea City",),
    "Tottenham": ("Tottenham Hotspur",),
    "West Brom": ("West Bromwich Albion",),
    "West Ham": ("West Ham United",),
    "Wigan": ("Wigan Athletic",),
    "Wolves": ("Wolverhampton Wanderers",),
    "Birmingham": ("Birmingham City",),
    "Blackburn": ("Blackburn Rovers",),
    "Bolton": ("Bolton Wanderers",),
    "Burton": ("Burton Albion",),
    "Charlton": ("Charlton Athletic",),
    "Derby": ("Derby County",),
    "Doncaster": ("Doncaster Rovers",),
    "Lincoln": ("Lincoln City",),
    "Oxford": ("Oxford United",),
    "Peterboro": ("Peterborough United",),
    "Plymouth": ("Plymouth Argyle",),
    "Preston": ("Preston North End",),
    "Rotherham": ("Rotherham United",),
    "Wycombe": ("Wycombe Wanderers",),
    "Yeovil": ("Yeovil Town",),
    # Spain
    "Alaves": ("Deportivo Alaves",),
    "Almeria": ("UD Almeria",),
    "Ath Bilbao": ("Athletic Bilbao",),
    "Ath Madrid": ("Atletico Madrid",),
    "Barcelona": ("FC Barcelona",),
    "Betis": ("Real Betis",),
    "Celta": ("RC Celta de Vigo", "Celta Vigo"),
    "Eibar": ("SD Eibar",),
    "Espanol": ("Espanyol", "RCD Espanyol"),
    "Girona": ("Girona FC",),
    "Granada": ("Granada CF",),
    "Huesca": ("SD Huesca",),
    "La Coruna": ("Deportivo La Coruna", "Deportivo"),
    "Las Palmas": ("UD Las Palmas",),
    "Leganes": ("CD Leganes",),
    "Levante": ("Levante UD",),
    "Mallorca": ("RCD Mallorca",),
    "Osasuna": ("CA Osasuna",),
    "Oviedo": ("Real Oviedo",),
    "Real Madrid": ("Real Madrid CF", "Real Madrid Club de Futbol"),
    "Santander": ("Racing Santander",),
    "Sevilla": ("Sevilla FC",),
    "Sociedad": ("Real Sociedad",),
    "Sp Gijon": ("Sporting Gijon",),
    "Valencia": ("Valencia CF",),
    "Valladolid": ("Real Valladolid",),
    "Vallecano": ("Rayo Vallecano",),
    "Villarreal": ("Villarreal CF",),
    "Zaragoza": ("Real Zaragoza",),
    # Germany
    "Augsburg": ("FC Augsburg",),
    "Bayern Munich": ("FC Bayern Munich", "Bayern Munchen"),
    "Bielefeld": ("Arminia Bielefeld",),
    "Bochum": ("VfL Bochum",),
    "Braunschweig": ("Eintracht Braunschweig",),
    "Darmstadt": ("SV Darmstadt 98",),
    "Dortmund": ("Borussia Dortmund",),
    "Ein Frankfurt": ("Eintracht Frankfurt", "Frankfurt"),
    "Elversberg": ("SV Elversberg",),
    "FC Koln": ("Koln", "1. FC Koln", "FC Cologne", "Cologne"),
    "Freiburg": ("SC Freiburg",),
    "Greuther Furth": ("SpVgg Greuther Furth",),
    "Hamburg": ("Hamburger SV",),
    "Hannover": ("Hannover 96",),
    "Heidenheim": ("1. FC Heidenheim",),
    "Hertha": ("Hertha BSC",),
    "Hoffenheim": ("TSG Hoffenheim", "TSG 1899 Hoffenheim"),
    "Ingolstadt": ("FC Ingolstadt",),
    "Leverkusen": ("Bayer Leverkusen", "Bayer 04 Leverkusen"),
    "M'gladbach": ("Monchengladbach", "Borussia Monchengladbach", "Borussia M'gladbach"),
    "Mainz": ("Mainz 05", "1. FSV Mainz 05"),
    "Nurnberg": ("1. FC Nurnberg",),
    "Paderborn": ("SC Paderborn",),
    "St Pauli": ("St. Pauli",),
    "Stuttgart": ("VfB Stuttgart",),
    "Union Berlin": ("1. FC Union Berlin",),
    "Wolfsburg": ("VfL Wolfsburg",),
    # Italy
    "Atalanta": ("Atalanta BC",),
    "Benevento": ("Benevento Calcio",),
    "Bologna": ("Bologna F.C. 1909", "Bologna 1909"),
    "Brescia": ("Brescia Calcio",),
    "Cagliari": ("Cagliari Calcio",),
    "Catania": ("Calcio Catania",),
    "Chievo": ("Chievo Verona",),
    "Cremonese": ("US Cremonese",),
    "Empoli": ("Empoli FC",),
    "Fiorentina": ("ACF Fiorentina",),
    "Frosinone": ("Frosinone Calcio",),
    "Genoa": ("Genoa CFC",),
    "Inter": ("Inter Milan", "Internazionale"),
    "Lazio": ("SS Lazio",),
    "Lecce": ("US Lecce",),
    "Milan": ("AC Milan",),
    "Monza": ("AC Monza",),
    "Napoli": ("SSC Napoli",),
    "Palermo": ("Palermo FC",),
    "Parma": ("Parma Calcio 1913",),
    "Roma": ("AS Roma",),
    "Sampdoria": ("UC Sampdoria",),
    "Sassuolo": ("US Sassuolo",),
    "Spezia": ("Spezia Calcio",),
    "Torino": ("Torino FC",),
    "Udinese": ("Udinese Calcio",),
    "Venezia": ("Venezia FC",),
    "Verona": ("Hellas Verona",),
    # France
    "Ajaccio": ("AC Ajaccio",),
    "Amiens": ("Amiens SC",),
    "Angers": ("Angers SCO",),
    "Auxerre": ("AJ Auxerre",),
    "Bastia": ("SC Bastia",),
    "Bordeaux": ("Girondins de Bordeaux",),
    "Brest": ("Stade Brestois 29", "Stade Brestois"),
    "Caen": ("SM Caen",),
    "Clermont": ("Clermont Foot",),
    "Dijon": ("Dijon FCO",),
    "Guingamp": ("EA Guingamp",),
    "Le Havre": ("Le Havre AC",),
    "Le Mans": ("Le Mans FC",),
    "Lens": ("RC Lens",),
    "Lille": ("LOSC Lille",),
    "Lorient": ("FC Lorient",),
    "Lyon": ("Olympique Lyonnais",),
    "Marseille": ("Olympique de Marseille",),
    "Metz": ("FC Metz",),
    "Monaco": ("AS Monaco",),
    "Montpellier": ("Montpellier HSC",),
    "Nancy": ("AS Nancy",),
    "Nantes": ("FC Nantes",),
    "Nice": ("OGC Nice",),
    "Nimes": ("Nimes Olympique",),
    "Paris SG": ("Paris Saint-Germain",),
    "Reims": ("Stade de Reims",),
    "Rennes": ("Stade Rennais",),
    "St Etienne": ("Saint-Etienne",),
    "Strasbourg": ("RC Strasbourg",),
    "Toulouse": ("Toulouse FC",),
    "Troyes": ("ES Troyes AC",),
    "Valenciennes": ("Valenciennes FC",),
}

# Wikidata refuses anonymous robot traffic with HTTP 403.  A descriptive
# User-Agent with a contact address plus an explicit local proxy is the
# combination that was verified against the live endpoint.
WIKIDATA_PROXY = "http://127.0.0.1:7892"
WIKIDATA_USER_AGENT = (
    "TaiCaiTracker/1.0 (personal research; xueh15473@gmail.com)"
)
WIKIDATA_TIMEOUT_SECONDS = 20


def _wikidata_fetch(url: str) -> dict:
    """Fetch a Wikidata JSON object through the verified proxy configuration.

    One retry is attempted because a transient proxy or TLS failure would
    otherwise leave a team permanently unresolved.  The raw ``dict`` is
    accepted by ``wikidata._payload_and_hash``.
    """

    opener = build_opener(
        ProxyHandler(
            {"http": WIKIDATA_PROXY, "https": WIKIDATA_PROXY}
        )
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": WIKIDATA_USER_AGENT,
        },
    )
    last_error: Optional[BaseException] = None
    for _ in range(2):
        try:
            with opener.open(request, timeout=WIKIDATA_TIMEOUT_SECONDS) as response:
                raw = response.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Wikidata response is not an object")
            return payload
        except Exception as error:  # retry once, then surface the raw failure
            last_error = error
    assert last_error is not None
    raise last_error


def normalize_name(value: object) -> str:
    """Lowercase, strip accents, remove punctuation/whitespace, map common words."""

    if not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    folded = without_marks.casefold()
    tokens = re.split(r"[\W_]+", folded, flags=re.UNICODE)
    normalized: List[str] = []
    for token in tokens:
        if not token:
            continue
        mapped = COMMON_WORD_MAP.get(token, token)
        if mapped:
            normalized.append(mapped)
    return "".join(normalized)


# Club-type tokens are not part of a club's identity.  They are removed only
# as standalone tokens before resolver normalization, so the remaining
# comparison is still strict equality.
_FD_CLUB_SUFFIX = re.compile(
    r"(?<![A-Za-z])(?:A\.?F\.?C|F\.?C|C\.?F|S\.?C|A\.?C)\.?(?![A-Za-z])",
    re.IGNORECASE,
)


def _fold_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )


def team_match_key(value: object) -> str:
    """Return an exact-comparison key for an English or football-data team name.

    The key is ``resolver.normalize_team_name`` over an accent-folded name with
    standalone club acronyms removed.  No similarity measure is involved: two
    names match only when these keys are identical.
    """

    if not isinstance(value, str):
        return ""
    without_suffix = _FD_CLUB_SUFFIX.sub(" ", _fold_accents(value))
    return normalize_team_name(without_suffix)


def load_wikidata_cache(path: Path = WIKIDATA_CACHE_PATH) -> Dict[str, dict]:
    """Load cached Wikidata identity results keyed by official Chinese name."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    teams = payload.get("teams") if isinstance(payload, Mapping) else None
    if not isinstance(teams, Mapping):
        return {}
    return {
        name: dict(entry)
        for name, entry in teams.items()
        if isinstance(name, str) and isinstance(entry, Mapping)
    }


def load_chinese_team_map(path: Path = CHINESE_TEAM_MAP_PATH) -> Dict[str, str]:
    """Load the explicit ``{Chinese name: English name}`` table.

    This table is the first mapping step.  Every value is a real club name
    written by hand; nothing is inferred from spelling similarity.
    """

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return {
        chinese: english.strip()
        for chinese, english in payload.items()
        if isinstance(chinese, str)
        and isinstance(english, str)
        and chinese.strip()
        and english.strip()
    }


def load_team_alias_table(path: Path = TEAM_ALIAS_PATH) -> Dict[str, List[str]]:
    """Load the football-data alias table, ignoring malformed entries."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    table: Dict[str, List[str]] = {}
    for name, aliases in payload.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(aliases, list):
            continue
        cleaned = [alias.strip() for alias in aliases if isinstance(alias, str) and alias.strip()]
        if cleaned:
            table[name.strip()] = cleaned
    return table


def merge_fd_alias_table(
    generated: Mapping[str, Sequence[str]],
    curated: Mapping[str, Sequence[str]],
) -> Dict[str, List[str]]:
    """Merge the checked-in alias table over the code defaults, keeping order."""

    merged: Dict[str, List[str]] = {
        name: list(aliases) for name, aliases in generated.items()
    }
    for name, aliases in curated.items():
        entries = merged.setdefault(name, [name])
        for alias in [name, *aliases]:
            if alias not in entries:
                entries.append(alias)
    return merged


def _english_candidates(
    chinese_name: str,
    chinese_map: Mapping[str, str],
    fotmob_teams: Mapping[str, object],
    wikidata_teams: Mapping[str, object],
) -> List[Tuple[str, str]]:
    """Return ordered ``(English name, provider)`` candidates for one club.

    Order is fixed: explicit Chinese map, then FotMob's verified name, then
    Wikidata's English labels and aliases.  The first candidate that resolves
    to exactly one football-data club wins.
    """

    candidates: List[Tuple[str, str]] = []
    mapped = chinese_map.get(chinese_name)
    if isinstance(mapped, str) and mapped.strip():
        candidates.append((mapped.strip(), "chinese_team_map"))

    fotmob = fotmob_teams.get(chinese_name)
    if isinstance(fotmob, Mapping):
        source_name = fotmob.get("source_name")
        if isinstance(source_name, str) and source_name.strip():
            candidates.append((source_name.strip(), "fotmob"))

    wikidata_entry = wikidata_teams.get(chinese_name)
    if isinstance(wikidata_entry, Mapping):
        for name in [
            wikidata_entry.get("english_name"),
            *(wikidata_entry.get("english_names") or []),
        ]:
            if isinstance(name, str) and name.strip():
                candidates.append((name.strip(), "wikidata"))
    return candidates


def resolve_chinese_targets(
    team_names: Iterable[str],
    fd_alias_document: Mapping[str, Sequence[str]],
    chinese_map: Optional[Mapping[str, str]] = None,
    fotmob_teams: Optional[Mapping[str, object]] = None,
    wikidata_teams: Optional[Mapping[str, object]] = None,
) -> Tuple[Dict[str, str], dict]:
    """Resolve Chinese names to football-data names by strict key equality.

    The result maps each Chinese name to the single football-data club whose
    normalized name or listed alias equals the candidate exactly.  A name with
    no candidate, or with several football-data matches, is left unresolved and
    reported with ``reason=team_unmapped``.
    """

    chinese_map = chinese_map if isinstance(chinese_map, Mapping) else {}
    fotmob_teams = fotmob_teams if isinstance(fotmob_teams, Mapping) else {}
    wikidata_teams = wikidata_teams if isinstance(wikidata_teams, Mapping) else {}

    by_key: Dict[str, List[str]] = {}
    for fd_name, aliases in fd_alias_document.items():
        for alias in [fd_name, *aliases]:
            key = normalize_name(alias)
            if key and fd_name not in by_key.setdefault(key, []):
                by_key[key].append(fd_name)

    targets: Dict[str, str] = {}
    resolved: List[dict] = []
    unmatched: List[dict] = []
    for chinese_name in sorted(
        {name.strip() for name in team_names if isinstance(name, str) and name.strip()}
    ):
        chosen = None
        for english, provider in _english_candidates(
            chinese_name, chinese_map, fotmob_teams, wikidata_teams
        ):
            matches = by_key.get(normalize_name(english), [])
            if len(matches) == 1:
                chosen = {
                    "official_team": chinese_name,
                    "english_name": english,
                    "provider": provider,
                    "fd_name": matches[0],
                }
                break
        if chosen is None:
            unmatched.append(
                {
                    "official_team": chinese_name,
                    "reason": "team_unmapped",
                    "candidates": [
                        english
                        for english, _ in _english_candidates(
                            chinese_name, chinese_map, fotmob_teams, wikidata_teams
                        )
                    ],
                }
            )
            continue
        targets[chinese_name] = chosen["fd_name"]
        resolved.append(chosen)
    return targets, {"resolved": resolved, "unmatched": unmatched}
def write_wikidata_cache(
    teams: Mapping[str, Mapping[str, object]], path: Path = WIKIDATA_CACHE_PATH
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"version": 1, "teams": teams},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _wikidata_failure(team_name: str, error: BaseException) -> dict:
    http_status = getattr(error, "code", None)
    return {
        "status": "failed",
        "official_team": team_name,
        "reason": type(error).__name__,
        "http_status": http_status if isinstance(http_status, int) else None,
        "error": str(error)[:300],
    }


def seed_wikidata_cache_from_registry(
    team_names: Iterable[str],
    cache_path: Path = WIKIDATA_CACHE_PATH,
    registry_path: Path = SOURCE_LINK_REGISTRY_PATH,
) -> Dict[str, dict]:
    """Reuse already verified Wikidata identities without a new request.

    ``source_link_registry.json`` stores identities that were previously
    accepted by the same Wikidata module.  Re-validating them through
    ``cached_wikidata_team_identity`` avoids duplicate public requests and keeps
    the evidence (QID, URLs, capture time) intact.
    """

    teams = load_wikidata_cache(cache_path)
    changed = False
    for team_name in sorted(
        {name.strip() for name in team_names if isinstance(name, str) and name.strip()}
    ):
        cached = teams.get(team_name)
        if isinstance(cached, Mapping) and cached.get("status") == "resolved":
            continue
        identity = cached_wikidata_team_identity(registry_path, team_name)
        if identity is None:
            continue
        teams[team_name] = {
            "status": "resolved",
            **identity,
            "cache_source": "source_link_registry",
        }
        changed = True
    if changed:
        write_wikidata_cache(teams, cache_path)
    return teams


def resolve_wikidata_teams(
    team_names: Iterable[str],
    cache_path: Path = WIKIDATA_CACHE_PATH,
    resolve_identity=None,
    fetch_json=None,
    max_workers: int = 1,
) -> Tuple[Dict[str, dict], List[dict]]:
    """Resolve and cache Chinese team names through the existing Wikidata module.

    Successful cache entries are never re-requested.  ``fetch_json`` injects the
    verified proxy/User-Agent transport; ``max_workers`` above one resolves the
    remaining names concurrently and writes the cache once at the end.
    Request failures are stored with their HTTP status so they can be reported
    instead of being guessed.
    """

    resolve_identity = (
        wikidata.resolve_team_identity if resolve_identity is None else resolve_identity
    )
    if fetch_json is not None:
        base_resolve = resolve_identity

        def resolve_identity(team_name):
            return base_resolve(team_name, fetch_json=fetch_json)

    teams = load_wikidata_cache(cache_path)
    pending = [
        team_name
        for team_name in sorted(
            {name.strip() for name in team_names if isinstance(name, str) and name.strip()}
        )
        if not (
            isinstance(teams.get(team_name), Mapping)
            and teams[team_name].get("status") == "resolved"
        )
    ]

    def resolve_one(team_name: str) -> Tuple[str, dict]:
        try:
            result = dict(resolve_identity(team_name))
        except Exception as error:  # network and HTTP failures must be reported
            result = _wikidata_failure(team_name, error)
        return team_name, result

    if max_workers > 1 and len(pending) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(resolve_one, pending))
    else:
        results = [resolve_one(team_name) for team_name in pending]

    failures: List[dict] = []
    for team_name, result in results:
        if result.get("status") != "resolved":
            failures.append(result)
        teams[team_name] = result
    if results:
        write_wikidata_cache(teams, cache_path)
    return teams, failures


def build_wikidata_alias_sources(
    teams: Mapping[str, Mapping[str, object]],
    fd_names: Iterable[str],
    alias_document: Optional[Mapping[str, Sequence[str]]] = None,
) -> Tuple[Dict[str, dict], dict]:
    """Map Chinese names to one football-data name by exact normalized equality.

    ``alias_document`` makes the explicit football-data alias table part of the
    comparison: a Wikidata English name matches when it equals any listed alias
    exactly after normalization.  No similarity threshold is involved.
    """

    by_key: Dict[str, List[str]] = {}
    if alias_document is None:
        for name in fd_names:
            key = team_match_key(name)
            if key:
                by_key.setdefault(key, []).append(name.strip())
    else:
        for fd_name, aliases in alias_document.items():
            for alias in [fd_name, *aliases]:
                key = team_match_key(alias)
                if key and fd_name not in by_key.setdefault(key, []):
                    by_key[key].append(fd_name)

    sources: Dict[str, dict] = {}
    detail = {"resolved": [], "unmatched": [], "ambiguous": [], "failed": []}
    for chinese_name, entry in sorted(teams.items()):
        status = entry.get("status")
        if status == "failed":
            detail["failed"].append(
                {
                    "official_team": chinese_name,
                    "http_status": entry.get("http_status"),
                    "reason": entry.get("reason"),
                }
            )
            continue
        if status != "resolved":
            detail["failed"].append(
                {
                    "official_team": chinese_name,
                    "http_status": None,
                    "reason": entry.get("reason"),
                }
            )
            continue

        english_names: List[str] = []
        for name in [entry.get("english_name"), *(entry.get("english_names") or [])]:
            if isinstance(name, str) and name.strip() and name not in english_names:
                english_names.append(name)
        matched: List[str] = []
        for name in english_names:
            for fd_name in by_key.get(team_match_key(name), []):
                if fd_name not in matched:
                    matched.append(fd_name)
        record = {
            "official_team": chinese_name,
            "qid": entry.get("qid"),
            "english_name": entry.get("english_name"),
            "fd_names": sorted(matched),
        }
        if len(matched) == 1:
            sources[chinese_name] = {
                "source_name": matched[0],
                "source_provider": "Wikidata",
                "qid": entry.get("qid"),
                "source_url": entry.get("source_url"),
            }
            detail["resolved"].append(record)
        elif not matched:
            detail["unmatched"].append(record)
        else:
            detail["ambiguous"].append(record)
    return sources, detail


def load_jsonl(path: Path) -> Iterator[dict]:
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if text:
                yield json.loads(text)


def write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            )


def coerce_three_way(value: object) -> Optional[Dict[str, float]]:
    """Return a ``home_win/draw/away_win`` probability map or ``None``."""

    if not isinstance(value, Mapping):
        return None
    result: Dict[str, float] = {}
    for outcome, keys in _KEYS.items():
        for key in keys:
            if key in value and value[key] is not None:
                try:
                    result[outcome] = float(value[key])
                except (TypeError, ValueError):
                    return None
                break
    if len(result) != 3:
        return None
    return result


def coerce_outcomes_list(value: object) -> Optional[Dict[str, float]]:
    if not isinstance(value, list):
        return None
    result: Dict[str, float] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        label = item.get("label")
        if label not in OUTCOMES:
            continue
        try:
            result[label] = float(item["probability"])
        except (KeyError, TypeError, ValueError):
            return None
    return result if len(result) == 3 else None


def _round_map(value: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if value is None:
        return None
    return {outcome: round(float(value[outcome]), 6) for outcome in OUTCOMES}


def eligible_predictions(
    records: Sequence[Mapping[str, object]],
) -> Tuple[List[Tuple[Mapping[str, object], Mapping[str, object]]], int]:
    """Return settled primary predictions in target competitions and the skip count.

    Settlements explicitly invalidated by a later ``settlement_invalidation`` record
    are ignored.  When a prediction has more than one surviving settlement the last
    one in ledger order is used.
    """

    predictions: Dict[str, Mapping[str, object]] = {}
    for record in records:
        if record.get("record_type") != "prediction":
            continue
        content_hash = record.get("content_hash")
        if isinstance(content_hash, str) and content_hash:
            predictions[content_hash] = record

    invalidated = {
        record.get("settlement_hash")
        for record in records
        if record.get("record_type") == "settlement_invalidation"
    }
    settlements: Dict[str, Mapping[str, object]] = {}
    for record in records:
        if record.get("record_type") != "settlement":
            continue
        if record.get("content_hash") in invalidated:
            continue
        prediction_hash = record.get("prediction_hash")
        if isinstance(prediction_hash, str) and prediction_hash:
            settlements[prediction_hash] = record

    eligible: List[Tuple[Mapping[str, object], Mapping[str, object]]] = []
    skipped_by_competition = 0
    for prediction_hash, settlement in settlements.items():
        prediction = predictions.get(prediction_hash)
        if prediction is None:
            continue
        if prediction.get("prediction_role") != "primary":
            continue
        context = prediction.get("match_context")
        competition = context.get("competition") if isinstance(context, Mapping) else None
        if competition not in TARGET_COMPETITIONS:
            skipped_by_competition += 1
            continue
        eligible.append((prediction, settlement))
    return eligible, skipped_by_competition


# Post-match benchmark price, in fixed fallback order.  football-data.co.uk
# stopped carrying Pinnacle columns partway through 2025/26, so the 2026/27
# rows have no Pinnacle close at all; the average and B365 close are complete.
MARKET_CLOSE_SOURCES: Tuple[str, ...] = ("pinnacle", "avg", "b365")


def market_close(
    odds: object,
) -> Tuple[Optional[Dict[str, float]], Optional[str]]:
    """Return the first available closing fair price and its source name.

    Order is fixed: Pinnacle, then the market average, then B365.  Each source
    is read from ``close.fair`` when present, otherwise from the row-level
    ``fair`` that the history collector derives from the same close prices.
    """

    if not isinstance(odds, Mapping):
        return None, None
    for source in MARKET_CLOSE_SOURCES:
        entry = odds.get(source)
        if not isinstance(entry, Mapping):
            continue
        fair = None
        close = entry.get("close")
        if isinstance(close, Mapping):
            fair = coerce_three_way(close.get("fair"))
        if fair is None:
            fair = coerce_three_way(entry.get("fair"))
        if fair is not None:
            return fair, source
    return None, None


def load_history_rows(path: Path) -> List[dict]:
    """Load the big-five divisions and their closing fair prices."""

    rows: List[dict] = []
    for record in load_jsonl(path):
        division = record.get("division")
        if division not in DIVISIONS:
            continue
        home_team = record.get("home_team")
        away_team = record.get("away_team")
        match_date = record.get("date")
        if not isinstance(home_team, str) or not isinstance(away_team, str):
            continue
        if not isinstance(match_date, str) or not match_date:
            continue
        odds = record.get("odds") if isinstance(record.get("odds"), Mapping) else {}
        pinnacle = odds.get("pinnacle") if isinstance(odds.get("pinnacle"), Mapping) else {}
        fair = coerce_three_way(pinnacle.get("fair"))
        if fair is None:
            close = pinnacle.get("close")
            if isinstance(close, Mapping):
                fair = coerce_three_way(close.get("fair"))
        implied = coerce_three_way(pinnacle.get("implied"))
        close_fair, close_source = market_close(odds)
        rows.append(
            {
                "division": division,
                "date": match_date,
                "home_team": home_team,
                "away_team": away_team,
                "pinnacle_fair": fair,
                "pinnacle_implied": implied,
                "market_close_fair": close_fair,
                "market_close_source": close_source,
            }
        )
    return rows


def team_names_from_history(history_rows: Sequence[Mapping[str, object]]) -> List[str]:
    names = set()
    for row in history_rows:
        for key in ("home_team", "away_team"):
            name = row.get(key)
            if isinstance(name, str) and name.strip():
                names.add(name)
    return sorted(names)


def build_fd_alias_document(fd_names: Iterable[str]) -> Dict[str, List[str]]:
    """Return ``{fd_name: [aliases...]}`` for every football-data name.

    Every name appears, even when it has no known expansion.  Aliases come from
    the fixed expansion table and from other real history spellings whose
    normalized key is identical.  No similarity threshold is used.
    """

    unique = sorted({name for name in fd_names if isinstance(name, str) and name.strip()})
    document: Dict[str, List[str]] = {name: [name] for name in unique}
    for name in unique:
        for alias in FD_ALIAS_EXPANSIONS.get(name, ()):
            if alias not in document[name]:
                document[name].append(alias)

    by_key: Dict[str, List[str]] = {}
    for name in unique:
        for alias in document[name]:
            key = team_match_key(alias)
            if key and name not in by_key.setdefault(key, []):
                by_key[key].append(name)
    for names in by_key.values():
        if len(names) < 2:
            continue
        for fd_name in names:
            for other in names:
                if other != fd_name and other not in document[fd_name]:
                    document[fd_name].append(other)
    return document


def build_team_alias(
    fd_names: Iterable[str],
    aliases: Mapping[str, object],
    base_aliases: Optional[Mapping[str, Sequence[str]]] = None,
    chinese_targets: Optional[Mapping[str, str]] = None,
) -> Dict[str, List[str]]:
    """Build ``{fd_name: [aliases...]}`` from real history and alias data only.

    Every football-data team keeps at least itself.  ``base_aliases`` supplies
    the fixed full-name/abbreviation table.  ``chinese_targets`` maps an
    already resolved Chinese name to exactly one football-data club; those names
    are attached first and are never re-resolved through the generic alias
    loop, so the documented priority cannot be overridden.  A Chinese alias in
    ``aliases`` is attached only when its recorded ``source_name`` normalizes
    exactly to a football-data name or one of those explicit aliases.  Names
    that normalize to the same key are cross-listed.
    """

    unique = sorted({name for name in fd_names if isinstance(name, str) and name.strip()})
    base = base_aliases if isinstance(base_aliases, Mapping) else {}
    document: Dict[str, List[str]] = {}
    for name in unique:
        entries = [name]
        for alias in base.get(name, ()):
            if isinstance(alias, str) and alias.strip() and alias not in entries:
                entries.append(alias)
        document[name] = entries

    by_normalized: Dict[str, List[str]] = {}
    for name in unique:
        for alias in document[name]:
            key = normalize_name(alias)
            if key and name not in by_normalized.setdefault(key, []):
                by_normalized[key].append(name)

    targeted = set()
    if isinstance(chinese_targets, Mapping):
        for chinese_name, fd_name in sorted(chinese_targets.items()):
            if not isinstance(chinese_name, str) or not chinese_name.strip():
                continue
            if fd_name not in document:
                continue
            if chinese_name not in document[fd_name]:
                document[fd_name].append(chinese_name)
            targeted.add(chinese_name)

    for alias, info in sorted(aliases.items()):
        if alias in targeted:
            continue
        if not isinstance(alias, str) or not isinstance(info, Mapping):
            continue
        source_name = info.get("source_name")
        if not isinstance(source_name, str) or not source_name.strip():
            continue
        for fd_name in by_normalized.get(normalize_name(source_name), []):
            if alias not in document[fd_name]:
                document[fd_name].append(alias)
            if source_name != fd_name and source_name not in document[fd_name]:
                document[fd_name].append(source_name)

    for names in by_normalized.values():
        if len(names) < 2:
            continue
        for fd_name in names:
            for other in names:
                if other != fd_name and other not in document[fd_name]:
                    document[fd_name].append(other)
    return document


def write_team_alias(path: Path, document: Mapping[str, Sequence[str]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_alias_lookup(document: Mapping[str, Sequence[str]]) -> Dict[str, set]:
    lookup: Dict[str, set] = {}
    for fd_name, aliases in document.items():
        for alias in [fd_name, *aliases]:
            normalized = normalize_name(alias)
            if normalized:
                lookup.setdefault(normalized, set()).add(fd_name)
    return lookup


def resolve_team(raw_name: object, lookup: Mapping[str, set]) -> Optional[str]:
    normalized = normalize_name(raw_name)
    candidates = lookup.get(normalized)
    if not candidates or len(candidates) != 1:
        return None
    return next(iter(candidates))


def build_history_index(
    history_rows: Sequence[Mapping[str, object]],
) -> Dict[Tuple[str, str, str], List[Mapping[str, object]]]:
    index: Dict[Tuple[str, str, str], List[Mapping[str, object]]] = {}
    for row in history_rows:
        key = (
            str(row.get("division")),
            normalize_name(row.get("home_team")),
            normalize_name(row.get("away_team")),
        )
        index.setdefault(key, []).append(row)
    return index


def _parse_date(value: object) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def kickoff_utc_date(value: object) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone(timedelta(hours=8)))
    return moment.astimezone(timezone.utc).date()


def _base_row(
    prediction: Mapping[str, object],
    settlement: Mapping[str, object],
    competition: object,
    home_raw: object,
    away_raw: object,
) -> dict:
    model = prediction.get("model") if isinstance(prediction.get("model"), Mapping) else {}
    blended = coerce_three_way(model.get("blended_outcomes"))
    if blended is None:
        blended = coerce_outcomes_list(prediction.get("outcomes"))
    market = coerce_three_way(model.get("market_outcomes"))
    actual = settlement.get("actual_outcome")
    return {
        "prediction_hash": prediction.get("content_hash"),
        "match_id": prediction.get("match_id"),
        "kickoff": prediction.get("kickoff_at"),
        "competition": competition,
        "home_raw": home_raw,
        "away_raw": away_raw,
        "fd_home": None,
        "fd_away": None,
        "fd_date": None,
        "actual": actual if actual in OUTCOMES else None,
        "your_blended": _round_map(blended),
        "prematch_market": _round_map(market),
        "pinnacle_fair": None,
        "pinnacle_implied": None,
        "market_close_fair": None,
        "market_close_source": None,
        "matched": False,
        "reason": None,
    }


def match_prediction(
    prediction: Mapping[str, object],
    settlement: Mapping[str, object],
    history_index: Mapping[Tuple[str, str, str], Sequence[Mapping[str, object]]],
    alias_lookup: Mapping[str, set],
) -> dict:
    context = prediction.get("match_context")
    context = context if isinstance(context, Mapping) else {}
    competition = context.get("competition")
    home_raw = context.get("home_team")
    away_raw = context.get("away_team")
    row = _base_row(prediction, settlement, competition, home_raw, away_raw)

    division = COMPETITION_DIVISION.get(competition)
    if division is None or division not in DIVISIONS:
        row["reason"] = "competition_unmapped"
        return row

    fd_home = resolve_team(home_raw, alias_lookup)
    fd_away = resolve_team(away_raw, alias_lookup)
    row["fd_home"] = fd_home
    row["fd_away"] = fd_away
    if fd_home is None or fd_away is None:
        row["reason"] = "team_unmapped"
        return row

    utc_date = kickoff_utc_date(prediction.get("kickoff_at"))
    if utc_date is None:
        row["reason"] = "no_date_candidate"
        return row

    key = (division, normalize_name(fd_home), normalize_name(fd_away))
    candidates: List[Tuple[int, Mapping[str, object]]] = []
    for candidate in history_index.get(key, ()):
        candidate_date = _parse_date(candidate.get("date"))
        if candidate_date is None:
            continue
        distance = abs((candidate_date - utc_date).days)
        if distance <= 1:
            candidates.append((distance, candidate))
    if not candidates:
        row["reason"] = "no_date_candidate"
        return row

    best_distance = min(distance for distance, _ in candidates)
    best = [candidate for distance, candidate in candidates if distance == best_distance]
    if len(best) > 1:
        row["reason"] = "ambiguous"
        return row

    match = best[0]
    row.update(
        {
            "fd_home": match.get("home_team"),
            "fd_away": match.get("away_team"),
            "fd_date": match.get("date"),
            "pinnacle_fair": _round_map(match.get("pinnacle_fair")),
            "pinnacle_implied": _round_map(match.get("pinnacle_implied")),
            "market_close_fair": _round_map(match.get("market_close_fair")),
            "market_close_source": match.get("market_close_source"),
            "matched": True,
            "reason": None,
        }
    )
    return row


def match_eligible(
    eligible: Sequence[Tuple[Mapping[str, object], Mapping[str, object]]],
    history_rows: Sequence[Mapping[str, object]],
    alias_document: Mapping[str, Sequence[str]],
) -> List[dict]:
    index = build_history_index(history_rows)
    lookup = build_alias_lookup(alias_document)
    return [
        match_prediction(prediction, settlement, index, lookup)
        for prediction, settlement in eligible
    ]


def summarize(
    rows: Sequence[Mapping[str, object]],
    eligible_count: int,
    skipped_by_competition: int,
) -> dict:
    matched = sum(1 for row in rows if row.get("matched"))
    reasons = Counter(
        str(row["reason"])
        for row in rows
        if not row.get("matched") and row.get("reason")
    )
    team_counts: Counter = Counter()
    for row in rows:
        if row.get("matched") or row.get("reason") == "competition_unmapped":
            continue
        for key in ("home_raw", "away_raw"):
            name = row.get(key)
            if isinstance(name, str) and name:
                team_counts[name] += 1
    unmatched_teams = [
        name
        for name, _ in sorted(team_counts.items(), key=lambda item: (-item[1], item[0]))[
            :10
        ]
    ]
    return {
        "eligible": eligible_count,
        "matched": matched,
        "match_rate": round(matched / eligible_count, 4) if eligible_count else 0.0,
        "skipped_by_competition": skipped_by_competition,
        "reasons": dict(sorted(reasons.items())),
        "unmatched_teams": unmatched_teams,
    }


def _load_json_object(path: Path, key: str) -> Mapping[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    section = payload.get(key)
    return section if isinstance(section, Mapping) else {}


def run_link(
    ledger_path: Path = LEDGER_PATH,
    history_path: Path = HISTORY_PATH,
    team_alias_path: Path = TEAM_ALIAS_PATH,
    chinese_team_map_path: Path = CHINESE_TEAM_MAP_PATH,
    output_path: Path = OUTPUT_PATH,
    wikidata_cache_path: Path = WIKIDATA_CACHE_PATH,
    source_link_registry_path: Path = SOURCE_LINK_REGISTRY_PATH,
    resolve_identity: bool = False,
) -> Tuple[List[dict], dict]:
    records = list(load_jsonl(ledger_path))
    eligible, skipped_by_competition = eligible_predictions(records)
    history_rows = load_history_rows(history_path)

    requested = sorted(
        {
            name
            for prediction, _ in eligible
            for context in [prediction.get("match_context")]
            if isinstance(context, Mapping)
            for name in (context.get("home_team"), context.get("away_team"))
            if isinstance(name, str) and name.strip()
        }
    )
    seed_wikidata_cache_from_registry(
        requested, wikidata_cache_path, source_link_registry_path
    )
    if resolve_identity:
        _, failures = resolve_wikidata_teams(
            requested,
            wikidata_cache_path,
            fetch_json=_wikidata_fetch,
            max_workers=4,
        )
        for failure in failures:
            print(
                "wikidata failure: "
                + json.dumps(failure, ensure_ascii=False, separators=(",", ":")),
                file=sys.stderr,
            )

    fd_names = team_names_from_history(history_rows)
    fd_alias_document = merge_fd_alias_table(
        build_fd_alias_document(fd_names), load_team_alias_table(team_alias_path)
    )
    wikidata_teams = dict(
        _load_json_object(source_link_registry_path, "wikidata_teams")
    )
    for name, entry in load_wikidata_cache(wikidata_cache_path).items():
        wikidata_teams.setdefault(name, entry)
    targets, detail = resolve_chinese_targets(
        requested,
        fd_alias_document,
        chinese_map=load_chinese_team_map(chinese_team_map_path),
        fotmob_teams=_load_json_object(source_link_registry_path, "fotmob_teams"),
        wikidata_teams=wikidata_teams,
    )
    print(
        "team mapping: "
        + json.dumps(
            {
                "resolved": len(detail["resolved"]),
                "unmatched": len(detail["unmatched"]),
                "unmatched_teams": [
                    item["official_team"] for item in detail["unmatched"]
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )
    alias_document = build_team_alias(
        fd_names, {}, base_aliases=fd_alias_document, chinese_targets=targets
    )
    write_team_alias(team_alias_path, alias_document)

    rows = match_eligible(eligible, history_rows, alias_document)
    write_jsonl(output_path, rows)
    return rows, summarize(rows, len(eligible), skipped_by_competition)


def rps_score(probabilities: Mapping[str, float], actual: str) -> float:
    """Rank probability score over the ordered home/draw/away outcomes."""

    if actual not in OUTCOMES:
        raise ValueError(f"unknown actual outcome: {actual!r}")
    cumulative_probability = 0.0
    cumulative_outcome = 0.0
    score = 0.0
    for outcome in OUTCOMES:
        cumulative_probability += float(probabilities.get(outcome, 0.0))
        cumulative_outcome += 1.0 if outcome == actual else 0.0
        score += (cumulative_probability - cumulative_outcome) ** 2
    return score / 2.0


def brier_score(probabilities: Mapping[str, float], actual: str) -> float:
    if actual not in OUTCOMES:
        raise ValueError(f"unknown actual outcome: {actual!r}")
    return sum(
        (float(probabilities.get(outcome, 0.0)) - (1.0 if outcome == actual else 0.0))
        ** 2
        for outcome in OUTCOMES
    )


def top1(probabilities: Mapping[str, float]) -> str:
    return max(OUTCOMES, key=lambda outcome: float(probabilities.get(outcome, 0.0)))


def _score_series(rows: Sequence[Mapping[str, object]], field: str) -> dict:
    scored = [
        row
        for row in rows
        if row.get("matched")
        and isinstance(row.get(field), Mapping)
        and row.get("actual") in OUTCOMES
    ]
    if not scored:
        return {"n": 0, "rps": None, "brier": None, "top1": None}
    rps_values = [rps_score(row[field], str(row["actual"])) for row in scored]
    brier_values = [brier_score(row[field], str(row["actual"])) for row in scored]
    hits = sum(1 for row in scored if top1(row[field]) == row["actual"])
    return {
        "n": len(scored),
        "rps": sum(rps_values) / len(scored),
        "brier": sum(brier_values) / len(scored),
        "top1": hits / len(scored),
    }


def evaluate_rows(rows: Sequence[Mapping[str, object]]) -> dict:
    metrics = {
        field: _score_series(rows, field)
        for field in ("your_blended", "prematch_market", "market_close_fair")
    }
    paired = [
        row
        for row in rows
        if row.get("matched")
        and isinstance(row.get("your_blended"), Mapping)
        and isinstance(row.get("market_close_fair"), Mapping)
        and row.get("actual") in OUTCOMES
    ]
    different = [
        row for row in paired if top1(row["your_blended"]) != top1(row["market_close_fair"])
    ]
    hits = sum(1 for row in different if top1(row["your_blended"]) == row["actual"])
    metrics["divergence"] = {
        "n": len(different),
        "paired": len(paired),
        "pred_top1_hit": hits / len(different) if different else None,
    }
    scored_close = [
        row
        for row in rows
        if row.get("matched")
        and isinstance(row.get("market_close_fair"), Mapping)
        and row.get("actual") in OUTCOMES
    ]
    sources = Counter(
        str(row["market_close_source"])
        for row in scored_close
        if row.get("market_close_source")
    )
    metrics["market_close_sources"] = dict(sorted(sources.items()))
    return metrics


def _format(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def format_evaluation(metrics: Mapping[str, Mapping[str, object]]) -> List[str]:
    lines: List[str] = []
    for field in ("your_blended", "prematch_market", "market_close_fair"):
        item = metrics[field]
        lines.append(
            f"eval {field} rps={_format(item['rps'])} "
            f"brier={_format(item['brier'])} top1={_format(item['top1'])} "
            f"n={item['n']}"
        )
    lines.append(
        "eval sources="
        + json.dumps(metrics["market_close_sources"], ensure_ascii=False, separators=(",", ":"))
    )
    divergence = metrics["divergence"]
    lines.append(
        f"eval divergence n={divergence['n']} paired={divergence['paired']} "
        f"pred_top1_hit={_format(divergence['pred_top1_hit'])}"
    )
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Match settled primary paper predictions to football-data.co.uk "
            "closing prices and optionally score the three-way comparison."
        )
    )
    parser.add_argument(
        "--link",
        action="store_true",
        help="match predictions and write ledger_benchmark.jsonl",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        dest="evaluate",
        help="print RPS/Brier/top1 for your_blended, prematch_market, pinnacle_fair",
    )
    parser.add_argument(
        "--resolve-wikidata",
        action="store_true",
        help=(
            "resolve missing Chinese team names through the throttled Wikidata "
            "module and cache the results before linking"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.link and not args.evaluate:
        parser.error("choose --link and/or --eval")

    rows, summary = run_link(resolve_identity=args.resolve_wikidata)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    if args.evaluate:
        for line in format_evaluation(evaluate_rows(rows)):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
