"""Post-match closing-line benchmark for settled paper predictions.

Closing lines are used only for post-match evaluation. They must never enter a
prediction or freeze workflow, because the closing line already contains
information about the eventual result direction: it is the last price after the
market has absorbed late team news and money. This module reads settled primary
predictions from the immutable paper ledger, matches them to football-data.co.uk
history by date and team name, and writes a separate benchmark JSONL. It never
writes to the paper ledger and never changes a frozen prediction.

Only competitions whose teams are covered by the local football-data history are
eligible. ``英冠`` is filtered in but has no collected history division
(``E1``), so it is reported as ``competition_unmapped`` rather than silently
dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

DATA_DIR = Path(__file__).resolve().parent / "data"
LEDGER_PATH = DATA_DIR / "paper_ledger.jsonl"
HISTORY_DIR = DATA_DIR / "history"
HISTORY_PATH = HISTORY_DIR / "history_matches.jsonl"
SOURCE_ALIASES_PATH = DATA_DIR / "source_aliases.json"
TEAM_ALIAS_PATH = HISTORY_DIR / "team_alias_fd.json"
OUTPUT_PATH = HISTORY_DIR / "ledger_benchmark.jsonl"

OUTCOMES: Tuple[str, ...] = ("home_win", "draw", "away_win")
DIVISIONS: Tuple[str, ...] = ("E0", "D1", "SP1", "I1", "F1")
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
    "united": "utd",
    "utd": "utd",
}

_KEYS = {
    "home_win": ("home_win", "home", "H"),
    "draw": ("draw", "D"),
    "away_win": ("away_win", "away", "A"),
}


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


def load_history_rows(path: Path) -> List[dict]:
    """Load the big-five divisions and their Pinnacle closing evidence."""

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
        rows.append(
            {
                "division": division,
                "date": match_date,
                "home_team": home_team,
                "away_team": away_team,
                "pinnacle_fair": fair,
                "pinnacle_implied": implied,
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


def build_team_alias(
    fd_names: Iterable[str], aliases: Mapping[str, object]
) -> Dict[str, List[str]]:
    """Build ``{fd_name: [aliases...]}`` from real history and alias data only.

    Every football-data team keeps at least itself.  A Chinese alias is attached
    only when its recorded ``source_name`` normalizes exactly to a football-data
    name.  Football-data names that normalize to the same key are cross-listed.
    """

    unique = sorted({name for name in fd_names if isinstance(name, str) and name.strip()})
    document: Dict[str, List[str]] = {name: [name] for name in unique}
    by_normalized: Dict[str, List[str]] = {}
    for name in unique:
        by_normalized.setdefault(normalize_name(name), []).append(name)

    for alias, info in sorted(aliases.items()):
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


def run_link(
    ledger_path: Path = LEDGER_PATH,
    history_path: Path = HISTORY_PATH,
    source_aliases_path: Path = SOURCE_ALIASES_PATH,
    team_alias_path: Path = TEAM_ALIAS_PATH,
    output_path: Path = OUTPUT_PATH,
) -> Tuple[List[dict], dict]:
    records = list(load_jsonl(ledger_path))
    eligible, skipped_by_competition = eligible_predictions(records)
    history_rows = load_history_rows(history_path)

    alias_source = json.loads(Path(source_aliases_path).read_text(encoding="utf-8"))
    aliases = alias_source.get("aliases") if isinstance(alias_source, Mapping) else {}
    aliases = aliases if isinstance(aliases, Mapping) else {}
    alias_document = build_team_alias(team_names_from_history(history_rows), aliases)
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
        for field in ("your_blended", "prematch_market", "pinnacle_fair")
    }
    paired = [
        row
        for row in rows
        if row.get("matched")
        and isinstance(row.get("your_blended"), Mapping)
        and isinstance(row.get("pinnacle_fair"), Mapping)
        and row.get("actual") in OUTCOMES
    ]
    different = [row for row in paired if top1(row["your_blended"]) != top1(row["pinnacle_fair"])]
    hits = sum(1 for row in different if top1(row["your_blended"]) == row["actual"])
    metrics["divergence"] = {
        "n": len(different),
        "paired": len(paired),
        "pred_top1_hit": hits / len(different) if different else None,
    }
    return metrics


def _format(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def format_evaluation(metrics: Mapping[str, Mapping[str, object]]) -> List[str]:
    lines: List[str] = []
    for field in ("your_blended", "prematch_market", "pinnacle_fair"):
        item = metrics[field]
        lines.append(
            f"eval {field} rps={_format(item['rps'])} "
            f"brier={_format(item['brier'])} top1={_format(item['top1'])} "
            f"n={item['n']}"
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
            "Pinnacle closing prices and optionally score the three-way comparison."
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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.link and not args.evaluate:
        parser.error("choose --link and/or --eval")

    rows, summary = run_link()
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    if args.evaluate:
        for line in format_evaluation(evaluate_rows(rows)):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
