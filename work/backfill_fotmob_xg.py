"""Read-only xG backfill for primary ledger predictions using FotMob.

Writes only outputs/tai_cai_tracker/data/fotmob_xg_backfill.jsonl and never
touches paper_ledger.jsonl or any other existing ledger file.
"""

import difflib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outputs.tai_cai_tracker.fotmob_match_xg import (  # noqa: E402
    _XG_KEYS,
    list_matches_by_date,
    match_xg,
)


LEDGER_PATH = ROOT / "outputs" / "tai_cai_tracker" / "data" / "paper_ledger.jsonl"
OUTPUT_PATH = ROOT / "outputs" / "tai_cai_tracker" / "data" / "fotmob_xg_backfill.jsonl"
TIME_TOLERANCE_SECONDS = 600
NAME_THRESHOLD = 0.6


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


def _load_missing_predictions():
    records = []
    with LEDGER_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("record_type") != "prediction":
                continue
            if record.get("prediction_role") != "primary":
                continue
            xg_inputs = record.get("xg_inputs")
            if xg_inputs and (xg_inputs.get("model_version") or xg_inputs.get("home")):
                continue
            context = record.get("match_context") if isinstance(record.get("match_context"), dict) else {}
            kickoff_raw = record.get("kickoff_at") or context.get("kickoff_at")
            records.append({
                "match_id": record.get("match_id"),
                "kickoff_raw": kickoff_raw,
                "kickoff": _parse_iso_utc(kickoff_raw),
                "home_team": record.get("home_team") or context.get("home_team"),
                "away_team": record.get("away_team") or context.get("away_team"),
            })
    return records


def _similarity(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return 0.0
    return difflib.SequenceMatcher(
        None, left.strip().casefold(), right.strip().casefold()
    ).ratio()


def _pick_candidate(candidates, kickoff, home_team, away_team):
    if not candidates:
        return None, None, None
    if len(candidates) == 1:
        candidate = candidates[0]
        delta = abs(int((candidate["utc_dt"] - kickoff).total_seconds()))
        return candidate, "time", delta

    best = None
    best_score = -1.0
    best_delta = None
    for candidate in candidates:
        score = _similarity(home_team, candidate["home"]) + _similarity(away_team, candidate["away"])
        if score > best_score:
            best = candidate
            best_score = score
            best_delta = abs(int((candidate["utc_dt"] - kickoff).total_seconds()))
    if best is None or best_score < NAME_THRESHOLD:
        return None, None, None
    return best, "time+name", best_delta


def main():
    records = _load_missing_predictions()
    stats = {
        "total_missing": len(records),
        "dates_fetched": 0,
        "matched": 0,
        "xg_available": 0,
        "no_coverage": 0,
        "unmatched": 0,
        "errors": 0,
    }
    date_cache = {}
    rows = []

    for index, record in enumerate(records, start=1):
        kickoff = record["kickoff"]
        if kickoff is None:
            stats["errors"] += 1
            print(f"[error] {record['match_id']}: missing kickoff_at", file=sys.stderr)
            continue

        day = kickoff.strftime("%Y%m%d")
        if day not in date_cache:
            try:
                fetched = list_matches_by_date(day)
            except (OSError, ValueError) as error:
                date_cache[day] = {"error": str(error), "matches": None}
                print(f"[error] date {day}: {error}", file=sys.stderr)
            else:
                matches = []
                for item in fetched:
                    utc_dt = _parse_iso_utc(item.get("utc_time"))
                    if utc_dt is None:
                        continue
                    matches.append({**item, "utc_dt": utc_dt})
                date_cache[day] = {"error": None, "matches": matches}
                stats["dates_fetched"] += 1

        cached = date_cache[day]
        if cached["error"] is not None:
            stats["errors"] += 1
            continue

        within = [
            item for item in cached["matches"]
            if abs((item["utc_dt"] - kickoff).total_seconds()) <= TIME_TOLERANCE_SECONDS
        ]
        candidate, matched_by, time_delta = _pick_candidate(
            within, kickoff, record["home_team"], record["away_team"]
        )
        if candidate is None:
            stats["unmatched"] += 1
            continue

        stats["matched"] += 1
        coverage = None
        xg = {key: None for key in _XG_KEYS}
        try:
            details = match_xg(candidate["match_id"])
        except (OSError, ValueError) as error:
            stats["errors"] += 1
            print(f"[error] {record['match_id']} match {candidate['match_id']}: {error}", file=sys.stderr)
        else:
            if details:
                coverage = details.get("coverage")
                xg = {key: details.get(key) for key in _XG_KEYS}
                if xg["home_xg"] is not None and xg["away_xg"] is not None:
                    stats["xg_available"] += 1
                elif coverage in ("ratings", "lower"):
                    stats["no_coverage"] += 1

        rows.append({
            "ledger_match_id": record["match_id"],
            "kickoff_at": record["kickoff_raw"],
            "home_team": record["home_team"],
            "away_team": record["away_team"],
            "fotmob_match_id": candidate["match_id"],
            "matched_by": matched_by,
            "time_delta_s": time_delta,
            "coverage": coverage,
            "xg": xg,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

        if index % 20 == 0:
            print(f"[progress] {index}/{len(records)}", file=sys.stderr)

    with OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    ratio = (stats["xg_available"] / stats["matched"] * 100) if stats["matched"] else 0.0
    print(f"total_missing={stats['total_missing']}")
    print(f"dates_fetched={stats['dates_fetched']}")
    print(f"matched={stats['matched']}")
    print(f"xg_available={stats['xg_available']}")
    print(f"no_coverage={stats['no_coverage']}")
    print(f"unmatched={stats['unmatched']}")
    print(f"errors={stats['errors']}")
    print(f"xg_available_ratio={ratio:.1f}% of matched")
    print(f"output={OUTPUT_PATH}")


if __name__ == "__main__":
    main()
