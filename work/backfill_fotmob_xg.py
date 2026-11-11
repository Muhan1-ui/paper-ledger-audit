"""Read-only xG backfill for primary ledger predictions using FotMob.

Matching strategy (validated against the ledger):
  1. FotMob fixtures for the UTC kickoff date and the previous date.
  2. Hard +/-10 minute time filter.
  3. League name + country-code filter.
  4. Final-score filter when the ledger fixture is settled.
  5. Team-alias adjudication when several candidates survive.

Writes only outputs/tai_cai_tracker/data/fotmob_xg_backfill.jsonl and
outputs/tai_cai_tracker/data/fotmob_xg_unmatched.jsonl. It never touches
paper_ledger.jsonl or any other ledger file.
"""

import json
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outputs.tai_cai_tracker.fotmob_match_xg import (  # noqa: E402
    _XG_KEYS,
    list_matches_by_date,
    match_xg,
)


DATA_DIR = ROOT / "outputs" / "tai_cai_tracker" / "data"
LEDGER_PATH = DATA_DIR / "paper_ledger.jsonl"
OUTPUT_PATH = DATA_DIR / "fotmob_xg_backfill.jsonl"
UNMATCHED_PATH = DATA_DIR / "fotmob_xg_unmatched.jsonl"
TIME_TOLERANCE_SECONDS = 600
MAX_DIAGNOSTIC_CANDIDATES = 6
_ONE_DAY = timedelta(days=1)

LEAGUE = {
    "英超": ("premierleague", "ENG"),
    "英冠": ("championship", "ENG"),
    "西甲": ("laliga", "ESP"),
    "德甲": ("bundesliga", "GER"),
    "德乙": ("2bundesliga", "GER"),
    "意甲": ("seriea", "ITA"),
    "法甲": ("ligue1", "FRA"),
    "日职": ("jleague", "JPN"),
    "日乙": ("jleague2", "JPN"),
    "韩职": ("kleague1", "KOR"),
    "巴甲": ("seriea", "BRA"),
    "葡超": ("ligaportugal", "POR"),
    "瑞超": ("allsvenskan", "SWE"),
    "挪超": ("eliteserien", "NOR"),
    "沙职": ("saudiproleague", "KSA"),
    "美职": ("majorleaguesoccer", "USA"),
    "荷甲": ("eredivisie", "NED"),
    "欧冠": ("championsleague", None),
    "欧罗巴": ("europaleague", None),
    "欧国联": ("uefanationsleague", None),
    "亚冠精英": ("afcchampionsleagueelite", None),
    "解放者杯": ("copalibertadores", None),
    "英联赛杯": ("eflcup", None),
}

TEAM_ALIAS = {
    "诺丁汉": "nottingham forest",
    "热刺": "tottenham",
    "布伦特": "brentford",
    "桑德兰": "sunderland",
    "富勒姆": "fulham",
    "水晶宫": "crystal palace",
    "伯恩茅斯": "bournemouth",
    "切尔西": "chelsea",
    "纽伦堡": "nurnberg",
    "汉诺威96": "hannover 96",
    "巴黎FC": "paris fc",
    "里昂": "lyon",
    "格风暴": "sturm graz",
    "雷恩": "rennes",
    "鹿岛鹿角": "kashima antlers",
    "浦和红钻": "urawa red diamonds",
    "芝加哥": "chicago fire",
    "迈国际": "inter miami",
    "萨尔茨堡": "salzburg",
    "AC米兰": "milan",
    "利勒斯特": "lillestrom",
    "皇家社会": "real sociedad",
}

# Club-type tokens that carry no identifying value in either language.
_AFFIX_TOKENS = {
    "fc", "afc", "cf", "sc", "cfc", "ac", "club", "calcio",
    "ss", "ssc", "as", "us", "1",
}


def _parse_iso_utc(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_text(value):
    """Lowercase, strip accents and drop every non-alphanumeric character."""
    if not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.casefold() if ch.isalnum())


def _normalise_league(value):
    return _normalise_text(value)


def _normalise_team(value):
    if not isinstance(value, str):
        return ""
    text = value.strip()
    text = TEAM_ALIAS.get(text, text)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = []
    for token in stripped.casefold().split():
        token = "".join(ch for ch in token if ch.isalnum())
        if not token or token in _AFFIX_TOKENS:
            continue
        tokens.append(token)
    return "".join(tokens)


def _teams_match(ledger_name, fotmob_name):
    left = _normalise_team(ledger_name)
    right = _normalise_team(fotmob_name)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) < 4:
        return False
    return longer.startswith(shorter) or longer.endswith(shorter)


def _load_ledger():
    records = []
    with LEDGER_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _has_xg(record):
    xg_inputs = record.get("xg_inputs")
    return bool(xg_inputs and (xg_inputs.get("model_version") or xg_inputs.get("home")))


def _context(record):
    context = record.get("match_context")
    return context if isinstance(context, dict) else {}


def _resolve_scores(records):
    """Map prediction content_hash -> final score.

    settlement records link directly through prediction_hash;
    all_market_settlement links through snapshot_hash -> all_market_snapshot.
    """
    snapshot_prediction = {}
    for record in records:
        if record.get("record_type") == "all_market_snapshot":
            snapshot_prediction[record.get("content_hash")] = record.get("prediction_hash")

    scores = {}
    for record in records:
        record_type = record.get("record_type")
        final_score = record.get("final_score")
        if not isinstance(final_score, dict):
            continue
        home, away = final_score.get("home"), final_score.get("away")
        if not isinstance(home, int) or not isinstance(away, int):
            continue
        score = {"home": home, "away": away}
        if record_type == "settlement":
            prediction_hash = record.get("prediction_hash")
            if prediction_hash:
                scores[prediction_hash] = score
        elif record_type == "all_market_settlement":
            prediction_hash = snapshot_prediction.get(record.get("snapshot_hash"))
            if prediction_hash:
                scores.setdefault(prediction_hash, score)
    return scores


def _load_matches(scores):
    """Aggregate predictions by match_id and return primary rows missing xG."""
    records = _load_ledger()
    predictions = defaultdict(list)
    for record in records:
        if record.get("record_type") != "prediction":
            continue
        predictions[record.get("match_id")].append(record)

    matches = []
    for match_id, group in predictions.items():
        primaries = [r for r in group if r.get("prediction_role") == "primary"]
        if not primaries:
            continue
        primaries.sort(key=lambda r: r.get("captured_at") or "")
        primary = primaries[0]
        if _has_xg(primary):
            continue

        context = _context(primary)
        ledger_score = None
        if primary.get("content_hash") in scores:
            ledger_score = scores[primary["content_hash"]]
        else:
            for candidate in primaries:
                if candidate.get("content_hash") in scores:
                    ledger_score = scores[candidate["content_hash"]]
                    break

        kickoff_raw = primary.get("kickoff_at") or context.get("kickoff_at")
        matches.append({
            "match_id": match_id,
            "kickoff_raw": kickoff_raw,
            "kickoff": _parse_iso_utc(kickoff_raw),
            "competition": context.get("competition") or primary.get("competition"),
            "home_team": primary.get("home_team") or context.get("home_team"),
            "away_team": primary.get("away_team") or context.get("away_team"),
            "ledger_score": ledger_score,
        })
    return matches


class MatchListCache:
    def __init__(self):
        self._by_day = {}

    def get(self, day):
        if day in self._by_day:
            return self._by_day[day]
        try:
            fetched = list_matches_by_date(day)
        except (OSError, ValueError) as error:
            print(f"[error] date {day}: {error}", file=sys.stderr)
            self._by_day[day] = (None, str(error))
            return self._by_day[day]

        matches = []
        for item in fetched:
            utc_dt = _parse_iso_utc(item.get("utc_time"))
            if utc_dt is None:
                continue
            matches.append({**item, "utc_dt": utc_dt})
        self._by_day[day] = (matches, None)
        return self._by_day[day]


def _league_filtered(match, competition):
    """Return 2 for an exact league-name match, 1 for a prefix match, 0 otherwise."""
    if competition not in LEAGUE:
        return 0
    league_token, ccode = LEAGUE[competition]
    if ccode is not None:
        observed = match.get("ccode")
        if not isinstance(observed, str) or observed.casefold() != ccode.casefold():
            return 0
    name = _normalise_league(match.get("league"))
    if name == league_token:
        return 2
    if name.startswith(league_token):
        return 1
    return 0


def _score_matches(match, ledger_score):
    if ledger_score is None:
        return True
    raw = match.get("score")
    if not isinstance(raw, str):
        return False
    compact = raw.replace(" ", "")
    return compact == f"{ledger_score['home']}-{ledger_score['away']}"


def _alias_score(candidate, home_team, away_team):
    score = 0
    if _teams_match(home_team, candidate.get("home")):
        score += 1
    if _teams_match(away_team, candidate.get("away")):
        score += 1
    return score


def _pick(candidates, home_team, away_team):
    """Return (candidate, alias_used); (None, True) when ambiguous."""
    if len(candidates) == 1:
        return candidates[0], False
    scored = [(candidate, _alias_score(candidate, home_team, away_team)) for candidate in candidates]
    best_score = max(score for _, score in scored)
    if best_score <= 0:
        return None, True
    best = [candidate for candidate, score in scored if score == best_score]
    if len(best) != 1:
        return None, True
    return best[0], True


def _diagnostic_candidate(candidate):
    return {
        "fotmob_home": candidate.get("home"),
        "fotmob_away": candidate.get("away"),
        "score": candidate.get("score"),
        "league": candidate.get("league"),
        "ccode": candidate.get("ccode"),
        "utc_time": candidate.get("utc_time"),
    }


def _record_unmatched(match, reason, candidates, cache):
    return {
        "ledger_match_id": match["match_id"],
        "competition": match["competition"],
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "ledger_score": match["ledger_score"],
        "kickoff_at": match["kickoff_raw"],
        "unmatched_reason": reason,
        "candidate_count": len(candidates),
        "candidates": [_diagnostic_candidate(c) for c in candidates[:MAX_DIAGNOSTIC_CANDIDATES]],
    }


def main():
    cache = MatchListCache()
    scores = _resolve_scores(_load_ledger())
    matches = _load_matches(scores)

    stats = {
        "total_matches": len(matches),
        "with_score": sum(1 for m in matches if m["ledger_score"] is not None),
        "matched": 0,
        "xg_available": 0,
        "no_coverage": 0,
        "unmatched": 0,
        "errors": 0,
    }
    matched_by = Counter()
    unmatched_reasons = Counter()
    no_league_map = Counter()
    rows = []
    unmatched_rows = []

    for index, match in enumerate(matches, start=1):
        if index % 20 == 0:
            print(f"[progress] {index}/{len(matches)}", file=sys.stderr)

        kickoff = match["kickoff"]
        if kickoff is None:
            stats["errors"] += 1
            unmatched_reasons["missing_kickoff"] += 1
            unmatched_rows.append(_record_unmatched(match, "missing_kickoff", [], cache))
            continue

        if match["competition"] not in LEAGUE:
            no_league_map[match["competition"]] += 1
            stats["unmatched"] += 1
            unmatched_reasons["no_league_map"] += 1
            unmatched_rows.append(_record_unmatched(match, "no_league_map", [], cache))
            continue

        day = kickoff.strftime("%Y%m%d")
        previous_day = (kickoff.replace(hour=0) - _ONE_DAY).strftime("%Y%m%d")
        time_hits = {}
        error = None
        for date in (day, previous_day):
            day_matches, day_error = cache.get(date)
            if day_matches is None:
                error = day_error
                continue
            for item in day_matches:
                if abs((item["utc_dt"] - kickoff).total_seconds()) <= TIME_TOLERANCE_SECONDS:
                    time_hits[item["match_id"]] = item
        time_candidates = list(time_hits.values())

        if error is not None and not time_candidates:
            stats["errors"] += 1
            unmatched_reasons["fetch_error"] += 1
            unmatched_rows.append(_record_unmatched(match, "fetch_error", [], cache))
            continue

        scored_candidates = [
            (item, _league_filtered(item, match["competition"])) for item in time_candidates
        ]
        exact_candidates = [item for item, level in scored_candidates if level == 2]
        league_candidates = exact_candidates or [item for item, level in scored_candidates if level == 1]
        if not league_candidates:
            reason = "no_time_candidate" if not time_candidates else "no_league_candidate"
            stats["unmatched"] += 1
            unmatched_reasons[reason] += 1
            unmatched_rows.append(_record_unmatched(match, reason, time_candidates, cache))
            continue

        score_used = False
        candidates = league_candidates
        if match["ledger_score"] is not None:
            score_used = True
            candidates = [
                item for item in league_candidates
                if _score_matches(item, match["ledger_score"])
            ]
            if not candidates:
                stats["unmatched"] += 1
                unmatched_reasons["score_mismatch"] += 1
                unmatched_rows.append(
                    _record_unmatched(match, "score_mismatch", league_candidates, cache)
                )
                continue

        if len(candidates) == 1:
            candidate = candidates[0]
            alias_used = False
        else:
            candidate, alias_used = _pick(
                candidates, match["home_team"], match["away_team"]
            )
            if candidate is None:
                stats["unmatched"] += 1
                unmatched_reasons["ambiguous"] += 1
                unmatched_rows.append(_record_unmatched(match, "ambiguous", candidates, cache))
                continue

        if not alias_used and not score_used:
            label = "time"
        elif not alias_used:
            label = "time+score"
        elif score_used:
            label = "time+score+alias"
        else:
            label = "time+alias"

        time_delta = abs(int((candidate["utc_dt"] - kickoff).total_seconds()))
        coverage = None
        xg = {key: None for key in _XG_KEYS}
        try:
            details = match_xg(candidate["match_id"])
        except (OSError, ValueError) as error_text:
            stats["errors"] += 1
            print(
                f"[error] {match['match_id']} match {candidate['match_id']}: {error_text}",
                file=sys.stderr,
            )
        else:
            if details:
                coverage = details.get("coverage")
                xg = {key: details.get(key) for key in _XG_KEYS}
                if xg["home_xg"] is not None and xg["away_xg"] is not None:
                    stats["xg_available"] += 1
                elif coverage in ("ratings", "lower"):
                    stats["no_coverage"] += 1

        stats["matched"] += 1
        matched_by[label] += 1
        rows.append({
            "ledger_match_id": match["match_id"],
            "kickoff_at": match["kickoff_raw"],
            "competition": match["competition"],
            "home_team": match["home_team"],
            "away_team": match["away_team"],
            "ledger_score": match["ledger_score"],
            "fotmob_match_id": candidate["match_id"],
            "fotmob_home": candidate.get("home"),
            "fotmob_away": candidate.get("away"),
            "fotmob_score": candidate.get("score"),
            "matched_by": label,
            "time_delta_s": time_delta,
            "coverage": coverage,
            "xg": xg,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

    with OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with UNMATCHED_PATH.open("w", encoding="utf-8") as handle:
        for row in unmatched_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    hit_rate = (stats["matched"] / stats["total_matches"] * 100) if stats["total_matches"] else 0.0
    xg_ratio = (stats["xg_available"] / stats["matched"] * 100) if stats["matched"] else 0.0
    print(f"total_matches={stats['total_matches']}")
    print(f"with_score={stats['with_score']}")
    print(f"matched={stats['matched']}")
    print(f"matched_by={dict(matched_by)}")
    print(f"xg_available={stats['xg_available']}")
    print(f"no_coverage={stats['no_coverage']}")
    print(f"unmatched={stats['unmatched']}")
    print(f"unmatched_by_reason={dict(unmatched_reasons)}")
    print(f"hit_rate={hit_rate:.1f}%")
    print(f"xg_available_ratio={xg_ratio:.1f}% of matched")
    print(f"errors={stats['errors']}")
    print(f"output={OUTPUT_PATH}")
    print(f"unmatched_output={UNMATCHED_PATH}")
    print(f"no_league_map_competitions={dict(no_league_map)}")


if __name__ == "__main__":
    main()
