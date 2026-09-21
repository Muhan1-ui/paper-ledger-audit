"""Compose official, European-odds, and xG evidence into pregame templates."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import re

from .context import collect_pregame_context
from .context_resolver import resolve_context_event
from .thesportsdb import (
    collect_thesportsdb_pregame_context,
    resolve_thesportsdb_event,
)
from .fotmob import (
    collect_fotmob_season_bridge_xg,
    collect_match_xg_evidence,
    discover_match_team_stats_urls,
)
from .understat import collect_league_xg, collect_season_bridge_xg
from .xgscore import collect_xgscore, discover_xgscore_event_url
from .ledger import ALL_MARKET_OPTIONS
from .model import poisson_score_matrix
from .football433 import collect_433, discover_433_detail_url, event_matches_official_identity
from .resolver import (
    cached_fotmob_team_identity,
    cached_wikidata_team_identity,
    cached_source_links,
    context_source_aliases,
    record_adopted_aliases,
    record_source_links,
)
from .sporttery import collect_fixed_bonus, collect_schedule


def force_oddsportal_1x2_url(source_url):
    """Return an OddsPortal URL fragment that requests the full-time 1X2 market."""
    if "#" not in source_url:
        return source_url
    base, fragment = source_url.split("#", 1)
    event_id = fragment.split(":", 1)[0].split(";", 1)[0]
    return f"{base}#{event_id}:1X2;2"


def _is_named_bookmaker(name):
    return isinstance(name, str) and name.strip() and not name.startswith("provider:")


def select_named_1x2_capture(european_odds, match_id, kickoff_at):
    """Build an odds-import capture from one named European 1X2 row."""
    market = european_odds.get("market", {})
    if market.get("market_name") != "1X2":
        raise ValueError("European odds baseline must be the 1X2 market")
    rows = market.get("rows", [])
    row = next((entry for entry in rows if entry.get("label") == "1X2"), rows[0] if rows else None)
    if row is None:
        raise ValueError("European 1X2 market has no rows")
    bookmaker_rows = row.get("bookmakers", [row])
    for bookmaker in bookmaker_rows:
        name = bookmaker.get("bookmaker")
        prices = bookmaker.get("prices", {})
        if _is_named_bookmaker(name) and {"home_win", "draw", "away_win"}.issubset(prices):
            return {
                "match_id": match_id,
                "kickoff_at": kickoff_at,
                "captured_at": european_odds["captured_at"],
                "source_url": european_odds["source_url"],
                "bookmaker": name,
                "source_quality": "named_bookmaker",
                "odds": {
                    "home_win": prices["home_win"],
                    "draw": prices["draw"],
                    "away_win": prices["away_win"],
                },
            }
    raise ValueError("European 1X2 market has no complete named-bookmaker row")


def _readiness(official, capture, xg):
    missing = []
    official_markets = official.get("official_markets", {})
    spf = official_markets.get("spf")
    if not isinstance(spf, dict) or spf.get("status") != "frozen":
        missing.append("official SPF fixed prizes")
    for code in ALL_MARKET_OPTIONS:
        entry = official_markets.get(code)
        if not isinstance(entry, dict) or entry.get("status") != "frozen":
            missing.append(f"official {code} market")
    if capture is None:
        missing.append("named European 1X2 bookmaker capture")
    if not isinstance(xg.get("xg_inputs"), dict):
        missing.append("xG evidence")
    return {
        "can_prepare_prediction_input": "official SPF fixed prizes" not in missing
        and capture is not None
        and isinstance(xg.get("xg_inputs"), dict),
        "can_prepare_all_market_input": not any(item.startswith("official ") for item in missing),
        "missing": missing,
    }


def _timestamp_before_kickoff(value, kickoff_at):
    try:
        timestamp = datetime.fromisoformat(value)
        kickoff = datetime.fromisoformat(kickoff_at)
    except (TypeError, ValueError):
        return False
    return timestamp.tzinfo is not None and kickoff.tzinfo is not None and timestamp < kickoff


def _xg_has_minimum_sample(evidence, minimum=8):
    """Require the complete reported xG sample to meet the medium-tier floor."""
    sample_matches = evidence.get("sample_matches")
    if sample_matches is not None:
        return (
            isinstance(sample_matches, int)
            and not isinstance(sample_matches, bool)
            and sample_matches >= minimum
        )
    samples = (evidence.get("home_sample_matches"), evidence.get("away_sample_matches"))
    return all(
        isinstance(sample, int) and not isinstance(sample, bool) and sample >= minimum
        for sample in samples
    )


def _xg_evidence(xg):
    """Return model evidence only for an otherwise usable xG candidate."""
    if not isinstance(xg, dict):
        return None
    xg_inputs = xg.get("xg_inputs")
    evidence = xg_inputs.get("evidence") if isinstance(xg_inputs, dict) else None
    return evidence if isinstance(evidence, dict) else None


def _xg_quality_tier(xg):
    """Rank only evidence already collected during this action."""
    evidence = _xg_evidence(xg)
    if evidence is None:
        return 0
    scope = evidence.get("scope")
    if scope == "home_away_split":
        if _is_traceable_season_bridge(evidence):
            return 2
        return 3 if _xg_has_minimum_sample(evidence) else 0
    if scope == "source_match_model" and _xg_has_minimum_sample(evidence):
        return 2
    if scope == "league_total" and _xg_has_minimum_sample(evidence):
        return 1
    return 0


_XG_PROVIDER_TIEBREAK = {
    "Understat": 4,
    "SofaScore": 3,
    "xGscore": 2,
    "FotMob": 1,
}


def select_xg_candidate(candidates):
    """Select the strongest usable candidate without fetching another source."""
    usable = [candidate for candidate in candidates if _xg_evidence(candidate.get("xg"))]
    if not usable:
        return None
    return max(
        usable,
        key=lambda candidate: (
            _xg_quality_tier(candidate["xg"]),
            _XG_PROVIDER_TIEBREAK.get(candidate.get("provider"), 0),
        ),
    )


def _xg_sample_summary(evidence):
    if "sample_matches" in evidence:
        return evidence["sample_matches"]
    return {
        "home": evidence.get("home_sample_matches"),
        "away": evidence.get("away_sample_matches"),
    }


def build_xg_source_cascade(candidates, selected, unavailable):
    """Describe selection and collection gaps without turning them into evidence."""
    cascade = []
    selected_tier = _xg_quality_tier(selected["xg"]) if selected is not None else None
    for candidate in candidates:
        xg = candidate["xg"]
        evidence = _xg_evidence(xg)
        if evidence is None:
            continue
        tier = _xg_quality_tier(xg)
        entry = {
            "provider": candidate["provider"],
            "status": "selected" if candidate is selected else "available_not_selected",
            "scope": evidence.get("scope"),
            "sample_matches": _xg_sample_summary(evidence),
            "quality_tier": tier,
            "source_url": evidence.get("source_url"),
        }
        if candidate is selected:
            entry["reason"] = "highest_available_quality_tier"
        elif tier < selected_tier:
            entry["reason"] = "lower_quality_tier_than_selected"
        else:
            entry["reason"] = "same_tier_provider_tiebreak"
        cascade.append(entry)
    for item in unavailable:
        entry = {
            "provider": item["provider"],
            "status": "unavailable",
            "reason": item["reason"],
        }
        if isinstance(item.get("source_url"), str):
            entry["source_url"] = item["source_url"]
        cascade.append(entry)
    return cascade


def _is_traceable_season_bridge(evidence):
    """Recognize the explicit two-season schema without treating it as high confidence."""
    if not isinstance(evidence, dict) or not {"current_season", "prior_season"}.issubset(evidence):
        return False
    current = evidence["current_season"]
    prior = evidence["prior_season"]
    if not isinstance(current, dict) or not isinstance(prior, dict):
        return False
    current_samples = (current.get("home_sample_matches"), current.get("away_sample_matches"))
    prior_samples = (prior.get("home_sample_matches"), prior.get("away_sample_matches"))
    samples_valid = all(isinstance(sample, int) and not isinstance(sample, bool) and sample >= 2 for sample in current_samples) and all(
        isinstance(sample, int) and not isinstance(sample, bool) and sample >= 12 for sample in prior_samples
    )
    if not samples_valid:
        return False
    for block in (current, prior):
        hashes = block.get("raw_content_sha256")
        urls = block.get("source_urls")
        if isinstance(hashes, str):
            continue
        if hashes is None and urls is None:
            continue
        if not isinstance(hashes, dict) or not isinstance(urls, dict) or set(hashes) != set(urls) or len(hashes) < 4:
            return False
        if not all(isinstance(url, str) and url.startswith("https://data.fotmob.com/") for url in urls.values()):
            return False
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values()):
            return False
    return True


def quality_gate(match, official, capture, xg, pregame_context, generated_at):
    """Classify a pregame bundle's evidence readiness without changing its model."""
    reasons = []
    warnings = []
    kickoff_at = match.get("kickoff_at") if isinstance(match, dict) else None
    spf = official.get("official_markets", {}).get("spf") if isinstance(official, dict) else None
    xg_inputs = xg.get("xg_inputs") if isinstance(xg, dict) else None
    evidence = xg_inputs.get("evidence") if isinstance(xg_inputs, dict) else None

    if not isinstance(spf, dict) or spf.get("status") != "frozen":
        reasons.append("official_spf_unavailable")
    if capture is None:
        reasons.append("named_european_1x2_unavailable")
    if not isinstance(evidence, dict):
        reasons.append("xg_evidence_unavailable")
    elif "published_at" not in evidence:
        if not isinstance(evidence.get("published_at_unavailable_reason"), str):
            reasons.append("xg_evidence_unavailable")
        else:
            warnings.append("xg_publication_time_unavailable")

    timestamp_values = [generated_at]
    if isinstance(spf, dict):
        timestamp_values.append(spf.get("published_at"))
    if isinstance(capture, dict):
        timestamp_values.append(capture.get("captured_at"))
    if isinstance(evidence, dict):
        if isinstance(evidence.get("published_at"), str):
            timestamp_values.append(evidence["published_at"])
    if isinstance(xg, dict) and isinstance(xg.get("captured_at"), str):
        timestamp_values.append(xg["captured_at"])
    if not isinstance(kickoff_at, str) or not all(
        _timestamp_before_kickoff(value, kickoff_at) for value in timestamp_values
    ):
        reasons.append("pre_kickoff_capture_required")
    if reasons:
        return {"status": "blocked", "reasons": reasons}

    reasons.extend(warnings)
    scope = evidence.get("scope")
    split_status = pregame_context.get("split_xg", {}).get("status") if isinstance(pregame_context, dict) else None
    lineup_status = pregame_context.get("lineup", {}).get("status") if isinstance(pregame_context, dict) else None
    is_season_bridge = _is_traceable_season_bridge(evidence)
    if is_season_bridge:
        reasons.append("season_bridge_xg")
        if scope == "league_total":
            reasons.append("league_total_xg")
    elif scope in {"league_total", "source_match_model"}:
        reasons.append("league_total_xg")
        if scope == "source_match_model":
            reasons[-1] = "source_match_model_xg"
        if not _xg_has_minimum_sample(evidence):
            reasons.append("xg_sample_below_minimum")
    elif "current_season" in evidence or "prior_season" in evidence:
        reasons.append("season_bridge_xg")
        if not is_season_bridge:
            reasons.append("xg_sample_below_minimum")
    if lineup_status != "confirmed":
        reasons.append("starting_lineups_unconfirmed")
    if split_status != "available":
        reasons.append("context_event_unresolved")
    if (
        scope in {"league_total", "source_match_model"}
        and _xg_has_minimum_sample(evidence)
    ) or is_season_bridge:
        return {
            "status": "review_ready",
            "reasons": reasons,
            "confidence_ceiling": "medium",
        }
    return {
        "status": "observation_only" if reasons else "review_ready",
        "reasons": reasons,
    }


def _model_preview(xg_inputs):
    matrix = poisson_score_matrix(
        Decimal(str(xg_inputs["home_xg"])),
        Decimal(str(xg_inputs["away_xg"])),
    )
    return {
        "one_x_two": matrix["one_x_two"],
        "top_scores": matrix["top_scores"][:5],
        "total_goals": matrix["total_goals"],
        "half_time_full_time_top2": matrix["half_time_full_time_proxy"],
    }


def _unavailable_pregame_context():
    split_xg = {"status": "unavailable", "reason": "context_event_unresolved"}
    lineup = {"status": "unavailable", "reason": "complete_lineup_unavailable"}
    return {
        "split_xg": split_xg,
        "weather": {"status": "unavailable", "reason": "venue_coordinates_unavailable"},
        "lineup": lineup,
        "risk_summary": {
            "confidence_ceiling": "medium",
            "split_xg_status": split_xg["status"],
            "lineup_status": lineup["status"],
        },
    }


UNDERSTAT_LEAGUES = {
    "Serie A": "Serie_A",
    "Premier League": "EPL",
    "La Liga": "La_liga",
    "Bundesliga": "Bundesliga",
    "Ligue 1": "Ligue_1",
    "RFPL": "RFPL",
    "英超": "EPL",
    "意甲": "Serie_A",
    "西甲": "La_liga",
    "德甲": "Bundesliga",
    "法甲": "Ligue_1",
}


def _understat_fallback(match):
    competition = match.get("competition")
    league = UNDERSTAT_LEAGUES.get(competition)
    if league is None:
        raise ValueError(f"Understat fallback does not cover competition: {competition}")
    kickoff = datetime.fromisoformat(match["kickoff_at"])
    season_start = kickoff.year if kickoff.month >= 7 else kickoff.year - 1
    result = collect_league_xg(
        league,
        str(season_start),
        match["home_team"],
        match["away_team"],
    )
    result["xg_inputs"]["source_url"] = result["source_url"]
    return result


def collect_understat_xg(match):
    """Collect Understat fallback evidence for a supported league."""
    return _understat_fallback(match)


def _season_bridge_fallback(match, xg, model_weight):
    """Use provider-resolved names only; no alias or cross-league bridge is inferred."""
    competition = match.get("competition")
    league = UNDERSTAT_LEAGUES.get(competition)
    if league is None:
        raise ValueError(f"Understat fallback does not cover competition: {competition}")
    home_team = xg.get("home_team") if isinstance(xg, dict) else None
    away_team = xg.get("away_team") if isinstance(xg, dict) else None
    if not isinstance(home_team, str) and isinstance(xg, dict):
        home_team = xg.get("home", {}).get("team_name")
    if not isinstance(away_team, str) and isinstance(xg, dict):
        away_team = xg.get("away", {}).get("team_name")
    if not all(isinstance(name, str) and name.strip() for name in (home_team, away_team)):
        raise ValueError("season bridge requires exact provider team names")
    kickoff = datetime.fromisoformat(match["kickoff_at"])
    season_start = kickoff.year if kickoff.month >= 7 else kickoff.year - 1
    fixture = {**match, "home_team": home_team, "away_team": away_team}
    return collect_season_bridge_xg(
        league,
        str(season_start),
        fixture,
        model_weight=model_weight,
    )


def sofascore_split_xg_evidence(split_xg, model_weight="0.40"):
    """Convert a verified SofaScore home/away split into model-ready evidence."""
    required = {
        "source_url",
        "captured_at",
        "competition",
        "season",
        "home_sample_matches",
        "away_sample_matches",
        "home_attack_xg",
        "home_defense_xga",
        "away_attack_xg",
        "away_defense_xga",
        "home_xg",
        "away_xg",
        "formula",
    }
    if not isinstance(split_xg, dict) or split_xg.get("status") != "available" or not required.issubset(split_xg):
        raise ValueError("verified SofaScore home/away xG is unavailable")
    evidence = {
        "source_url": split_xg["source_url"],
        "published_at_unavailable_reason": "SofaScore statistics responses do not expose publication time",
        "competition": split_xg["competition"],
        "season": split_xg["season"],
        "home_sample_matches": split_xg["home_sample_matches"],
        "away_sample_matches": split_xg["away_sample_matches"],
        "scope": "home_away_split",
        "home_attack_xg": str(split_xg["home_attack_xg"]),
        "home_defense_xga": str(split_xg["home_defense_xga"]),
        "away_attack_xg": str(split_xg["away_attack_xg"]),
        "away_defense_xga": str(split_xg["away_defense_xga"]),
        "formula": split_xg["formula"],
    }
    return {
        "provider": "SofaScore",
        "captured_at": split_xg["captured_at"],
        "source_url": split_xg["source_url"],
        "xg_inputs": {
            "source_url": split_xg["source_url"],
            "home_xg": str(split_xg["home_xg"]),
            "away_xg": str(split_xg["away_xg"]),
            "model_version": "poisson-v1",
            "model_weight": str(model_weight),
            "confidence_ceiling": "high",
            "evidence": evidence,
        },
        "stat_observations": [],
    }


def _snapshot_pregame_context(context):
    required = {"split_xg", "weather", "lineup", "risk_summary"}
    if not isinstance(context, dict) or not required.issubset(context):
        return _unavailable_pregame_context()
    return {name: context[name] for name in required}


def _odds_evidence_event(match, european_odds, capture):
    raw_content_sha256 = european_odds.get("market_raw_content_sha256")
    if not isinstance(raw_content_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_content_sha256):
        return None
    match_id = match.get("match_num") or match.get("match_id")
    match_id = str(match_id)
    if not (
        re.fullmatch(r"周[一二三四五六日][0-9]{3}", match_id)
        or (match_id.isdecimal() and len(match_id) == 11)
    ):
        return None
    return {
        "record_type": "source_observation",
        "match_id": match_id,
        "evidence_kind": "european_1x2",
        "source_url": european_odds["source_url"],
        "captured_at": european_odds["captured_at"],
        "published_at_unavailable_reason": "provider market response has no visible publication time",
        "collector": "433-european-1x2",
        "parser_version": "1.0",
        "raw_content_sha256": raw_content_sha256,
        "observed_facts": {
            "provider": european_odds["provider"],
            "bookmaker": capture["bookmaker"],
            "odds": capture["odds"],
        },
    }


def _evidence_match_id(match):
    value = match.get("match_num") or match.get("match_id")
    match_id = str(value)
    if re.fullmatch(r"周[一二三四五六日][0-9]{3}", match_id):
        return match_id
    if match_id.isdecimal() and len(match_id) == 11:
        return match_id
    return None


def _valid_raw_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)


def _official_market_evidence_events(match, official):
    match_id = _evidence_match_id(match)
    raw_content_sha256 = official.get("raw_content_sha256")
    captured_at = official.get("captured_at")
    if not match_id or not _valid_raw_hash(raw_content_sha256) or not isinstance(captured_at, str):
        return []
    events = []
    for market, entry in official.get("official_markets", {}).items():
        if not isinstance(entry, dict) or entry.get("status") != "frozen":
            continue
        source_url = entry.get("official_source_url") or official.get("official_source_url")
        published_at = entry.get("published_at")
        fixed_prizes = entry.get("fixed_prizes")
        if not isinstance(source_url, str) or not isinstance(published_at, str) or not isinstance(fixed_prizes, dict):
            continue
        facts = {"market": market, "fixed_prizes": fixed_prizes}
        if "home_handicap" in entry:
            facts["home_handicap"] = entry["home_handicap"]
        events.append({
            "record_type": "source_observation",
            "match_id": match_id,
            "evidence_kind": "official_market",
            "source_url": source_url,
            "captured_at": captured_at,
            "published_at": published_at,
            "collector": "sporttery-fixed-bonus",
            "parser_version": "1.0",
            "raw_content_sha256": raw_content_sha256,
            "observed_facts": facts,
        })
    return events


def _xg_evidence_events(match, xg):
    match_id = _evidence_match_id(match)
    evidence = xg.get("xg_inputs", {}).get("evidence", {})
    if not match_id or not isinstance(evidence, dict):
        return []
    events = []
    for observation in xg.get("stat_observations", []):
        if not isinstance(observation, dict):
            continue
        raw_content_sha256 = observation.get("raw_content_sha256")
        source_url = observation.get("source_url")
        captured_at = observation.get("captured_at")
        if not _valid_raw_hash(raw_content_sha256) or not isinstance(source_url, str) or not isinstance(captured_at, str):
            continue
        facts = {
            "provider": xg.get("provider", "FotMob"),
            "competition": evidence.get("competition"),
            "season": evidence.get("season"),
            "scope": evidence.get("scope"),
            "team": observation.get("team"),
            "side": observation.get("side"),
            "stat_name": observation.get("stat_name"),
            "value": observation.get("value"),
            "sample_matches": observation.get("sample_matches"),
        }
        if any(value is None for value in facts.values()):
            continue
        event = {
            "record_type": "source_observation",
            "match_id": match_id,
            "evidence_kind": "xg",
            "source_url": source_url,
            "captured_at": captured_at,
            "collector": {
                "Understat": "understat-xg-evidence",
                "xGscore": "xgscore-xg-evidence",
            }.get(xg.get("provider"), "fotmob-xg-evidence"),
            "parser_version": "1.0",
            "raw_content_sha256": raw_content_sha256,
            "observed_facts": facts,
        }
        if isinstance(observation.get("published_at"), str):
            event["published_at"] = observation["published_at"]
        else:
            provider = xg.get("provider", "FotMob")
            event["published_at_unavailable_reason"] = f"{provider} stat response has no publication time"
        events.append(event)
    return events


def _context_evidence_event(match, context):
    match_id = _evidence_match_id(match)
    event = context.get("event", {}) if isinstance(context, dict) else {}
    raw_content_sha256 = event.get("raw_content_sha256") if isinstance(event, dict) else None
    if (
        not match_id
        or not isinstance(event, dict)
        or event.get("status") != "matched"
        or not _valid_raw_hash(raw_content_sha256)
        or not isinstance(event.get("source_url"), str)
        or not isinstance(event.get("captured_at"), str)
    ):
        return None
    return {
        "record_type": "source_observation",
        "match_id": match_id,
        "evidence_kind": "pregame_context",
        "source_url": event["source_url"],
        "captured_at": event["captured_at"],
        "published_at_unavailable_reason": (
            f"{event.get('provider', 'SofaScore')} event response has no publication time"
        ),
        "collector": {
            "TheSportsDB": "thesportsdb-pregame-context",
        }.get(event.get("provider"), "sofascore-pregame-context"),
        "parser_version": "1.0",
        "raw_content_sha256": raw_content_sha256,
        "observed_facts": {
            "provider": event.get("provider", "SofaScore"),
            "event_id": event.get("event_id"),
            "competition": event.get("competition"),
            "season": event.get("season"),
            "split_xg_status": context.get("split_xg", {}).get("status"),
            "lineup_status": context.get("lineup", {}).get("status"),
            "confidence_ceiling": context.get("risk_summary", {}).get("confidence_ceiling"),
        },
    }


def build_pregame_bundle(
    match,
    official,
    european_odds,
    xg,
    pregame_context=None,
    context_resolution=None,
    xg_source_cascade=None,
    generated_at=None,
):
    """Build auditable pregame inputs without appending to the ledger."""
    capture = select_named_1x2_capture(
        european_odds,
        match_id=match["match_id"],
        kickoff_at=match["kickoff_at"],
    )
    spf = official["official_markets"]["spf"]
    generated_at = generated_at or datetime.now(timezone.utc).astimezone().isoformat()
    context_event = _context_evidence_event(match, pregame_context)
    pregame_context = _snapshot_pregame_context(pregame_context)
    context_resolution = context_resolution or {
        "status": "skipped",
        "reason": "context_not_collected",
    }
    prediction_input_template = {
        "market": "SPF",
        "match_id": match["match_id"],
        "kickoff_at": match["kickoff_at"],
        "captured_at": generated_at,
        "source_urls": {
            "S1": official["official_source_url"],
            "S2": european_odds["source_url"],
        },
        "match_context": {
            "competition": match.get("competition") or xg["xg_inputs"]["evidence"]["competition"],
            "home_team": match.get("home_team") or xg.get("home_team"),
            "away_team": match.get("away_team") or xg.get("away_team"),
        },
        "fixed_prizes": spf["fixed_prizes"],
        "european_odds": {"capture_hash": "<fill_after_odds-import>"},
        "xg_inputs": xg["xg_inputs"],
    }
    if context_resolution.get("status") == "adopted":
        prediction_input_template["pregame_context"] = pregame_context
    all_market_template = {
        code: (
            {field: value for field, value in entry.items() if field != "status"}
            if entry.get("status") == "frozen"
            else dict(entry)
        )
        for code, entry in official["official_markets"].items()
    }
    all_market_snapshot_input_template = {
        "prediction_hash": "<fill_after_snapshot>",
        "captured_at": generated_at,
        "official_source_url": official["official_source_url"],
        "official_markets": all_market_template,
    }
    odds_import_input = {
        "provider": european_odds["provider"],
        "captures": [capture],
    }
    evidence_event_inputs = _official_market_evidence_events(match, official)
    odds_event = _odds_evidence_event(match, european_odds, capture)
    if odds_event is not None:
        evidence_event_inputs.append(odds_event)
    evidence_event_inputs.extend(_xg_evidence_events(match, xg))
    if context_event is not None:
        evidence_event_inputs.append(context_event)
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "ledger_action": "observation_only",
        "identity": match,
        "readiness": _readiness(official, capture, xg),
        "context_resolution": context_resolution,
        "pregame_context": pregame_context,
        "xg_source_cascade": xg_source_cascade or [],
        "quality_gate": quality_gate(
            match,
            official,
            capture,
            xg,
            pregame_context,
            generated_at,
        ),
        "evidence_event_inputs": evidence_event_inputs,
        "odds_import_input": odds_import_input,
        "prediction_input_template": prediction_input_template,
        "all_market_snapshot_input_template": all_market_snapshot_input_template,
        "model_preview": _model_preview(xg["xg_inputs"]),
        "evidence": {
            "official": official,
            "european_1x2": european_odds,
            "xg": xg,
        },
    }


def build_xg_unavailable_bundle(
    match,
    official,
    european_odds,
    pregame_context,
    context_resolution,
    xg_source_cascade=None,
    generated_at=None,
):
    """Keep independently collected evidence reviewable when no free xG source is usable."""
    generated_at = generated_at or datetime.now(timezone.utc).astimezone().isoformat()
    capture = select_named_1x2_capture(
        european_odds,
        match_id=match["match_id"],
        kickoff_at=match["kickoff_at"],
    )
    xg = {
        "status": "unavailable",
        "reason": "all_free_xg_sources_unavailable",
    }
    spf = official["official_markets"]["spf"]
    prediction_input_template = {
        "market": "SPF",
        "match_id": match["match_id"],
        "kickoff_at": match["kickoff_at"],
        "captured_at": generated_at,
        "source_urls": {
            "S1": official["official_source_url"],
            "S2": european_odds["source_url"],
        },
        "match_context": {
            "competition": match.get("competition"),
            "home_team": match.get("home_team"),
            "away_team": match.get("away_team"),
        },
        "fixed_prizes": spf["fixed_prizes"],
        "european_odds": {"capture_hash": "<fill_after_odds-import>"},
    }
    odds_import_input = {
        "provider": european_odds["provider"],
        "captures": [capture],
    }
    evidence_event_inputs = _official_market_evidence_events(match, official)
    odds_event = _odds_evidence_event(match, european_odds, capture)
    if odds_event is not None:
        evidence_event_inputs.append(odds_event)
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "ledger_action": "observation_only",
        "identity": match,
        "readiness": _readiness(official, capture, xg),
        "context_resolution": context_resolution,
        "pregame_context": pregame_context,
        "xg_source_cascade": xg_source_cascade or [],
        "quality_gate": quality_gate(
            match,
            official,
            capture,
            xg,
            pregame_context,
            generated_at,
        ),
        "evidence_event_inputs": evidence_event_inputs,
        "odds_import_input": odds_import_input,
        "prediction_input_template": prediction_input_template,
        "evidence": {
            "official": official,
            "european_1x2": european_odds,
            "xg": xg,
        },
    }


def _find_schedule_match(schedule, match_id):
    wanted = str(match_id)
    for match in schedule.get("matches", []):
        if str(match.get("match_id")) == wanted:
            return match
    raise ValueError(f"match_id {match_id} was not found in the official Sporttery schedule")


def match_identity_from_overrides(
    match_id,
    kickoff_at,
    home_team,
    away_team,
    competition,
    official_source_url,
):
    """Build match identity when the match is no longer in the current schedule."""
    missing = [
        name
        for name, value in {
            "kickoff_at": kickoff_at,
            "home_team": home_team,
            "away_team": away_team,
            "competition": competition,
        }.items()
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise ValueError(f"manual match identity requires {', '.join(missing)}")
    return {
        "match_id": int(match_id) if str(match_id).isdigit() else str(match_id),
        "match_num": None,
        "competition": competition.strip(),
        "home_team": home_team.strip(),
        "away_team": away_team.strip(),
        "kickoff_at": kickoff_at.strip(),
        "official_source_url": official_source_url,
    }


DEFAULT_SOURCE_LINK_REGISTRY_PATH = (
    Path(__file__).resolve().parent / "data" / "source_link_registry.json"
)
DEFAULT_SOURCE_ALIAS_REGISTRY_PATH = (
    Path(__file__).resolve().parent / "data" / "source_aliases.json"
)


def _understat_batch_xg(batch_fixture, expected_home_team, expected_away_team):
    """Turn one confirmed batch item into the normal xG evidence shape."""
    if not isinstance(batch_fixture, dict):
        return None
    if batch_fixture.get("status") != "available":
        return {
            "status": "unavailable",
            "reason": batch_fixture.get("reason", "understat_batch_unavailable"),
        }
    home_team = batch_fixture.get("home_team") or batch_fixture.get("home", {}).get("team_name")
    away_team = batch_fixture.get("away_team") or batch_fixture.get("away", {}).get("team_name")
    if home_team != expected_home_team or away_team != expected_away_team:
        return {"status": "unavailable", "reason": "understat_batch_provider_identity_mismatch"}
    source_url = batch_fixture.get("source_url")
    captured_at = batch_fixture.get("captured_at")
    raw_content_sha256 = batch_fixture.get("raw_content_sha256")
    xg_inputs = batch_fixture.get("xg_inputs")
    if not all(isinstance(value, str) and value for value in (source_url, captured_at, raw_content_sha256)) or not isinstance(xg_inputs, dict):
        return {"status": "unavailable", "reason": "understat_batch_incomplete_evidence"}
    evidence = xg_inputs.get("evidence", {})
    home_samples = evidence.get("home_sample_matches") if isinstance(evidence, dict) else None
    away_samples = evidence.get("away_sample_matches") if isinstance(evidence, dict) else None
    observations = []
    for team, side, value, samples in (
        (home_team, "home", xg_inputs.get("home_xg"), home_samples),
        (away_team, "away", xg_inputs.get("away_xg"), away_samples),
    ):
        if value is not None and samples is not None:
            observations.append({
                "source_url": source_url,
                "captured_at": captured_at,
                "raw_content_sha256": raw_content_sha256,
                "team": team,
                "side": side,
                "stat_name": "venue_expected_goals",
                "value": str(value),
                "sample_matches": samples,
            })
    return {
        "provider": "Understat",
        "source_url": source_url,
        "captured_at": captured_at,
        "home_team": home_team,
        "away_team": away_team,
        "xg_inputs": xg_inputs,
        "stat_observations": observations,
    }


def _with_resolved_source_links(
    bundle,
    match,
    european_odds,
    xg,
    home_fotmob_url,
    away_fotmob_url,
):
    """Expose only verified source URLs for the automatic link registry."""
    links = {}
    source_url = european_odds.get("source_url") if isinstance(european_odds, dict) else None
    event = european_odds.get("event") if isinstance(european_odds, dict) else None
    if (
        isinstance(source_url, str)
        and event_matches_official_identity(
            event,
            match["home_team"],
            match["away_team"],
            match["kickoff_at"],
        )
    ):
        links["european_433_url"] = source_url
    if isinstance(xg, dict) and str(xg.get("provider", "")).startswith("FotMob"):
        if isinstance(home_fotmob_url, str):
            links["home_fotmob_url"] = home_fotmob_url
        if isinstance(away_fotmob_url, str):
            links["away_fotmob_url"] = away_fotmob_url
    if isinstance(xg, dict) and xg.get("provider") == "xGscore":
        source_url = xg.get("source_url")
        if isinstance(source_url, str):
            links["xgscore_url"] = source_url
    if links:
        bundle["resolved_source_links"] = links
    return bundle


def _context_team_names(official_team, source_aliases, fotmob_identity, wikidata_identity):
    """Build strict discovery names from previously verified source identities."""
    names = []
    alias = source_aliases.get(re.sub(r"\s+", "", official_team.casefold()))
    if isinstance(alias, str) and alias.strip():
        names.append(alias.strip())
    if isinstance(fotmob_identity, dict):
        source_name = fotmob_identity.get("source_name")
        if isinstance(source_name, str) and source_name.strip():
            names.append(source_name.strip())
    if isinstance(wikidata_identity, dict):
        english_names = wikidata_identity.get(
            "english_names", [wikidata_identity.get("english_name")]
        )
        if isinstance(english_names, list):
            names.extend(
                name.strip()
                for name in english_names
                if isinstance(name, str) and name.strip()
            )
    unique_names = []
    seen = set()
    for name in names:
        key = re.sub(r"\s+", "", name.casefold())
        if key not in seen:
            seen.add(key)
            unique_names.append(name)
    return unique_names


def collect_pregame_bundle(
    match_id,
    european_odds_url,
    home_fotmob_url=None,
    away_fotmob_url=None,
    method="concern",
    model_weight="0.40",
    kickoff_at=None,
    home_team=None,
    away_team=None,
    competition=None,
    schedule_match=None,
    understat_xg=None,
    xgscore_url=None,
    xgscore_home_team=None,
    xgscore_away_team=None,
    context_home_team_names=None,
    context_away_team_names=None,
):
    """Collect public data for one match and build reviewable ledger inputs."""
    with ThreadPoolExecutor(max_workers=3) as executor:
        official_future = executor.submit(collect_fixed_bonus, match_id)
        european_odds_future = (
            executor.submit(collect_433, european_odds_url)
            if european_odds_url is not None
            else None
        )
        official = official_future.result()
        european_odds = european_odds_future.result() if european_odds_future is not None else None
        schedule_future = (
            executor.submit(collect_schedule, method=method)
            if schedule_match is None
            else None
        )
        schedule = schedule_future.result() if schedule_future is not None else None
    if schedule_match is None:
        try:
            schedule_match = _find_schedule_match(schedule, match_id)
        except ValueError:
            schedule_match = match_identity_from_overrides(
                match_id,
                kickoff_at,
                home_team,
                away_team,
                competition,
                official["official_source_url"],
            )
    elif str(schedule_match.get("match_id")) != str(match_id):
        raise ValueError("supplied schedule_match does not match match_id")
    context_resolution = resolve_context_event(
        schedule_match,
        home_team_names=context_home_team_names,
        away_team_names=context_away_team_names,
    )
    pregame_context = _unavailable_pregame_context()
    if context_resolution.get("status") == "adopted":
        collected_context = collect_pregame_context(
            schedule_match,
            context_resolution["event_url"],
            home_team_names=context_home_team_names,
            away_team_names=context_away_team_names,
        )
        pregame_context = _snapshot_pregame_context(collected_context)
    else:
        context_resolution = resolve_thesportsdb_event(
            schedule_match,
            context_home_team_names,
            context_away_team_names,
        )
        if context_resolution.get("status") == "adopted":
            collected_context = collect_thesportsdb_pregame_context(
                schedule_match,
                context_resolution["event_url"],
                context_home_team_names,
                context_away_team_names,
            )
            pregame_context = _snapshot_pregame_context(collected_context)
    candidates = []
    unavailable = []
    xgscore_candidate = None

    if european_odds is None:
        if xgscore_url is None:
            raise ValueError("European 1X2 source is unavailable: no 433 or xGscore event URL")
        try:
            xgscore_candidate = collect_xgscore(
                xgscore_url,
                xgscore_home_team or schedule_match["home_team"],
                xgscore_away_team or schedule_match["away_team"],
                schedule_match["kickoff_at"],
                schedule_match["competition"],
                model_weight=model_weight,
            )
            select_named_1x2_capture(
                xgscore_candidate,
                match_id=match_id,
                kickoff_at=schedule_match["kickoff_at"],
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError("xGscore fallback has no verified named 1X2 evidence") from error
        european_odds = xgscore_candidate

    def add_candidate(provider, candidate):
        if _xg_evidence(candidate) is None:
            return False
        candidates.append({"provider": provider, "xg": candidate})
        return True

    if xgscore_candidate is not None and not add_candidate("xGscore", xgscore_candidate):
        unavailable.append({
            "provider": "xGscore",
            "reason": "xgscore_response_missing_model_evidence",
            "source_url": xgscore_url,
        })

    try:
        sofa_xg = sofascore_split_xg_evidence(
            pregame_context.get("split_xg"),
            model_weight=model_weight,
        )
    except (TypeError, ValueError, KeyError):
        unavailable.append({"provider": "SofaScore", "reason": "context_split_xg_unavailable"})
    else:
        add_candidate("SofaScore", sofa_xg)

    if isinstance(understat_xg, dict):
        if understat_xg.get("status") == "unavailable":
            unavailable.append({
                "provider": "Understat",
                "reason": understat_xg.get("reason", "supplied_understat_xg_unavailable"),
            })
        elif not add_candidate("Understat", understat_xg):
            unavailable.append({"provider": "Understat", "reason": "supplied_understat_xg_invalid"})

    selected = select_xg_candidate(candidates)
    selected_evidence = _xg_evidence(selected["xg"]) if selected is not None else None
    if selected_evidence is not None and selected_evidence.get("scope") == "home_away_split":
        skip_reason = (
            "not_collected_higher_tier_xg_available"
            if _xg_quality_tier(selected["xg"]) == 3
            else "not_collected_home_away_xg_available"
        )
        if xgscore_candidate is None:
            unavailable.append({
                "provider": "xGscore",
                "reason": skip_reason,
                "source_url": xgscore_url,
            })
        unavailable.append({
            "provider": "FotMob",
            "reason": skip_reason,
        })
    else:
        xgscore_collected = xgscore_candidate is not None
        if xgscore_url is not None and xgscore_candidate is None:
            try:
                xgscore_candidate = collect_xgscore(
                    xgscore_url,
                    xgscore_home_team or schedule_match["home_team"],
                    xgscore_away_team or schedule_match["away_team"],
                    schedule_match["kickoff_at"],
                    schedule_match["competition"],
                    model_weight=model_weight,
                )
                xgscore_collected = add_candidate("xGscore", xgscore_candidate)
                if not xgscore_collected:
                    unavailable.append({
                        "provider": "xGscore",
                        "reason": "xgscore_response_missing_model_evidence",
                        "source_url": xgscore_url,
                    })
            except (OSError, ValueError, KeyError, TypeError):
                unavailable.append({
                    "provider": "xGscore",
                    "reason": "xgscore_collection_unavailable",
                    "source_url": xgscore_url,
                })
        else:
            unavailable.append({"provider": "xGscore", "reason": "xgscore_url_unavailable"})

        fotmob_collected = False
        if not xgscore_collected:
            try:
                if home_fotmob_url is None or away_fotmob_url is None:
                    fotmob_urls = discover_match_team_stats_urls(
                        schedule_match["home_team"],
                        schedule_match["away_team"],
                    )
                    home_fotmob_url = fotmob_urls["home"]["source_url"]
                    away_fotmob_url = fotmob_urls["away"]["source_url"]
                fotmob_candidate = collect_match_xg_evidence(
                    home_fotmob_url,
                    away_fotmob_url,
                    model_weight=model_weight,
                )
                fotmob_collected = add_candidate("FotMob", fotmob_candidate)
                if not fotmob_collected:
                    unavailable.append({"provider": "FotMob", "reason": "fotmob_response_missing_model_evidence"})
            except (OSError, ValueError, KeyError, TypeError):
                unavailable.append({"provider": "FotMob", "reason": "fotmob_collection_unavailable"})
        else:
            unavailable.append({"provider": "FotMob", "reason": "not_collected_explicit_xgscore_available"})

        if not fotmob_collected and not xgscore_collected and understat_xg is None:
            try:
                add_candidate("Understat", collect_understat_xg(schedule_match))
            except (OSError, ValueError, KeyError, TypeError):
                unavailable.append({"provider": "Understat", "reason": "understat_collection_unavailable"})
        selected = select_xg_candidate(candidates)

    if selected is None:
        return _with_resolved_source_links(
            build_xg_unavailable_bundle(
                schedule_match,
                official,
                european_odds,
                pregame_context,
                context_resolution,
                xg_source_cascade=build_xg_source_cascade(candidates, None, unavailable),
            ),
            schedule_match,
            european_odds,
            {"status": "unavailable"},
            home_fotmob_url,
            away_fotmob_url,
        )

    xg = selected["xg"]
    evidence = _xg_evidence(xg) or {}
    if not _xg_has_minimum_sample(evidence):
        try:
            add_candidate("Understat", _season_bridge_fallback(schedule_match, xg, model_weight))
            selected = select_xg_candidate(candidates)
            xg = selected["xg"]
        except (OSError, ValueError, KeyError, TypeError):
            try:
                if xg.get("provider") == "FotMob" and home_fotmob_url and away_fotmob_url:
                    add_candidate(
                        "FotMob",
                        collect_fotmob_season_bridge_xg(
                            home_fotmob_url,
                            away_fotmob_url,
                            model_weight=model_weight,
                        ),
                    )
                    selected = select_xg_candidate(candidates)
                    xg = selected["xg"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
    return _with_resolved_source_links(
        build_pregame_bundle(
            schedule_match,
            official,
            european_odds,
            xg,
            pregame_context=pregame_context,
            context_resolution=context_resolution,
            xg_source_cascade=build_xg_source_cascade(candidates, selected, unavailable),
        ),
        schedule_match,
        european_odds,
        xg,
        home_fotmob_url,
        away_fotmob_url,
    )


def collect_auto_pregame_bundle(
    match_id,
    european_odds_url=None,
    method="concern",
    model_weight="0.40",
    schedule_match=None,
    source_link_registry_path=DEFAULT_SOURCE_LINK_REGISTRY_PATH,
    source_alias_registry_path=DEFAULT_SOURCE_ALIAS_REGISTRY_PATH,
    allow_433_discovery=True,
    understat_xg=None,
    xgscore_url=None,
    xgscore_home_team=None,
    xgscore_away_team=None,
):
    """Resolve 433 then run the normal free-source evidence cascade."""
    if schedule_match is None:
        schedule = collect_schedule(method=method)
        match = _find_schedule_match(schedule, match_id)
    else:
        if str(schedule_match.get("match_id")) != str(match_id):
            raise ValueError("supplied schedule_match does not match match_id")
        match = schedule_match
    cached_links = cached_source_links(source_link_registry_path, match)
    if european_odds_url is None:
        european_odds_url = cached_links.get("european_433_url")
    pregame_kwargs = {
        "method": method,
        "model_weight": model_weight,
        "kickoff_at": match["kickoff_at"],
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "competition": match["competition"],
        "schedule_match": match,
    }
    source_aliases = context_source_aliases(source_alias_registry_path)
    home_identity = cached_fotmob_team_identity(source_link_registry_path, match["home_team"])
    away_identity = cached_fotmob_team_identity(source_link_registry_path, match["away_team"])
    home_wikidata = cached_wikidata_team_identity(source_link_registry_path, match["home_team"])
    away_wikidata = cached_wikidata_team_identity(source_link_registry_path, match["away_team"])
    pregame_kwargs["context_home_team_names"] = _context_team_names(
        match["home_team"], source_aliases, home_identity, home_wikidata
    )
    pregame_kwargs["context_away_team_names"] = _context_team_names(
        match["away_team"], source_aliases, away_identity, away_wikidata
    )
    if understat_xg is not None:
        pregame_kwargs["understat_xg"] = understat_xg
    if xgscore_url is not None:
        pregame_kwargs["xgscore_url"] = xgscore_url
        if xgscore_home_team is not None and xgscore_away_team is not None:
            pregame_kwargs["xgscore_home_team"] = xgscore_home_team
            pregame_kwargs["xgscore_away_team"] = xgscore_away_team
    else:
        xgscore_url = cached_links.get("xgscore_url")
        if xgscore_url is None:
            if home_identity is not None and away_identity is not None:
                try:
                    xgscore_url = discover_xgscore_event_url(
                        home_identity["source_name"], away_identity["source_name"]
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    xgscore_url = None
        if xgscore_url is not None:
            pregame_kwargs["xgscore_url"] = xgscore_url
    if "xgscore_url" in pregame_kwargs:
        if (
            "xgscore_home_team" not in pregame_kwargs
            and home_identity is not None
            and away_identity is not None
        ):
            pregame_kwargs["xgscore_home_team"] = home_identity["source_name"]
            pregame_kwargs["xgscore_away_team"] = away_identity["source_name"]
        elif "xgscore_home_team" not in pregame_kwargs:
            if home_wikidata is not None and away_wikidata is not None:
                pregame_kwargs["xgscore_home_team"] = home_wikidata.get(
                    "english_names", [home_wikidata["english_name"]]
                )
                pregame_kwargs["xgscore_away_team"] = away_wikidata.get(
                    "english_names", [away_wikidata["english_name"]]
                )
    if european_odds_url is None and allow_433_discovery:
        try:
            european_odds_url = discover_433_detail_url(
                match["home_team"],
                match["away_team"],
                match["kickoff_at"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            if "xgscore_url" not in pregame_kwargs:
                raise
    if european_odds_url is None and "xgscore_url" not in pregame_kwargs:
        raise ValueError("European 1X2 source is unavailable from batch discovery")
    for name in ("home_fotmob_url", "away_fotmob_url"):
        if cached_links.get(name):
            pregame_kwargs[name] = cached_links[name]
    bundle = collect_pregame_bundle(
        match_id,
        european_odds_url,
        **pregame_kwargs,
    )
    links = bundle.get("resolved_source_links") if isinstance(bundle, dict) else None
    if isinstance(links, dict) and links:
        record_source_links(source_link_registry_path, match, links)
    context_resolution = bundle.get("context_resolution") if isinstance(bundle, dict) else None
    if isinstance(context_resolution, dict) and context_resolution.get("provider") != "TheSportsDB":
        record_adopted_aliases(source_alias_registry_path, match, context_resolution)
    return bundle
