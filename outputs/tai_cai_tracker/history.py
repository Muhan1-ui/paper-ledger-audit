"""Independent historical-score and closing-odds baseline for the big five leagues.

This module is deliberately isolated from the paper ledger, the tracker CLI, and
the dashboard.  It downloads public football-data.co.uk season CSV files, keeps
a local raw cache, parses results and pre-match/closing 1X2 prices, and scores
Pinnacle closing prices with rank probability score (RPS) and multiclass Brier
score.  It does not place, fund, or recommend any lottery purchase.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

HISTORY_DIR = Path(__file__).resolve().parent / "data" / "history"
RAW_DIR = HISTORY_DIR / "raw"
MATCHES_PATH = HISTORY_DIR / "history_matches.jsonl"

SOURCE_TEMPLATE = "https://www.football-data.co.uk/mmz4281/{code}/{division}.csv"
MIRROR_TEMPLATE = (
    "https://raw.githubusercontent.com/xgabora/"
    "Club-Football-Match-Data-2000-2025/master/data/{division}.csv"
)
USER_AGENT = "Mozilla/5.0"

DIVISIONS: Tuple[str, ...] = ("E0", "D1", "SP1", "I1", "F1", "E1")
DEFAULT_FIRST_SEASON = 2012
DEFAULT_LAST_SEASON = 2027  # exclusive end year: the 2026/27 season

# football-data.co.uk answers some season files (notably 2026/27) with a 302
# from www to the apex host.  Follow it exactly like a browser instead of
# treating the redirect as a download failure.
_REDIRECT_OPENER = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())

OUTCOMES: Tuple[str, ...] = ("home_win", "draw", "away_win")
FTR_TO_RESULT = {"H": "home_win", "D": "draw", "A": "away_win"}
DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y")

# Per-source (open, close) column triples in the football-data.co.uk CSV.
ODDS_COLUMNS: Dict[str, Tuple[Tuple[str, str, str], Optional[Tuple[str, str, str]]]] = {
    "pinnacle": (("PSH", "PSD", "PSA"), ("PSCH", "PSCD", "PSCA")),
    "b365": (("B365H", "B365D", "B365A"), ("B365CH", "B365CD", "B365CA")),
    "avg": (("", "", ""), ("AvgCH", "AvgCD", "AvgCA")),
    "max": (("", "", ""), ("MaxCH", "MaxCD", "MaxCA")),
}


class HistoryFetchError(RuntimeError):
    """A public CSV could not be retrieved from any permitted source."""

    def __init__(self, attempts: Sequence[str]) -> None:
        self.attempts = list(attempts)
        super().__init__("; ".join(self.attempts))


def season_code(season_start: int) -> str:
    """Return the football-data.co.uk season code, e.g. 2012 -> ``1213``."""

    return f"{season_start % 100:02d}{(season_start + 1) % 100:02d}"


def season_label(season_start: int) -> str:
    return f"{season_start}/{str(season_start + 1)[-2:]}"


def season_date_bounds(season_start: int) -> Tuple[date, date]:
    return date(season_start, 7, 1), date(season_start + 1, 6, 30)


def parse_date(value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number <= 0:  # NaN or non-positive price
        return None
    return number


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _price_triple(
    row: Mapping[str, Optional[str]], columns: Sequence[str]
) -> Optional[Dict[str, float]]:
    if not all(columns):
        return None
    home = _parse_float(row.get(columns[0]))
    draw = _parse_float(row.get(columns[1]))
    away = _parse_float(row.get(columns[2]))
    if home is None or draw is None or away is None:
        return None
    return {"home": home, "draw": draw, "away": away}


def implied_probabilities(
    odds: Optional[Mapping[str, float]]
) -> Optional[Dict[str, float]]:
    """Return raw 1/odds probabilities without removing the bookmaker margin."""

    if not odds:
        return None
    try:
        return {key: 1.0 / float(odds[key]) for key in ("home", "draw", "away")}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def normalize_probabilities(
    probabilities: Optional[Mapping[str, float]]
) -> Optional[Dict[str, float]]:
    """Normalize a three-way probability mapping so that it sums to one."""

    if not probabilities:
        return None
    try:
        values = [float(probabilities[key]) for key in ("home", "draw", "away")]
    except (KeyError, TypeError, ValueError):
        return None
    total = sum(values)
    if total <= 0:
        return None
    return {key: value / total for key, value in zip(("home", "draw", "away"), values)}


def implied_and_fair(
    close_odds: Optional[Mapping[str, float]]
) -> Tuple[Optional[Dict[str, float]], Optional[Dict[str, float]]]:
    implied = implied_probabilities(close_odds)
    return implied, normalize_probabilities(implied)


def _market(
    row: Mapping[str, Optional[str]],
    open_columns: Sequence[str],
    close_columns: Sequence[str],
) -> Dict[str, Optional[Dict[str, float]]]:
    open_odds = _price_triple(row, open_columns)
    close_odds = _price_triple(row, close_columns)
    implied, fair = implied_and_fair(close_odds)
    return {"open": open_odds, "close": close_odds, "implied": implied, "fair": fair}


def _clean(value: Optional[str]) -> str:
    return "" if value is None else value.strip()


def parse_csv_text(
    text: str, division: str, season_start: int, filter_season: bool = False
) -> Tuple[List[dict], Dict[str, int]]:
    """Parse one season CSV.  Missing columns yield nulls, never exceptions.

    ``filter_season`` is used only for the all-seasons mirror file; a normal
    football-data.co.uk season file is authoritative for its own season, even
    when a postponed fixture (for example the 2019/20 restart) falls in July.
    """

    label = season_label(season_start)
    first_day, last_day = season_date_bounds(season_start)
    matches: List[dict] = []
    skipped = {"bad_date": 0, "bad_result": 0, "bad_team": 0, "out_of_season": 0}

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for raw_row in reader:
        row = {
            (key.strip() if isinstance(key, str) else key): value
            for key, value in raw_row.items()
        }
        home_team = _clean(row.get("HomeTeam"))
        away_team = _clean(row.get("AwayTeam"))
        if not home_team or not away_team:
            skipped["bad_team"] += 1
            continue

        match_date = parse_date(row.get("Date"))
        if match_date is None:
            skipped["bad_date"] += 1
            continue
        if filter_season and not (first_day <= match_date <= last_day):
            skipped["out_of_season"] += 1
            continue

        result = FTR_TO_RESULT.get(_clean(row.get("FTR")).upper())
        if result is None:
            skipped["bad_result"] += 1
            continue

        odds: Dict[str, dict] = {}
        for source, (open_columns, close_columns) in ODDS_COLUMNS.items():
            odds[source] = _market(row, open_columns, close_columns)

        matches.append(
            {
                "match_id": (
                    f"{division}|{match_date.isoformat()}|"
                    f"{home_team}|{away_team}"
                ).lower(),
                "date": match_date.isoformat(),
                "season": label,
                "division": division,
                "home_team": home_team,
                "away_team": away_team,
                "home_goals": _parse_int(row.get("FTHG")),
                "away_goals": _parse_int(row.get("FTAG")),
                "result": result,
                "odds": odds,
            }
        )
    return matches, skipped


def _outcome_key(outcome: str) -> Tuple[str, ...]:
    if outcome == "home_win":
        return ("home_win", "home", "H")
    if outcome == "draw":
        return ("draw", "D")
    return ("away_win", "away", "A")


def _probability(prob_dict: Mapping[str, float], outcome: str) -> float:
    for key in _outcome_key(outcome):
        if key in prob_dict:
            return float(prob_dict[key])
    return 0.0


def rps(prob_dict: Mapping[str, float], actual: str) -> float:
    """Rank probability score for the ordered outcomes home_win, draw, away_win.

    The sum of squared cumulative differences is divided by ``r - 1 == 2`` so
    the score is normalized to the unit interval.
    """

    if actual not in OUTCOMES:
        raise ValueError(f"unknown actual outcome: {actual!r}")
    cumulative_probability = 0.0
    cumulative_outcome = 0.0
    score = 0.0
    for outcome in OUTCOMES:
        cumulative_probability += _probability(prob_dict, outcome)
        cumulative_outcome += 1.0 if outcome == actual else 0.0
        score += (cumulative_probability - cumulative_outcome) ** 2
    return score / 2.0


def brier_multiclass(prob_dict: Mapping[str, float], actual: str) -> float:
    """Three-class Brier score: the sum of squared probability errors."""

    if actual not in OUTCOMES:
        raise ValueError(f"unknown actual outcome: {actual!r}")
    return sum(
        (_probability(prob_dict, outcome) - (1.0 if outcome == actual else 0.0)) ** 2
        for outcome in OUTCOMES
    )


def _http_get(url: str, timeout: float = 60.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _REDIRECT_OPENER.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() or b""
        raise HistoryFetchError(
            [f"{url} -> HTTP {exc.code}; first 200 bytes: {body[:200]!r}"]
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HistoryFetchError([f"{url} -> network error: {exc}"]) from exc
    if not body:
        raise HistoryFetchError([f"{url} -> HTTP {status}; empty response body"])
    return body


def fetch_season_csv(division: str, season_start: int) -> Tuple[bytes, str]:
    """Download one season CSV, falling back once to the documented mirror.

    Returns ``(body, source)`` where source is ``primary`` or ``mirror``.
    """

    code = season_code(season_start)
    primary = SOURCE_TEMPLATE.format(code=code, division=division)
    failures: List[str] = []
    try:
        return _http_get(primary), "primary"
    except HistoryFetchError as exc:
        failures.extend(exc.attempts)

    mirror = MIRROR_TEMPLATE.format(division=division)
    try:
        return _http_get(mirror), "mirror"
    except HistoryFetchError as exc:
        failures.extend(exc.attempts)
    raise HistoryFetchError(failures)


def _decode(body: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def backfill(
    divisions: Iterable[str] = DIVISIONS,
    first_season: int = DEFAULT_FIRST_SEASON,
    last_season: int = DEFAULT_LAST_SEASON,
) -> Tuple[List[dict], dict]:
    """Ensure every CSV is cached, parse all seasons, and write the JSONL file."""

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    divisions = list(divisions)

    all_matches: List[dict] = []
    skipped_total: Dict[str, int] = {}
    file_count = 0
    season_count = 0
    for season_start in range(first_season, last_season):
        season_count += 1
        label = season_label(season_start)
        code = season_code(season_start)
        for division in divisions:
            raw_path = RAW_DIR / f"{division}_{code}.csv"
            mirror_path = RAW_DIR / f"{division}_{code}.mirror.csv"
            filter_season = False
            if raw_path.exists() and raw_path.stat().st_size > 1000:
                path = raw_path
            elif mirror_path.exists() and mirror_path.stat().st_size > 1000:
                path = mirror_path
                filter_season = True
            else:
                body, source = fetch_season_csv(division, season_start)
                path = raw_path if source == "primary" else mirror_path
                path.write_bytes(body)
                filter_season = source == "mirror"
            matches, skipped = parse_csv_text(
                _decode(path.read_bytes()),
                division,
                season_start,
                filter_season=filter_season,
            )
            file_count += 1
            all_matches.extend(matches)
            for key, count in skipped.items():
                skipped_total[key] = skipped_total.get(key, 0) + count
            print(
                f"history: {division} {label} -> {len(matches)} matches",
                file=sys.stderr,
            )

    MATCHES_PATH.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in all_matches
        ),
        encoding="utf-8",
    )
    summary = summarize(all_matches, season_count, file_count)
    summary["skipped"] = skipped_total
    return all_matches, summary


def summarize(matches: Sequence[dict], seasons: int, files: int) -> dict:
    dates = [record["date"] for record in matches]
    coverage = {}
    for source in ("pinnacle", "b365", "avg"):
        covered = sum(
            1
            for record in matches
            if record["odds"].get(source, {}).get("fair") is not None
        )
        coverage[f"{source}_close"] = (
            round(covered / len(matches), 3) if matches else 0.0
        )
    return {
        "seasons": seasons,
        "files": files,
        "matches": len(matches),
        "date_range": [min(dates), max(dates)] if dates else [],
        **coverage,
    }


def evaluate(matches: Sequence[dict]) -> List[dict]:
    results: List[dict] = []
    for source in ("pinnacle", "b365", "avg", "max"):
        scored = [
            record
            for record in matches
            if record["odds"].get(source, {}).get("fair") is not None
        ]
        if not scored:
            results.append(
                {"source": source, "n": 0, "rps": None, "brier": None, "top1": None}
            )
            continue
        rps_values = [
            rps(record["odds"][source]["fair"], record["result"]) for record in scored
        ]
        brier_values = [
            brier_multiclass(record["odds"][source]["fair"], record["result"])
            for record in scored
        ]
        hits = sum(
            1
            for record in scored
            if max(
                ("home", "draw", "away"),
                key=lambda key: record["odds"][source]["fair"][key],
            )
            == {"home_win": "home", "draw": "draw", "away_win": "away"}[
                record["result"]
            ]
        )
        results.append(
            {
                "source": source,
                "n": len(scored),
                "rps": round(sum(rps_values) / len(scored), 4),
                "brier": round(sum(brier_values) / len(scored), 4),
                "top1": round(hits / len(scored), 4),
            }
        )
    return results


def evaluate_pinnacle_by_season(matches: Sequence[dict]) -> List[dict]:
    grouped: Dict[str, List[dict]] = {}
    for record in matches:
        if record["odds"].get("pinnacle", {}).get("fair") is None:
            continue
        grouped.setdefault(record["season"], []).append(record)
    output = []
    for season in sorted(grouped):
        rows = grouped[season]
        values = [rps(record["odds"]["pinnacle"]["fair"], record["result"]) for record in rows]
        output.append(
            {"season": season, "n": len(rows), "rps": round(sum(values) / len(rows), 4)}
        )
    return output


def _print_evaluation(matches: Sequence[dict]) -> None:
    for item in evaluate(matches):
        if item["n"] == 0:
            print(
                f"eval source={item['source']} market=close "
                "rps=n/a brier=n/a top1=n/a n=0"
            )
        else:
            print(
                f"eval source={item['source']} market=close "
                f"rps={item['rps']:.4f} brier={item['brier']:.4f} "
                f"top1={item['top1']:.4f} n={item['n']}"
            )
    for item in evaluate_pinnacle_by_season(matches):
        print(
            f"eval season={item['season']} source=pinnacle market=close "
            f"rps={item['rps']:.4f} n={item['n']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download football-data.co.uk big-five-league results and closing "
            "odds into an isolated local cache, then score closing prices."
        )
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="download missing season CSVs and rewrite history_matches.jsonl",
    )
    parser.add_argument(
        "--eval", action="store_true", help="print RPS, Brier, and top-1 hit rates"
    )
    parser.add_argument(
        "--divisions",
        default=",".join(DIVISIONS),
        help="comma-separated divisions (default: E0,D1,SP1,I1,F1,E1)",
    )
    parser.add_argument(
        "--first-season",
        type=int,
        default=DEFAULT_FIRST_SEASON,
        help="first season start year (default: 2012 for 2012/13)",
    )
    parser.add_argument(
        "--last-season",
        type=int,
        default=DEFAULT_LAST_SEASON,
        help="exclusive end year (default: 2027, meaning through 2026/27)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    divisions = [item.strip().upper() for item in args.divisions.split(",") if item.strip()]
    if not divisions:
        parser.error("--divisions must contain at least one division")
    if args.first_season >= args.last_season:
        parser.error("--first-season must be lower than --last-season")
    if not args.backfill:
        parser.error("--backfill is required for this command")

    try:
        matches, summary = backfill(divisions, args.first_season, args.last_season)
    except HistoryFetchError as exc:
        for attempt in exc.attempts:
            print(attempt, file=sys.stderr)
        print("history: download failed; no data was written", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"history: local cache error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    if args.eval:
        _print_evaluation(matches)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
